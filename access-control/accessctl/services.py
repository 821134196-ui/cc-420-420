"""核心领域服务：发卡、权限、挂失、回收、设备适配、事件与审计。"""
from __future__ import annotations

import json
from datetime import datetime, date
from uuid import uuid4

from .db import Database, row_to_dict


# ---------------------------------------------------------------- 基础工具

def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return date.today().isoformat()


def mask_card_no(card_no: str) -> str:
    """敏感号码默认部分遮挡：保留前两位与末位，其余用 ● 代替。"""
    if not card_no:
        return ""
    if len(card_no) <= 4:
        return card_no[0] + "●●" + card_no[-1]
    return card_no[:2] + "●" * (len(card_no) - 3) + card_no[-1]


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.code = code


class AuthError(ServiceError):
    def __init__(self, message: str = "无操作权限"):
        super().__init__(message, "forbidden")


ROLE_LABELS = {"admin": "管理员", "clerk": "发卡柜台", "guard": "门岗保安", "auditor": "审计员"}
ROLES_CAN_ISSUE = ("admin", "clerk")
ROLES_CAN_LOST = ("admin", "clerk", "guard")
ROLES_CAN_RECYCLE = ("admin", "clerk")
ROLES_CAN_DEVICE = ("admin", "guard")
ROLES_ADMIN_ONLY = ("admin",)


# ---------------------------------------------------------------- 服务主体

