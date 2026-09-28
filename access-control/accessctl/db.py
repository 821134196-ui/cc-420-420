"""楼宇门禁卡系统 —— SQLite 持久化层。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operators (
    id INTEGER PRIMARY KEY,
    login TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','clerk','guard','auditor')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    floors TEXT NOT NULL DEFAULT '1,2,3'
);

CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    contact_person TEXT,
    phone TEXT,
    unit TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    floor INTEGER NOT NULL,
    room_no TEXT NOT NULL,
    label TEXT,
    tenant_id INTEGER REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    person_no TEXT UNIQUE NOT NULL,
    tenant_id INTEGER REFERENCES tenants(id),
    room_id INTEGER REFERENCES rooms(id),
    position TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','departed')),
    departed_at TEXT
);

CREATE TABLE IF NOT EXISTS permission_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    time_start TEXT NOT NULL DEFAULT '00:00',
    time_end TEXT NOT NULL DEFAULT '23:59',
    valid_from TEXT,
    valid_to TEXT,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_floors (
    group_id INTEGER NOT NULL REFERENCES permission_groups(id) ON DELETE CASCADE,
    floor INTEGER NOT NULL,
    PRIMARY KEY (group_id, floor)
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT,
    building_id INTEGER REFERENCES buildings(id),
    status TEXT NOT NULL DEFAULT 'online' CHECK(status IN ('online','offline')),
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    card_no TEXT UNIQUE NOT NULL,
    card_type TEXT NOT NULL DEFAULT 'normal' CHECK(card_type IN ('normal','temporary')),
    status TEXT NOT NULL DEFAULT 'in_stock'
        CHECK(status IN ('in_stock','active','lost','damaged','frozen','recycled','expired')),
    person_id INTEGER REFERENCES people(id),
    group_id INTEGER REFERENCES permission_groups(id),
    valid_from TEXT,
    valid_to TEXT,
    expires_at TEXT,
    issued_at TEXT,
    activated_at TEXT,
    frozen_at TEXT,
    recycled_at TEXT,
    replaced_card_id INTEGER REFERENCES cards(id),
    replaced_by_card_id INTEGER REFERENCES cards(id),
    batch_no TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS issue_drafts (
    id INTEGER PRIMARY KEY,
    draft_no TEXT UNIQUE NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES operators(id),
    card_id INTEGER NOT NULL REFERENCES cards(id),
    card_no TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES people(id),
    group_id INTEGER NOT NULL REFERENCES permission_groups(id),
    card_type TEXT NOT NULL DEFAULT 'normal',
    valid_from TEXT,
    valid_to TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','cancelled')),
    note TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS card_loans (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    from_person_id INTEGER NOT NULL REFERENCES people(id),
    to_person_id INTEGER NOT NULL REFERENCES people(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','returned')),
    note TEXT,
    borrowed_at TEXT NOT NULL,
    due_at TEXT,
    returned_at TEXT,
    operator_id INTEGER REFERENCES operators(id)
);

CREATE TABLE IF NOT EXISTS lifecycle_records (
    id INTEGER PRIMARY KEY,
    card_id INTEGER REFERENCES cards(id),
    card_no TEXT NOT NULL,
    action TEXT NOT NULL,
    person_id INTEGER REFERENCES people(id),
    operator_id INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permission_changes (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES permission_groups(id),
    operator_id INTEGER NOT NULL REFERENCES operators(id),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
    reason TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS device_outbox (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    card_id INTEGER REFERENCES cards(id),
    card_no TEXT NOT NULL,
    command TEXT NOT NULL CHECK(command IN ('issue','revoke')),
    payload_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered','failed')),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    failure TEXT
);

CREATE TABLE IF NOT EXISTS access_events (
    id INTEGER PRIMARY KEY,
    device_id INTEGER REFERENCES devices(id),
    card_no TEXT NOT NULL,
    floor INTEGER,
    event_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('granted','denied')),
    reason TEXT,
    synced INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    card_id INTEGER REFERENCES cards(id),
    card_no TEXT,
    device_id INTEGER REFERENCES devices(id),
    person_id INTEGER REFERENCES people(id),
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    operator_id INTEGER,
    operator_name TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_person ON cards(person_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_card ON lifecycle_records(card_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON device_outbox(status);
"""


class Database:
    """每个工作线程获取独立连接（check_same_thread=False + 线程本地绑定）。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._new_conn()
            self._local.conn = c
        return c

    def init_schema(self) -> None:
        with self._init_lock:
            conn = self._new_conn()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA_SQL)
            finally:
                conn.close()

    @contextmanager
    def tx(self):
        """立即拿写锁的事务，用于并发发卡等需要先查后写的场景。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @contextmanager
    def ro(self):
        yield self.conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
