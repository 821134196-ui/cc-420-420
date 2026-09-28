"""HTTP API：ThreadingHTTPServer + JSON，操作员身份通过 X-Operator-Id 头传递。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from .db import Database
from .services import Service, ServiceError, ROLE_LABELS
from .tasks import ExpiryScheduler

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "AccessCtl/1.0"

    # --- 静默标准日志，改由应用层输出关键信息
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    # -------------------------------------------------------- 响应辅助

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: ServiceError, status):
        self._send_json({"error": exc.message, "code": exc.code}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ServiceError("请求体不是合法 JSON", "bad_json")

    def _operator_id(self):
        raw = self.headers.get("X-Operator-Id")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            raise ServiceError("X-Operator-Id 非法", "bad_operator")

    # -------------------------------------------------------- 静态文件

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._serve_file("index.html")
                return
            if path.startswith("/static/"):
                self._serve_file(path[len("/static/"):])
                return
            if path.startswith("/api/"):
                self._route_get(path, parse_qs(parsed.query))
                return
            self._send_json({"error": "not found"}, 404)
        except ServiceError as exc:
            status = 403 if exc.code == "forbidden" else (
                404 if exc.code == "not_found" else 400)
            self._send_error(exc, status)
        except Exception as exc:  # pragma: no cover - 兜底
            self._send_json({"error": f"服务器内部错误: {exc}",
                             "code": "internal"}, 500)

    def do_POST(self):  # noqa: N802
        try:
            self._route_post(urlparse(self.path).path)
        except ServiceError as exc:
            status = 403 if exc.code == "forbidden" else (
                404 if exc.code == "not_found" else 409 if exc.code in (
                    "draft_conflict", "card_unavailable", "draft_closed",
                    "change_closed", "card_status", "person_departed",
                    "lost_restore_denied") else 400)
            self._send_error(exc, status)
        except Exception as exc:  # pragma: no cover - 兜底
            self._send_json({"error": f"服务器内部错误: {exc}",
                             "code": "internal"}, 500)

    def _serve_file(self, rel: str):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        suffix = target.suffix
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(suffix, "application/octet-stream"))
        data = target.read_bytes()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @property
    def svc(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    # -------------------------------------------------------- GET 路由

    def _route_get(self, path, query):
        s = self.svc
        if path == "/api/meta":
            self._send_json({
                "roles": ROLE_LABELS,
                "operators": s.list_operators(),
                "card_status": s.list_stock(),
            })
        elif path == "/api/overview":
            self._send_json(s.overview())
        elif path == "/api/buildings":
            self._send_json(s.list_buildings())
        elif path == "/api/rooms":
            self._send_json(s.list_rooms())
        elif path == "/api/tenants":
            self._send_json(s.list_tenants())
        elif path == "/api/people":
            self._send_json(s.list_people())
        elif path == "/api/groups":
            self._send_json(s.list_groups())
        elif path == "/api/cards":
            self._send_json(s.list_cards(
                query.get("status", [None])[0]))
        elif path == "/api/stock":
            self._send_json(s.list_stock())
        elif path == "/api/drafts":
            self._send_json(s.list_drafts())
        elif path == "/api/loans":
            self._send_json(s.list_loans())
        elif path == "/api/lifecycle":
            self._send_json(s.list_lifecycle())
        elif path == "/api/changes":
            self._send_json(s.list_permission_changes())
        elif path == "/api/devices":
            self._send_json(s.list_devices())
        elif path == "/api/outbox":
            self._send_json(s.list_outbox(
                query.get("status", [None])[0]))
        elif path == "/api/access-events":
            synced = query.get("synced", [None])[0]
            self._send_json(s.list_access_events(
                None if synced is None else synced == "1"))
        elif path == "/api/events":
            self._send_json(s.list_events(
                query.get("type", [None])[0]))
        elif path == "/api/audit":
            op = query.get("operator_id", [None])[0]
            self._send_json(s.list_audit(
                action=query.get("action", [None])[0],
                operator_id=int(op) if op else None))
        elif path == "/api/permission/check":
            # 权限试算：?group_id=&floor=&time=HH:MM
            g = int(query["group_id"][0])
            f = int(query["floor"][0])
            from datetime import datetime
            hm = query.get("time", [None])[0]
            at = None
            if hm:
                at = datetime.strptime(hm, "%H:%M")
            ok, reasons = s.evaluate_permission(g, f, at=at)
            self._send_json({"allowed": ok, "reasons": reasons})
        else:
            self._send_json({"error": "not found"}, 404)

    # -------------------------------------------------------- POST 路由

    def _route_post(self, path):
        s = self.svc
        op = self._operator_id()
        body = self._body()

        if path == "/api/issues/draft":
            self._send_json(s.create_issue_draft(
                op, body["card_no"], int(body["person_id"]),
                int(body["group_id"]), body.get("card_type", "normal"),
                body.get("valid_from"), body.get("valid_to"),
                body.get("expires_at"), body.get("note")), 201)
        elif path == "/api/issues/confirm":
            self._send_json(s.confirm_issue_draft(op, int(body["draft_id"])))
        elif path == "/api/issues/cancel":
            self._send_json(s.cancel_issue_draft(op, int(body["draft_id"])))
        elif path == "/api/cards/lost":
            self._send_json(s.report_lost(op, body["card_no"], body.get("note")))
        elif path == "/api/cards/damaged":
            self._send_json(s.report_damaged(
                op, body["card_no"], body.get("note")))
        elif path == "/api/cards/replace":
            self._send_json(s.replace_card(
                op, body["old_card_no"], body["new_card_no"],
                body.get("note")))
        elif path == "/api/cards/restore-attempt":
            s.restore_lost_directly(op, body["card_no"])  # 必然抛错
        elif path == "/api/cards/borrow":
            self._send_json(s.borrow_card(
                op, body["card_no"], int(body["to_person_id"]),
                body.get("due_at"), body.get("note")), 201)
        elif path == "/api/people/depart":
            self._send_json(s.depart_person(
                op, int(body["person_id"]), body.get("note")))
        elif path == "/api/cards/recycle":
            self._send_json(s.recycle_card(
                op, body["card_no"], body.get("note")))
        elif path == "/api/groups/change":
            self._send_json(s.create_permission_change(
                op, int(body["group_id"]), body.get("name"),
                body.get("description"),
                body.get("floors"), body.get("time_start"),
                body.get("time_end"), body.get("valid_from"),
                body.get("valid_to"), body.get("reason")), 201)
        elif path == "/api/groups/change/confirm":
            self._send_json(s.confirm_permission_change(
                op, int(body["change_id"]), bool(body.get("confirm", True))))
        elif path == "/api/devices/status":
            self._send_json(s.set_device_status(
                op, int(body["device_id"]), bool(body["online"])))
        elif path == "/api/access/attempt":
            self._send_json(s.attempt_access(
                op, int(body["device_id"]), body["card_no"],
                int(body["floor"])), 201)
        elif path == "/api/tasks/expire":
            # 手动触发到期扫描（测试/演示用）
            s.require_role(op, ("admin",))
            self._send_json({"expired": s.expire_temporary_cards()})
        else:
            self._send_json({"error": "not found"}, 404)


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8088,
                 run_scheduler: bool = True) -> tuple[ThreadingHTTPServer, ExpiryScheduler | None]:
    db = Database(db_path)
    service = Service(db)
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service  # type: ignore[attr-defined]
    scheduler = None
    if run_scheduler:
        scheduler = ExpiryScheduler(service, interval=10.0)
        scheduler.start()
        server.scheduler = scheduler  # type: ignore[attr-defined]
    return server, scheduler
