"""First-party SQLite chat database with messages, embeddings, and summaries."""
from __future__ import annotations

import sqlite3
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_SCHEMA_VERSION = 1

_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS message_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    session_id TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_session ON message_embeddings(session_id);

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    summary_tokens INTEGER NOT NULL DEFAULT 0,
    messages_summarized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries(session_id, updated_at);
"""


@dataclass(slots=True)
class MessageRow:
    id: int
    session_id: str
    role: str
    content: str
    token_count: int
    created_at: str


@dataclass(slots=True)
class EmbeddingRow:
    message_id: int
    session_id: str
    embedding: np.ndarray
    content: str  # denormalised from messages for search results
    role: str
    created_at: str


@dataclass(slots=True)
class SummaryRow:
    session_id: str
    summary: str
    summary_tokens: int
    messages_summarized: int
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_embedding(vec: np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _decode_embedding(blob: bytes) -> np.ndarray:
    count = len(blob) // 4
    return np.array(struct.unpack(f"{count}f", blob), dtype=np.float32)


class ChatDatabase:
    """Thread-safe SQLite store for chat messages, embeddings, and summaries."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema(self._get_conn())
        self._migrate_langchain_history()

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_CREATE_TABLES)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            conn.commit()

    def _migrate_langchain_history(self) -> None:
        """One-time migration: import rows from LangChain's message_store table if it exists."""
        conn = self._get_conn()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        except Exception:
            return
        if "message_store" not in tables:
            return
        already = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if already > 0:
            return
        try:
            from app.llm.token_utils import estimate_tokens
            rows = conn.execute(
                "SELECT id, session_id, message FROM message_store ORDER BY id"
            ).fetchall()
            import json
            for row_id, session_id, message_json in rows:
                try:
                    msg = json.loads(message_json) if isinstance(message_json, str) else {}
                except Exception:
                    continue
                msg_type = msg.get("type", "")
                content = ""
                data = msg.get("data", {})
                if isinstance(data, dict):
                    content = data.get("content", "")
                if not content:
                    continue
                role = "user" if msg_type == "human" else "assistant" if msg_type == "ai" else ""
                if not role:
                    continue
                token_count = estimate_tokens(content)
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, token_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, token_count, _now_iso()),
                )
            conn.commit()
        except Exception:
            pass

    # ── Messages ──

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        token_count: int = 0,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, token_count, _now_iso()),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[MessageRow]:
        conn = self._get_conn()
        if limit:
            rows = conn.execute(
                "SELECT id, session_id, role, content, token_count, created_at "
                "FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(
                "SELECT id, session_id, role, content, token_count, created_at "
                "FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [MessageRow(*r) for r in rows]

    def get_message_count(self, session_id: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row else 0

    # ── Embeddings ──

    def add_embedding(
        self,
        message_id: int,
        session_id: str,
        embedding: np.ndarray,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO message_embeddings (message_id, session_id, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (message_id, session_id, _encode_embedding(embedding), _now_iso()),
        )
        conn.commit()

    def get_all_embeddings(self, session_id: str | None = None) -> list[EmbeddingRow]:
        conn = self._get_conn()
        if session_id:
            rows = conn.execute(
                "SELECT me.message_id, me.session_id, me.embedding, m.content, m.role, me.created_at "
                "FROM message_embeddings me JOIN messages m ON me.message_id = m.id "
                "WHERE me.session_id = ? ORDER BY me.message_id",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT me.message_id, me.session_id, me.embedding, m.content, m.role, me.created_at "
                "FROM message_embeddings me JOIN messages m ON me.message_id = m.id "
                "ORDER BY me.message_id",
            ).fetchall()
        return [
            EmbeddingRow(
                message_id=r[0],
                session_id=r[1],
                embedding=_decode_embedding(r[2]),
                content=r[3],
                role=r[4],
                created_at=r[5],
            )
            for r in rows
        ]

    def get_message_ids_with_embeddings(self, session_id: str) -> set[int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT message_id FROM message_embeddings WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {r[0] for r in rows}

    # ── Summaries ──

    def get_latest_summary(self, session_id: str) -> SummaryRow | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT session_id, summary, summary_tokens, messages_summarized, updated_at "
            "FROM session_summaries WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return SummaryRow(*row) if row else None

    def save_summary(
        self,
        session_id: str,
        summary: str,
        summary_tokens: int,
        messages_summarized: int,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO session_summaries (session_id, summary, summary_tokens, messages_summarized, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, summary, summary_tokens, messages_summarized, _now_iso()),
        )
        conn.commit()
