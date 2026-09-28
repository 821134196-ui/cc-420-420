"""楼宇门禁卡发放与离场回收系统 —— 核心领域层。

职责：
- SQLite 持久化（楼栋/租户/人员/门禁卡/权限组/权限规则/事件/回收记录/设备）
- 发卡、激活、挂失、补卡、损坏、借用转交、离场回收等业务规则
- 权限按楼层 × 时段 × 生效日期计算
- 本地设备适配器：模拟发卡写入与离线补传
- 进程内任务：周期扫描临时卡到期自动失效
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 卡片状态
CARD_AVAILABLE = "available"      # 可用（已激活）
CARD_ISSUED = "issued"            # 已发放未激活（未绑定权限组或人员不完整）
CARD_LOST = "lost"                # 挂失
CARD_DAMAGED = "damaged"          # 损坏
CARD_REVOKED = "revoked"         # 离场回收
CARD_EXPIRED = "expired"          # 临时卡到期
CARD_RECYCLED = "recycled"        # 已入库再利用
CARD_FROZEN = "frozen"            # 冻结（离场流程中）

ACTIVE_STATES = {CARD_AVAILABLE, CARD_ISSUED}

# 人员离场状态
PERSON_ACTIVE = "active"
PERSON_LEFT = "left"

# 卡片类型
CARD_REGULAR = "regular"
CARD_TEMP = "temp"

# 事件类型
EV = {
    "ISSUE": "issue",
    "ACTIVATE": "activate",
    "LOST": "lost",
    "LOST_REISSUE_REJECTED": "lost_reissue_rejected",
    "REPLACE": "replace",
    "DAMAGE": "damage",
    "BORROW": "borrow",
    "PERMISSION_DENIED": "permission_denied",
    "DEVICE_OFFLINE": "device_offline",
    "DEVICE_SYNC": "device_sync",
    "RECYCLE": "recycle",
    "EXPIRE": "expire",
    "PERMISSION_CHANGE": "permission_change",
    "PERMISSION_CHANGE_REJECTED": "permission_change_rejected",
    "TEMP_ISSUE": "temp_issue",
}

# 角色
ROLE_ADMIN = "admin"
ROLE_CLERK = "clerk"
ROLE_AUDITOR = "auditor"

ROLE_LABELS = {
    ROLE_ADMIN: "管理员",
    ROLE_CLERK: "发卡柜台",
    ROLE_AUDITOR: "审计员",
}

# 权限计算结果码
ACCESS_ALLOW = "allow"
ACCESS_DENY_FLOOR = "deny_floor"
ACCESS_DENY_TIME = "deny_time"
ACCESS_DENY_DATE = "deny_date"
ACCESS_DENY_CARD = "deny_card"


class AccessError(Exception):
    """业务规则错误，HTTP 层映射为 400。"""


class PermissionDenied(Exception):
    """岗位权限不足，HTTP 层映射为 403。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return date.today().isoformat()


def mask_number(number: str) -> str:
    """敏感号码默认部分遮挡：保留前 3 位与后 2 位。"""
    if number is None:
        return ""
    s = str(number)
    if len(s) <= 5:
        return s[0] + "*" * (len(s) - 1) if s else s
    return s[:3] + "*" * (len(s) - 5) + s[-2:]


def parse_time_hhmm(value: str) -> int:
    """'HH:MM' -> 当日分钟数。"""
    parts = value.split(":")
    return int(parts[0]) * 60 + int(parts[1])


# ---------------------------------------------------------------------------
# 设备适配器
# ---------------------------------------------------------------------------