class Service:
    def __init__(self, db: Database):
        self.db = db

    # ============================================================== 内部辅助

    def _operator(self, operator_id: int | None) -> dict:
        if not operator_id:
            raise AuthError("缺少操作员身份")
        row = self.db.conn.execute(
            "SELECT * FROM operators WHERE id=? AND active=1", (operator_id,)
        ).fetchone()
        if not row:
            raise AuthError("操作员不存在或已停用")
        return dict(row)

    def require_role(self, operator_id: int | None, roles) -> dict:
        op = self._operator(operator_id)
        if op["role"] not in roles:
            raise AuthError(
                f"{ROLE_LABELS.get(op['role'], op['role'])}无权执行该操作"
            )
        return op

    def _audit(self, conn, op, action, entity=None, entity_id=None, detail=None):
        conn.execute(
            "INSERT INTO audit_logs(operator_id,operator_name,action,entity,entity_id,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (op.get("id") if op else None,
             op.get("name") if op else "系统", action, entity,
             str(entity_id) if entity_id is not None else None,
             detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False) if detail else None,
             now_iso()),
        )

    def _event(self, conn, etype, severity="info", card_id=None, card_no=None,
               device_id=None, person_id=None, detail=None):
        conn.execute(
            "INSERT INTO events(type,severity,card_id,card_no,device_id,person_id,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (etype, severity, card_id, card_no, device_id, person_id,
             detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False) if detail else None,
             now_iso()),
        )

    def _lifecycle(self, conn, card, action, op, person_id=None, detail=None):
        conn.execute(
            "INSERT INTO lifecycle_records(card_id,card_no,action,person_id,operator_id,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (card["id"] if isinstance(card, dict) else card,
             card["card_no"] if isinstance(card, dict) else str(card),
             action, person_id, op.get("id") if op else None,
             detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False) if detail else None,
             now_iso()),
        )

    def _get_card(self, conn, card_no: str) -> dict:
        row = conn.execute("SELECT * FROM cards WHERE card_no=?", (card_no,)).fetchone()
        if not row:
            raise ServiceError(f"卡号 {card_no} 不存在", "not_found")
        return dict(row)

    def _floors_of(self, conn, group_id: int) -> list[int]:
        return [r[0] for r in conn.execute(
            "SELECT floor FROM group_floors WHERE group_id=? ORDER BY floor", (group_id,))]

    # ============================================================== 权限计算

    def evaluate_permission(self, group_id: int, floor: int,
                            at: datetime | None = None) -> tuple[bool, list[str]]:
        """按楼层、时段、生效日期计算是否放行，返回 (是否允许, 原因列表)。"""
        conn = self.db.conn
        group = conn.execute(
            "SELECT * FROM permission_groups WHERE id=?", (group_id,)
        ).fetchone()
        if not group:
            return False, ["权限组不存在"]
        group = dict(group)
        at = at or now()
        reasons: list[str] = []

        floors = self._floors_of(conn, group_id)
        if floor not in floors:
            reasons.append(f"楼层 {floor} 不在授权范围 {floors}")

        hm = at.strftime("%H:%M")
        if not (group["time_start"] <= hm <= group["time_end"]):
            reasons.append(
                f"当前 {hm} 不在允许时段 {group['time_start']}–{group['time_end']}"
            )

        d = at.date().isoformat()
        if group["valid_from"] and d < group["valid_from"]:
            reasons.append(f"权限组自 {group['valid_from']} 起生效")
        if group["valid_to"] and d > group["valid_to"]:
            reasons.append(f"权限组已于 {group['valid_to']} 到期")
        return (len(reasons) == 0), reasons

    # ============================================================== 发卡（两步）

    def create_issue_draft(self, operator_id, card_no, person_id, group_id,
                           card_type="normal", valid_from=None, valid_to=None,
                           expires_at=None, note=None) -> dict:
        """第一步：柜台建单。同一卡号只允许一个柜台进入发放流程。"""
        op = self.require_role(operator_id, ROLES_CAN_ISSUE)
        card_no = card_no.strip().upper()
        with self.db.tx() as conn:
            card = self._get_card(conn, card_no)
            if card["status"] != "in_stock":
                status_cn = CARD_STATUS_CN.get(card["status"], card["status"])
                raise ServiceError(
                    f"卡片 {mask_card_no(card_no)} 当前为「{status_cn}」，不可发放。"
                    + ("挂失卡须走补卡换发流程，不能直接补录恢复。"
                       if card["status"] == "lost" else ""),
                    "card_unavailable",
                )
            clash = conn.execute(
                "SELECT id FROM issue_drafts WHERE card_id=? AND status='pending'",
                (card["id"],),
            ).fetchone()
            if clash:
                raise ServiceError(
                    f"卡片 {mask_card_no(card_no)} 正在另一柜台的发放流程中",
                    "draft_conflict",
                )
            person = conn.execute(
                "SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            if not person:
                raise ServiceError("人员不存在", "not_found")
            person = dict(person)
            if person["status"] != "active":
                raise ServiceError("该人员已离场，不能发卡", "person_departed")
            group = conn.execute(
                "SELECT * FROM permission_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise ServiceError("权限组不存在", "not_found")

            if card_type == "temporary" and not expires_at:
                raise ServiceError("临时卡必须填写到期时间", "validation")

            draft_no = f"BL{now().strftime('%Y%m%d%H%M%S')}{card['id']:03d}-{uuid4().hex[:6]}"
            cur = conn.execute(
                "INSERT INTO issue_drafts(draft_no,operator_id,card_id,card_no,person_id,"
                "group_id,card_type,valid_from,valid_to,expires_at,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (draft_no, op["id"], card["id"], card_no, person_id, group_id,
                 card_type, valid_from, valid_to, expires_at, note, now_iso()),
            )
            self._audit(conn, op, "创建发卡单", "issue_draft", cur.lastrowid,
                        {"card": mask_card_no(card_no), "person": person["name"],
                         "group": dict(group)["name"], "step": 1})
            return self._load_draft(conn, cur.lastrowid)

    def confirm_issue_draft(self, operator_id, draft_id) -> dict:
        """第二步：确认激活。卡必须同时绑定人员与权限组，事务内复查状态。"""
        op = self.require_role(operator_id, ROLES_CAN_ISSUE)
        with self.db.tx() as conn:
            draft = conn.execute(
                "SELECT * FROM issue_drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise ServiceError("发卡单不存在", "not_found")
            draft = dict(draft)
            if draft["status"] != "pending":
                raise ServiceError("发卡单已处理，请勿重复确认", "draft_closed")
            card = self._get_card(conn, draft["card_no"])
            if card["status"] != "in_stock":
                conn.execute(
                    "UPDATE issue_drafts SET status='cancelled' WHERE id=?", (draft_id,))
                raise ServiceError("卡片已被其他柜台先行发放", "card_unavailable")

            # 激活条件：人员 + 权限组同时落库
            conn.execute(
                "UPDATE cards SET status='active', person_id=?, group_id=?, card_type=?,"
                " valid_from=?, valid_to=?, expires_at=?, issued_at=?, activated_at=?,"
                " note=? WHERE id=?",
                (draft["person_id"], draft["group_id"], draft["card_type"],
                 draft["valid_from"], draft["valid_to"], draft["expires_at"],
                 now_iso(), now_iso(), draft["note"], card["id"]),
            )
            conn.execute(
                "UPDATE issue_drafts SET status='confirmed', confirmed_at=? WHERE id=?",
                (now_iso(), draft_id),
            )
            card.update(status="active", person_id=draft["person_id"],
                        group_id=draft["group_id"])
            self._device_command(conn, card, "issue", op=op)
            self._lifecycle(conn, card, "issue", op,
                            person_id=draft["person_id"],
                            detail=f"发卡激活，权限组#{draft['group_id']}")
            self._event(conn, "card_issued", "info", card["id"], card["card_no"],
                        person_id=draft["person_id"],
                        detail=f"{draft['card_type']} 卡发放激活")
            self._audit(conn, op, "确认发卡激活", "card", card["id"],
                        {"card": mask_card_no(card["card_no"]), "step": 2})
            return self._load_draft(conn, draft_id)

    def cancel_issue_draft(self, operator_id, draft_id) -> dict:
        op = self.require_role(operator_id, ROLES_CAN_ISSUE)
        with self.db.tx() as conn:
            draft = conn.execute(
                "SELECT * FROM issue_drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise ServiceError("发卡单不存在", "not_found")
            draft = dict(draft)
            if draft["status"] != "pending":
                raise ServiceError("发卡单已处理", "draft_closed")
            conn.execute(
                "UPDATE issue_drafts SET status='cancelled' WHERE id=?", (draft_id,))
            self._audit(conn, op, "撤销发卡单", "issue_draft", draft_id,
                        {"card": mask_card_no(draft["card_no"])})
            return self._load_draft(conn, draft_id)

    def _load_draft(self, conn, draft_id) -> dict:
        row = conn.execute(
            """SELECT d.*, c.status AS card_status, p.name AS person_name,
                      g.name AS group_name, o.name AS operator_name
               FROM issue_drafts d
               JOIN cards c ON c.id=d.card_id
               JOIN people p ON p.id=d.person_id
               JOIN permission_groups g ON g.id=d.group_id
               JOIN operators o ON o.id=d.operator_id
               WHERE d.id=?""", (draft_id,)).fetchone()
        return dict(row)

    # ============================================================== 挂失 / 损坏 / 补卡

    def report_lost(self, operator_id, card_no, note=None) -> dict:
        op = self.require_role(operator_id, ROLES_CAN_LOST)
        card_no = card_no.strip().upper()
        with self.db.tx() as conn:
            card = self._get_card(conn, card_no)
            if card["status"] != "active":
                raise ServiceError(
                    "仅使用中的卡片可挂失，当前状态："
                    + CARD_STATUS_CN.get(card["status"], card["status"]),
                    "card_status",
                )
            conn.execute(
                "UPDATE cards SET status='lost' WHERE id=?", (card["id"],))
            card["status"] = "lost"
            self._device_command(conn, card, "revoke", op=op)
            self._lifecycle(conn, card, "lost", op, person_id=card["person_id"],
                            detail=note or "持卡人报失，权限即时冻结")
            self._event(conn, "card_lost", "warning", card["id"], card["card_no"],
                        person_id=card["person_id"], detail=note or "卡片遗失挂失")
            self._audit(conn, op, "挂失卡片", "card", card["id"],
                        {"card": mask_card_no(card_no), "note": note})
            return self._card_view(conn, card)

    def report_damaged(self, operator_id, card_no, note=None) -> dict:
        op = self.require_role(operator_id, ROLES_CAN_LOST)
        card_no = card_no.strip().upper()
        with self.db.tx() as conn:
            card = self._get_card(conn, card_no)
            if card["status"] not in ("active", "frozen"):
                raise ServiceError(
                    "当前状态不可登记损坏："
                    + CARD_STATUS_CN.get(card["status"], card["status"]),
                    "card_status",
                )
            conn.execute(
                "UPDATE cards SET status='damaged' WHERE id=?", (card["id"],))
            card["status"] = "damaged"
            self._device_command(conn, card, "revoke", op=op)
            self._lifecycle(conn, card, "damaged", op, person_id=card["person_id"],
                            detail=note or "卡片损坏报废")
            self._event(conn, "card_damaged", "warning", card["id"], card["card_no"],
                        person_id=card["person_id"], detail=note or "卡片损坏")
            self._audit(conn, op, "登记卡片损坏", "card", card["id"],
                        {"card": mask_card_no(card_no)})
            return self._card_view(conn, card)

    def replace_card(self, operator_id, old_card_no, new_card_no, note=None) -> dict:
        """新卡替换旧卡：保留与旧卡的关系，旧卡维持挂失/损坏，不做补录恢复。"""
        op = self.require_role(operator_id, ROLES_CAN_ISSUE)
        old_card_no = old_card_no.strip().upper()
        new_card_no = new_card_no.strip().upper()
        with self.db.tx() as conn:
            old = self._get_card(conn, old_card_no)
            if old["status"] not in ("lost", "damaged"):
                raise ServiceError(
                    "仅挂失或损坏卡允许替换补卡，当前状态："
                    + CARD_STATUS_CN.get(old["status"], old["status"]),
                    "card_status",
                )
            if not old["person_id"] or not old["group_id"]:
                raise ServiceError("旧卡缺少人员或权限组绑定，无法补卡", "validation")
            new = self._get_card(conn, new_card_no)
            if new["status"] != "in_stock":
                raise ServiceError(
                    f"新卡 {mask_card_no(new_card_no)} 不在库，无法用于补卡",
                    "card_unavailable",
                )
            conn.execute(
                "UPDATE cards SET status='active', person_id=?, group_id=?, card_type=?,"
                " valid_from=?, valid_to=?, expires_at=NULL, issued_at=?, activated_at=?,"
                " replaced_card_id=?, note=? WHERE id=?",
                (old["person_id"], old["group_id"], "normal",
                 today(), None, now_iso(), now_iso(), old["id"],
                 note or f"替换 {old_card_no}", new["id"]),
            )
            conn.execute(
                "UPDATE cards SET replaced_by_card_id=? WHERE id=?",
                (new["id"], old["id"]),
            )
            new.update(status="active", person_id=old["person_id"],
                       group_id=old["group_id"], replaced_card_id=old["id"])
            self._device_command(conn, new, "issue", op=op)
            self._lifecycle(conn, new, "issue", op,
                            person_id=old["person_id"],
                            detail=f"补卡替换旧卡 {old_card_no}，旧卡关系保留")
            self._lifecycle(conn, old, "replaced", op,
                            person_id=old["person_id"],
                            detail=f"由新卡 {new_card_no} 替换")
            self._event(conn, "card_replaced", "info", new["id"], new["card_no"],
                        person_id=old["person_id"],
                        detail=f"{new_card_no} 替换 {old_card_no}")
            self._audit(conn, op, "补卡换发", "card", new["id"],
                        {"old": mask_card_no(old_card_no),
                         "new": mask_card_no(new_card_no)})
            return self._card_view(conn, new)

    def restore_lost_directly(self, operator_id, card_no) -> None:
        """显式拦截：挂失卡不能通过补录直接恢复。"""
        self.require_role(operator_id, ROLES_CAN_ISSUE)
        with self.db.ro():
            card = self._get_card(self.db.conn, card_no.strip().upper())
        if card["status"] == "lost":
            raise ServiceError(
                "挂失卡不能通过补录直接恢复，须使用新卡补卡换发，旧卡永久注销。",
                "lost_restore_denied",
            )
        raise ServiceError("该卡不是挂失状态，无需恢复", "card_status")

    # ============================================================== 借用转交

    def borrow_card(self, operator_id, card_no, to_person_id, due_at=None, note=None) -> dict:
        op = self.require_role(operator_id, ROLES_CAN_ISSUE)
        card_no = card_no.strip().upper()
        with self.db.tx() as conn:
            card = self._get_card(conn, card_no)
            if card["status"] != "active":
                raise ServiceError(
                    "仅使用中的卡片可借用转交，当前状态："
                    + CARD_STATUS_CN.get(card["status"], card["status"]),
                    "card_status",
                )
            borrower = conn.execute(
                "SELECT * FROM people WHERE id=?", (to_person_id,)).fetchone()
            if not borrower or dict(borrower)["status"] != "active":
                raise ServiceError("借用人不存在或已离场", "not_found")
            if to_person_id == card["person_id"]:
                raise ServiceError("不能转交给本人", "validation")
            cur = conn.execute(
                "INSERT INTO card_loans(card_id,from_person_id,to_person_id,note,"
                "borrowed_at,due_at,operator_id) VALUES(?,?,?,?,?,?,?)",
                (card["id"], card["person_id"], to_person_id, note,
                 now_iso(), due_at, op["id"]),
            )
            self._lifecycle(conn, card, "loan", op, person_id=to_person_id,
                            detail=note or f"借用/转交，借用人#{to_person_id}")
            self._event(conn, "card_borrowed", "info", card["id"], card["card_no"],
                        person_id=to_person_id,
                        detail=f"卡片借用/转交给人员#{to_person_id}，权限仍随卡")
            self._audit(conn, op, "卡片借用转交", "card", card["id"],
                        {"card": mask_card_no(card_no), "to_person": to_person_id})
            return dict(conn.execute(
                "SELECT * FROM card_loans WHERE id=?", (cur.lastrowid,)).fetchone())

    # ============================================================== 离场冻结 / 回收

    def depart_person(self, operator_id, person_id, note=None) -> dict:
        """离场：冻结名下全部有效卡并下发吊销。"""
        op = self.require_role(operator_id, ROLES_CAN_RECYCLE)
        with self.db.tx() as conn:
            person = conn.execute(
                "SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            if not person:
                raise ServiceError("人员不存在", "not_found")
            person = dict(person)
            if person["status"] == "departed":
                raise ServiceError("该人员已办理离场", "person_departed")
            cards = [dict(r) for r in conn.execute(
                "SELECT * FROM cards WHERE person_id=? AND status IN ('active','lost')",
                (person_id,))]
            frozen = []
            for card in cards:
                if card["status"] == "active":
                    conn.execute(
                        "UPDATE cards SET status='frozen', frozen_at=? WHERE id=?",
                        (now_iso(), card["id"]))
                    card["status"] = "frozen"
                    self._device_command(conn, card, "revoke", op=op)
                    self._lifecycle(conn, card, "freeze", op,
                                    person_id=person_id,
                                    detail=note or "人员离场，权限冻结")
                    self._event(conn, "card_frozen", "warning", card["id"],
                                card["card_no"], person_id=person_id,
                                detail="离场冻结")
                    frozen.append(card["card_no"])
            conn.execute(
                "UPDATE people SET status='departed', departed_at=? WHERE id=?",
                (now_iso(), person_id))
            self._audit(conn, op, "人员离场冻结", "person", person_id,
                        {"cards": [mask_card_no(c) for c in frozen]})
            return {"person_id": person_id, "name": person["name"],
                    "frozen_cards": frozen}

    def recycle_card(self, operator_id, card_no, note=None) -> dict:
        """回收：冻结卡收回入库登记，记录终态。"""
        op = self.require_role(operator_id, ROLES_CAN_RECYCLE)
        card_no = card_no.strip().upper()
        with self.db.tx() as conn:
            card = self._get_card(conn, card_no)
            if card["status"] not in ("frozen", "damaged"):
                raise ServiceError(
                    "仅冻结或损坏卡可回收登记，当前状态："
                    + CARD_STATUS_CN.get(card["status"], card["status"]),
                    "card_status",
                )
            conn.execute(
                "UPDATE cards SET status='recycled', recycled_at=? WHERE id=?",
                (now_iso(), card["id"]))
            card["status"] = "recycled"
            self._device_command(conn, card, "revoke", op=op)
            self._lifecycle(conn, card, "recycle", op,
                            person_id=card["person_id"],
                            detail=note or "离场卡片回收登记")
            self._event(conn, "card_recycled", "info", card["id"], card["card_no"],
                        person_id=card["person_id"], detail="卡片回收")
            self._audit(conn, op, "卡片回收", "card", card["id"],
                        {"card": mask_card_no(card_no)})
            return self._card_view(conn, card)

    # ============================================================== 权限组变更（二次确认）

    def create_permission_change(self, operator_id, group_id, name=None,
                                 description=None, floors=None, time_start=None,
                                 time_end=None, valid_from=None, valid_to=None,
                                 reason=None) -> dict:
        """管理员第一步：提交权限变更申请（暂不落库）。"""
        op = self.require_role(operator_id, ROLES_ADMIN_ONLY)
        with self.db.ro():
            group = self.db.conn.execute(
                "SELECT * FROM permission_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise ServiceError("权限组不存在", "not_found")
            group = dict(group)
        before = {"name": group["name"], "description": group["description"],
                  "floors": self._floors_of(self.db.conn, group_id),
                  "time_start": group["time_start"], "time_end": group["time_end"],
                  "valid_from": group["valid_from"], "valid_to": group["valid_to"]}
        after = dict(before)
        payload = {"name": name, "description": description, "floors": floors,
                   "time_start": time_start, "time_end": time_end,
                   "valid_from": valid_from, "valid_to": valid_to}
        for k, v in payload.items():
            if v is not None:
                after[k] = v
        if isinstance(after["floors"], list):
            after["floors"] = sorted(int(f) for f in after["floors"])
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO permission_changes(group_id,operator_id,before_json,"
                "after_json,status,reason,created_at) VALUES(?,?,?,?, 'pending',?,?)",
                (group_id, op["id"], json.dumps(before, ensure_ascii=False),
                 json.dumps(after, ensure_ascii=False), reason, now_iso()),
            )
            self._audit(conn, op, "提交权限变更(待确认)", "permission_group", group_id,
                        {"change_id": cur.lastrowid, "after": after})
            return {"id": cur.lastrowid, "group_id": group_id,
                    "before": before, "after": after, "status": "pending",
                    "reason": reason}

    def confirm_permission_change(self, operator_id, change_id, confirm: bool) -> dict:
        """管理员第二步：二次确认后才生效。"""
        op = self.require_role(operator_id, ROLES_ADMIN_ONLY)
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM permission_changes WHERE id=?", (change_id,)).fetchone()
            if not row:
                raise ServiceError("变更单不存在", "not_found")
            change = dict(row)
            if change["status"] != "pending":
                raise ServiceError("变更单已处理", "change_closed")
            if not confirm:
                conn.execute(
                    "UPDATE permission_changes SET status='rejected', confirmed_at=?"
                    " WHERE id=?", (now_iso(), change_id))
                self._audit(conn, op, "驳回权限变更", "permission_change", change_id,
                            None)
                return {"id": change_id, "status": "rejected"}

            after = json.loads(change["after_json"])
            conn.execute(
                "UPDATE permission_groups SET name=?, description=?, time_start=?,"
                " time_end=?, valid_from=?, valid_to=? WHERE id=?",
                (after["name"], after["description"], after["time_start"],
                 after["time_end"], after["valid_from"], after["valid_to"],
                 change["group_id"]),
            )
            conn.execute("DELETE FROM group_floors WHERE group_id=?",
                         (change["group_id"],))
            conn.executemany(
                "INSERT INTO group_floors(group_id,floor) VALUES(?,?)",
                [(change["group_id"], f) for f in after["floors"]])
            conn.execute(
                "UPDATE permission_changes SET status='confirmed', confirmed_at=?"
                " WHERE id=?", (now_iso(), change_id))
            self._event(conn, "permission_changed", "warning",
                        detail=f"权限组#{change['group_id']} 变更经二次确认生效")
            self._audit(conn, op, "确认权限变更生效", "permission_group",
                        change["group_id"], {"after": after})
            return {"id": change_id, "status": "confirmed", "after": after}

    # ============================================================== 设备适配器 + 离线补传

    def _device_command(self, conn, card, command: str, op=None, device_id=None):
        """本地设备适配器：在线即送达，离线写入发件箱待补传。"""
        if device_id is None:
            dev = conn.execute(
                "SELECT * FROM devices WHERE status='online' ORDER BY id LIMIT 1"
            ).fetchone()
            if dev is None:
                dev = conn.execute(
                    "SELECT * FROM devices ORDER BY id LIMIT 1").fetchone()
        else:
            dev = conn.execute(
                "SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not dev:
            return
        dev = dict(dev)
        payload = json.dumps({"card_no": card["card_no"], "command": command},
                             ensure_ascii=False)
        if dev["status"] == "online":
            conn.execute(
                "INSERT INTO device_outbox(device_id,card_id,card_no,command,"
                "payload_json,status,created_at,delivered_at)"
                " VALUES(?,?,?,?,?, 'delivered',?,?)",
                (dev["id"], card["id"], card["card_no"], command, payload,
                 now_iso(), now_iso()),
            )
        else:
            conn.execute(
                "INSERT INTO device_outbox(device_id,card_id,card_no,command,"
                "payload_json,status,created_at) VALUES(?,?,?,?,?, 'pending',?)",
                (dev["id"], card["id"], card["card_no"], command, payload, now_iso()),
            )

    def set_device_status(self, operator_id, device_id, online: bool) -> dict:
        op = self.require_role(operator_id, ROLES_CAN_DEVICE)
        with self.db.tx() as conn:
            dev = conn.execute("SELECT * FROM devices WHERE id=?",
                               (device_id,)).fetchone()
            if not dev:
                raise ServiceError("设备不存在", "not_found")
            dev = dict(dev)
            new_status = "online" if online else "offline"
            if dev["status"] == new_status:
                raise ServiceError(f"设备当前已是{'在线' if online else '离线'}状态")
            conn.execute(
                "UPDATE devices SET status=?, last_seen=? WHERE id=?",
                (new_status, now_iso(), device_id))
            if online:
                delivered = self._flush_device(conn, device_id)
                self._event(conn, "device_online", "info", device_id=device_id,
                            detail=f"设备恢复在线，补传 {delivered} 条待发指令")
                self._audit(conn, op, "设备恢复在线/补传", "device", device_id,
                            {"delivered": delivered})
            else:
                self._event(conn, "device_offline", "warning", device_id=device_id,
                            detail="门禁设备离线，指令进入本地缓冲")
                self._audit(conn, op, "设备置为离线", "device", device_id, None)
            return {"device_id": device_id, "status": new_status}

    def _flush_device(self, conn, device_id) -> int:
        """设备恢复在线：补传发件箱指令 + 同步离线期间事件。"""
        rows = conn.execute(
            "SELECT id FROM device_outbox WHERE device_id=? AND status='pending'",
            (device_id,)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE device_outbox SET status='delivered', delivered_at=?,"
                " failure=NULL WHERE id=?", (now_iso(), r[0]))
        conn.execute(
            "UPDATE access_events SET synced=1 WHERE device_id=? AND synced=0",
            (device_id,))
        return len(rows)

    def attempt_access(self, operator_id, device_id, card_no, floor) -> dict:
        """门岗刷卡：综合卡片状态 + 权限组（楼层/时段/日期）判定。"""
        op = self.require_role(operator_id, ("admin", "guard", "clerk"))
        card_no = card_no.strip().upper()
        with self.db.tx() as conn:
            dev = conn.execute("SELECT * FROM devices WHERE id=?",
                               (device_id,)).fetchone()
            if not dev:
                raise ServiceError("设备不存在", "not_found")
            dev = dict(dev)
            card = conn.execute("SELECT * FROM cards WHERE card_no=?",
                                (card_no,)).fetchone()
            result, reasons = "denied", []
            card_id, person_id = None, None
            if not card:
                reasons = ["卡号不存在"]
            else:
                card = dict(card)
                card_id, person_id = card["id"], card["person_id"]
                if card["status"] != "active":
                    reasons = [f"卡片状态：{CARD_STATUS_CN.get(card['status'], card['status'])}"]
                elif card["card_type"] == "temporary" and card["expires_at"] \
                        and card["expires_at"] <= now_iso():
                    reasons = ["临时卡已到期"]
                else:
                    ok, reasons = self.evaluate_permission(card["group_id"], int(floor))
                    result = "granted" if ok else "denied"
            reason_text = "；".join(reasons) if reasons else "权限校验通过"
            # 离线设备照常本地记录，synced=0，恢复在线后补传
            conn.execute(
                "INSERT INTO access_events(device_id,card_no,floor,event_at,result,"
                "reason,synced,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (device_id, card_no, floor, now_iso(), result, reason_text,
                 1 if dev["status"] == "online" else 0, now_iso()),
            )
            if result == "denied":
                self._event(conn, "permission_violation", "warning", card_id, card_no,
                            device_id=device_id, person_id=person_id,
                            detail=f"{floor}F 越界/拒绝：{reason_text}")
            return {"result": result, "reasons": reasons,
                    "device_status": dev["status"]}

    # ============================================================== 临时卡到期

    def expire_temporary_cards(self) -> list[str]:
        """进程内定时任务调用：到期临时卡自动失效并吊销。"""
        expired = []
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM cards WHERE card_type='temporary' AND status='active'"
                " AND expires_at IS NOT NULL AND expires_at<=?",
                (now_iso(),)).fetchall()
            for r in rows:
                card = dict(r)
                conn.execute(
                    "UPDATE cards SET status='expired' WHERE id=?", (card["id"],))
                self._device_command(conn, card, "revoke")
                self._lifecycle(conn, card, "expire", None,
                                person_id=card["person_id"],
                                detail=f"临时卡到期自动失效（到期 {card['expires_at']}）")
                self._event(conn, "temp_expired", "warning", card["id"],
                            card["card_no"], person_id=card["person_id"],
                            detail="临时卡到期自动失效")
                expired.append(card["card_no"])
        return expired

    # ============================================================== 查询视图

    def _card_view(self, conn, card) -> dict:
        row = conn.execute(
            """SELECT c.*, p.name AS person_name, p.person_no, g.name AS group_name,
                      b.name AS building_name, r.room_no
               FROM cards c
               LEFT JOIN people p ON p.id=c.person_id
               LEFT JOIN permission_groups g ON g.id=c.group_id
               LEFT JOIN rooms r ON r.id=p.room_id
               LEFT JOIN buildings b ON b.id=r.building_id
               WHERE c.id=?""", (card["id"],)).fetchone()
        view = dict(row)
        view["card_no_masked"] = mask_card_no(view["card_no"])
        return view

    def list_cards(self, status=None) -> list[dict]:
        sql = (
            """SELECT c.*, p.name AS person_name, p.person_no, g.name AS group_name,
                      b.name AS building_name, r.room_no,
                      old.card_no AS old_card_no, new.card_no AS new_card_no
               FROM cards c
               LEFT JOIN people p ON p.id=c.person_id
               LEFT JOIN permission_groups g ON g.id=c.group_id
               LEFT JOIN rooms r ON r.id=p.room_id
               LEFT JOIN buildings b ON b.id=r.building_id
               LEFT JOIN cards old ON old.id=c.replaced_card_id
               LEFT JOIN cards new ON new.id=c.replaced_by_card_id""")
        params = ()
        if status:
            sql += " WHERE c.status=?"
            params = (status,)
        sql += " ORDER BY c.id"
        out = []
        for r in self.db.conn.execute(sql, params):
            d = dict(r)
            d["card_no_masked"] = mask_card_no(d["card_no"])
            if d.get("old_card_no"):
                d["old_card_no_masked"] = mask_card_no(d["old_card_no"])
            if d.get("new_card_no"):
                d["new_card_no_masked"] = mask_card_no(d["new_card_no"])
            out.append(d)
        return out

    def list_people(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            """SELECT pe.*, t.name AS tenant_name, b.name AS building_name,
                      r.room_no, r.floor
               FROM people pe
               LEFT JOIN tenants t ON t.id=pe.tenant_id
               LEFT JOIN rooms r ON r.id=pe.room_id
               LEFT JOIN buildings b ON b.id=r.building_id
               ORDER BY pe.id""")]

    def list_groups(self) -> list[dict]:
        groups = [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM permission_groups ORDER BY id")]
        for g in groups:
            g["floors"] = self._floors_of(self.db.conn, g["id"])
            g["active_card_count"] = self.db.conn.execute(
                "SELECT COUNT(*) FROM cards WHERE group_id=? AND status='active'",
                (g["id"],)).fetchone()[0]
        return groups

    def list_buildings(self) -> list[dict]:
        out = []
        for r in self.db.conn.execute("SELECT * FROM buildings ORDER BY id"):
            d = dict(r)
            d["floor_list"] = [int(x) for x in d["floors"].split(",") if x]
            out.append(d)
        return out

    def list_devices(self) -> list[dict]:
        devs = [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM devices ORDER BY id")]
        for d in devs:
            d["pending_count"] = self.db.conn.execute(
                "SELECT COUNT(*) FROM device_outbox WHERE device_id=? AND status='pending'",
                (d["id"],)).fetchone()[0]
        return devs

    def list_loans(self, active_only=False) -> list[dict]:
        sql = (
            """SELECT l.*, c.card_no, fp.name AS from_name, tp.name AS to_name,
                      o.name AS operator_name
               FROM card_loans l
               JOIN cards c ON c.id=l.card_id
               JOIN people fp ON fp.id=l.from_person_id
               JOIN people tp ON tp.id=l.to_person_id
               LEFT JOIN operators o ON o.id=l.operator_id""")
        if active_only:
            sql += " WHERE l.status='active'"
        sql += " ORDER BY l.id DESC"
        out = []
        for r in self.db.conn.execute(sql):
            d = dict(r)
            d["card_no_masked"] = mask_card_no(d["card_no"])
            out.append(d)
        return out

    def list_lifecycle(self, limit=200) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            """SELECT l.*, c.card_type, p.name AS person_name, o.name AS operator_name
               FROM lifecycle_records l
               LEFT JOIN cards c ON c.id=l.card_id
               LEFT JOIN people p ON p.id=l.person_id
               LEFT JOIN operators o ON o.id=l.operator_id
               ORDER BY l.id DESC LIMIT ?""", (limit,))]

    def list_drafts(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            """SELECT d.*, p.name AS person_name, g.name AS group_name,
                      o.name AS operator_name, c.status AS card_status_now
               FROM issue_drafts d
               JOIN people p ON p.id=d.person_id
               JOIN permission_groups g ON g.id=d.group_id
               JOIN operators o ON o.id=d.operator_id
               JOIN cards c ON c.id=d.card_id
               ORDER BY d.id DESC""")]

    def list_permission_changes(self) -> list[dict]:
        out = []
        for r in self.db.conn.execute(
                "SELECT * FROM permission_changes ORDER BY id DESC"):
            d = dict(r)
            d["before"] = json.loads(d.pop("before_json"))
            d["after"] = json.loads(d.pop("after_json"))
            out.append(d)
        return out

    def list_events(self, etype=None, limit=300) -> list[dict]:
        sql = (
            """SELECT e.*, d.name AS device_name, p.name AS person_name
               FROM events e
               LEFT JOIN devices d ON d.id=e.device_id
               LEFT JOIN people p ON p.id=e.person_id""")
        params = []
        if etype:
            sql += " WHERE e.type=?"
            params.append(etype)
        sql += " ORDER BY e.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.conn.execute(sql, params)]

    def list_audit(self, action=None, operator_id=None, limit=300) -> list[dict]:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params = []
        if action:
            sql += " AND action LIKE ?"
            params.append(f"%{action}%")
        if operator_id:
            sql += " AND operator_id=?"
            params.append(operator_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.conn.execute(sql, params)]

    def list_access_events(self, synced=None, limit=200) -> list[dict]:
        sql = (
            """SELECT a.*, d.name AS device_name
               FROM access_events a JOIN devices d ON d.id=a.device_id""")
        params = []
        if synced is not None:
            sql += " WHERE a.synced=?"
            params.append(1 if synced else 0)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.conn.execute(sql, params)]

    def list_outbox(self, status=None) -> list[dict]:
        sql = (
            """SELECT o.*, d.name AS device_name
               FROM device_outbox o JOIN devices d ON d.id=o.device_id""")
        params = []
        if status:
            sql += " WHERE o.status=?"
            params.append(status)
        sql += " ORDER BY o.id DESC"
        return [dict(r) for r in self.db.conn.execute(sql, params)]

    def list_stock(self) -> dict:
        rows = self.db.conn.execute(
            "SELECT status, COUNT(*) n FROM cards GROUP BY status").fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {"total": sum(counts.values()), "counts": counts}

    def list_tenants(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM tenants ORDER BY id")]

    def list_operators(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            "SELECT id,login,name,role,active FROM operators ORDER BY id")]

    def list_rooms(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            """SELECT r.*, b.name AS building_name, t.name AS tenant_name
               FROM rooms r JOIN buildings b ON b.id=r.building_id
               LEFT JOIN tenants t ON t.id=r.tenant_id ORDER BY r.id""")]

    def overview(self) -> dict:
        c = self.db.conn
        card_counts = dict(c.execute(
            "SELECT status, COUNT(*) FROM cards GROUP BY status").fetchall())
        return {
            "people_total": c.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "people_departed": c.execute(
                "SELECT COUNT(*) FROM people WHERE status='departed'").fetchone()[0],
            "cards": card_counts,
            "pending_drafts": c.execute(
                "SELECT COUNT(*) FROM issue_drafts WHERE status='pending'").fetchone()[0],
            "open_loans": c.execute(
                "SELECT COUNT(*) FROM card_loans WHERE status='active'").fetchone()[0],
            "offline_devices": c.execute(
                "SELECT COUNT(*) FROM devices WHERE status='offline'").fetchone()[0],
            "pending_outbox": c.execute(
                "SELECT COUNT(*) FROM device_outbox WHERE status='pending'").fetchone()[0],
            "warning_events": c.execute(
                "SELECT COUNT(*) FROM events WHERE severity='warning'").fetchone()[0],
        }


CARD_STATUS_CN = {
    "in_stock": "在库",
    "active": "可用",
    "lost": "挂失",
    "damaged": "损坏",
    "frozen": "待回收(已冻结)",
    "recycled": "已回收",
    "expired": "已失效",
}

EVENT_TYPE_CN = {
    "card_issued": "发卡激活",
    "card_lost": "卡片遗失",
    "card_damaged": "卡片损坏",
    "card_replaced": "补卡换发",
    "card_borrowed": "借用转交",
    "card_frozen": "离场冻结",
    "card_recycled": "卡片回收",
    "temp_expired": "临时卡到期",
    "permission_changed": "权限变更",
    "permission_violation": "权限越界",
    "device_offline": "设备离线",
    "device_online": "设备恢复",
}
