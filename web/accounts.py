"""Account provisioning shared by the CLI (`web/manage.py`) and the
web-based admin account management API (`web/app.py`'s `/api/admin/*`).

Kept as one place so "create a login account" — including the one-time
first-account data migration — isn't implemented twice.
"""

from __future__ import annotations

import uuid
from typing import Any

from web import auth
from web.database import connect, init_db

MIN_PASSWORD_LEN = 8

_MIGRATE_TABLES = (
    "stores", "opportunities", "import_batches", "imported_rows", "chat_sessions", "documents", "audit_log",
)


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码太短，至少 {MIN_PASSWORD_LEN} 位")
    if len(password.encode("utf-8")) > auth.MAX_PASSWORD_BYTES:
        raise ValueError(f"密码过长（UTF-8 编码后需 ≤{auth.MAX_PASSWORD_BYTES} 字节）")


def create_user(username: str, password: str, name: str | None = None,
                is_admin: bool = False) -> str:
    """建一个新登录账号。

    第一个真实账号（此前没有任何 username 非空的用户）会自动继承 'default' 用户
    的历史数据（机会点/导入记录/聊天记录等），并自动成为管理员，与调用方传入的
    is_admin 无关。之后的账号各自拿到一个全新的空店铺，不共用 'default' 那个 id
    （否则会撞上首个账号已经占用的店铺）。
    """
    _validate_password(password)
    # bcrypt 在拿写锁之前算好：hashpw 本身不依赖任何数据库状态，放在 BEGIN IMMEDIATE
    # 里面的话，这几十上百毫秒的 CPU 时间会一直攥着全库写锁，阻塞聊天/导入/登录等其他写入。
    password_hash = auth.hash_password(password)
    init_db()
    with connect() as db:
        # BEGIN IMMEDIATE：立刻拿写锁，避免并发建号都读到"我是第一个账号"。
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError(f"用户名已存在：{username}")
        is_first_real_account = not db.execute(
            "SELECT 1 FROM users WHERE username IS NOT NULL"
        ).fetchone()
        user_id = f"user_{uuid.uuid4().hex[:10]}"
        admin_flag = 1 if (is_admin or is_first_real_account) else 0
        db.execute(
            "INSERT INTO users(id,name,username,password_hash,is_admin) VALUES(?,?,?,?,?)",
            (user_id, name or username, username, password_hash, admin_flag),
        )
        if is_first_real_account:
            for table in _MIGRATE_TABLES:
                db.execute(f"UPDATE {table} SET user_id=? WHERE user_id='default'", (user_id,))
        else:
            store_id = f"store_{uuid.uuid4().hex[:10]}"
            db.execute("INSERT INTO stores(id,user_id,name,marketplace) VALUES(?,?,?,?)",
                      (store_id, user_id, "默认店铺", "US"))
    return user_id


def list_users() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT id,username,name,is_admin,created_at FROM users "
            "WHERE username IS NOT NULL ORDER BY created_at"
        ).fetchall()
    return [{**dict(r), "is_admin": bool(r["is_admin"])} for r in rows]


def reset_password(user_id: str, new_password: str) -> None:
    _validate_password(new_password)
    password_hash = auth.hash_password(new_password)  # bcrypt 算在拿锁之前，同 create_user
    with connect() as db:
        row = db.execute("SELECT 1 FROM users WHERE id=? AND username IS NOT NULL",
                         (user_id,)).fetchone()
        if not row:
            raise ValueError("账号不存在")
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        # 重置密码后把这个账号所有已登录的 session 都踢下线，不然重置形同虚设。
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
