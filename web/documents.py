"""Durable, versioned business documents created from assistant replies."""

from __future__ import annotations

import uuid
from typing import Any

from web.database import connect


DOCUMENT_TYPES = {"listing_copy", "pricing_sheet", "competitor_report"}


def _validate(doc_type: str, title: str, content: str) -> tuple[str, str, str]:
    if doc_type not in DOCUMENT_TYPES:
        raise ValueError("不支持的文档类型")
    clean_title = (title or "未命名文档").strip()[:160]
    clean_content = (content or "").strip()
    if not clean_content:
        raise ValueError("文档内容不能为空")
    return doc_type, clean_title, clean_content[:120000]


def create_document(user_id: str, store_id: str, doc_type: str, title: str,
                    content: str, session_id: str | None = None,
                    source_message_id: int | None = None) -> dict[str, Any]:
    doc_type, title, content = _validate(doc_type, title, content)
    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    with connect() as db:
        if session_id:
            session = db.execute(
                "SELECT 1 FROM chat_sessions WHERE id=? AND user_id=? AND store_id=?",
                (session_id, user_id, store_id),
            ).fetchone()
            if not session:
                raise ValueError("对话不存在")
        if source_message_id is not None:
            message = db.execute(
                "SELECT 1 FROM chat_messages WHERE id=? AND session_id=? AND role='assistant'",
                (source_message_id, session_id),
            ).fetchone()
            if not message:
                raise ValueError("文档来源消息无效")
        db.execute(
            "INSERT INTO documents(id,user_id,store_id,session_id,source_message_id,doc_type,title,content) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (document_id, user_id, store_id, session_id, source_message_id, doc_type, title, content),
        )
        db.execute(
            "INSERT INTO document_versions(document_id,version,title,content) VALUES(?,1,?,?)",
            (document_id, title, content),
        )
    return get_document(document_id, user_id, store_id) or {}


def get_document(document_id: str, user_id: str, store_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT id,session_id,source_message_id,doc_type,title,content,current_version,created_at,updated_at "
            "FROM documents WHERE id=? AND user_id=? AND store_id=?",
            (document_id, user_id, store_id),
        ).fetchone()
    return dict(row) if row else None


def documents_for_session(session_id: str, user_id: str, store_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT id,session_id,source_message_id,doc_type,title,content,current_version,created_at,updated_at "
            "FROM documents WHERE session_id=? AND user_id=? AND store_id=? ORDER BY created_at,id",
            (session_id, user_id, store_id),
        ).fetchall()
    return [dict(row) for row in rows]


def update_document(document_id: str, user_id: str, store_id: str,
                    title: str, content: str) -> dict[str, Any] | None:
    with connect() as db:
        current = db.execute(
            "SELECT doc_type,title,content,current_version FROM documents "
            "WHERE id=? AND user_id=? AND store_id=?",
            (document_id, user_id, store_id),
        ).fetchone()
        if not current:
            return None
        _, title, content = _validate(current["doc_type"], title, content)
        if title == current["title"] and content == current["content"]:
            return get_document(document_id, user_id, store_id)
        version = int(current["current_version"]) + 1
        db.execute(
            "UPDATE documents SET title=?,content=?,current_version=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, content, version, document_id),
        )
        db.execute(
            "INSERT INTO document_versions(document_id,version,title,content) VALUES(?,?,?,?)",
            (document_id, version, title, content),
        )
    return get_document(document_id, user_id, store_id)


def list_versions(document_id: str, user_id: str, store_id: str) -> list[dict[str, Any]] | None:
    with connect() as db:
        owned = db.execute(
            "SELECT 1 FROM documents WHERE id=? AND user_id=? AND store_id=?",
            (document_id, user_id, store_id),
        ).fetchone()
        if not owned:
            return None
        rows = db.execute(
            "SELECT version,title,content,created_at FROM document_versions "
            "WHERE document_id=? ORDER BY version DESC",
            (document_id,),
        ).fetchall()
    return [dict(row) for row in rows]
