"""First-party SQLite chat database for messages, summaries, and memories."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 8

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
    use_count INTEGER NOT NULL DEFAULT 0,
    -- Schema v5: pinned rows are never decayed or pruned. User-curated
    -- "always keep this" flag wired through MemoryStore.set_pinned(). Old
    -- databases get this column added via the v4->v5 ALTER in
    -- ``_init_schema`` below; new databases get it from this CREATE.
    pinned INTEGER NOT NULL DEFAULT 0,
    -- Schema v7: optional JSON metadata blob. Used today by the
    -- ``shared_moment`` kind to carry ``{when, what, vibe,
    -- participants, source_message_ids, last_anniversaried_at}``, but
    -- intentionally generic so future structured kinds can ride the
    -- same column. NULL on existing kinds. v6 databases get the column
    -- added via ALTER in ``_init_schema``.
    metadata TEXT,
    -- Schema v8: memory tiers. ``scratchpad`` rows decay fast and get
    -- pruned/promoted by ``MemoryPromotionWorker``; ``long_term`` is the
    -- default home for verified anchors; ``archive`` decays at zero so
    -- cold history sticks around without crowding retrieval. Pinned
    -- rows are always forced to ``long_term``. v7 databases get the
    -- column added via ALTER in ``_init_schema``.
    tier TEXT NOT NULL DEFAULT 'long_term',
    -- Schema v8: revival_score in [0, 1] tracks how often a retrieval
    -- was followed by Aiko actually citing the memory (keyword overlap
    -- >= memory_revival_min_word_overlap). The decay pass applies a
    -- small rebate proportional to revival_score so high-revival rows
    -- drift toward salience=1.0 and act like soft pins.
    revival_score REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_salience ON memories(salience);
-- Note: idx_memories_tier is created in ``_init_schema`` after the
-- v7→v8 ALTER guarantees the ``tier`` column exists. Including it
-- here would crash the executescript on legacy v6/v7 databases
-- where ``tier`` hasn't been added yet.

-- Phase 2b: persistent emotional state per user.
CREATE TABLE IF NOT EXISTS affect_state (
    user_id TEXT PRIMARY KEY,
    valence REAL NOT NULL DEFAULT 0.0,
    arousal REAL NOT NULL DEFAULT 0.4,
    baseline_valence REAL NOT NULL DEFAULT 0.0,
    baseline_arousal REAL NOT NULL DEFAULT 0.4,
    mood_label TEXT NOT NULL DEFAULT 'content',
    mood_intensity REAL NOT NULL DEFAULT 0.5,
    valence_trend_24h REAL NOT NULL DEFAULT 0.0,
    arousal_trend_24h REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);

-- Phase 3a: structured user profile (one row per (user_id, field)).
CREATE TABLE IF NOT EXISTS user_profile (
    user_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, field)
);

-- Phase 3a: per-turn user state (single row per user, overwritten each turn).
CREATE TABLE IF NOT EXISTS user_state_now (
    user_id TEXT PRIMARY KEY,
    perceived_mood TEXT NOT NULL DEFAULT 'unknown',
    perceived_energy TEXT NOT NULL DEFAULT 'unknown',
    perceived_focus TEXT NOT NULL DEFAULT 'unknown',
    last_topic TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Phase 3b: relationship phase tracking (one row per user).
CREATE TABLE IF NOT EXISTS user_relationship (
    user_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    total_turns INTEGER NOT NULL DEFAULT 0,
    total_sessions INTEGER NOT NULL DEFAULT 0,
    last_milestone_at TEXT,
    milestone_label TEXT
);

-- Schema v7: relationship "axes" — four floats in [-1, 1] tracking
-- closeness, humor, trust, comfort. Drift per-turn from reactions,
-- moments, milestones (see ``app.core.relationship_axes``). Slowly
-- decay toward 0 over a ~30-day half-life when there's no signal.
CREATE TABLE IF NOT EXISTS relationship_axes (
    user_id TEXT PRIMARY KEY,
    closeness REAL NOT NULL DEFAULT 0.0,
    humor REAL NOT NULL DEFAULT 0.0,
    trust REAL NOT NULL DEFAULT 0.0,
    comfort REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);

-- Phase 4a: agenda items extracted from [[agenda:...]] tags or auto-promoted.
CREATE TABLE IF NOT EXISTS agenda (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    source_session TEXT,
    created_at TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    importance REAL NOT NULL DEFAULT 0.5,
    last_groomed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agenda_user_status ON agenda(user_id, status);

-- Phase 4c: rolling conversation arc label (single row per user).
CREATE TABLE IF NOT EXISTS conversation_arc (
    user_id TEXT PRIMARY KEY,
    arc TEXT NOT NULL DEFAULT 'casual_check_in',
    since_turn INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL
);

-- Phase 4c: prepared nudges (single row per user; ProactiveDirector consumes).
CREATE TABLE IF NOT EXISTS prepared_nudge (
    user_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'mixed',
    source_id TEXT,
    prepared_at TEXT NOT NULL,
    ttl_seconds REAL NOT NULL DEFAULT 600.0
);

-- Phase 4b: consolidator resume state (one row per user).
CREATE TABLE IF NOT EXISTS consolidator_state (
    user_id TEXT PRIMARY KEY,
    last_cluster_index INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT
);

-- Schema v6: Aiko's virtual room. Three small tables managed by
-- :class:`app.core.world_store.WorldStore`. The world is a single shared
-- model (no per-user partitioning) since there's one Aiko per assistant.
-- ``world_state`` is a singleton row (id == 1) holding her current
-- location/posture/activity. ``world_items`` rows with ``location_id IS
-- NULL`` represent items Aiko is carrying.
CREATE TABLE IF NOT EXISTS world_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS world_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other',
    consumable INTEGER NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL DEFAULT 1,
    location_id INTEGER REFERENCES world_locations(id) ON DELETE SET NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    given_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_world_items_location ON world_items(location_id);
CREATE INDEX IF NOT EXISTS idx_world_items_slug ON world_items(slug);

CREATE TABLE IF NOT EXISTS world_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    location_id INTEGER REFERENCES world_locations(id) ON DELETE SET NULL,
    posture TEXT NOT NULL DEFAULT 'sitting',
    activity TEXT NOT NULL DEFAULT 'idle',
    mood_note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Schema v8: tiny key/value store for cross-process bookkeeping that
-- doesn't deserve its own table. Used by ``MemoryStore.decay()`` to
-- persist ``last_decay_run_at`` (so wall-clock catch-up works across
-- restarts) and by the ``IdleWorkerScheduler`` to remember each
-- worker's last_run_at + last_error.
CREATE TABLE IF NOT EXISTS kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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

    # ── small generic helpers for inner-life modules (Phase 3+) ──────────
    # These let lightweight stores (UserProfileStore, UserStateStore, ...)
    # avoid duplicating connection bookkeeping. Heavy paths still go
    # through dedicated methods on ChatDatabase.

    def execute_fetchall(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [tuple(r) for r in rows]

    def execute_fetchone(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> tuple[Any, ...] | None:
        conn = self._get_conn()
        row = conn.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None

    def execute_commit(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.lastrowid or 0)

    # ── kv_meta helpers (schema v8) ─────────────────────────────────────
    # Tiny string-value store for cross-process bookkeeping. Reserved
    # key namespaces:
    #   ``memory.last_decay_run_at`` -- wall-clock anchor for
    #       :meth:`MemoryStore.decay`.
    #   ``idle_worker.<name>.last_run_at`` / ``.last_error`` /
    #       ``.run_count`` -- :class:`IdleWorkerScheduler` records.
    # Values are always strings (callers JSON-encode when needed).

    def kv_get(self, key: str) -> str | None:
        row = self.execute_fetchone(
            "SELECT value FROM kv_meta WHERE key = ?", (str(key),),
        )
        return str(row[0]) if row is not None else None

    def kv_set(self, key: str, value: str) -> None:
        self.execute_commit(
            "INSERT INTO kv_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (str(key), str(value), _now_iso()),
        )

    def kv_delete(self, key: str) -> None:
        self.execute_commit("DELETE FROM kv_meta WHERE key = ?", (str(key),))

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_CREATE_TABLES)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            # Fresh database: CREATE TABLE memories above already
            # includes the ``tier`` column, so the dependent index can
            # be created here.
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            conn.commit()
            return
        if row[0] >= _SCHEMA_VERSION:
            # Already on current schema -- make sure the tier index
            # exists in case a prior partial migration skipped it.
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)"
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass
            return
        # On upgrade: drop tables that are no longer used so they stop wasting
        # space and confusing later readers. ``messages`` and
        # ``session_summaries`` are kept intact. The new Phase-2/3/4 tables
        # are created above by ``executescript`` (CREATE IF NOT EXISTS), so
        # an existing v3 database picks them up automatically here.
        for table in _OBSOLETE_TABLES:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                pass
        # v4 -> v5: add ``memories.pinned`` column to existing databases.
        # New tables created above by ``CREATE TABLE IF NOT EXISTS`` already
        # include the column, so this only touches upgraded databases. SQLite
        # raises ``OperationalError`` when the column already exists -- we
        # swallow that so re-running the migration is a no-op.
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # v5 -> v6: world tables (``world_locations``, ``world_items``,
        # ``world_state``). The ``CREATE TABLE IF NOT EXISTS`` block above
        # already creates them on upgrade — there's nothing to ALTER, but
        # we log the bump explicitly for the migration trail.
        # v6 -> v7: add ``memories.metadata`` JSON column (used by the
        # ``shared_moment`` kind, but generic) and the new
        # ``relationship_axes`` table. The table CREATE above is idempotent;
        # only the ALTER needs guarding so re-runs are a no-op.
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN metadata TEXT")
        except sqlite3.OperationalError:
            pass
        # v7 -> v8: memory tiers + revival score. Existing rows default
        # to ``tier='long_term'`` (the safe baseline -- doesn't change
        # decay behavior vs v7) and ``revival_score=0.0``. The
        # ``kv_meta`` table CREATE above is idempotent. The tier index
        # is added explicitly here since ``CREATE INDEX IF NOT EXISTS``
        # in ``executescript`` runs before the column exists on
        # upgraded databases.
        try:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN tier TEXT NOT NULL DEFAULT 'long_term'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN revival_score REAL NOT NULL DEFAULT 0.0"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)"
            )
        except sqlite3.OperationalError:
            pass
        conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
        conn.commit()

    # ── Phase-2/3/4 helper hooks (used by AffectStore et al.) ────────

    def _ensure_affect_state_schema(self) -> None:
        """No-op: the table is already created in ``_init_schema``.

        Exists so :class:`app.core.affect_state.AffectStore` can call it
        defensively (the symbol is reserved for future field migrations
        without breaking the AffectStore API).
        """
        return None

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

    def update_message_content(
        self,
        message_id: int,
        content: str,
        *,
        token_count: int | None = None,
    ) -> bool:
        """Replace the ``content`` (and optionally ``token_count``) of an
        existing message row.

        Used by the voice-merge flow in ``SessionController``: when phrase B
        is detected as a continuation of phrase A while the in-flight LLM
        turn hasn't reached TTS yet, we merge the texts into the existing
        ``role="user"`` row instead of inserting a second one. Persisting
        the row update before the merged turn re-runs keeps the chat
        history a single coherent user message.

        Returns True if a row was updated, False if no row matched the id.
        """
        if message_id <= 0:
            return False
        from app.llm.token_utils import estimate_tokens

        cleaned = str(content or "")
        conn = self._get_conn()
        if token_count is None:
            cursor = conn.execute(
                "UPDATE messages SET content = ?, token_count = ? WHERE id = ?",
                (cleaned, estimate_tokens(cleaned), int(message_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE messages SET content = ?, token_count = ? WHERE id = ?",
                (cleaned, int(token_count), int(message_id)),
            )
        conn.commit()
        return bool(cursor.rowcount)

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
