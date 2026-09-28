"""系统测试：权限边界、卡号唯一并发、到期失效、设备离线补传、岗位权限等。

运行：
    cd access-control && python -m pytest tests/ -v
    或：python tests/test_system.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accessctl.db import Database  # noqa: E402
from accessctl.services import (  # noqa: E402
    Service, ServiceError, mask_card_no)

ADMIN, CLERK1, CLERK2, GUARD, AUDITOR = 1, 2, 3, 4, 5
TMP_DB = "/tmp/accessctl_unittest.db"


def fresh_db() -> Database:
    for suf in ("", "-wal", "-shm"):
        pathlib.Path(TMP_DB + suf).unlink(missing_ok=True)
    return Database(TMP_DB)


def setup_minimal(db: Database) -> Service:
    """构造最小数据集：操作员、1 栋楼、人员、3 个权限组、若干卡。"""
    s = Service(db)
    with db.tx() as c:
        for login, name, role in [
            ("admin01", "林岚", "admin"), ("clerk01", "王晨", "clerk"),
            ("clerk02", "赵宁", "clerk"), ("guard01", "陈磊", "guard"),
            ("audit01", "周敏", "auditor")]:
            c.execute("INSERT INTO operators(login,name,role,active) VALUES(?,?,?,1)",
                      (login, name, role))
        c.execute("INSERT INTO buildings(name,floors) VALUES('测试楼','1,2,3,4')")
        c.execute("INSERT INTO tenants(name) VALUES('测试租户')")
        for floor, no in [(1, "101"), (3, "301"), (4, "401")]:
            c.execute("INSERT INTO rooms(building_id,floor,room_no,tenant_id)"
                      " VALUES(1,?,?,1)", (floor, no))
        for i in range(1, 6):
            c.execute("INSERT INTO people(name,person_no,tenant_id,room_id,status)"
                      " VALUES(?,?,?,?,'active')", (f"人员{i}", f"P{i}", 1, 2))
        # 组1：楼层 3,4 工作时段；组2：仅 1 层全天；组3：过期组
        c.execute("INSERT INTO permission_groups(name,time_start,time_end,valid_from,"
                  "valid_to,created_at) VALUES('研发组','08:00','20:00','2026-01-01',"
                  "'2026-12-31',?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("INSERT INTO permission_groups(name,time_start,time_end,created_at)"
                  " VALUES('大堂组','00:00','23:59',?)",
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("INSERT INTO permission_groups(name,time_start,time_end,valid_from,"
                  "valid_to,created_at) VALUES('过期组','00:00','23:59','2025-01-01',"
                  "'2025-12-31',?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        for gid, floors in [(1, [3, 4]), (2, [1]), (3, [1, 3])]:
            c.executemany("INSERT INTO group_floors(group_id,floor) VALUES(?,?)",
                          [(gid, f) for f in floors])
        c.execute("INSERT INTO devices(name,status,last_seen) VALUES('在线闸机', 'online',?)",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("INSERT INTO devices(name,status,last_seen) VALUES('离线闸机', 'offline',?)",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        for i in range(1, 8):
            c.execute("INSERT INTO cards(card_no,status) VALUES(?,'in_stock')",
                      (f"C-{i:03d}",))
        # 加两张临时库存卡
        c.execute("INSERT INTO cards(card_no,card_type,status) VALUES('T-001','temporary','in_stock')")
        c.execute("INSERT INTO cards(card_no,card_type,status) VALUES('T-002','temporary','in_stock')")
    return s


def issue(s, op, card, person, group, **kw):
    d = s.create_issue_draft(op, card, person, group, **kw)
    return s.confirm_issue_draft(op, d["id"])


# ============================================================== 权限边界

class TestPermissionBoundary(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_floor_allowed_denied(self):
        # 研发组 3 层允许、1 层拒绝
        at = datetime(2026, 9, 28, 10, 0)
        ok, _ = self.s.evaluate_permission(1, 3, at=at)
        self.assertTrue(ok)
        ok, reasons = self.s.evaluate_permission(1, 1, at=at)
        self.assertFalse(ok)
        self.assertTrue(any("楼层" in r for r in reasons))

    def test_time_window(self):
        # 研发组 08:00-20:00；7:59 拒绝，8:00 放行，20:00 边界放行，20:01 拒绝
        d = datetime(2026, 9, 28)
        for hm, expect in [("07:59", False), ("08:00", True),
                           ("20:00", True), ("20:01", False)]:
            at = d.replace(hour=int(hm[:2]), minute=int(hm[3:]))
            ok, _ = self.s.evaluate_permission(1, 3, at=at)
            self.assertEqual(ok, expect, f"{hm} 期望 {expect}")

    def test_valid_dates(self):
        # 过期组成立日期外 → 拒绝
        ok, reasons = self.s.evaluate_permission(3, 1)
        self.assertFalse(ok)
        self.assertTrue(any("到期" in r or "生效" in r for r in reasons))
        # 未来生效组
        with self.s.db.tx() as c:
            c.execute("INSERT INTO permission_groups(name,time_start,time_end,"
                      "valid_from,created_at) VALUES('未来组','00:00','23:59','2099-01-01',?)",
                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
            c.execute("INSERT INTO group_floors(group_id,floor) VALUES(4,1)")
        ok, reasons = self.s.evaluate_permission(4, 1)
        self.assertFalse(ok)
        self.assertTrue(any("起生效" in r for r in reasons))

    def test_access_denied_event_on_violation(self):
        issue(self.s, CLERK1, "C-001", 1, 1)  # 大堂组，仅 1 层
        r = self.s.attempt_access(GUARD, 1, "C-001", 4)  # 刷 4 层
        self.assertEqual(r["result"], "denied")
        types = [e["type"] for e in self.s.list_events("permission_violation")]
        self.assertIn("permission_violation", types)

    def test_lost_card_denied_at_gate(self):
        issue(self.s, CLERK1, "C-002", 2, 2)
        self.s.report_lost(GUARD, "C-002")
        r = self.s.attempt_access(GUARD, 1, "C-002", 1)
        self.assertEqual(r["result"], "denied")
        self.assertIn("挂失", r["reasons"][0])


# ============================================================== 发卡两步 / 激活前置

class TestIssueFlow(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_two_step_issue(self):
        d = self.s.create_issue_draft(CLERK1, "C-001", 1, 1)
        self.assertEqual(d["status"], "pending")
        card = self.s.db.conn.execute(
            "SELECT * FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(card["status"], "in_stock")  # 建单不激活
        confirmed = self.s.confirm_issue_draft(CLERK1, d["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        card = self.s.db.conn.execute(
            "SELECT * FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(card["status"], "active")
        self.assertEqual(card["person_id"], 1)
        self.assertEqual(card["group_id"], 1)
        self.assertIsNotNone(card["activated_at"])

    def test_duplicate_confirm_rejected(self):
        d = self.s.create_issue_draft(CLERK1, "C-001", 1, 1)
        self.s.confirm_issue_draft(CLERK1, d["id"])
        with self.assertRaises(ServiceError) as cm:
            self.s.confirm_issue_draft(CLERK1, d["id"])
        self.assertEqual(cm.exception.code, "draft_closed")

    def test_cancel_releases_card(self):
        d = self.s.create_issue_draft(CLERK1, "C-001", 1, 1)
        self.s.cancel_issue_draft(CLERK1, d["id"])
        # 撤销后其他柜台可发
        d2 = self.s.create_issue_draft(CLERK2, "C-001", 2, 2)
        self.s.confirm_issue_draft(CLERK2, d2["id"])
        card = self.s.db.conn.execute(
            "SELECT * FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(card["person_id"], 2)

    def test_cannot_issue_departed_person(self):
        self.s.depart_person(CLERK1, 3)
        with self.assertRaises(ServiceError) as cm:
            self.s.create_issue_draft(CLERK1, "C-003", 3, 1)
        self.assertEqual(cm.exception.code, "person_departed")

    def test_temporary_card_requires_expiry(self):
        with self.assertRaises(ServiceError):
            self.s.create_issue_draft(CLERK1, "T-001", 1, 1,
                                     card_type="temporary")


# ============================================================== 卡号唯一（并发）

class TestConcurrentIssue(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())
        self.barrier = threading.Barrier(2)
        self.results = {}

    def _clerk_flow(self, op_id, key):
        """两个柜台对同一卡号走完整建单→确认。"""
        try:
            self.barrier.wait(timeout=10)
            d = self.s.create_issue_draft(op_id, "C-001",
                                         1 if op_id == CLERK1 else 2, 1)
            self.s.confirm_issue_draft(op_id, d["id"])
            self.results[key] = ("ok", d["id"])
        except ServiceError as e:
            self.results[key] = ("rejected", e.code)

    def test_only_one_counter_succeeds(self):
        t1 = threading.Thread(target=self._clerk_flow, args=(CLERK1, "A"))
        t2 = threading.Thread(target=self._clerk_flow, args=(CLERK2, "B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(v[0] for v in self.results.values())
        self.assertEqual(statuses, ["ok", "rejected"])
        # 库中该卡只有一条 active 记录，只绑定一个人
        row = self.s.db.conn.execute(
            "SELECT status, person_id FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(row["status"], "active")
        self.assertIn(row["person_id"], (1, 2))
        active_count = self.s.db.conn.execute(
            "SELECT COUNT(*) FROM cards WHERE card_no='C-001' AND status='active'"
        ).fetchone()[0]
        self.assertEqual(active_count, 1)


# ============================================================== 挂失 / 损坏 / 补卡

class TestLostAndReplace(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())
        issue(self.s, CLERK1, "C-001", 1, 1)

    def test_lost_freezes_and_revokes(self):
        self.s.report_lost(GUARD, "C-001")
        card = self.s.db.conn.execute(
            "SELECT * FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(card["status"], "lost")
        # 挂失同时下发吊销指令
        last = self.s.db.conn.execute(
            "SELECT command FROM device_outbox WHERE card_no='C-001'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(last["command"], "revoke")

    def test_lost_cannot_be_restored_by_reentry(self):
        self.s.report_lost(CLERK1, "C-001")
        with self.assertRaises(ServiceError) as cm:
            self.s.restore_lost_directly(CLERK1, "C-001")
        self.assertEqual(cm.exception.code, "lost_restore_denied")
        # 状态依旧是挂失
        card = self.s.db.conn.execute(
            "SELECT status FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(card["status"], "lost")
        # 也不能走普通发卡流程补录
        with self.assertRaises(ServiceError):
            self.s.create_issue_draft(CLERK1, "C-001", 1, 1)

    def test_replace_keeps_old_card_relationship(self):
        self.s.report_lost(CLERK1, "C-001")
        new = self.s.replace_card(CLERK1, "C-001", "C-002")
        self.assertEqual(new["status"], "active")
        self.assertEqual(new["person_id"], 1)
        old = self.s.db.conn.execute(
            "SELECT * FROM cards WHERE card_no='C-001'").fetchone()
        new_row = self.s.db.conn.execute(
            "SELECT * FROM cards WHERE card_no='C-002'").fetchone()
        self.assertEqual(old["status"], "lost")  # 旧卡不恢复
        self.assertEqual(new_row["replaced_card_id"], old["id"])
        self.assertEqual(old["replaced_by_card_id"], new_row["id"])

    def test_damaged_card_can_be_replaced(self):
        self.s.report_damaged(GUARD, "C-001", note="折断")
        self.s.replace_card(CLERK1, "C-001", "C-003")
        old = self.s.db.conn.execute(
            "SELECT status FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(old["status"], "damaged")

    def test_active_cannot_replace(self):
        with self.assertRaises(ServiceError):
            self.s.replace_card(CLERK1, "C-001", "C-002")


# ============================================================== 离场冻结 / 回收

class TestDepartRecycle(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_depart_freezes_all_cards(self):
        issue(self.s, CLERK1, "C-001", 1, 1)
        issue(self.s, CLERK1, "C-002", 1, 2)
        r = self.s.depart_person(CLERK2, 1)
        self.assertEqual(sorted(r["frozen_cards"]), ["C-001", "C-002"])
        cnt = self.s.db.conn.execute(
            "SELECT COUNT(*) FROM cards WHERE person_id=1 AND status='frozen'"
        ).fetchone()[0]
        self.assertEqual(cnt, 2)
        person = self.s.db.conn.execute(
            "SELECT status FROM people WHERE id=1").fetchone()
        self.assertEqual(person["status"], "departed")

    def test_frozen_then_recycle(self):
        issue(self.s, CLERK1, "C-001", 1, 1)
        self.s.depart_person(CLERK1, 1)
        self.s.recycle_card(CLERK1, "C-001")
        card = self.s.db.conn.execute(
            "SELECT status,recycled_at FROM cards WHERE card_no='C-001'").fetchone()
        self.assertEqual(card["status"], "recycled")
        self.assertIsNotNone(card["recycled_at"])

    def test_active_cannot_directly_recycle(self):
        issue(self.s, CLERK1, "C-001", 1, 1)
        with self.assertRaises(ServiceError):
            self.s.recycle_card(CLERK1, "C-001")

    def test_frozen_card_denied_at_gate(self):
        issue(self.s, CLERK1, "C-001", 1, 2)
        self.s.depart_person(CLERK1, 1)
        r = self.s.attempt_access(GUARD, 1, "C-001", 1)
        self.assertEqual(r["result"], "denied")


# ============================================================== 临时卡到期

class TestTemporaryExpiry(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_due_temp_card_auto_expires(self):
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        d = self.s.create_issue_draft(CLERK1, "T-001", 1, 2,
                                      card_type="temporary", expires_at=past)
        self.s.confirm_issue_draft(CLERK1, d["id"])
        expired = self.s.expire_temporary_cards()
        self.assertIn("T-001", expired)
        card = self.s.db.conn.execute(
            "SELECT status FROM cards WHERE card_no='T-001'").fetchone()
        self.assertEqual(card["status"], "expired")
        # 到期卡门禁拒绝
        r = self.s.attempt_access(GUARD, 1, "T-001", 1)
        self.assertEqual(r["result"], "denied")

    def test_future_temp_card_stays_active(self):
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        d = self.s.create_issue_draft(CLERK1, "T-002", 1, 2,
                                      card_type="temporary", expires_at=future)
        self.s.confirm_issue_draft(CLERK1, d["id"])
        self.assertEqual(self.s.expire_temporary_cards(), [])
        card = self.s.db.conn.execute(
            "SELECT status FROM cards WHERE card_no='T-002'").fetchone()
        self.assertEqual(card["status"], "active")


# ============================================================== 设备离线 / 补传

class TestDeviceOffline(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_offline_device_buffers_and_flushes(self):
        # 设备2离线：发卡指令进入缓冲
        # 手动把卡发到离线设备：构造一条仅针对设备2的指令
        with self.s.db.tx() as c:
            card = dict(c.execute("SELECT * FROM cards WHERE card_no='C-001'").fetchone())
            self.s._device_command(c, card, "issue", device_id=2)
        pending = self.s.db.conn.execute(
            "SELECT COUNT(*) FROM device_outbox WHERE device_id=2 AND status='pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)

        # 离线期间刷卡本地记录、synced=0
        issue(self.s, CLERK1, "C-001", 1, 2)
        r = self.s.attempt_access(GUARD, 2, "C-001", 1)
        self.assertEqual(r["device_status"], "offline")
        ev = self.s.db.conn.execute(
            "SELECT synced FROM access_events WHERE device_id=2 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ev["synced"], 0)

        # 恢复在线 → 自动补传
        self.s.set_device_status(GUARD, 2, True)
        pending_after = self.s.db.conn.execute(
            "SELECT COUNT(*) FROM device_outbox WHERE device_id=2 AND status='pending'"
        ).fetchone()[0]
        self.assertEqual(pending_after, 0)
        ev2 = self.s.db.conn.execute(
            "SELECT synced FROM access_events WHERE device_id=2 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ev2["synced"], 1)
        # 恢复事件
        self.assertTrue(self.s.list_events("device_online"))

    def test_offline_then_online_generates_events(self):
        with self.assertRaises(ServiceError):
            self.s.set_device_status(GUARD, 2, False)  # 已是离线
        self.s.set_device_status(GUARD, 1, False)
        self.assertTrue(self.s.list_events("device_offline"))

    def test_guard_cannot_issue_but_can_toggle_device(self):
        # 门岗可以切换设备，不能发卡
        self.s.set_device_status(GUARD, 1, False)  # 不抛错即通过
        with self.assertRaises(ServiceError) as cm:
            self.s.create_issue_draft(GUARD, "C-004", 1, 1)
        self.assertEqual(cm.exception.code, "forbidden")


# ============================================================== 岗位权限

class TestRolePermissions(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_auditor_readonly(self):
        # 审计员不能发卡/挂失/改设备/改权限
        for fn in [
            lambda: self.s.create_issue_draft(AUDITOR, "C-001", 1, 1),
            lambda: self.s.report_lost(AUDITOR, "C-001"),
            lambda: self.s.set_device_status(AUDITOR, 1, False),
        ]:
            with self.assertRaises(ServiceError) as cm:
                fn()
            self.assertEqual(cm.exception.code, "forbidden")
        with self.assertRaises(ServiceError):
            self.s.create_permission_change(AUDITOR, 1, floors=[1])
        # 但可以读审计日志
        self.assertIsInstance(self.s.list_audit(), list)

    def test_clerk_cannot_change_permissions(self):
        with self.assertRaises(ServiceError) as cm:
            self.s.create_permission_change(CLERK1, 1, floors=[1, 3])
        self.assertEqual(cm.exception.code, "forbidden")

    def test_guard_can_report_lost(self):
        issue(self.s, CLERK1, "C-001", 1, 1)
        self.s.report_lost(GUARD, "C-001")  # 不抛错
        self.assertEqual(
            self.s.db.conn.execute(
                "SELECT status FROM cards WHERE card_no='C-001'").fetchone()["status"],
            "lost")

    def test_anonymous_rejected(self):
        with self.assertRaises(ServiceError) as cm:
            self.s.create_issue_draft(None, "C-001", 1, 1)
        self.assertEqual(cm.exception.code, "forbidden")


# ============================================================== 权限变更二次确认

class TestPermissionChange(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())

    def test_change_requires_two_steps(self):
        ch = self.s.create_permission_change(ADMIN, 1, floors=[3, 4, 1],
                                             time_end="22:00", reason="加班")
        # 提交后组尚未变化
        g = self.s.db.conn.execute(
            "SELECT time_end FROM permission_groups WHERE id=1").fetchone()
        self.assertEqual(g["time_end"], "20:00")
        floors = [r[0] for r in self.s.db.conn.execute(
            "SELECT floor FROM group_floors WHERE group_id=1")]
        self.assertEqual(sorted(floors), [3, 4])
        # 二次确认后生效
        self.s.confirm_permission_change(ADMIN, ch["id"], True)
        g = self.s.db.conn.execute(
            "SELECT time_end FROM permission_groups WHERE id=1").fetchone()
        self.assertEqual(g["time_end"], "22:00")
        floors = [r[0] for r in self.s.db.conn.execute(
            "SELECT floor FROM group_floors WHERE group_id=1")]
        self.assertEqual(sorted(floors), [1, 3, 4])

    def test_rejected_change_has_no_effect(self):
        ch = self.s.create_permission_change(ADMIN, 2, floors=[1, 2])
        self.s.confirm_permission_change(ADMIN, ch["id"], False)
        floors = [r[0] for r in self.s.db.conn.execute(
            "SELECT floor FROM group_floors WHERE group_id=2")]
        self.assertEqual(floors, [1])
        with self.assertRaises(ServiceError):
            self.s.confirm_permission_change(ADMIN, ch["id"], True)

    def test_permission_change_audited(self):
        ch = self.s.create_permission_change(ADMIN, 1, time_end="21:00")
        self.s.confirm_permission_change(ADMIN, ch["id"], True)
        actions = [a["action"] for a in self.s.list_audit()]
        self.assertIn("提交权限变更(待确认)", actions)
        self.assertIn("确认权限变更生效", actions)


# ============================================================== 借用转交

class TestBorrow(unittest.TestCase):
    def setUp(self):
        self.s = setup_minimal(fresh_db())
        issue(self.s, CLERK1, "C-001", 1, 2)  # 大堂组：1层全天，避免时段依赖

    def test_borrow_keeps_card_permission(self):
        self.s.borrow_card(CLERK1, "C-001", 2, note="周末借用")
        loan = self.s.db.conn.execute(
            "SELECT * FROM card_loans WHERE card_id=1").fetchone()
        self.assertEqual(loan["status"], "active")
        self.assertEqual(loan["to_person_id"], 2)
        # 卡仍可用，权限随卡
        r = self.s.attempt_access(GUARD, 1, "C-001", 1)
        self.assertEqual(r["result"], "granted")

    def test_cannot_borrow_to_self_or_lost(self):
        with self.assertRaises(ServiceError):
            self.s.borrow_card(CLERK1, "C-001", 1)
        self.s.report_lost(GUARD, "C-001")
        with self.assertRaises(ServiceError):
            self.s.borrow_card(CLERK1, "C-001", 2)


# ============================================================== 遮挡与其他

class TestMasking(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask_card_no("A-1009"), "A-●●●9")
        self.assertEqual(mask_card_no("C-001"), "C-●●1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
