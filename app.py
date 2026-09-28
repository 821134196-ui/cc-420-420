"""楼宇门禁卡系统 —— HTTP 服务与单条命令本地启动入口。

启动（自动建库并预置发卡/挂失/回收演示数据）：
    python3 app.py                 # 默认 0.0.0.0:8000，数据库 access.db
    python3 app.py --port 8080 --reset

岗位身份通过请求头 X-User-Id（用户名）传递，角色由 admin_users 表决定；
前端顶栏可切换当前操作岗位（管理员 / A柜台 / B柜台 / 审计员）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from core import (
    AccessError, AccessSystem, PermissionDenied, ROLE_LABELS,
    CARD_REGULAR, CARD_TEMP, ROLE_ADMIN, ROLE_CLERK,
)
from seed import build_system

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 发卡两步确认的服务端暂存（prepare -> confirm）
_PREPARE_TTL = 300  # 秒


class IssueStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, str, dict[str, Any]]] = {}

    def put(self, username: str, payload: dict[str, Any]) -> str:
        token = secrets.token_hex(8)
        with self._lock:
            self._items[token] = (time.time() + _PREPARE_TTL, username, payload)
        return token

    def take(self, token: str, username: str) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._items.get(token)
            if not item:
                return None
            expires, owner, payload = item
            # 非属主或已过期的尝试不得消费掉预填单，属主仍可完成第二步
            if owner != username or time.time() > expires:
                return None
            self._items.pop(token)
        return payload


SYSTEM: AccessSystem = None  # type: ignore[assignment]
ISSUES = IssueStore()


# ---------------------------------------------------------------------------
# 业务处理
# ---------------------------------------------------------------------------

def current_user(handler: "Handler") -> dict[str, Any]:
    username = handler.headers.get("X-User-Id", "clerk_a")
    row = SYSTEM.conn.execute(
        "SELECT username, display_name, role FROM admin_users WHERE username=?",
        (username,)).fetchone()
    if not row:
        # 未知身份降级为只读审计视角
        row = SYSTEM.conn.execute(
            "SELECT username, display_name, role FROM admin_users WHERE username='auditor'"
        ).fetchone()
    return dict(row)


def build_state(reveal: bool, role: str) -> dict[str, Any]:
    # 敏感号码：默认遮挡；仅管理员/审计员可查看明文
    can_reveal = reveal and role in ("admin", "auditor")
    cards = SYSTEM.list_cards(include_masked=True)
    if not can_reveal:
        for c in cards:
            c["card_no"] = c["card_no_masked"]
    people = SYSTEM.list_people()
    if not can_reveal:
        for p in people:
            p["id_no"] = p["id_no_masked"]
            p["phone"] = p["phone_masked"]
    return {
        "buildings": SYSTEM.list_buildings(),
        "tenants": SYSTEM.list_tenants(),
        "people": people,
        "groups": SYSTEM.list_groups(),
        "cards": cards,
        "recycles": SYSTEM.list_recycles(),
        "events": SYSTEM.list_events(),
        "staff": SYSTEM.list_staff(),
        "device": SYSTEM.device.status(),
        "matrix": SYSTEM.floor_matrix(),
        "event_digest": SYSTEM.event_digest(),
        "role_labels": ROLE_LABELS,
        "revealed": can_reveal,
    }


def resolve_card_no(body: dict[str, Any]) -> str:
    """前端列表默认仅持有遮挡卡号，操作统一用 card_id 解析真实卡号。"""
    if body.get("card_id"):
        row = SYSTEM.conn.execute(
            "SELECT card_no FROM cards WHERE id=?", (int(body["card_id"]),)).fetchone()
        if not row:
            raise AccessError("卡片不存在")
        return row["card_no"]
    if not body.get("card_no"):
        raise AccessError("缺少 card_id 或 card_no")
    return body["card_no"]


def handle_api(method: str, path: str, query: dict[str, list[str]],
               body: dict[str, Any], user: dict[str, Any]) -> tuple[int, Any]:
    role = user["role"]
    actor = user["display_name"]

    if method == "GET" and path == "/api/state":
        reveal = query.get("reveal", ["0"])[0] in ("1", "true")
        return 200, {"me": user, **build_state(reveal, role)}

    if method == "GET" and path == "/api/matrix":
        return 200, SYSTEM.floor_matrix()

    if method == "GET" and path == "/api/events":
        etype = query.get("type", [None])[0]
        return 200, {"events": SYSTEM.list_events(etype), "digest": SYSTEM.event_digest()}

    if method == "POST" and path == "/api/access/check":
        at = None
        if body.get("at"):
            from datetime import datetime
            at = datetime.strptime(body["at"], "%Y-%m-%dT%H:%M")
        card_no = resolve_card_no(body)
        result = SYSTEM.evaluate_access(card_no, int(body["floor"]),
                                        at=at, record=True, actor="门禁仿真")
        return 200, result

    # ---- 发卡：两步（prepare / confirm） -------------------------------
    if method == "POST" and path == "/api/issue/prepare":
        AccessSystem._require(role, (ROLE_ADMIN, ROLE_CLERK), "发卡")
        card_type = body.get("card_type", CARD_REGULAR)
        expires_at = body.get("expires_at") or None
        if expires_at and len(expires_at) == 16:  # datetime-local 控件
            expires_at = expires_at.replace("T", " ") + ":00"
        person = SYSTEM.conn.execute(
            "SELECT p.id,p.name,t.unit_no FROM people p LEFT JOIN tenants t ON t.id=p.tenant_id "
            "WHERE p.id=?", (int(body["person_id"]),)).fetchone()
        group = SYSTEM.get_group(int(body["group_id"]))
        if not person:
            raise AccessError("人员不存在")
        if not group:
            raise AccessError("权限组不存在")
        exists = SYSTEM.conn.execute("SELECT 1 FROM cards WHERE card_no=?",
                                     (body["card_no"],)).fetchone()
        if exists:
            raise AccessError(f"卡号 {body['card_no']} 已存在，无法发放")
        floors = [r["floor"] for r in group["rules"]]
        payload = {
            "card_no": body["card_no"].strip().upper(),
            "person_id": int(body["person_id"]),
            "group_id": int(body["group_id"]),
            "card_type": card_type,
            "expires_at": expires_at,
            "counter": body.get("counter", "A柜台"),
        }
        preview = {
            "card_no": payload["card_no"],
            "person": dict(person),
            "group": group["name"],
            "floors": floors,
            "rules": group["rules"],
            "card_type": card_type,
            "expires_at": expires_at,
            "counter": payload["counter"],
            "device_online": SYSTEM.device.online,
        }
        token = ISSUES.put(user["username"], payload)
        return 200, {"token": token, "preview": preview}

    if method == "POST" and path == "/api/issue/confirm":
        payload = ISSUES.take(body.get("token", ""), user["username"])
        if not payload:
            raise AccessError("发卡预填单不存在或已过期，请重新走两步发卡")
        result = SYSTEM.issue_card(
            payload["card_no"], payload["person_id"], payload["group_id"],
            card_type=payload["card_type"], expires_at=payload["expires_at"],
            counter=payload["counter"], role=role, actor=actor)
        return 200, {"ok": True, **result}

    # ---- 单卡操作 -------------------------------------------------------
    card_actions = {
        "/api/card/activate": lambda b: SYSTEM.activate_card(resolve_card_no(b), role=role, actor=actor),
        "/api/card/lost": lambda b: SYSTEM.report_lost(resolve_card_no(b), role=role, actor=actor),
        "/api/card/damage": lambda b: SYSTEM.report_damaged(resolve_card_no(b), role=role, actor=actor),
    }
    if method == "POST" and path in card_actions:
        return 200, card_actions[path](body)

    if method == "POST" and path == "/api/card/reissue":
        if body.get("old_card_id"):
            row = SYSTEM.conn.execute("SELECT card_no FROM cards WHERE id=?",
                                      (int(body["old_card_id"]),)).fetchone()
            if not row:
                raise AccessError("旧卡不存在")
            old_no = row["card_no"]
        else:
            old_no = body["old_card_no"]
        return 200, SYSTEM.reissue_card(old_no, body["new_card_no"].strip().upper(),
                                        role=role, actor=actor)

    if method == "POST" and path == "/api/card/transfer":
        return 200, SYSTEM.transfer_card(resolve_card_no(body), int(body["person_id"]),
                                         role=role, actor=actor)

    if method == "POST" and path == "/api/card/recycle":
        return 200, SYSTEM.recycle_card(resolve_card_no(body), body.get("reason", "租户离场"),
                                        role=role, actor=actor)

    # ---- 权限组 ---------------------------------------------------------
    if method == "POST" and path == "/api/groups":
        gid = SYSTEM.create_group(body["name"], body.get("description", ""), role=role)
        return 200, {"id": gid}

    if method == "POST" and path == "/api/groups/rules":
        try:
            SYSTEM.set_group_rule(
                int(body["group_id"]), int(body["floor"]),
                body.get("start_time", "00:00"), body.get("end_time", "23:59"),
                body["valid_from"], body["valid_to"],
                confirm=bool(body.get("confirm")), role=role, actor=actor)
        except AccessError as e:
            if "二次确认" in str(e):
                return HTTPStatus.BAD_REQUEST, {"error": str(e), "needs_confirm": True}
            raise
        return 200, {"ok": True}

    # ---- 设备 -----------------------------------------------------------
    if method == "POST" and path == "/api/device/offline":
        return 200, SYSTEM.set_device_online(False, role=role)
    if method == "POST" and path == "/api/device/online":
        return 200, SYSTEM.set_device_online(True, role=role)
    if method == "POST" and path == "/api/device/sync":
        return 200, SYSTEM.sync_device(role=role)

    return HTTPStatus.NOT_FOUND, {"error": f"未知接口 {method} {path}"}


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "AccessCard/1.0"

    def _send_json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, rel: str, content_type: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with open(path, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path.startswith("/api/"):
            self._dispatch_api("GET", path, query, {})
            return
        if path == "/":
            self._send_file("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._send_file("app.js", "application/javascript; charset=utf-8")
        elif path == "/style.css":
            self._send_file("style.css", "text/css; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        self._dispatch_api("POST", parsed.path, {}, body)

    def _dispatch_api(self, method: str, path: str,
                      query: dict[str, list[str]], body: dict[str, Any]) -> None:
        user = current_user(self)
        try:
            status, data = handle_api(method, path, query, body, user)
        except PermissionDenied as e:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": str(e)})
        except AccessError as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务器错误：{e}"})
        else:
            self._send_json(status, data)

    def log_message(self, fmt: str, *args: Any) -> None:  # 精简启动日志
        pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def ensure_db(db_path: str, reset: bool) -> AccessSystem:
    if reset and db_path != ":memory:" and os.path.exists(db_path):
        os.remove(db_path)
    fresh = db_path == ":memory:" or not os.path.exists(db_path)
    system = AccessSystem(db_path)
    if fresh:
        from seed import seed
        seed(system)
    return system


def main() -> None:
    parser = argparse.ArgumentParser(description="楼宇门禁卡发放与离场回收系统")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="access.db")
    parser.add_argument("--reset", action="store_true", help="删除旧库并重新预置数据")
    parser.add_argument("--no-scheduler", action="store_true", help="关闭到期自动失效定时任务")
    args = parser.parse_args()

    global SYSTEM
    SYSTEM = ensure_db(args.db, args.reset)
    if not args.no_scheduler:
        SYSTEM.start_scheduler(interval=10.0)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 64)
    print("  楼宇门禁卡发放与离场回收系统已启动")
    print(f"  地址：http://localhost:{args.port}")
    print(f"  数据库：{args.db}（演示数据已{'重置并' if args.reset else ''}预置）")
    print("  岗位切换（顶栏下拉）：admin 管理员 / clerk_a A柜台 / clerk_b B柜台 / auditor 审计员")
    print("  Ctrl+C 停止")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.server_close()
        SYSTEM.close()


if __name__ == "__main__":
    main()