class DeviceAdapter:
    """模拟门禁发卡设备。

    设备可能离线；离线期间的发卡操作进入 pending 队列，
    设备恢复后由 sync_pending() 补传。线程安全。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.online = True
        self.pending: list[dict[str, Any]] = []
        self.written: list[dict[str, Any]] = []
        self.last_sync: Optional[str] = None

    def set_online(self, online: bool) -> None:
        with self._lock:
            self.online = online

    def write_card(self, card_no: str, floors: list[int], device_id: str) -> dict[str, Any]:
        """向设备写入卡片楼层授权。设备离线则排队等待补传。"""
        payload = {"card_no": card_no, "floors": floors, "device_id": device_id, "ts": now_str()}
        with self._lock:
            if self.online:
                self.written.append(payload)
                return {"delivered": True, "queued": False}
            self.pending.append(payload)
            return {"delivered": False, "queued": True}

    def sync_pending(self) -> dict[str, int]:
        """设备恢复后补传全部排队操作。离线时调用会抛错。"""
        with self._lock:
            if not self.online:
                raise AccessError("设备仍处于离线状态，无法补传")
            n = len(self.pending)
            for payload in self.pending:
                payload["synced_at"] = now_str()
                self.written.append(payload)
            self.pending.clear()
            if n:
                self.last_sync = now_str()
            return {"synced": n, "at": self.last_sync}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "online": self.online,
                "pending": len(self.pending),
                "written": len(self.written),
                "last_sync": self.last_sync,
            }


# ---------------------------------------------------------------------------
# 核心系统
# ---------------------------------------------------------------------------

class AccessSystem:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS buildings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        floors INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        building_id INTEGER NOT NULL REFERENCES buildings(id),
        name TEXT NOT NULL,
        unit_no TEXT NOT NULL,
        floor INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        id_no TEXT UNIQUE,
        phone TEXT,
        tenant_id INTEGER REFERENCES tenants(id),
        status TEXT NOT NULL DEFAULT 'active',
        left_at TEXT
    );
    CREATE TABLE IF NOT EXISTS access_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS group_floors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL REFERENCES access_groups(id),
        floor INTEGER NOT NULL,
        start_time TEXT NOT NULL DEFAULT '00:00',
        end_time TEXT NOT NULL DEFAULT '23:59',
        valid_from TEXT NOT NULL,
        valid_to TEXT NOT NULL,
        UNIQUE(group_id, floor)
    );
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_no TEXT NOT NULL UNIQUE,
        card_type TEXT NOT NULL DEFAULT 'regular',
        person_id INTEGER REFERENCES people(id),
        group_id INTEGER REFERENCES access_groups(id),
        status TEXT NOT NULL DEFAULT 'issued',
        issued_at TEXT,
        expires_at TEXT,
        valid_from TEXT,
        valid_to TEXT,
        counter TEXT DEFAULT 'A柜台',
        replaced_by_id INTEGER REFERENCES cards(id),
        device_id TEXT DEFAULT 'DEV-01',
        device_delivered INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        card_no TEXT,
        person_id INTEGER,
        detail TEXT DEFAULT '',
        actor TEXT DEFAULT '系统',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS recycle_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_no TEXT NOT NULL,
        person_id INTEGER,
        person_name TEXT,
        card_status TEXT NOT NULL,
        reason TEXT DEFAULT '',
        counter TEXT DEFAULT 'A柜台',
        recycled_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str = ":memory:", device: Optional[DeviceAdapter] = None) -> None:
        self.db_path = db_path
        # check_same_thread=False：HTTP 线程与到期任务线程共用一个连接，
        # 由 self._lock + BEGIN IMMEDIATE 保证互斥与事务完整性。
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self.device = device or DeviceAdapter()
        self._stop = threading.Event()
        self.scheduler: Optional[AccessScheduler] = None
        self.init_db()

    # ---- 基础 ------------------------------------------------------------

    def init_db(self) -> None:
        with self._lock:
            self.conn.executescript(self.SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.stop_scheduler()
        with self._lock:
            self.conn.close()

    def start_scheduler(self, interval: float = 20.0) -> None:
        if self.scheduler is None:
            self.scheduler = AccessScheduler(self, interval)
            self.scheduler.daemon = True
            self.scheduler.start()

    def stop_scheduler(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()
            self.scheduler = None

    def _event(self, cur: sqlite3.Cursor, event_type: str, *, card_no: Optional[str] = None,
               person_id: Optional[int] = None, detail: str = "", actor: str = "系统") -> None:
        cur.execute(
            "INSERT INTO events(event_type, card_no, person_id, detail, actor, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (event_type, card_no, person_id, detail, actor, now_str()),
        )

    def log_event(self, event_type: str, **kw: Any) -> None:
        with self._lock:
            cur = self.conn.cursor()
            self._event(cur, event_type, **kw)
            self.conn.commit()

    # ---- 岗位鉴权 --------------------------------------------------------

    @staticmethod
    def _require(role: str, allowed: tuple[str, ...], action: str) -> None:
        if role not in allowed:
            raise PermissionDenied(f"{ROLE_LABELS.get(role, role)}无权执行「{action}」")

    # ---- 查询 ------------------------------------------------------------

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_people(self) -> list[dict[str, Any]]:
        rows = self._all(
            """SELECT p.*, t.name tenant_name, t.unit_no, t.floor, b.name building_name
               FROM people p
               LEFT JOIN tenants t ON t.id = p.tenant_id
               LEFT JOIN buildings b ON b.id = t.building_id
               ORDER BY p.id"""
        )
        for r in rows:
            r["id_no_masked"] = mask_number(r["id_no"])
            r["phone_masked"] = mask_number(r["phone"])
        return rows

    def list_groups(self) -> list[dict[str, Any]]:
        groups = self._all("SELECT * FROM access_groups ORDER BY id")
        for g in groups:
            g["rules"] = self._all(
                "SELECT * FROM group_floors WHERE group_id=? ORDER BY floor", (g["id"],))
        return groups

    def get_group(self, group_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM access_groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            return None
        g = dict(row)
        g["rules"] = self._all(
            "SELECT * FROM group_floors WHERE group_id=? ORDER BY floor", (group_id,))
        return g

    def list_buildings(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM buildings ORDER BY id")

    def list_tenants(self) -> list[dict[str, Any]]:
        return self._all(
            """SELECT t.*, b.name building_name FROM tenants t
               JOIN buildings b ON b.id=t.building_id ORDER BY t.id"""
        )

    def list_cards(self, include_masked: bool = True) -> list[dict[str, Any]]:
        rows = self._all(
            """SELECT c.*, p.name person_name, g.name group_name,
                      bc.card_no replaced_by_no
               FROM cards c
               LEFT JOIN people p ON p.id=c.person_id
               LEFT JOIN access_groups g ON g.id=c.group_id
               LEFT JOIN cards bc ON bc.id=c.replaced_by_id
               ORDER BY c.id"""
        )
        for r in rows:
            if include_masked:
                r["card_no_masked"] = mask_number(r["card_no"])
            r["active"] = r["status"] in ACTIVE_STATES
        return rows

    def get_card(self, card_no: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            """SELECT c.*, p.name person_name, g.name group_name
               FROM cards c
               LEFT JOIN people p ON p.id=c.person_id
               LEFT JOIN access_groups g ON g.id=c.group_id
               WHERE c.card_no=?""", (card_no,)).fetchone()
        return dict(row) if row else None

    def list_events(self, event_type: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        if event_type:
            return self._all(
                "SELECT * FROM events WHERE event_type=? ORDER BY id DESC LIMIT ?",
                (event_type, limit))
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def list_recycles(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM recycle_records ORDER BY id DESC")

    def list_staff(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, username, display_name, role FROM admin_users ORDER BY id")

    def floor_matrix(self) -> dict[str, Any]:
        """楼层 × 权限组矩阵：单元格为当日当前时刻是否放行及规则。"""
        buildings = self.list_buildings()
        max_floor = max((b["floors"] for b in buildings), default=0)
        groups = self.list_groups()
        now = datetime.now()
        today = now.date()
        minutes = now.hour * 60 + now.minute
        matrix: dict[str, Any] = {"floors": list(range(1, max_floor + 1)), "groups": []}
        for g in groups:
            by_floor = {r["floor"]: r for r in g["rules"]}
            cells = []
            for f in matrix["floors"]:
                r = by_floor.get(f)
                state = "none"
                rule = None
                if r:
                    rule = dict(r)
                    if not (r["valid_from"] <= today.isoformat() <= r["valid_to"]):
                        state = "date"
                    elif parse_time_hhmm(r["start_time"]) <= minutes <= parse_time_hhmm(r["end_time"]):
                        state = "allow"
                    else:
                        state = "time"
                cells.append({"floor": f, "state": state, "rule": rule})
            matrix["groups"].append({"id": g["id"], "name": g["name"], "cells": cells})
        return matrix

    # ---- 人员 / 租户 -----------------------------------------------------

    def add_person(self, name: str, tenant_id: Optional[int], id_no: str, phone: str = "",
                   role: str = ROLE_CLERK) -> int:
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "登记人员")
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO people(name,id_no,phone,tenant_id,status) VALUES (?,?,?,?,'active')",
                    (name, id_no, phone, tenant_id))
            except sqlite3.IntegrityError:
                raise AccessError("证件号已存在，无法重复登记")
            pid = cur.lastrowid
            self.conn.commit()
            return pid

    # ---- 权限组管理（管理员，修改需二次确认） ----------------------------

    def create_group(self, name: str, description: str = "", role: str = ROLE_ADMIN) -> int:
        self._require(role, (ROLE_ADMIN,), "创建权限组")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("INSERT INTO access_groups(name,description) VALUES (?,?)", (name, description))
            gid = cur.lastrowid
            self.conn.commit()
            return gid

    def set_group_rule(self, group_id: int, floor: int, start_time: str, end_time: str,
                       valid_from: str, valid_to: str, *, confirm: bool = False,
                       role: str = ROLE_ADMIN, actor: str = "管理员") -> None:
        """新增/修改某楼层规则。管理员修改权限必须 confirm=true（二次确认）。"""
        self._require(role, (ROLE_ADMIN,), "修改权限组")
        group = self.get_group(group_id)
        if not group:
            raise AccessError("权限组不存在")
        existed = self.conn.execute(
            "SELECT 1 FROM group_floors WHERE group_id=? AND floor=?", (group_id, floor)).fetchone()
        if existed and not confirm:
            self.log_event(EV["PERMISSION_CHANGE_REJECTED"],
                           detail=f"权限组「{group['name']}」楼层{floor}规则修改被拦截：缺少二次确认",
                           actor=actor)
            raise AccessError("该楼层已有规则，修改权限需要二次确认")
        if valid_from > valid_to:
            raise AccessError("生效日期起止不合法")
        if parse_time_hhmm(start_time) > parse_time_hhmm(end_time):
            raise AccessError("时段起止不合法")
        affected_cards = self._all(
            "SELECT card_no FROM cards WHERE group_id=? AND status='available'", (group_id,))
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO group_floors(group_id,floor,start_time,end_time,valid_from,valid_to)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(group_id,floor) DO UPDATE SET
                     start_time=excluded.start_time, end_time=excluded.end_time,
                     valid_from=excluded.valid_from, valid_to=excluded.valid_to""",
                (group_id, floor, start_time, end_time, valid_from, valid_to))
            self._event(
                cur, EV["PERMISSION_CHANGE"],
                detail=f"权限组「{group['name']}」楼层{floor} 时段{start_time}-{end_time} "
                       f"日期{valid_from}~{valid_to}（{'修改' if existed else '新增'}），"
                       f"影响在持卡片{len(affected_cards)}张",
                actor=actor)
            self.conn.commit()

    # ---- 发卡（两步完成 + 卡号唯一并发约束） -----------------------------

    def issue_card(self, card_no: str, person_id: int, group_id: int, *,
                   card_type: str = CARD_REGULAR, expires_at: Optional[str] = None,
                   valid_from: Optional[str] = None, valid_to: Optional[str] = None,
                   counter: str = "A柜台", device_id: str = "DEV-01",
                   role: str = ROLE_CLERK, actor: Optional[str] = None,
                   auto_activate: bool = True) -> dict[str, Any]:
        """柜台发卡：卡号唯一（并发时仅一笔成功）；绑定人员+权限组后激活。"""
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "发卡")
        actor = actor or counter
        person = self.conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
        if not person:
            raise AccessError("人员不存在")
        if dict(person)["status"] == PERSON_LEFT:
            raise AccessError("该人员已离场，不能发卡")
        group = self.get_group(group_id)
        if not group:
            raise AccessError("权限组不存在")
        if card_type == CARD_TEMP and not expires_at:
            raise AccessError("临时卡必须指定到期时间")
        vf = valid_from or today_str()
        vt = valid_to or (expires_at[:10] if expires_at else "2999-12-31")

        # 立即事务（BEGIN IMMEDIATE）抢占写锁：两个柜台并发发放同一卡号，
        # 只有一笔能插入成功，另一笔收到 UNIQUE 冲突。
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                self.conn.execute("ROLLBACK")
                raise
            cur = self.conn.cursor()
            try:
                cur.execute(
                    """INSERT INTO cards(card_no,card_type,person_id,group_id,status,
                          issued_at,expires_at,valid_from,valid_to,counter,device_id,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (card_no, card_type, person_id, group_id, CARD_ISSUED,
                     now_str(), expires_at, vf, vt, counter, device_id, now_str()))
                card_id = cur.lastrowid
                self._event(cur, EV["TEMP_ISSUE"] if card_type == CARD_TEMP else EV["ISSUE"],
                            card_no=card_no, person_id=person_id,
                            detail=f"{counter}发放{'临时卡' if card_type == CARD_TEMP else '正式卡'}，"
                                   f"待激活" + (f"，到期{expires_at}" if expires_at else ""),
                            actor=actor)
                self.conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                self.conn.execute("ROLLBACK")
                # 记录一次越界/冲突事件：重复发放被拒
                self.log_event(EV["PERMISSION_DENIED"], card_no=card_no, person_id=person_id,
                               detail=f"{counter}尝试发放已存在卡号 {card_no}，被唯一约束拒绝",
                               actor=actor)
                raise AccessError(f"卡号 {card_no} 已被发放，该笔发放失败")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        result: dict[str, Any] = {"card_id": card_id, "activated": False}
        if auto_activate:
            result.update(self.activate_card(card_no, role=role, actor=actor))
        return result

    # ---- 激活（必须绑定人员 + 权限组） -----------------------------------

    def activate_card(self, card_no: str, *, role: str = ROLE_CLERK,
                      actor: str = "系统") -> dict[str, Any]:
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "激活卡片")
        with self._lock:
            card = self.get_card(card_no)
            if not card:
                raise AccessError("卡片不存在")
            if card["status"] != CARD_ISSUED:
                raise AccessError(f"卡片状态为 {card['status']}，不能激活")
            if not card["person_id"] or not card["group_id"]:
                raise AccessError("门禁卡必须绑定人员和权限组后才能激活")
            floors = [r["floor"] for r in self.get_group(card["group_id"])["rules"]]
            write = self.device.write_card(card_no, floors, card["device_id"] or "DEV-01")
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE cards SET status=?, device_delivered=? WHERE card_no=?",
                (CARD_AVAILABLE, 1 if write["delivered"] else 0, card_no))
            self._event(
                cur, EV["ACTIVATE"], card_no=card_no, person_id=card["person_id"],
                detail=("设备写入成功" if write["delivered"]
                        else f"门禁设备离线，授权排队等待补传（排队第{len(self.device.pending)}笔）"),
                actor=actor)
            if not write["delivered"]:
                self._event(cur, EV["DEVICE_OFFLINE"], card_no=card_no, person_id=card["person_id"],
                            detail=f"设备 {card['device_id']} 离线，卡片授权未下发", actor=actor)
            self.conn.commit()
            return {"activated": True, "device": write}

    # ---- 权限判定（楼层 × 时段 × 生效日期 + 卡状态） ---------------------

    def evaluate_access(self, card_no: str, floor: int, *, at: Optional[datetime] = None,
                        record: bool = False, actor: str = "门禁设备") -> dict[str, Any]:
        at = at or datetime.now()
        card = self.get_card(card_no)
        if not card:
            result = {"allowed": False, "code": ACCESS_DENY_CARD, "reason": "卡号不存在"}
        elif card["status"] != CARD_AVAILABLE:
            result = {"allowed": False, "code": ACCESS_DENY_CARD,
                      "reason": f"卡片状态为{card['status']}，禁止通行"}
        elif card["card_type"] == CARD_TEMP and card["expires_at"] and at.strftime("%Y-%m-%d %H:%M:%S") >= card["expires_at"]:
            result = {"allowed": False, "code": ACCESS_DENY_DATE, "reason": "临时卡已到期"}
        elif card["valid_from"] and at.date().isoformat() < card["valid_from"]:
            result = {"allowed": False, "code": ACCESS_DENY_DATE, "reason": "卡片尚未到生效日期"}
        elif card["valid_to"] and at.date().isoformat() > card["valid_to"]:
            result = {"allowed": False, "code": ACCESS_DENY_DATE, "reason": "卡片已过有效期"}
        else:
            rule = self.conn.execute(
                "SELECT * FROM group_floors WHERE group_id=? AND floor=?",
                (card["group_id"], floor)).fetchone()
            if not rule:
                result = {"allowed": False, "code": ACCESS_DENY_FLOOR,
                          "reason": f"权限组不含 {floor} 层"}
            else:
                rule = dict(rule)
                today = at.date().isoformat()
                if not (rule["valid_from"] <= today <= rule["valid_to"]):
                    result = {"allowed": False, "code": ACCESS_DENY_DATE,
                              "reason": "楼层规则不在生效日期内"}
                else:
                    minutes = at.hour * 60 + at.minute
                    if not (parse_time_hhmm(rule["start_time"]) <= minutes <= parse_time_hhmm(rule["end_time"])):
                        result = {"allowed": False, "code": ACCESS_DENY_TIME,
                                  "reason": f"当前不在允许时段 {rule['start_time']}-{rule['end_time']}"}
                    else:
                        result = {"allowed": True, "code": ACCESS_ALLOW,
                                  "reason": f"允许进入 {floor} 层"}
        if record and not result["allowed"]:
            self.log_event(EV["PERMISSION_DENIED"], card_no=card_no,
                           person_id=card["person_id"] if card else None,
                           detail=f"尝试进入{floor}层被拒：{result['reason']}", actor=actor)
        return result

    # ---- 挂失 / 补卡（新卡替换，保留旧卡关系；挂失卡不能补录恢复） -------

    def report_lost(self, card_no: str, *, role: str = ROLE_CLERK,
                    actor: str = "A柜台") -> dict[str, Any]:
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "挂失")
        with self._lock:
            card = self.get_card(card_no)
            if not card:
                raise AccessError("卡片不存在")
            if card["status"] == CARD_LOST:
                raise AccessError("卡片已处于挂失状态")
            if card["status"] not in ACTIVE_STATES:
                raise AccessError(f"卡片状态为 {card['status']}，不能挂失")
            cur = self.conn.cursor()
            cur.execute("UPDATE cards SET status=? WHERE card_no=?", (CARD_LOST, card_no))
            self._event(cur, EV["LOST"], card_no=card_no, person_id=card["person_id"],
                        detail="卡片挂失，权限即时冻结，原卡号不得通过补录恢复", actor=actor)
            self.conn.commit()
            return {"status": CARD_LOST}

    def reissue_card(self, old_card_no: str, new_card_no: str, *,
                     role: str = ROLE_CLERK, actor: str = "A柜台") -> dict[str, Any]:
        """补卡：旧卡必须为挂失态；发放新卡并在新卡上保留 replaced_by->旧卡 关系。"""
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "补卡")
        with self._lock:
            old = self.get_card(old_card_no)
            if not old:
                raise AccessError("旧卡不存在")
            if old["status"] != CARD_LOST:
                # 挂失卡不能通过补录直接恢复：只允许“挂失 -> 换新卡”
                self._event_rejected_reissue(old_card_no, old, actor,
                                             f"仅挂失卡片可补卡，当前状态 {old['status']}")
                raise AccessError("只有挂失状态的卡片才能补卡；已挂失旧卡不会恢复，须换发新卡号")
            if new_card_no == old_card_no:
                self._event_rejected_reissue(old_card_no, old, actor, "新卡号与旧卡号相同")
                raise AccessError("补卡必须使用新卡号，原挂失卡号不能补录恢复")
            exists = self.conn.execute("SELECT 1 FROM cards WHERE card_no=?", (new_card_no,)).fetchone()
            if exists:
                self._event_rejected_reissue(old_card_no, old, actor,
                                             f"新卡号 {new_card_no} 已存在")
                raise AccessError(f"新卡号 {new_card_no} 已被占用")

            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO cards(card_no,card_type,person_id,group_id,status,issued_at,
                      expires_at,valid_from,valid_to,counter,device_id,device_delivered,
                      replaced_by_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_card_no, old["card_type"], old["person_id"], old["group_id"], CARD_ISSUED,
                 now_str(), old["expires_at"], old["valid_from"], old["valid_to"],
                 actor, old["device_id"], 0, old["id"], now_str()))
            new_id = cur.lastrowid
            self._event(cur, EV["REPLACE"], card_no=new_card_no, person_id=old["person_id"],
                        detail=f"新卡 {new_card_no} 替换挂失旧卡 {old_card_no}，保留旧卡关联",
                        actor=actor)
            self.conn.commit()
        act = self.activate_card(new_card_no, role=role, actor=actor)
        return {"new_card_no": new_card_no, "new_card_id": new_id, **act}

    def _event_rejected_reissue(self, card_no: str, old: Optional[dict[str, Any]],
                                actor: str, detail: str) -> None:
        self.log_event(EV["LOST_REISSUE_REJECTED"], card_no=card_no,
                       person_id=old["person_id"] if old else None,
                       detail=f"非法补录被拒：{detail}", actor=actor)

    # ---- 损坏 ------------------------------------------------------------

    def report_damaged(self, card_no: str, *, role: str = ROLE_CLERK,
                       actor: str = "A柜台") -> dict[str, Any]:
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "损坏登记")
        with self._lock:
            card = self.get_card(card_no)
            if not card:
                raise AccessError("卡片不存在")
            if card["status"] not in ACTIVE_STATES:
                raise AccessError(f"卡片状态为 {card['status']}，不能登记损坏")
            cur = self.conn.cursor()
            cur.execute("UPDATE cards SET status=? WHERE card_no=?", (CARD_DAMAGED, card_no))
            self._event(cur, EV["DAMAGE"], card_no=card_no, person_id=card["person_id"],
                        detail="卡片损坏，权限冻结，待回收换新", actor=actor)
            self.conn.commit()
            return {"status": CARD_DAMAGED}

    # ---- 借用转交 --------------------------------------------------------

    def transfer_card(self, card_no: str, to_person_id: int, *,
                      role: str = ROLE_CLERK, actor: str = "A柜台") -> dict[str, Any]:
        """卡片借用转交给另一人员；权限组不变。离场/挂失/损坏/回收卡不可转交。"""
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "借用转交")
        with self._lock:
            card = self.get_card(card_no)
            if not card:
                raise AccessError("卡片不存在")
            if card["status"] not in ACTIVE_STATES:
                raise AccessError(f"卡片状态为 {card['status']}，不能转交")
            to_person = self.conn.execute(
                "SELECT * FROM people WHERE id=?", (to_person_id,)).fetchone()
            if not to_person:
                raise AccessError("接收人不存在")
            if dict(to_person)["status"] == PERSON_LEFT:
                raise AccessError("接收人已离场，不能转交")
            cur = self.conn.cursor()
            cur.execute("UPDATE cards SET person_id=? WHERE card_no=?", (to_person_id, card_no))
            self._event(cur, EV["BORROW"], card_no=card_no, person_id=to_person_id,
                        detail=f"卡片由人员#{card['person_id']}借用转交给人员#{to_person_id}",
                        actor=actor)
            self.conn.commit()
            return {"person_id": to_person_id}

    # ---- 离场回收 --------------------------------------------------------

    def recycle_card(self, card_no: str, reason: str = "租户离场", *,
                     role: str = ROLE_CLERK, actor: str = "A柜台") -> dict[str, Any]:
        """离场回收：先冻结权限，记录回收时卡片状态，再置为已回收。"""
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "离场回收")
        with self._lock:
            card = self.get_card(card_no)
            if not card:
                raise AccessError("卡片不存在")
            if card["status"] == CARD_REVOKED:
                raise AccessError("卡片已回收，请勿重复操作")
            old_status = card["status"]
            person = self.conn.execute(
                "SELECT name FROM people WHERE id=?", (card["person_id"],)).fetchone()
            person_name = dict(person)["name"] if person else ""
            cur = self.conn.cursor()
            # 第一步：冻结权限
            cur.execute("UPDATE cards SET status=? WHERE card_no=?", (CARD_FROZEN, card_no))
            self._event(cur, EV["RECYCLE"], card_no=card_no, person_id=card["person_id"],
                        detail=f"离场回收：权限冻结（回收前状态 {old_status}），原因：{reason}",
                        actor=actor)
            # 第二步：登记回收记录并置为已回收
            cur.execute(
                """INSERT INTO recycle_records(card_no,person_id,person_name,card_status,
                      reason,counter,recycled_at) VALUES (?,?,?,?,?,?,?)""",
                (card_no, card["person_id"], person_name, old_status, reason, actor, now_str()))
            cur.execute("UPDATE cards SET status=? WHERE card_no=?", (CARD_REVOKED, card_no))
            # 该人员若已无在持卡片，标记离场
            holding = cur.execute(
                "SELECT COUNT(*) c FROM cards WHERE person_id=? AND status IN (?,?)",
                (card["person_id"], CARD_AVAILABLE, CARD_ISSUED)).fetchone()["c"]
            if holding == 0:
                cur.execute(
                    "UPDATE people SET status=?, left_at=? WHERE id=?",
                    (PERSON_LEFT, now_str(), card["person_id"]))
            self.conn.commit()
            return {"status": CARD_REVOKED, "old_status": old_status,
                    "person_left": holding == 0}

    # ---- 设备离线 / 补传 -------------------------------------------------

    def set_device_online(self, online: bool, *, role: str = ROLE_ADMIN,
                          device_id: str = "DEV-01") -> dict[str, Any]:
        self._require(role, (ROLE_ADMIN,), "切换设备状态")
        self.device.set_online(online)
        if not online:
            self.log_event(EV["DEVICE_OFFLINE"],
                           detail=f"门禁设备 {device_id} 离线", actor="管理员")
        return self.device.status()

    def sync_device(self, *, role: str = ROLE_ADMIN) -> dict[str, Any]:
        self._require(role, (ROLE_ADMIN, ROLE_CLERK), "设备补传")
        result = self.device.sync_pending()
        if result["synced"]:
            with self._lock:
                cur = self.conn.cursor()
                cur.execute("UPDATE cards SET device_delivered=1 WHERE device_delivered=0")
                self._event(cur, EV["DEVICE_SYNC"],
                            detail=f"设备恢复在线，补传 {result['synced']} 笔离线期间发卡授权",
                            actor="管理员")
                self.conn.commit()
        return result

    # ---- 到期失效（进程内任务调用，亦可手动触发） ------------------------

    def expire_temp_cards(self, *, at: Optional[datetime] = None) -> list[str]:
        """临时卡到期自动失效：状态置 expired 并记录事件。返回失效卡号列表。"""
        at = at or datetime.now()
        stamp = at.strftime("%Y-%m-%d %H:%M:%S")
        expired: list[str] = []
        with self._lock:
            rows = self.conn.execute(
                """SELECT card_no, person_id FROM cards
                   WHERE card_type='temp' AND status IN (?,?) AND expires_at IS NOT NULL
                     AND expires_at <= ?""",
                (CARD_AVAILABLE, CARD_ISSUED, stamp)).fetchall()
            if not rows:
                return []
            cur = self.conn.cursor()
            for r in rows:
                cur.execute("UPDATE cards SET status=? WHERE card_no=?",
                            (CARD_EXPIRED, r["card_no"]))
                self._event(cur, EV["EXPIRE"], card_no=r["card_no"], person_id=r["person_id"],
                            detail=f"临时卡到期（{r['card_no']} 到期时间已过），系统自动失效",
                            actor="系统定时任务")
                expired.append(r["card_no"])
            self.conn.commit()
        return expired

    # ---- 审计：事件类型说明 ---------------------------------------------

    @staticmethod
    def event_digest() -> dict[str, str]:
        return {
            EV["ISSUE"]: "发卡", EV["ACTIVATE"]: "卡片激活", EV["LOST"]: "卡片挂失",
            EV["LOST_REISSUE_REJECTED"]: "非法补录被拒", EV["REPLACE"]: "补卡换新",
            EV["DAMAGE"]: "卡片损坏", EV["BORROW"]: "借用转交",
            EV["PERMISSION_DENIED"]: "权限越界/重复发放", EV["DEVICE_OFFLINE"]: "设备离线",
            EV["DEVICE_SYNC"]: "离线补传", EV["RECYCLE"]: "离场回收",
            EV["EXPIRE"]: "临时卡到期", EV["PERMISSION_CHANGE"]: "管理员改权限",
            EV["PERMISSION_CHANGE_REJECTED"]: "改权限缺少二次确认",
            EV["TEMP_ISSUE"]: "临时卡发放",
        }


class AccessScheduler(threading.Thread):
    """进程内定时任务：周期扫描并自动失效到期临时卡。"""

    def __init__(self, system: AccessSystem, interval: float = 20.0) -> None:
        super().__init__(daemon=True)
        self.system = system
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.system.expire_temp_cards()
            except Exception:
                # 定时任务异常不应杀死线程
                pass

    def stop(self) -> None:
        self._stop.set()
