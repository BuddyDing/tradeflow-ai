"""SQLite persistence for the public MVP.

The schema is deliberately small but every business row is scoped by user and
store so moving to Postgres later does not require changing API contracts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(os.environ.get("TRADEFLOW_DB_PATH", Path(__file__).parent / "_store" / "tradeflow.db"))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _ensure_column(db: sqlite3.Connection, table: str, column: str, decl: str) -> bool:
    """加列（幂等）。返回这次调用是不是真的新加了这一列——用来判断"列刚刚才出现"，
    从而把一次性迁移逻辑绑定在这个时刻，而不是每次启动都重新判断、变成一条常驻规则。"""
    cols = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in cols:
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stores (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
              marketplace TEXT NOT NULL DEFAULT 'US', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS opportunities (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, store_id TEXT NOT NULL,
              item_key TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(store_id) REFERENCES stores(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_opp_key
              ON opportunities(user_id, store_id, item_key) WHERE item_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS import_batches (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, store_id TEXT NOT NULL,
              filename TEXT NOT NULL, report_type TEXT NOT NULL, status TEXT NOT NULL,
              row_count INTEGER NOT NULL DEFAULT 0, columns_json TEXT NOT NULL DEFAULT '[]',
              mapping_json TEXT NOT NULL DEFAULT '{}', error TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(store_id) REFERENCES stores(id)
            );
            CREATE TABLE IF NOT EXISTS imported_rows (
              id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
              user_id TEXT NOT NULL, store_id TEXT NOT NULL, row_json TEXT NOT NULL,
              FOREIGN KEY(batch_id) REFERENCES import_batches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS chat_sessions (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, store_id TEXT NOT NULL,
              title TEXT NOT NULL, agent TEXT NOT NULL DEFAULT 'default',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(store_id) REFERENCES stores(id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_scope
              ON chat_sessions(user_id, store_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS chat_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('user','assistant')),
              content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session
              ON chat_messages(session_id, id);
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, store_id TEXT NOT NULL,
              session_id TEXT, source_message_id INTEGER,
              doc_type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
              current_version INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(store_id) REFERENCES stores(id),
              FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE SET NULL,
              FOREIGN KEY(source_message_id) REFERENCES chat_messages(id) ON DELETE SET NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_source_message
              ON documents(source_message_id) WHERE source_message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_documents_scope
              ON documents(user_id, store_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS document_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
              version INTEGER NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
              UNIQUE(document_id, version)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, store_id TEXT,
              action TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            """
        )
        _ensure_column(db, "users", "username", "TEXT")
        _ensure_column(db, "users", "password_hash", "TEXT")
        is_admin_col_new = _ensure_column(db, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username ON users(username) WHERE username IS NOT NULL")
        # 一次性迁移，只在 is_admin 这一列刚被加出来的那次 init_db() 里跑：账号管理功能
        # 上线前建的账号（比如最早那个登录账号）没有机会被标成管理员，只能靠 DEFAULT 0
        # 落地。只在这一刻把最早建的真实账号补成管理员，不能做成"没有管理员就自动提权"
        # 的常驻规则——那样以后任何原因导致零管理员（哪怕是 bug）都会在下次重启时把某个
        # 普通成员悄悄提权，等于绕过审批的权限提升。
        if is_admin_col_new:
            row = db.execute(
                "SELECT id FROM users WHERE username IS NOT NULL ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row:
                db.execute("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
        db.execute("INSERT OR IGNORE INTO users(id,name) VALUES('default','默认用户')")
        db.execute("INSERT OR IGNORE INTO stores(id,user_id,name,marketplace) VALUES('default','default','默认店铺','US')")


def audit(user_id: str, store_id: str | None, action: str, detail: Any = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO audit_log(user_id,store_id,action,detail_json) VALUES(?,?,?,?)",
            (user_id, store_id, action, json.dumps(detail or {}, ensure_ascii=False)),
        )
