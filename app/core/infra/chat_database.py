"""First-party SQLite chat database for messages, summaries, and memories."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 17

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
    created_at TEXT NOT NULL,
    arc TEXT,
    dialogue_act TEXT,
    -- Schema v15: K31 soft-physicality. JSON array of gesture kinds
    -- Aiko emitted with this message (e.g. ``["hug"]``). NULL on
    -- messages with no gesture. Survives reload so the chat-bubble
    -- footer badge persists across reconnects / new tabs. The
    -- taxonomy lives in ``app/core/touch/touch_gestures.py``.
    gestures TEXT,
    -- Schema v15: K32 user reactions. JSON object mapping reaction
    -- kind -> count (e.g. ``{"heart": 1, "laugh": 1}``). NULL on
    -- messages with no reactions. The taxonomy lives in
    -- ``app/core/relationship/user_reactions.py``.
    reactions TEXT
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
    revival_score REAL NOT NULL DEFAULT 0.0,
    -- Schema v9: confidence in [0, 1]. Default 0.7 = "MemoryExtractor
    -- wrote this from chat". Self-tags clamp to 0.85, tool-result
    -- writes 0.95, pinned rows clamp to >= 0.9. The F1 background
    -- fact-checker mutates this column up on positive verification and
    -- down on contradiction. RAG demotes low-confidence hits during
    -- retrieval; the prompt assembler tags them "(uncertain)".
    confidence REAL NOT NULL DEFAULT 0.7,
    -- Schema v10: temporal awareness. ``temporal_type`` classifies how
    -- the memory relates to time: ``durable`` (default, timeless fact),
    -- ``preference`` (taste/identity, also timeless), ``ongoing`` (an
    -- active project/state with a soft expiry), ``past_event`` (already
    -- happened — Aiko should ask retrospectively, never as if it's
    -- live), ``future_plan`` (mentioned by the user as upcoming —
    -- ``event_time`` is when it's supposed to happen). ``event_time``
    -- is the ISO-8601 timestamp the *event* refers to (parsed by
    -- MemoryExtractor from the user's words, not the row insert
    -- moment). ``relevance_until`` is when retrieval should stop
    -- surfacing the row in normal RAG (the row stays in DB for archive
    -- / reflection use). All three default to NULL/'durable' so legacy
    -- rows keep their pre-v10 behavior — they render with no time
    -- suffix, exactly like today.
    event_time TEXT,
    temporal_type TEXT NOT NULL DEFAULT 'durable',
    relevance_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_salience ON memories(salience);
-- Note: idx_memories_tier is created in ``_init_schema`` after the
-- v7→v8 ALTER guarantees the ``tier`` column exists. Including it
-- here would crash the executescript on legacy v6/v7 databases
-- where ``tier`` hasn't been added yet. The same applies to the v10
-- ``idx_memories_event_time`` and ``idx_memories_temporal_type``
-- indices, which depend on the v9->v10 ALTERs.

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
-- moments, milestones (see ``app.core.relationship.relationship_axes``). Slowly
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
-- :class:`app.core.world.world_store.WorldStore`. The world is a single shared
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

-- Schema v11: F5 conflicting-memory detector. Each row pins ONE
-- pair of conflicting memories (memory_a_id < memory_b_id, enforced
-- by the worker on insert). ``status`` walks ``open`` ->
-- ``user_resolved`` / ``auto_resolved`` / ``dismissed``. The
-- ``flagged_by`` column distinguishes worker-found pairs (``auto``)
-- from Aiko-flagged ones (``aiko``, via the ``[[conflict:reason]]``
-- self-tag) so the UI can label the surface accordingly. ``winner_id``
-- / ``loser_id`` / ``resolution_action`` are NULL until resolved.
-- The CASCADE-on-delete behavior is implemented in
-- :class:`MemoryConflictStore.delete_for_memory` rather than via SQL
-- foreign keys because ``memories`` rows are managed through
-- ``MemoryStore`` (which keeps an in-memory mirror that would drift
-- if SQL deleted rows behind its back).
CREATE TABLE IF NOT EXISTS memory_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_a_id INTEGER NOT NULL,
    memory_b_id INTEGER NOT NULL,
    similarity REAL NOT NULL,
    confidence_delta REAL NOT NULL,
    heuristic_label TEXT NOT NULL,
    heuristic_signals TEXT,
    llm_verdict TEXT,
    llm_reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    winner_id INTEGER,
    loser_id INTEGER,
    resolution_action TEXT,
    flagged_by TEXT NOT NULL DEFAULT 'auto',
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(memory_a_id, memory_b_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_conflicts_status ON memory_conflicts(status);

-- Schema v12: K2 theory-of-mind / belief tracking. Each row is one
-- belief Aiko holds *about* the user: either a mood prediction
-- ("Jacob is excited about the tokyo trip") or a topical opinion
-- ("Jacob thinks Rust is overhyped"). The ``UNIQUE(user_id, kind,
-- topic)`` constraint dedupes naturally; the :class:`BeliefStore`
-- upsert path also collapses near-duplicate topics by embedding
-- cosine. ``status`` walks ``active`` -> ``confirmed`` /
-- ``contradicted`` / ``stale``. ``valence`` and ``arousal`` are
-- ``NULL`` for opinions; mood beliefs fill them so the gap detector
-- can compare directly against :class:`AffectState`. ``source``
-- distinguishes self-tag, worker, and manual creates. ``metadata``
-- is a generic JSON blob for future fields (e.g. polarity tags).
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    topic_embedding BLOB,
    predicted_state TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.6,
    valence REAL,
    arousal REAL,
    source TEXT NOT NULL DEFAULT 'self_tag',
    source_message_id INTEGER,
    observed_at TEXT NOT NULL,
    last_checked_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    gap_seen_at TEXT,
    metadata TEXT,
    UNIQUE(user_id, kind, topic)
);
CREATE INDEX IF NOT EXISTS idx_beliefs_status ON beliefs(status);
CREATE INDEX IF NOT EXISTS idx_beliefs_topic ON beliefs(topic);
CREATE INDEX IF NOT EXISTS idx_beliefs_user_kind ON beliefs(user_id, kind);

-- K13 stylometric mirror: a single JSON blob per user holding the
-- rolling-window style features (terseness, formality, emoji density,
-- slang density, question rate). Persisted so the analyzer survives
-- restart with the window already populated; the alternative is to
-- always re-warm from past user messages each boot. Schema is
-- intentionally generic so we can extend the JSON shape without a
-- column migration.
CREATE TABLE IF NOT EXISTS user_style_signal (
    user_id TEXT PRIMARY KEY,
    signal_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- K20 metacognitive calibration: per-user JSON blob holding the
-- global calibration scalar plus a bounded ring of topic slots
-- (centroid + score + last_signal_at + signal_count). Persisted so
-- the decay clock advances across restarts. Schema is intentionally
-- generic so the payload shape can extend without a column
-- migration. See app/core/affect/calibration_store.py for the blob shape.
CREATE TABLE IF NOT EXISTS user_calibration_state (
    user_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Schema v16: brain-orchestration tasks. One row per long-running
-- background task initiated by Aiko (or a background system process).
-- The handler owns ``state`` (an opaque JSON blob it reads/writes via
-- the orchestrator on every named transition). The orchestrator owns
-- every other column. Status walks:
--     running -> awaiting_input -> running -> done|failed|cancelled
--     running -> done|failed|cancelled
--     <any non-terminal at app exit> -> interrupted (on next boot, via
--                                       TaskStore.recover_interrupted_on_boot)
-- Visibility flags:
--   ``notify_aiko``      1 = park a cue on Aiko's next turn when the task ends
--                        0 = silent background work (e.g. internal maintenance)
--   ``visible_to_user``  1 = surface in /api/tasks + TaskStrip
--                        0 = orchestrator-only (e.g. system-spawned probes)
-- ``initiated_by`` ('aiko' | 'background' | 'system') distinguishes Aiko's
-- LLM-tool spawns from internal worker spawns and admin/MCP spawns.
-- ``metadata`` is a handler-extensibility JSON blob with a STRICT role:
-- handler-specific config, debugging flags, non-critical hints ONLY.
-- It MUST NOT carry conversational / lifecycle state -- that belongs
-- in ``state`` (hot blob) or ``task_events`` (append-only audit).
-- Schema v17: three new columns + two new sibling tables.
--   ``phase``           free-text per-handler phase label (e.g.
--                       "browsing_results"). Promoted to a column so
--                       every WS / prompt / cue site can read it
--                       without parsing the ``state`` JSON.
--   ``parent_task_id``  optional parent in a task tree. Single column
--                       (not a join table) because real agent
--                       dependencies are tree-shaped -- if multi-parent
--                       ever lands, add a ``task_dependencies`` table.
--                       No SQL FK so cascade-cancel can be explicit
--                       and log-friendly. ``ON DELETE`` semantics live
--                       in the cleanup worker.
--   ``heartbeat_at``    ISO timestamp bumped by the orchestrator on
--                       every emit. The heartbeat sweep flags rows
--                       whose ``heartbeat_at`` is stale (handler alive
--                       in-process but stuck on a syscall / network).
-- Full design lives in ``docs/brain-orchestration.md``.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    handler_name TEXT NOT NULL,
    args TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    title TEXT NOT NULL,
    progress REAL,
    last_message TEXT,
    input_request TEXT,
    result TEXT,
    error TEXT,
    notify_aiko INTEGER NOT NULL DEFAULT 1,
    visible_to_user INTEGER NOT NULL DEFAULT 1,
    initiated_by TEXT NOT NULL DEFAULT 'aiko',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    metadata TEXT,
    phase TEXT,
    parent_task_id INTEGER,
    heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat ON tasks(status, heartbeat_at);

-- Schema v17: append-only per-task event log. One row per emit (and
-- per orchestrator-internal lifecycle moment). The handler treats
-- ``state`` as the hot decision blob (a few hundred bytes); long-form
-- audit ("I visited URL X then URL Y then ...") goes here so it can
-- be paginated + replayed without bloating ``tasks.state``. The
-- orchestrator appends on every emit; handlers can also append custom
-- entries via ``TaskEvent`` outcome. ``type`` is a free-text label
-- (see ``app/core/tasks/task_events.py`` for the stable constants);
-- ``data`` is an opaque JSON blob owned by the producer.
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);

-- Schema v17: dedicated input/answer history. One row per question
-- the handler asked. The legacy ``tasks.input_request`` column stays
-- as a denormalised view of the latest pending row for backward
-- compat; the new table is the source of truth. Status walks:
--     pending -> answered      (user supplied an answer)
--     pending -> superseded    (another question was asked first,
--                               or the task was cancelled)
--     pending -> cancelled     (handler-initiated cancel of the
--                               question)
-- ``kind`` is a hint for the UI ("choice" / "free_text" / "confirm"),
-- ``options`` is a jsonable list when ``kind="choice"``. Both are
-- nullable. ``response`` carries the raw user text (or the resolved
-- option label). Indexed on ``(task_id, status)`` so
-- ``latest_pending`` is an index-only lookup.
CREATE TABLE IF NOT EXISTS task_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    kind TEXT,
    options TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    response TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_inputs_task_status ON task_inputs(task_id, status);
"""

