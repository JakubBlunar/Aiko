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

_SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS personality_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_personality_session ON personality_notes(session_id, confidence);

CREATE TABLE IF NOT EXISTS recent_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_session ON recent_topics(session_id, used_at);
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


@dataclass(slots=True)
class PersonalityNoteRow:
    id: int
    session_id: str
    category: str
    note: str
    confidence: float
    created_at: str
    updated_at: str


@dataclass(slots=True)
class RecentTopicRow:
    id: int
    session_id: str
    topic: str
    used_at: str


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
        elif row[0] < _SCHEMA_VERSION:
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
        """Delete all messages (and their embeddings) for a session. Returns deleted count.

        When *full_reset* is True, also clears personality notes and recent topics
        so the assistant has no residual memory of the session.
        """
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM message_embeddings WHERE session_id = ?", (session_id,)
        )
        cursor = conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        conn.execute(
            "DELETE FROM session_summaries WHERE session_id = ?", (session_id,)
        )
        if full_reset:
            conn.execute(
                "DELETE FROM personality_notes WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "DELETE FROM recent_topics WHERE session_id = ?", (session_id,)
            )
        conn.commit()
        return cursor.rowcount

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

    def get_all_embeddings(
        self,
        session_id: str | None = None,
        *,
        max_rows: int | None = None,
    ) -> list[EmbeddingRow]:
        """Return embedding rows, optionally scoped to a session and capped.

        When *max_rows* is set, only the most recent *max_rows* embeddings are
        returned (by message_id descending), keeping search cost bounded.
        """
        conn = self._get_conn()
        limit_clause = f" LIMIT {int(max_rows)}" if max_rows and max_rows > 0 else ""
        if session_id:
            rows = conn.execute(
                "SELECT me.message_id, me.session_id, me.embedding, m.content, m.role, me.created_at "
                "FROM message_embeddings me JOIN messages m ON me.message_id = m.id "
                f"WHERE me.session_id = ? ORDER BY me.message_id DESC{limit_clause}",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT me.message_id, me.session_id, me.embedding, m.content, m.role, me.created_at "
                "FROM message_embeddings me JOIN messages m ON me.message_id = m.id "
                f"ORDER BY me.message_id DESC{limit_clause}",
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

    # ── Personality Notes ──

    def get_personality_notes(
        self,
        session_id: str,
        *,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list[PersonalityNoteRow]:
        conn = self._get_conn()
        query = (
            "SELECT id, session_id, category, note, confidence, created_at, updated_at "
            "FROM personality_notes WHERE session_id = ? AND confidence >= ? "
            "ORDER BY confidence DESC, updated_at DESC"
        )
        params: list[Any] = [session_id, min_confidence]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [PersonalityNoteRow(*r) for r in rows]

    def upsert_personality_note(
        self,
        session_id: str,
        category: str,
        note: str,
        confidence: float,
    ) -> int:
        """Insert or update a personality note. Matches on session_id + note text similarity."""
        conn = self._get_conn()
        now = _now_iso()
        existing = conn.execute(
            "SELECT id FROM personality_notes WHERE session_id = ? AND category = ? AND note = ?",
            (session_id, category, note),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE personality_notes SET confidence = ?, updated_at = ? WHERE id = ?",
                (confidence, now, existing[0]),
            )
            conn.commit()
            return existing[0]
        cursor = conn.execute(
            "INSERT INTO personality_notes (session_id, category, note, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, category, note, confidence, now, now),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def replace_personality_notes(
        self,
        session_id: str,
        notes: list[tuple[str, str, float]],
    ) -> None:
        """Replace all personality notes for a session with a new set.

        Each tuple is (category, note, confidence).
        """
        conn = self._get_conn()
        now = _now_iso()
        conn.execute("DELETE FROM personality_notes WHERE session_id = ?", (session_id,))
        for category, note, confidence in notes:
            conn.execute(
                "INSERT INTO personality_notes (session_id, category, note, confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, category, note, confidence, now, now),
            )
        conn.commit()

    def decay_personality_notes(
        self,
        session_id: str,
        decay_rate: float,
        prune_threshold: float,
    ) -> int:
        """Decay all notes by decay_rate, prune those below threshold. Returns pruned count."""
        conn = self._get_conn()
        now = _now_iso()
        conn.execute(
            "UPDATE personality_notes SET confidence = confidence - ?, updated_at = ? "
            "WHERE session_id = ?",
            (decay_rate, now, session_id),
        )
        cursor = conn.execute(
            "DELETE FROM personality_notes WHERE session_id = ? AND confidence < ?",
            (session_id, prune_threshold),
        )
        conn.commit()
        return cursor.rowcount

    def cap_personality_notes(self, session_id: str, max_notes: int) -> int:
        """Keep only the top max_notes by confidence. Returns deleted count."""
        conn = self._get_conn()
        excess_ids = conn.execute(
            "SELECT id FROM personality_notes WHERE session_id = ? "
            "ORDER BY confidence DESC, updated_at DESC LIMIT -1 OFFSET ?",
            (session_id, max_notes),
        ).fetchall()
        if not excess_ids:
            return 0
        id_list = [r[0] for r in excess_ids]
        placeholders = ",".join("?" * len(id_list))
        cursor = conn.execute(
            f"DELETE FROM personality_notes WHERE id IN ({placeholders})", id_list
        )
        conn.commit()
        return cursor.rowcount

    # ── Recent Topics ──

    def get_recent_topics(self, session_id: str, limit: int = 20) -> list[RecentTopicRow]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, session_id, topic, used_at FROM recent_topics "
            "WHERE session_id = ? ORDER BY used_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [RecentTopicRow(*r) for r in rows]

    def add_recent_topic(self, session_id: str, topic: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO recent_topics (session_id, topic, used_at) VALUES (?, ?, ?)",
            (session_id, topic, _now_iso()),
        )
        # Keep only last 20 per session
        conn.execute(
            "DELETE FROM recent_topics WHERE session_id = ? AND id NOT IN "
            "(SELECT id FROM recent_topics WHERE session_id = ? ORDER BY used_at DESC LIMIT 20)",
            (session_id, session_id),
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
        """Remove all data for a session (messages, embeddings, summaries, notes, topics)."""
        self.clear_messages(session_id, full_reset=True)

    def export_session(self, session_id: str) -> list[dict[str, str]]:
        """Export a session's messages as a list of dicts."""
        msgs = self.get_messages(session_id)
        return [
            {"role": m.role, "content": m.content, "created_at": m.created_at}
            for m in msgs
        ]
