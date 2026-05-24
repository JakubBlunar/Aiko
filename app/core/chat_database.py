"""First-party SQLite chat database for messages, summaries, and memories."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 3

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

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    summary_tokens INTEGER NOT NULL DEFAULT 0,
    messages_summarized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries(session_id, updated_at);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    salience REAL NOT NULL DEFAULT 0.5,
    embedding BLOB NOT NULL,
    source_session TEXT,
    source_message_id INTEGER,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_salience ON memories(salience);
"""

# Tables that existed in earlier schemas but are no longer used.
_OBSOLETE_TABLES = ("personality_notes", "recent_topics", "message_embeddings")


@dataclass(slots=True)
class MessageRow:
    id: int
    session_id: str
    role: str
    content: str
    token_count: int
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


class ChatDatabase:
    """Thread-safe SQLite store for chat messages, embeddings, and summaries."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema(self._get_conn())
        self._migrate_langchain_history()
        self._backfill_legacy_meta_tags()
        # Listeners notified after each successful add_message. Used by
        # :class:`app.core.message_indexer.MessageIndexer` to embed and write
        # to the RAG store asynchronously.
        self._add_listeners: list[Any] = []

    def add_message_listener(self, callback: Any) -> None:
        """Register a ``callback(MessageRow)`` invoked after add_message.

        Listeners run synchronously on the caller thread; they should
        offload any heavy work themselves.
        """
        if callback is not None and callback not in self._add_listeners:
            self._add_listeners.append(callback)

    def remove_message_listener(self, callback: Any) -> None:
        try:
            self._add_listeners.remove(callback)
        except ValueError:
            pass

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
            return
        if row[0] >= _SCHEMA_VERSION:
            return
        # On upgrade: drop tables that are no longer used so they stop wasting
        # space and confusing later readers. ``messages`` and
        # ``session_summaries`` are kept intact.
        for table in _OBSOLETE_TABLES:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                pass
        conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
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

    def _backfill_legacy_meta_tags(self) -> None:
        """One-shot pass: re-strip already-stored assistant messages.

        Older rows were saved with raw ``[[spoken]]``/``[[detail]]``/
        ``[[reaction:X]]``/``[[remember:...]]`` markers because the streaming
        strip had a partial-tag leak bug. Run the current stripper over them
        so they display clean on the next reload. Idempotent: rows that
        already strip to themselves are skipped.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, content FROM messages "
                "WHERE role = 'assistant' AND ("
                "  content LIKE '%[[spoken]]%'"
                "  OR content LIKE '%[[/spoken]]%'"
                "  OR content LIKE '%[[detail]]%'"
                "  OR content LIKE '%[[/detail]]%'"
                "  OR content LIKE '%[[reaction:%'"
                "  OR content LIKE '%[[remember:%'"
                ")"
            ).fetchall()
        except Exception:
            return
        if not rows:
            return
        try:
            from app.core.services.response_text_service import strip_all_meta_tags
            from app.core.session_text_utils import sanitize_assistant_text
            from app.llm.token_utils import estimate_tokens
        except Exception:
            return
        updated = 0
        for row_id, content in rows:
            stripped = strip_all_meta_tags(str(content or ""))
            cleaned = sanitize_assistant_text(stripped)
            if cleaned == content:
                continue
            try:
                conn.execute(
                    "UPDATE messages SET content = ?, token_count = ? WHERE id = ?",
                    (cleaned, estimate_tokens(cleaned), row_id),
                )
                updated += 1
            except Exception:
                continue
        if updated:
            conn.commit()

    # ── Messages ──

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        token_count: int = 0,
    ) -> int:
        conn = self._get_conn()
        created_at = _now_iso()
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, token_count, created_at),
        )
        conn.commit()
        msg_id = int(cursor.lastrowid or 0)
        if self._add_listeners:
            row = MessageRow(
                id=msg_id,
                session_id=session_id,
                role=role,
                content=content,
                token_count=token_count,
                created_at=created_at,
            )
            for listener in list(self._add_listeners):
                try:
                    listener(row)
                except Exception:
                    # Listeners must not break the write path.
                    pass
        return msg_id

    def get_messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MessageRow]:
        """Return messages for a session, ordered by id (oldest first).

        *limit* — return at most this many rows (taken from the end, i.e. most recent).
        *offset* — skip the first *offset* rows (oldest), then return the rest (or *limit* of them).
        When both are given, *offset* rows are skipped first, then *limit* rows are taken
        from the end of the remaining set.
        """
        conn = self._get_conn()
        if offset and limit:
            rows = conn.execute(
                "SELECT id, session_id, role, content, token_count, created_at "
                "FROM messages WHERE session_id = ? ORDER BY id LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        elif offset:
            rows = conn.execute(
                "SELECT id, session_id, role, content, token_count, created_at "
                "FROM messages WHERE session_id = ? ORDER BY id LIMIT -1 OFFSET ?",
                (session_id, offset),
            ).fetchall()
        elif limit:
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

    def clear_messages(self, session_id: str, *, full_reset: bool = False) -> int:
        """Delete all messages and summaries for a session. Returns deleted count.

        ``full_reset`` is accepted for backward-compat but no longer has a
        distinct effect now that personality_notes / recent_topics tables are
        gone. Long-term memories are cross-session and untouched here.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        conn.execute(
            "DELETE FROM session_summaries WHERE session_id = ?", (session_id,)
        )
        conn.commit()
        return cursor.rowcount

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
        *,
        keep_latest: int = 3,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO session_summaries (session_id, summary, summary_tokens, messages_summarized, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, summary, summary_tokens, messages_summarized, _now_iso()),
        )
        if keep_latest and keep_latest > 0:
            conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ? AND id NOT IN "
                "(SELECT id FROM session_summaries WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?)",
                (session_id, session_id, keep_latest),
            )
        conn.commit()

    # ── Session management ──

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return all distinct sessions with message count and last activity."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT session_id, COUNT(*) AS msg_count, MAX(created_at) AS last_at "
            "FROM messages GROUP BY session_id ORDER BY last_at DESC"
        ).fetchall()
        return [
            {"session_id": r[0], "message_count": r[1], "last_activity": r[2]}
            for r in rows
        ]

    def delete_session(self, session_id: str) -> None:
        """Remove all messages and summaries for a session."""
        self.clear_messages(session_id, full_reset=True)

    def export_session(self, session_id: str) -> list[dict[str, str]]:
        """Export a session's messages as a list of dicts."""
        msgs = self.get_messages(session_id)
        return [
            {"role": m.role, "content": m.content, "created_at": m.created_at}
            for m in msgs
        ]