# Tables that existed in earlier schemas but are no longer used.
_OBSOLETE_TABLES = ("personality_notes", "recent_topics", "message_embeddings")

# Indices over ``memories`` columns that were added by post-v3
# migrations. They cannot live in ``_CREATE_TABLES`` because the
# corresponding columns may not exist yet on legacy databases (the
# executescript runs CREATE INDEX before the v?->vN ALTERs do their
# work). ``_init_schema`` runs each statement in a try/except so the
# fresh-DB / already-current / upgraded-DB paths can all share the
# same definition list.
_DEPENDENT_MEMORY_INDICES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)",
    "CREATE INDEX IF NOT EXISTS idx_memories_event_time ON memories(event_time)",
    "CREATE INDEX IF NOT EXISTS idx_memories_temporal_type ON memories(temporal_type)",
)


@dataclass(slots=True)
class MessageRow:
    id: int
    session_id: str
    role: str
    content: str
    token_count: int
    created_at: str
    arc: str | None = None
    dialogue_act: str | None = None
    # Schema v15: K31 + K32 soft-physicality.
    # ``gestures`` is a JSON array of touch kinds Aiko emitted on
    # this message (e.g. ``["hug"]``). ``reactions`` is a JSON
    # object of user-reaction counts (e.g. ``{"heart": 1}``).
    # Both NULL when nothing applies. See
    # :func:`app.core.touch.touch_gestures.TouchService.try_dispatch`
    # and :func:`app.core.relationship.user_reactions.apply_daily_cap`
    # for the producer paths.
    gestures: str | None = None
    reactions: str | None = None


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
        # :class:`app.core.rag.message_indexer.MessageIndexer` to embed and write
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
            # includes the ``tier`` and v10 ``event_time`` /
            # ``temporal_type`` columns, so all dependent indices can
            # be created here.
            for stmt in _DEPENDENT_MEMORY_INDICES:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            conn.commit()
            return
        if row[0] >= _SCHEMA_VERSION:
            # Already on current schema -- make sure the dependent
            # indices exist in case a prior partial migration skipped
            # any of them.
            for stmt in _DEPENDENT_MEMORY_INDICES:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
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
        # v8 -> v9: ``memories.confidence`` column for the F3 confidence
        # tier work. Existing rows default to ``0.7`` (the
        # MemoryExtractor baseline). Pinned rows backfill to ``0.9`` so
        # the user-curated anchor never sits below the pin clamp.
        try:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 0.7"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "UPDATE memories SET confidence = 0.9 "
                "WHERE pinned = 1 AND confidence < 0.9"
            )
        except sqlite3.OperationalError:
            pass
        # v9 -> v10: temporal awareness columns. ``temporal_type``
        # backfills to ``'durable'`` so legacy rows render with no time
        # suffix in retrieval -- exactly the pre-v10 behavior. The
        # ``event_time`` and ``relevance_until`` columns stay NULL for
        # legacy rows; only memories the new MemoryExtractor flow
        # writes will populate them. The dependent indices are created
        # via ``_DEPENDENT_MEMORY_INDICES`` below.
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN event_time TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN temporal_type TEXT NOT NULL DEFAULT 'durable'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN relevance_until TEXT")
        except sqlite3.OperationalError:
            pass
        for stmt in _DEPENDENT_MEMORY_INDICES:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # v10 -> v11: F5 conflicting-memory detector. The
        # ``memory_conflicts`` table CREATE above is idempotent, so on
        # upgrade there's nothing to ALTER -- the executescript already
        # added it. We do create the status index defensively here in
        # case ``CREATE INDEX IF NOT EXISTS`` ran before the table
        # existed (it can't on this codepath, but the pattern
        # matches the v7->v8 tier-index handling).
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_conflicts_status "
                "ON memory_conflicts(status)"
            )
        except sqlite3.OperationalError:
            pass
        # v11 -> v12: K2 belief tracking. The ``beliefs`` table CREATE
        # above is idempotent. Defensive index creates mirror the v11
        # pattern so legacy databases that pre-date the CREATE INDEX
        # statements pick them up on upgrade.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_beliefs_status ON beliefs(status)",
            "CREATE INDEX IF NOT EXISTS idx_beliefs_topic ON beliefs(topic)",
            "CREATE INDEX IF NOT EXISTS idx_beliefs_user_kind ON beliefs(user_id, kind)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # v12 -> v13: H1 + K4 conversational signals. ``messages.arc`` stores
        # Aiko's read of the conversation phase (one of six values from
        # ``conversation_arc.VALID_ARCS``); ``messages.dialogue_act`` stores
        # the user's per-turn intent (question / story / vent / banter /
        # planning / chitchat). Both columns default NULL so historical rows
        # stay clean -- only turns tagged after rollout populate them. No
        # index: filter-by-equality on a small per-session table; the
        # existing ``idx_messages_session`` already narrows the scan.
        for stmt in (
            "ALTER TABLE messages ADD COLUMN arc TEXT",
            "ALTER TABLE messages ADD COLUMN dialogue_act TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # v13 -> v14: K20 metacognitive calibration.
        # ``user_calibration_state`` holds a per-user JSON blob with the
        # global calibration scalar and a bounded ring of topic slots
        # (centroid + score + last_signal_at + signal_count). The
        # ``CREATE TABLE IF NOT EXISTS`` block above already creates the
        # table on upgrade -- there's nothing to ALTER. The migration
        # entry exists for the audit trail.
        # v14 -> v15: K31 + K32 soft physicality.
        # ``messages.gestures`` stores a JSON array of touch kinds Aiko
        # emitted on this message (e.g. ``["hug"]``); ``messages.reactions``
        # stores a JSON object of user-reaction counts (e.g. ``{"heart":
        # 1}``). Both default NULL so historical rows stay clean -- only
        # turns tagged after rollout populate them. The taxonomies live in
        # ``app/core/touch/touch_gestures.py`` and
        # ``app/core/relationship/user_reactions.py``. No index: both
        # columns are accessed by message_id PK only.
        for stmt in (
            "ALTER TABLE messages ADD COLUMN gestures TEXT",
            "ALTER TABLE messages ADD COLUMN reactions TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # v15 -> v16: brain-orchestration ``tasks`` table. The CREATE
        # TABLE above is idempotent, so on upgrade there's nothing to
        # ALTER -- the executescript already added the table + indices.
        # The migration entry exists for the audit trail. New
        # ``status`` values surface immediately because the column is
        # plain TEXT (validation lives in Python, not SQLite CHECK).
        # Defensive index creates mirror the v11 pattern so legacy
        # databases that pre-date the CREATE INDEX statements pick
        # them up on upgrade.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_tasks_user_status "
            "ON tasks(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # v16 -> v17: brain-orchestration phase 2. Three new ``tasks``
        # columns + two new sibling tables (``task_events``,
        # ``task_inputs``) + two new indices on ``tasks``. The CREATE
        # TABLE / CREATE INDEX statements above are idempotent so on a
        # fresh DB they land in one pass; on upgrade the ALTERs below
        # add the columns to the existing ``tasks`` table. Each ALTER
        # is wrapped in try/except OperationalError so a legacy DB
        # that's already been partially upgraded (e.g. one column
        # added by a hand-edit) doesn't trip the boot path.
        for stmt in (
            "ALTER TABLE tasks ADD COLUMN phase TEXT",
            "ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER",
            "ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_tasks_parent "
            "ON tasks(parent_task_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat "
            "ON tasks(status, heartbeat_at)",
            "CREATE INDEX IF NOT EXISTS idx_task_events_task "
            "ON task_events(task_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_task_inputs_task_status "
            "ON task_inputs(task_id, status)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
        conn.commit()

    # ── Phase-2/3/4 helper hooks (used by AffectStore et al.) ────────

    def _ensure_affect_state_schema(self) -> None:
        """No-op: the table is already created in ``_init_schema``.

        Exists so :class:`app.core.affect.affect_state.AffectStore` can call it
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
            from app.core.session.session_text_utils import sanitize_assistant_text
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
        *,
        arc: str | None = None,
        dialogue_act: str | None = None,
    ) -> int:
        conn = self._get_conn()
        created_at = _now_iso()
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, token_count, created_at, arc, dialogue_act) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, token_count, created_at, arc, dialogue_act),
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
                arc=arc,
                dialogue_act=dialogue_act,
            )
            for listener in list(self._add_listeners):
                try:
                    listener(row)
                except Exception:
                    # Listeners must not break the write path.
                    pass
        return msg_id

    def update_message_gestures(
        self, message_id: int, gestures_json: str | None,
    ) -> bool:
        """Set the K31 ``gestures`` JSON column for a message row.

        ``gestures_json`` may be ``None`` to clear the column.
        Returns True if a row was updated, False if the id is missing
        or non-positive.
        """
        if message_id <= 0:
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE messages SET gestures = ? WHERE id = ?",
            (gestures_json, int(message_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def update_message_reactions(
        self, message_id: int, reactions_json: str | None,
    ) -> bool:
        """Set the K32 ``reactions`` JSON column for a message row.

        ``reactions_json`` may be ``None`` to clear the column.
        Returns True if a row was updated, False otherwise.
        """
        if message_id <= 0:
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE messages SET reactions = ? WHERE id = ?",
            (reactions_json, int(message_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def get_message_row(self, message_id: int) -> MessageRow | None:
        """Return one full :class:`MessageRow` by id, or ``None``."""
        if message_id <= 0:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, session_id, role, content, token_count, "
            "       created_at, arc, dialogue_act, gestures, reactions "
            "FROM messages WHERE id = ?",
            (int(message_id),),
        ).fetchone()
        if row is None:
            return None
        return MessageRow(*row)

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

    def update_message_arc(self, message_id: int, arc: str | None) -> bool:
        """Set the conversation-arc tag for a message row.

        Used by the H1 self-tag pipeline (Aiko's ``[[arc:X]]`` emission)
        and the opportunistic per-turn arc stamping. ``arc`` may be
        ``None`` to clear the column.
        """
        if message_id <= 0:
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE messages SET arc = ? WHERE id = ?",
            (arc if arc is None else str(arc), int(message_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def get_message_signals(
        self, message_ids: Iterable[int]
    ) -> dict[int, tuple[str | None, str | None]]:
        """Return ``{id: (arc, dialogue_act)}`` for the given message ids.

        Used by the RAG retriever to apply the H1 + K4 alignment boost
        (cap ``+0.05``) without an extra row-by-row SQL roundtrip. Ids
        that don't resolve are simply absent from the returned dict;
        callers fall back to ``(None, None)`` per missing key.
        """
        ids = [int(mid) for mid in message_ids if mid is not None and int(mid) > 0]
        if not ids:
            return {}
        unique_ids = list(dict.fromkeys(ids))
        placeholders = ",".join("?" for _ in unique_ids)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"SELECT id, arc, dialogue_act FROM messages WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {int(r[0]): (r[1], r[2]) for r in rows}

    def update_message_dialogue_act(
        self, message_id: int, dialogue_act: str | None
    ) -> bool:
        """Set the user-side dialogue-act tag for a message row.

        Used by the K4 ``DialogueActTagger`` -- regex hot path writes the
        column inline; the LLM cold-path upgrade calls this again to
        replace the value if it disagrees with the regex.
        """
        if message_id <= 0:
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE messages SET dialogue_act = ? WHERE id = ?",
            (dialogue_act if dialogue_act is None else str(dialogue_act), int(message_id)),
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
        select = (
            "SELECT id, session_id, role, content, token_count, created_at, "
            "       arc, dialogue_act, gestures, reactions "
            "FROM messages WHERE session_id = ?"
        )
        if offset and limit:
            rows = conn.execute(
                f"{select} ORDER BY id LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        elif offset:
            rows = conn.execute(
                f"{select} ORDER BY id LIMIT -1 OFFSET ?",
                (session_id, offset),
            ).fetchall()
        elif limit:
            rows = conn.execute(
                f"{select} ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(
                f"{select} ORDER BY id",
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
