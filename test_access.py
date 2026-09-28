"""系统测试：权限边界、卡片唯一并发、到期失效、设备离线补传、岗位权限，
以及激活前置、挂失补卡、离场回收、二次确认等核心规则。

运行：python3 -m unittest -v
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from core import (
    AccessError, AccessScheduler, AccessSystem, DeviceAdapter, PermissionDenied,
    ACCESS_ALLOW, ACCESS_DENY_FLOOR, ACCESS_DENY_TIME, ACCESS_DENY_DATE, ACCESS_DENY_CARD,
    CARD_AVAILABLE, CARD_EXPIRED, CARD_ISSUED, CARD_LOST, CARD_REVOKED,
    CARD_TEMP, EV, ROLE_ADMIN, ROLE_AUDITOR, ROLE_CLERK, mask_number,
)
from seed import build_system


def _fresh():
    return build_system(":memory:")


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.s = _fresh()
        # seed 结束时设备处于离线且有 1 笔排队（AC-3002），各用例先恢复并补传，
        # 需要离线场景的用例再自行置离线。
        self.s.device.set_online(True)
        self.s.sync_device(role=ROLE_ADMIN)

    def tearDown(self):
        self.s.close()


# --------------------------------------------------------------------------
# 1. 权限边界：楼层 × 时段 × 生效日期 × 卡片状态
# --------------------------------------------------------------------------

class PermissionBoundaryTests(BaseCase):
    def test_floor_outside_group_denied(self):
        # AC-1001 属楼内办公组（1/8/12 层），刷 6 层机房 -> 拒绝
        r = self.s.evaluate_access("AC-1001", 6, record=True)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["code"], ACCESS_DENY_FLOOR)
        evts = self.s.list_events(EV["PERMISSION_DENIED"])
        self.assertTrue(any("6 层" in e["detail"] for e in evts))

    def test_floor_allowed_in_window(self):
        at = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        r = self.s.evaluate_access("AC-1001", 12, at=at)
        self.assertTrue(r["allowed"])
        self.assertEqual(r["code"], ACCESS_ALLOW)

    def test_time_window_denied(self):
        # 办公组 08:00-20:00；早上 7 点拒绝
        at = datetime.now().replace(hour=7, minute=5, second=0, microsecond=0)
        r = self.s.evaluate_access("AC-1001", 12, at=at)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["code"], ACCESS_DENY_TIME)
        # 维护组机房 09:00-11:30；中午拒绝
        night = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        # MC-0001 已被 seed 回收，直接发一张维护组新卡
        self.s.issue_card("MC-0099", self._pid("柳眠"), 2, role=ROLE_CLERK)
        r2 = self.s.evaluate_access("MC-0099", 6, at=night)
        self.assertEqual(r2["code"], ACCESS_DENY_TIME)

    def test_validity_dates_denied(self):
        # 访客临时组规则 7 天后失效
        future = datetime.now() + timedelta(days=30)
        at = future.replace(hour=10, minute=0, second=0, microsecond=0)
        r = self.s.evaluate_access("TC-9001", 1, at=at)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["code"], ACCESS_DENY_DATE)

    def test_card_status_blocks_access(self):
        # 挂失卡
        self.assertEqual(self.s.get_card("AC-1002R")["status"], CARD_AVAILABLE)
        self.s.report_lost("AC-1002R", role=ROLE_CLERK)
        r = self.s.evaluate_access("AC-1002R", 12)
        self.assertEqual(r["code"], ACCESS_DENY_CARD)
        # 已回收卡
        r2 = self.s.evaluate_access("MC-0001", 1)
        self.assertEqual(r2["code"], ACCESS_DENY_CARD)
        self.assertEqual(r2["reason"].split("，")[0], "卡片状态为revoked")

    def test_unknown_card_denied(self):
        r = self.s.evaluate_access("NO-SUCH", 1)
        self.assertEqual(r["code"], ACCESS_DENY_CARD)

    def _pid(self, name):
        for p in self.s.list_people():
            if p["name"] == name:
                return p["id"]
        raise KeyError(name)


# --------------------------------------------------------------------------
# 2. 激活前置 + 挂失不能补录恢复 + 补卡保留旧卡关系
# --------------------------------------------------------------------------

class IssueAndReissueTests(BaseCase):
    def _pid(self, name):
        return next(p["id"] for p in self.s.list_people() if p["name"] == name)

    def test_card_must_bind_person_and_group(self):
        # 手工造一张未绑定卡：激活必须失败
        cur = self.s.conn.cursor()
        cur.execute(
            "INSERT INTO cards(card_no,card_type,status,created_at) VALUES (?,?,?,?)",
            ("AC-BARE", "regular", CARD_ISSUED, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.s.conn.commit()
        with self.assertRaises(AccessError) as cm:
            self.s.activate_card("AC-BARE", role=ROLE_CLERK)
        self.assertIn("绑定人员和权限组", str(cm.exception))
        # 正常发卡（绑定齐全）自动激活
        r = self.s.issue_card("AC-5001", self._pid("简言"), 1,
                              counter="A柜台", role=ROLE_CLERK)
        self.assertTrue(r["activated"])
        self.assertEqual(self.s.get_card("AC-5001")["status"], CARD_AVAILABLE)

    def test_lost_card_cannot_be_restored_by_reentry(self):
        self.s.report_lost("AC-1001", role=ROLE_CLERK, actor="沈括")
        # 用原卡号“补录”必须被拒绝
        with self.assertRaises(AccessError):
            self.s.reissue_card("AC-1001", "AC-1001", role=ROLE_CLERK)
        # 挂失卡也不能直接重新激活
        with self.assertRaises(AccessError):
            self.s.activate_card("AC-1001", role=ROLE_CLERK)
        # 只能换新卡号
        r = self.s.reissue_card("AC-1001", "AC-1001N", role=ROLE_CLERK)
        self.assertEqual(r["new_card_no"], "AC-1001N")
        old = self.s.get_card("AC-1001")
        new = self.s.get_card("AC-1001N")
        self.assertEqual(old["status"], CARD_LOST)          # 旧卡保持挂失
        self.assertEqual(new["status"], CARD_AVAILABLE)
        self.assertEqual(new["replaced_by_id"], old["id"])  # 新卡保留旧卡关系

    def test_reissue_only_for_lost(self):
        # 可用卡不能走补卡
        with self.assertRaises(AccessError):
            self.s.reissue_card("AC-1003", "AC-1003N", role=ROLE_CLERK)
        rejected = self.s.list_events(EV["LOST_REISSUE_REJECTED"])
        self.assertTrue(any("非法补录" in e["detail"] for e in rejected))

    def test_concurrent_same_card_no_only_one_wins(self):
        """两个柜台线程同时发放同一卡号：仅一笔成功。"""
        pid = self._pid("蔺雨时")
        barrier = threading.Barrier(2)
        results = []

        def clerk(counter, actor):
            barrier.wait()
            try:
                self.s.issue_card("AC-DUAL", pid, 1, counter=counter,
                                  role=ROLE_CLERK, actor=actor)
                results.append(("ok", counter))
            except AccessError:
                results.append(("fail", counter))

        t1 = threading.Thread(target=clerk, args=("A柜台", "沈括"))
        t2 = threading.Thread(target=clerk, args=("B柜台", "周知音"))
        t1.start(); t2.start(); t1.join(); t2.join()
        outcomes = sorted(r[0] for r in results)
        self.assertEqual(outcomes, ["fail", "ok"])
        winners = [r[1] for r in results if r[0] == "ok"]
        self.assertEqual(len(winners), 1)
        # 库里只有一张 AC-DUAL
        n = self.s.conn.execute(
            "SELECT COUNT(*) c FROM cards WHERE card_no='AC-DUAL'").fetchone()["c"]
        self.assertEqual(n, 1)
        # 被拒一方留下审计事件
        denied = self.s.list_events(EV["PERMISSION_DENIED"])
        self.assertTrue(any("AC-DUAL" in (e["detail"] or "") for e in denied))


# --------------------------------------------------------------------------
# 3. 临时卡到期自动失效
# --------------------------------------------------------------------------

class TempCardExpiryTests(BaseCase):
    def _pid(self, name):
        return next(p["id"] for p in self.s.list_people() if p["name"] == name)

    def test_manual_expire_scan(self):
        # seed 已预置到期卡 TC-9002（expired）与未到期卡 TC-9001
        self.assertEqual(self.s.get_card("TC-9002")["status"], CARD_EXPIRED)
        self.assertEqual(self.s.get_card("TC-9001")["status"], CARD_AVAILABLE)
        self.assertEqual(self.s.expire_temp_cards(), [])  # 幂等，不重复处理

    def test_expire_then_denied(self):
        soon = (datetime.now() + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.s.issue_card("TC-9010", self._pid("薛葡萄"), 1, card_type=CARD_TEMP,
                          expires_at=soon, role=ROLE_CLERK)
        time.sleep(1.2)
        expired = self.s.expire_temp_cards()
        self.assertEqual(expired, ["TC-9010"])
        r = self.s.evaluate_access("TC-9010", 1)
        self.assertEqual(r["code"], ACCESS_DENY_CARD)
        self.assertTrue(self.s.list_events(EV["EXPIRE"]))

    def test_scheduler_auto_expires(self):
        sched = AccessScheduler(self.s, interval=0.2)
        sched.start()
        try:
            past = (datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            self.s.issue_card("TC-9020", self._pid("阮清越"), 1, card_type=CARD_TEMP,
                              expires_at=past, role=ROLE_CLERK)
            deadline = time.time() + 5
            while time.time() < deadline and self.s.get_card("TC-9020")["status"] != CARD_EXPIRED:
                time.sleep(0.1)
            self.assertEqual(self.s.get_card("TC-9020")["status"], CARD_EXPIRED)
        finally:
            sched.stop()


# --------------------------------------------------------------------------
# 4. 设备离线补传
# --------------------------------------------------------------------------

class DeviceOfflineSyncTests(BaseCase):
    def _pid(self, name):
        return next(p["id"] for p in self.s.list_people() if p["name"] == name)

    def test_offline_issue_queues_then_sync_delivers(self):
        self.s.set_device_online(False, role=ROLE_ADMIN)
        r = self.s.issue_card("AC-7001", self._pid("陆昭"), 1, role=ROLE_CLERK)
        self.assertFalse(r["device"]["delivered"])
        self.assertTrue(r["device"]["queued"])
        card = self.s.get_card("AC-7001")
        self.assertEqual(card["status"], CARD_AVAILABLE)        # 卡本身可用…
        self.assertEqual(card["device_delivered"], 0)          # …但授权未下发
        self.assertEqual(self.s.device.status()["pending"], 1)
        self.assertTrue(any(e["event_type"] == EV["DEVICE_OFFLINE"]
                            for e in self.s.list_events()))
        # 离线期间无法补传
        with self.assertRaises(AccessError):
            self.s.sync_device(role=ROLE_ADMIN)
        # 恢复后补传
        self.s.device.set_online(True)
        sync = self.s.sync_device(role=ROLE_ADMIN)
        self.assertEqual(sync["synced"], 1)
        self.assertEqual(self.s.get_card("AC-7001")["device_delivered"], 1)
        self.assertEqual(self.s.device.status()["pending"], 0)
        self.assertTrue(any(e["event_type"] == EV["DEVICE_SYNC"]
                            for e in self.s.list_events()))
        # 再次补传为空操作
        self.assertEqual(self.s.sync_device(role=ROLE_ADMIN)["synced"], 0)

    def test_adapter_directly(self):
        d = DeviceAdapter()
        self.assertTrue(d.write_card("X", [1], "DEV")["delivered"])
        d.set_online(False)
        self.assertTrue(d.write_card("Y", [2], "DEV")["queued"])
        with self.assertRaises(AccessError):
            d.sync_pending()
        d.set_online(True)
        self.assertEqual(d.sync_pending()["synced"], 1)


# --------------------------------------------------------------------------
# 5. 岗位权限
# --------------------------------------------------------------------------

class RoleTests(BaseCase):
    def test_clerk_cannot_manage_groups_or_device(self):
        with self.assertRaises(PermissionDenied):
            self.s.create_group("越权组", role=ROLE_CLERK)
        with self.assertRaises(PermissionDenied):
            self.s.set_group_rule(1, 2, "09:00", "18:00",
                                  "2026-01-01", "2026-12-31", confirm=True, role=ROLE_CLERK)
        with self.assertRaises(PermissionDenied):
            self.s.set_device_online(False, role=ROLE_CLERK)

    def test_clerk_can_issue_and_recycle(self):
        pid = next(p["id"] for p in self.s.list_people() if p["name"] == "陆昭")
        r = self.s.issue_card("AC-8001", pid, 1, role=ROLE_CLERK)
        self.assertTrue(r["activated"])
        rec = self.s.recycle_card("AC-8001", role=ROLE_CLERK)
        self.assertEqual(rec["status"], CARD_REVOKED)

    def test_auditor_read_only(self):
        for fn in (
            lambda: self.s.issue_card("AC-8002", 1, 1, role=ROLE_AUDITOR),
            lambda: self.s.report_lost("AC-1001", role=ROLE_AUDITOR),
            lambda: self.s.recycle_card("AC-1003", role=ROLE_AUDITOR),
            lambda: self.s.create_group("x", role=ROLE_AUDITOR),
            lambda: self.s.sync_device(role=ROLE_AUDITOR),
        ):
            with self.assertRaises(PermissionDenied):
                fn()
        # 查询不受影响
        self.assertTrue(len(self.s.list_cards()) > 0)
        self.assertTrue(len(self.s.list_events()) > 0)


# --------------------------------------------------------------------------
# 6. 离场回收 / 损坏 / 转交 / 二次确认
# --------------------------------------------------------------------------

class RecycleAndAdminTests(BaseCase):
    def test_recycle_freezes_and_records_status(self):
        r = self.s.recycle_card("AC-2001", reason="合同到期", role=ROLE_CLERK)
        self.assertEqual(r["old_status"], CARD_AVAILABLE)
        self.assertEqual(self.s.get_card("AC-2001")["status"], CARD_REVOKED)
        rec = [x for x in self.s.list_recycles() if x["card_no"] == "AC-2001"][0]
        self.assertEqual(rec["card_status"], CARD_AVAILABLE)   # 回收时的旧状态
        self.assertEqual(rec["reason"], "合同到期")
        self.assertEqual(rec["person_name"], "霍连峰")
        person = next(p for p in self.s.list_people() if p["name"] == "霍连峰")
        self.assertEqual(person["status"], "left")            # 无在持卡 -> 离场
        self.assertIsNotNone(person["left_at"])
        # 已离场人员不能再发卡
        with self.assertRaises(AccessError):
            self.s.issue_card("AC-8010", person["id"], 1, role=ROLE_CLERK)

    def test_damage_and_transfer(self):
        self.s.report_damaged("AC-2002", role=ROLE_CLERK)
        with self.assertRaises(AccessError):  # 损坏卡不能转交
            self.s.transfer_card("AC-2002", 1, role=ROLE_CLERK)
        pid = next(p["id"] for p in self.s.list_people() if p["name"] == "程述")
        self.s.transfer_card("AC-3001", pid, role=ROLE_CLERK)
        self.assertEqual(self.s.get_card("AC-3001")["person_id"], pid)
        self.assertTrue(self.s.list_events(EV["BORROW"]))

    def test_permission_change_requires_second_confirm(self):
        today = datetime.now().date().isoformat()
        # 楼层 1 已有规则：首次无 confirm 被拒
        with self.assertRaises(AccessError) as cm:
            self.s.set_group_rule(1, 1, "09:00", "11:00", today, today,
                                  confirm=False, role=ROLE_ADMIN)
        self.assertIn("二次确认", str(cm.exception))
        self.assertTrue(self.s.list_events(EV["PERMISSION_CHANGE_REJECTED"]))
        # 二次确认后成功并留痕
        self.s.set_group_rule(1, 1, "09:00", "11:00", today, today,
                              confirm=True, role=ROLE_ADMIN)
        changes = self.s.list_events(EV["PERMISSION_CHANGE"])
        self.assertTrue(any("楼层1" in e["detail"] for e in changes))

    def test_sensitive_number_masked(self):
        self.assertEqual(mask_number("AC-1001"), "AC-**01")
        self.assertEqual(mask_number("110101199003071234"), "110" + "*" * 13 + "34")
        p = self.s.list_people()[0]
        self.assertIn("*", p["id_no_masked"])


# --------------------------------------------------------------------------
# 7. HTTP 端到端（两步发卡、岗位 403、状态查询、离线补传接口）
# --------------------------------------------------------------------------

class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as appmod
        cls.appmod = appmod
        appmod.SYSTEM = _fresh()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.appmod.SYSTEM.close()

    def _req(self, method, path, body=None, user="clerk_a"):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data if method == "POST" else None,
            method=method,
            headers={"Content-Type": "application/json", "X-User-Id": user})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_state_and_masking(self):
        status, data = self._req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["me"]["username"], "clerk_a")
        self.assertTrue(all("*" in c["card_no"] for c in data["cards"]))
        # 柜台不能看明文
        status2, data2 = self._req("GET", "/api/state?reveal=1", user="clerk_a")
        self.assertFalse(data2["revealed"])
        # 管理员可以
        _, data3 = self._req("GET", "/api/state?reveal=1", user="admin")
        self.assertTrue(data3["revealed"])
        self.assertIn("AC-", data3["cards"][0]["card_no"])

    def test_two_step_issue_flow(self):
        person_id = next(p["id"] for p in self.appmod.SYSTEM.list_people()
                         if p["name"] == "蔺雨时")
        s1, pre = self._req("POST", "/api/issue/prepare", {
            "card_no": "AC-HTTP-1", "person_id": person_id, "group_id": 1,
            "counter": "A柜台"}, user="clerk_a")
        self.assertEqual(s1, 200)
        self.assertIn("token", pre)
        # token 与岗位绑定：B柜台不能确认 A 的预填单
        s_other, body_other = self._req("POST", "/api/issue/confirm",
                                        {"token": pre["token"]}, user="clerk_b")
        self.assertEqual(s_other, 400)
        # A 柜台确认成功
        s2, done = self._req("POST", "/api/issue/confirm",
                             {"token": pre["token"]}, user="clerk_a")
        self.assertEqual(s2, 200)
        self.assertTrue(done["activated"])
        # 同卡号再发 -> prepare 阶段就被拒
        s3, body3 = self._req("POST", "/api/issue/prepare", {
            "card_no": "AC-HTTP-1", "person_id": person_id, "group_id": 1},
            user="clerk_b")
        self.assertEqual(s3, 400)
        self.assertIn("已存在", body3["error"])

    def test_role_enforcement_over_http(self):
        # 柜台切设备离线 -> 403
        s, body = self._req("POST", "/api/device/offline", {}, user="clerk_a")
        self.assertEqual(s, 403)
        # 审计员发卡 -> 403
        s2, _ = self._req("POST", "/api/issue/prepare",
                          {"card_no": "AC-AUD", "person_id": 1, "group_id": 1},
                          user="auditor")
        self.assertEqual(s2, 403)
        # 管理员可以切离线，随后发卡排队、恢复补传
        self.assertEqual(self._req("POST", "/api/device/offline", {}, user="admin")[0], 200)
        pid = next(p["id"] for p in self.appmod.SYSTEM.list_people() if p["name"] == "简言")
        self._req("POST", "/api/issue/prepare",
                  {"card_no": "AC-OFFLINE", "person_id": pid, "group_id": 3},
                  user="clerk_b")
        # 取 prepare 返回的 token
        # （上面的请求结果未保留，重新走一遍）
        _, pre = self._req("POST", "/api/issue/prepare",
                           {"card_no": "AC-OFFLINE2", "person_id": pid, "group_id": 3},
                           user="clerk_b")
        self._req("POST", "/api/issue/confirm", {"token": pre["token"]}, user="clerk_b")
        self.assertEqual(self.appmod.SYSTEM.get_card("AC-OFFLINE2")["device_delivered"], 0)
        self.assertEqual(self._req("POST", "/api/device/online", {}, user="admin")[0], 200)
        s_sync, sync = self._req("POST", "/api/device/sync", {}, user="clerk_a")
        self.assertEqual(s_sync, 200)
        self.assertGreaterEqual(sync["synced"], 1)
        self.assertEqual(self.appmod.SYSTEM.get_card("AC-OFFLINE2")["device_delivered"], 1)

    def test_access_check_api(self):
        _, r = self._req("POST", "/api/access/check",
                         {"card_no": "AC-1001", "floor": 6}, user="clerk_a")
        self.assertFalse(r["allowed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
