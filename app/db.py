"""SQLite 存储：发票计算结果以 JSON 原文落库。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from uuid import uuid4

def _default_db_path() -> str:
    # 容器内固定 /data（见 Dockerfile ENV）；本地直接运行时落到项目目录
    env = os.environ.get("INVOICE_DB_PATH")
    if env:
        return env
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/invoices.db"
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "invoices.db")


DB_PATH = _default_db_path()

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    path = DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id          TEXT PRIMARY KEY,
                invoice_no  TEXT NOT NULL UNIQUE,
                created_at  TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        _conn.commit()


def insert_invoice(result: dict) -> dict:
    assert _conn is not None
    record = {
        "id": uuid4().hex,
        "invoice_no": result["invoice_no"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    with _lock:
        _conn.execute(
            "INSERT INTO invoices (id, invoice_no, created_at, result_json) "
            "VALUES (?, ?, ?, ?)",
            (
                record["id"],
                record["invoice_no"],
                record["created_at"],
                json.dumps(result, default=_default),
            ),
        )
        _conn.commit()
    return record


def get_invoice(invoice_id: str | None = None, invoice_no: str | None = None) -> dict | None:
    assert _conn is not None
    with _lock:
        if invoice_id is not None:
            row = _conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        else:
            row = _conn.execute(
                "SELECT * FROM invoices WHERE invoice_no = ?", (invoice_no,)
            ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "invoice_no": row["invoice_no"],
        "created_at": row["created_at"],
        "result": json.loads(row["result_json"], parse_float=lambda x: x),
    }


def _default(obj):
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return format(obj, "f")
    raise TypeError(f"Unsupported type: {type(obj)}")
