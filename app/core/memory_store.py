"""SQLite-backed long-term memory store with cosine search.

One row per durable fact about the user. Embeddings live alongside the row
as a packed float32 BLOB. The store keeps an in-memory mirror of every row
so cosine search runs in pure NumPy without a per-query SQL roundtrip.

Capacity is bounded (``max_memories``, default 500); ``prune()`` evicts the
oldest least-used / lowest-salience rows once the cap is hit. Cross-session
by design: there's exactly one memory store for the assistant.

Phase C also mirrors every write into a :class:`RagStore` (LanceDB-backed)
when one is attached, so that the new RagRetriever has a single read path.
The SQLite store remains the source of truth for now; if the RagStore
disappears (e.g., embedding-dim swap rebuilds the table), the next search
will simply hit the SQLite path until the RagStore catches up via a fresh
migration.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np

from app.llm.embedder import cosine_similarity

if TYPE_CHECKING:
    from app.core.rag_store import RagStore


log = logging.getLogger("app.memory_store")


# Schema v8 — memory tiers. ``scratchpad`` is the fast-decay
# probationary lane (new auto-extracted observations land here);
# ``long_term`` is the default home for verified anchors; ``archive``
# decays at zero so cold history sticks around without crowding
# retrieval. Pinned rows are always coerced to ``long_term``.
VALID_TIERS = ("scratchpad", "long_term", "archive")
_DEFAULT_TIER = "long_term"


# Schema v10 — temporal type. ``durable`` (default, timeless fact) and
# ``preference`` (taste/identity, also timeless) render with no time
# suffix in retrieval. ``ongoing`` is an active project/state with a
# soft expiry (``relevance_until``). ``past_event`` is a historical
# moment Aiko should reference retrospectively, never as if it just
# happened. ``future_plan`` is something the user mentioned as
# upcoming; ``event_time`` carries the ISO-8601 moment it's supposed
# to take place. The ``MemoryDecayWorker`` flips ``future_plan`` rows
# to ``past_event`` once their ``event_time`` passes; the
# ``FollowUpWorker`` schedules a one-shot nudge near ``event_time``
# so Aiko can ask retrospectively when the moment fits.
VALID_TEMPORAL_TYPES = (
    "durable",
    "preference",
    "ongoing",
    "past_event",
    "future_plan",
)
_DEFAULT_TEMPORAL_TYPE = "durable"


def _coerce_temporal_type(value: str | None) -> str:
    """Normalize and validate a temporal_type string.

    Falls back to ``'durable'`` (the safe baseline) for unknown or
    missing values so legacy callers and bad LLM output don't crash
    inserts. Raises only on completely non-string input.
    """
    if value is None:
        return _DEFAULT_TEMPORAL_TYPE
    if not isinstance(value, str):
        raise TypeError(f"temporal_type must be a string, got {type(value).__name__}")
    cleaned = value.strip().lower()
    if cleaned in VALID_TEMPORAL_TYPES:
        return cleaned
    return _DEFAULT_TEMPORAL_TYPE


VALID_KINDS = {
    "fact",
    "preference",
    "event",
    "relationship",
    "self_tagged",
    "self",
    # Phase 2c — produced by ReflectionWorker (LLM journal during the
    # speaking window). open_question = something Aiko wonders about and
    # might surface later. callback = a thread she'd like to pick back up.
    "open_question",
    "callback",
    "reflection",
    # Phase 3c — explicit promises ("I'll do X", "remind me to Y").
    # Surfaced through RAG and consumed by ProactiveDirector.
    "promise",
    # "Aiko human-like upgrades" Phase 2c — recurring 3-7-word phrases
    # spoken by both Jacob and Aiko, mined offline by
    # :class:`CatchphraseMiner`. Surfaced through a dedicated
    # "Aiko's running jokes with Jacob:" inner-life block in the prompt
    # assembler (cap of 3 entries).
    "catchphrase",
    # Schema v7 — episodic "shared moment" between Jacob and Aiko. Carries
    # structured ``(when, what, vibe, participants, source_message_ids)``
    # in the ``metadata`` JSON column. Surfaced as anniversaries by
    # :func:`SessionController._render_anniversary_block` and shown on the
    # "Together" UI tab. Written by inline ``[[moment:vibe:text]]`` tags,
    # by the speaking-window LLM detector, or by an explicit user click.
    "shared_moment",
    # F2 personality backlog — explicit "I'm not sure / I don't know"
    # journal entry. Written by ``KnowledgeGapStore`` from inline
    # ``[[gap:topic:question]]`` tags Aiko emits in raw output. Carries
    # ``{topic, question, resolved_at, resolved_by_memory_id,
    # source_turn_id}`` in the ``metadata`` JSON column. F1's idle
    # fact-checker can resolve gaps by stamping ``resolved_at`` and
    # writing the answer as a sibling memory. Confidence defaults to
    # ``0.0`` (the row is a question, not a fact).
    "knowledge_gap",
    # G3 personality backlog — answer Aiko discovered on her own by
    # web-searching an existing ``open_question`` memory during idle
    # downtime. Written by
    # :class:`app.core.idle_curiosity_worker.IdleCuriosityWorker`.
    # Carries ``{source_open_question_id, source_query, discovered_at}``
    # in the ``metadata`` JSON column. The persona file tells Aiko to
    # surface these as "I was reading about X — turns out..." rather
    # than recite them as bare facts.
    "curiosity_finding",
    # K9 personality backlog — broad topic Aiko is quietly curious
    # about that hasn't come up with the user yet. Written by
    # :class:`app.core.curiosity_seed_worker.CuriositySeedWorker`
    # during idle windows: an LLM proposes 3-5 candidate topics
    # anchored on persona traits + recent rolling summary, the
    # :class:`app.core.topic_graph.TopicGraph` rejects candidates
    # that fall too close to existing memories ("we've already
    # discussed that"), and the survivors land here. Carries
    # ``{topic, prompt_text, source: 'llm'|'graph_gap',
    # generated_at, consumed_at?, candidate_score}`` in the
    # ``metadata`` JSON column. Surfaced as a Quiet-curiosity
    # inner-life bullet during normal turns AND as a
    # NarrativeWeaver candidate for typed proactive nudges. Auto-
    # resolves (``consumed_at`` stamped, tier demoted to archive)
    # once the conversation cosine-matches the seed.
    "curiosity_seed",
}


@dataclass(slots=True)
class Memory:
    id: int
    content: str
    kind: str
    salience: float
    embedding: np.ndarray
    source_session: str | None
    source_message_id: int | None
    created_at: str
    last_used_at: str | None
    use_count: int
    # Pinned rows are user-curated as "always keep". They are skipped by
    # ``decay()`` and never selected as victims by ``prune()``. Pinning a
    # row also nudges ``salience`` to ``1.0`` so an un-pin doesn't snap to a
    # stale low value (see :meth:`MemoryStore.set_pinned`). The flag lives
    # in SQLite only -- the LanceDB mirror is intentionally not aware of
    # it; the retriever applies a small score bonus by joining against the
    # in-memory mirror at query time.
    pinned: bool = False
    # Schema v7 — optional JSON metadata bag. Used today by ``shared_moment``
    # rows to carry ``{when, what, vibe, participants, source_message_ids,
    # last_anniversaried_at}``, but intentionally generic so future
    # structured kinds can ride the same column without a migration.
    metadata: dict[str, Any] = field(default_factory=dict)
    # Schema v8 — tier (``scratchpad`` / ``long_term`` / ``archive``).
    # See :data:`VALID_TIERS`. Pinned rows are always coerced to
    # ``long_term``. New auto-extracted memories default to
    # ``scratchpad`` (see :class:`MemoryExtractor`); explicit anchors
    # ([[remember:]], promises, shared moments, manual UI) default to
    # ``long_term``. The ``MemoryPromotionWorker`` shuffles rows
    # between tiers on age + ``use_count`` + ``revival_score``.
    tier: str = _DEFAULT_TIER
    # Schema v8 — revival_score in [0, 1]. Bumped post-turn when Aiko's
    # reply mentions enough of this memory's keywords (see
    # :func:`SessionController._mark_revived_memories`). The decay()
    # pass applies a small rebate proportional to revival_score so
    # high-revival rows drift toward salience=1.0 and act like soft
    # pins.
    revival_score: float = 0.0
    # Schema v9 — confidence in [0, 1]. Default ``0.7`` matches what
    # :class:`MemoryExtractor` writes from chat. Self-tagged
    # ``[[remember:...]]`` rows clamp to ``0.85``, manual UI creates to
    # ``1.0``, tool-result writes to ``0.95``. Pinning a row also clamps
    # confidence to ``>= 0.9`` (see :meth:`MemoryStore.set_pinned`). F1's
    # background fact-checker pushes confidence up on positive
    # verification and down on contradiction. RAG demotes low-confidence
    # hits during retrieval; the prompt assembler appends ``(uncertain)``
    # to lines with ``confidence < 0.5``. Knowledge-gap rows default to
    # ``0.0`` since they're open questions, not facts.
    confidence: float = 0.7
    # Schema v10 — temporal awareness. ``temporal_type`` classifies how
    # the memory relates to time (see :data:`VALID_TEMPORAL_TYPES`).
    # ``event_time`` is the ISO-8601 moment the *event* refers to as
    # parsed by :class:`MemoryExtractor` from the user's words ("gym
    # tonight at 8" -> 2026-05-28T20:00:00+02:00). ``relevance_until``
    # is when retrieval should stop surfacing the row in normal RAG
    # (the row stays in DB for archive / reflection use). All three
    # default to NULL/'durable' so legacy rows keep their pre-v10
    # behavior — they render with no time suffix, exactly like today.
    event_time: str | None = None
    temporal_type: str = _DEFAULT_TEMPORAL_TYPE
    relevance_until: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "kind": self.kind,
            "salience": float(self.salience),
            "source_session": self.source_session,
            "source_message_id": self.source_message_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": int(self.use_count),
            "pinned": bool(self.pinned),
            "metadata": dict(self.metadata) if self.metadata else {},
            "tier": str(self.tier),
            "revival_score": float(self.revival_score),
            "confidence": float(self.confidence),
            "event_time": self.event_time,
            "temporal_type": str(self.temporal_type),
            "relevance_until": self.relevance_until,
        }


@dataclass(slots=True)
class SearchHit:
    memory: Memory
    score: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(vec: np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _decode(blob: bytes) -> np.ndarray:
    count = len(blob) // 4
    return np.array(struct.unpack(f"{count}f", blob), dtype=np.float32)


def _encode_metadata(metadata: dict[str, Any] | None) -> str | None:
    """JSON-encode a metadata dict for storage. Returns None for empty/None."""
    if not metadata:
        return None
    try:
        return json.dumps(metadata, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        log.debug("metadata json encode failed; storing as empty", exc_info=True)
        return None


def _decode_metadata(value: Any) -> dict[str, Any]:
    """Decode whatever SQLite handed us back. Tolerates NULL, bad JSON, dicts."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_tier(tier: str | None, *, pinned: bool = False) -> str:
    """Return a valid tier name. Pinned rows are always coerced to long_term."""
    if pinned:
        return "long_term"
    if tier is None:
        return _DEFAULT_TIER
    cleaned = str(tier).strip().lower()
    if cleaned not in VALID_TIERS:
        return _DEFAULT_TIER
    return cleaned


class MemoryStore:
    """Thread-safe long-term memory backed by the ``memories`` SQLite table.

    The ``memories`` table is created by :class:`ChatDatabase` (schema v3).
    This class is a focused lens on that one table -- no foreign-key joins.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_memories: int = 500,
        scratchpad_cap: int = 1000,
        archive_cap: int = 10000,
        dedupe_threshold: float = 0.92,
    ) -> None:
        self._db_path = db_path
        self._max = max(50, int(max_memories))
        # Per-tier caps (schema v8). The long_term cap reuses ``_max``
        # for backward compat with the existing ``max_memories``
        # setting. ``prune()`` enforces these independently per tier so
        # scratchpad churn never crowds verified long-term anchors.
        self._tier_caps: dict[str, int] = {
            "scratchpad": max(50, int(scratchpad_cap)),
            "long_term": self._max,
            "archive": max(50, int(archive_cap)),
        }
        self._dedupe_threshold = float(dedupe_threshold)
        self._local = threading.local()
        self._lock = threading.Lock()
        # In-memory mirror so cosine search is a single NumPy pass.
        self._mirror: dict[int, Memory] = {}
        self._rag: "RagStore | None" = None
        # Listeners notified after each successful ``delete``. Used by
        # the F5 :class:`app.core.memory_conflict_store.MemoryConflictStore`
        # to cascade-clean any conflict pair that referenced the
        # deleted row. Listeners run synchronously on the caller
        # thread and any exception is swallowed so a buggy listener
        # cannot break a legit delete.
        self._delete_listeners: list[Any] = []
        self._reload_mirror()

    def add_delete_listener(self, callback: Any) -> None:
        """Register ``callback(memory_id: int)`` invoked after delete."""
        if callback is not None and callback not in self._delete_listeners:
            self._delete_listeners.append(callback)

    def remove_delete_listener(self, callback: Any) -> None:
        try:
            self._delete_listeners.remove(callback)
        except ValueError:
            pass

    def set_tier_caps(
        self,
        *,
        scratchpad: int | None = None,
        long_term: int | None = None,
        archive: int | None = None,
    ) -> None:
        """Update tier caps at runtime (e.g. when settings change)."""
        if scratchpad is not None:
            self._tier_caps["scratchpad"] = max(50, int(scratchpad))
        if long_term is not None:
            self._tier_caps["long_term"] = max(50, int(long_term))
            self._max = self._tier_caps["long_term"]
        if archive is not None:
            self._tier_caps["archive"] = max(50, int(archive))

    def attach_rag_store(self, rag_store: "RagStore | None") -> None:
        """Hook a :class:`RagStore` so subsequent writes mirror into LanceDB.

        Idempotent. Pass ``None`` to detach.
        """
        self._rag = rag_store

    def migrate_to_rag(self, rag_store: "RagStore") -> int:
        """Copy every existing memory into the RagStore (idempotent).

        Returns how many rows were written. Safe to call multiple times --
        :meth:`RagStore.add_memories_bulk` upserts on ``id`` so re-runs
        are no-ops content-wise but still pay the bulk delete+add cost.
        Rows with no embedding or empty content are skipped silently
        rather than aborting the whole migration.
        """
        if rag_store is None:
            return 0
        with self._lock:
            mems = list(self._mirror.values())
        records = [
            {
                "record_id": str(mem.id),
                "content": mem.content,
                "kind": mem.kind,
                "embedding": mem.embedding,
                "salience": mem.salience,
                "source_session": mem.source_session,
                "source_message_id": mem.source_message_id,
                "created_at": mem.created_at,
            }
            for mem in mems
            if mem.embedding is not None and (mem.content or "").strip()
        ]
        if not records:
            return 0
        try:
            written = rag_store.add_memories_bulk(records)
        except Exception:
            log.debug("rag bulk mirror failed", exc_info=True)
            return 0
        if written:
            log.info("RAG: mirrored %d existing memories into LanceDB", written)
        return written

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _reload_mirror(self) -> None:
        conn = self._get_conn()
        # Try the v10 shape first (event_time + temporal_type +
        # relevance_until). Fall back through v9 (no temporal), v8 (no
        # confidence), v7 (no tier/revival), v6 (no metadata). Pre-v6
        # databases land in the bottom-most ``except`` and start with
        # an empty mirror.
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned, "
                "metadata, tier, revival_score, confidence, "
                "event_time, temporal_type, relevance_until FROM memories"
            ).fetchall()
        except sqlite3.OperationalError:
            try:
                rows = conn.execute(
                    "SELECT id, content, kind, salience, embedding, source_session, "
                    "source_message_id, created_at, last_used_at, use_count, pinned, "
                    "metadata, tier, revival_score, confidence FROM memories"
                ).fetchall()
                # Append default temporal fields for pre-v10 rows.
                rows = [(*r, None, _DEFAULT_TEMPORAL_TYPE, None) for r in rows]
            except sqlite3.OperationalError:
                try:
                    rows = conn.execute(
                        "SELECT id, content, kind, salience, embedding, source_session, "
                        "source_message_id, created_at, last_used_at, use_count, pinned, "
                        "metadata, tier, revival_score FROM memories"
                    ).fetchall()
                    # Append default confidence + temporal fields for pre-v9 rows.
                    rows = [(*r, 0.7, None, _DEFAULT_TEMPORAL_TYPE, None) for r in rows]
                except sqlite3.OperationalError:
                    try:
                        rows = conn.execute(
                            "SELECT id, content, kind, salience, embedding, source_session, "
                            "source_message_id, created_at, last_used_at, use_count, pinned, "
                            "metadata FROM memories"
                        ).fetchall()
                        # Append default (tier, revival_score, confidence, event_time,
                        # temporal_type, relevance_until) for pre-v8 rows.
                        rows = [
                            (*r, _DEFAULT_TIER, 0.0, 0.7, None, _DEFAULT_TEMPORAL_TYPE, None)
                            for r in rows
                        ]
                    except sqlite3.OperationalError:
                        try:
                            rows = conn.execute(
                                "SELECT id, content, kind, salience, embedding, source_session, "
                                "source_message_id, created_at, last_used_at, use_count, pinned "
                                "FROM memories"
                            ).fetchall()
                            rows = [
                                (
                                    *r,
                                    None,
                                    _DEFAULT_TIER,
                                    0.0,
                                    0.7,
                                    None,
                                    _DEFAULT_TEMPORAL_TYPE,
                                    None,
                                )
                                for r in rows
                            ]
                        except sqlite3.OperationalError:
                            self._mirror = {}
                            return
        with self._lock:
            self._mirror = {
                r[0]: Memory(
                    id=r[0],
                    content=r[1],
                    kind=r[2],
                    salience=float(r[3]),
                    embedding=_decode(r[4]),
                    source_session=r[5],
                    source_message_id=r[6],
                    created_at=r[7],
                    last_used_at=r[8],
                    use_count=int(r[9]),
                    pinned=bool(r[10]),
                    metadata=_decode_metadata(r[11]),
                    tier=_normalize_tier(r[12], pinned=bool(r[10])),
                    revival_score=max(0.0, min(1.0, float(r[13] or 0.0))),
                    confidence=max(0.0, min(1.0, float(r[14] if r[14] is not None else 0.7))),
                    event_time=r[15] if r[15] else None,
                    temporal_type=_coerce_temporal_type(r[16]),
                    relevance_until=r[17] if r[17] else None,
                )
                for r in rows
            }
        log.info("memory store loaded with %d memories", len(self._mirror))

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ── writes ────────────────────────────────────────────────────────────

    def add(
        self,
        content: str,
        kind: str,
        embedding: np.ndarray,
        *,
        salience: float = 0.5,
        source_session: str | None = None,
        source_message_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        pinned: bool = False,
        skip_dedupe: bool = False,
        tier: str | None = None,
        confidence: float | None = None,
        event_time: str | None = None,
        temporal_type: str | None = None,
        relevance_until: str | None = None,
    ) -> Memory | None:
        """Insert a memory, deduplicating against near-identical existing rows.

        Returns the newly inserted ``Memory`` or ``None`` if the candidate
        was a near-duplicate of an existing memory (whose salience is bumped
        and ``last_used_at`` refreshed instead).

        ``metadata`` is a JSON-encodable dict written to the v7 ``metadata``
        column. Used today by ``shared_moment`` rows.

        ``pinned=True`` short-circuits the dedupe pass (kept rows shouldn't
        merge with similar non-pinned ones) and stores the row pinned from
        the start. ``skip_dedupe=True`` also bypasses dedupe — used when
        intentionally writing near-duplicate moments from different sources.

        ``tier`` selects ``scratchpad`` / ``long_term`` / ``archive``.
        Defaults to ``long_term`` (safety default for callers that forget).
        Pinned rows are always coerced to ``long_term``.

        ``confidence`` in [0, 1] is the F3 confidence-tier value. ``None``
        means "use the kind-aware default" (``0.85`` for ``self_tagged``,
        ``0.7`` for everything else; ``0.0`` for ``knowledge_gap`` which is
        a question, not a fact). Pinned rows clamp confidence to ``>= 0.9``.

        ``temporal_type`` / ``event_time`` / ``relevance_until`` are the v10
        temporal-awareness fields. ``temporal_type`` defaults to
        ``'durable'`` (the safe baseline — renders with no time suffix in
        retrieval, exactly like pre-v10 memories). ``event_time`` is the
        ISO-8601 moment the *event* refers to ("gym tonight at 8" stored
        on 2026-05-28T18:30 has ``event_time=2026-05-28T20:00``).
        ``relevance_until`` is when normal RAG retrieval should stop
        surfacing the row; the row stays in DB for archive use.
        """
        cleaned = (content or "").strip()
        if not cleaned or len(cleaned) < 4:
            return None
        kind = kind.strip().lower() or "fact"
        if kind not in VALID_KINDS:
            kind = "fact"
        salience_clipped = max(0.0, min(1.0, float(salience)))
        emb = np.asarray(embedding, dtype=np.float32)
        if emb.size == 0:
            return None
        # Normalize for cosine.
        norm = float(np.linalg.norm(emb))
        if norm > 0.0:
            emb = emb / norm
        tier_normalized = _normalize_tier(tier, pinned=pinned)
        if confidence is None:
            if kind == "knowledge_gap":
                confidence_value = 0.0
            elif kind in ("self_tagged", "self"):
                confidence_value = 0.85
            else:
                confidence_value = 0.7
        else:
            confidence_value = float(confidence)
        confidence_value = max(0.0, min(1.0, confidence_value))
        if pinned and confidence_value < 0.9:
            confidence_value = 0.9

        temporal_type_normalized = _coerce_temporal_type(temporal_type)
        event_time_clean = event_time.strip() if isinstance(event_time, str) and event_time.strip() else None
        relevance_until_clean = (
            relevance_until.strip()
            if isinstance(relevance_until, str) and relevance_until.strip()
            else None
        )

        # Dedupe pass against in-memory mirror. Pinned writes bypass dedupe
        # so user-curated moments are never silently merged into a fuzzy
        # nearby row (matters most for shared_moment).
        dup_id: int | None = None
        if not pinned and not skip_dedupe:
            with self._lock:
                for mem in self._mirror.values():
                    if cosine_similarity(emb, mem.embedding) >= self._dedupe_threshold:
                        dup_id = mem.id
                        break
        if dup_id is not None:
            self._touch_existing(dup_id, salience_clipped)
            return None

        # Real insert.
        conn = self._get_conn()
        now = _now_iso()
        meta_json = _encode_metadata(metadata)
        pinned_int = 1 if pinned else 0
        cursor = conn.execute(
            "INSERT INTO memories ("
            "  content, kind, salience, embedding, source_session, "
            "  source_message_id, created_at, last_used_at, use_count, pinned, "
            "  metadata, tier, revival_score, confidence, "
            "  event_time, temporal_type, relevance_until"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0.0, ?, ?, ?, ?)",
            (
                cleaned,
                kind,
                salience_clipped,
                _encode(emb),
                source_session,
                source_message_id,
                now,
                None,
                pinned_int,
                meta_json,
                tier_normalized,
                confidence_value,
                event_time_clean,
                temporal_type_normalized,
                relevance_until_clean,
            ),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        memory = Memory(
            id=new_id,
            content=cleaned,
            kind=kind,
            salience=salience_clipped,
            embedding=emb,
            source_session=source_session,
            source_message_id=source_message_id,
            created_at=now,
            last_used_at=None,
            use_count=0,
            pinned=bool(pinned),
            metadata=dict(metadata) if metadata else {},
            tier=tier_normalized,
            revival_score=0.0,
            confidence=confidence_value,
            event_time=event_time_clean,
            temporal_type=temporal_type_normalized,
            relevance_until=relevance_until_clean,
        )
        with self._lock:
            self._mirror[new_id] = memory
        if self._rag is not None:
            try:
                self._rag.add_memory(
                    record_id=str(new_id),
                    content=cleaned,
                    kind=kind,
                    embedding=emb,
                    salience=salience_clipped,
                    source_session=source_session,
                    source_message_id=source_message_id,
                    created_at=now,
                )
            except Exception:
                log.debug("rag add_memory failed", exc_info=True)
        # Per-tier opportunistic prune. Cheaper to check the just-grown
        # tier than to walk every row.
        with self._lock:
            tier_count = sum(1 for m in self._mirror.values() if m.tier == tier_normalized)
        if tier_count > self._tier_caps.get(tier_normalized, self._max):
            self.prune()
        return memory

    def _touch_existing(self, memory_id: int, candidate_salience: float) -> None:
        """Bump salience and refresh last_used_at on a deduped match."""
        conn = self._get_conn()
        now = _now_iso()
        with self._lock:
            mem = self._mirror.get(memory_id)
            if mem is None:
                return
            new_salience = max(mem.salience, candidate_salience, mem.salience + 0.05)
            new_salience = min(1.0, new_salience)
            mem.salience = new_salience
            mem.last_used_at = now
        conn.execute(
            "UPDATE memories SET salience = ?, last_used_at = ? WHERE id = ?",
            (new_salience, now, memory_id),
        )
        conn.commit()

    def mark_used(self, ids: Iterable[int]) -> None:
        ids_list = [int(i) for i in ids if i]
        if not ids_list:
            return
        conn = self._get_conn()
        now = _now_iso()
        placeholders = ",".join("?" * len(ids_list))
        conn.execute(
            f"UPDATE memories SET last_used_at = ?, use_count = use_count + 1 "
            f"WHERE id IN ({placeholders})",
            (now, *ids_list),
        )
        conn.commit()
        with self._lock:
            for mid in ids_list:
                mem = self._mirror.get(mid)
                if mem is not None:
                    mem.last_used_at = now
                    mem.use_count += 1

    def mark_revived(self, ids: Iterable[int], *, delta: float) -> None:
        """Bump ``revival_score`` for memories Aiko actually cited in her reply.

        Called from ``SessionController._post_turn_inner_life`` after a
        keyword-overlap scan over the assistant's reply text vs each
        surfaced memory's content. ``delta`` is small (default 0.15) and
        the result is clamped to ``[0, 1]``. Persistent revival drives
        the decay rebate (see :meth:`decay`) and counts toward
        :class:`MemoryPromotionWorker` promotion gates.
        """
        ids_list = [int(i) for i in ids if i]
        if not ids_list or delta == 0:
            return
        d = float(delta)
        conn = self._get_conn()
        placeholders = ",".join("?" * len(ids_list))
        conn.execute(
            f"UPDATE memories SET revival_score = "
            f"MAX(0.0, MIN(1.0, revival_score + ?)) "
            f"WHERE id IN ({placeholders})",
            (d, *ids_list),
        )
        conn.commit()
        with self._lock:
            for mid in ids_list:
                mem = self._mirror.get(mid)
                if mem is not None:
                    mem.revival_score = max(0.0, min(1.0, mem.revival_score + d))

    def delete(self, memory_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        conn.commit()
        with self._lock:
            self._mirror.pop(int(memory_id), None)
        if self._rag is not None:
            try:
                self._rag.delete_memory(str(int(memory_id)))
            except Exception:
                log.debug("rag delete_memory failed", exc_info=True)
        deleted = cursor.rowcount > 0
        if deleted and self._delete_listeners:
            for listener in list(self._delete_listeners):
                try:
                    listener(int(memory_id))
                except Exception:
                    log.debug(
                        "memory delete listener raised for id=%s",
                        memory_id,
                        exc_info=True,
                    )
        return deleted

    _UNSET: object = object()

    def update(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        kind: str | None = None,
        salience: float | None = None,
        embedding: np.ndarray | None = None,
        metadata: dict[str, Any] | None = None,
        metadata_merge: bool = False,
        tier: str | None = None,
        revival_score: float | None = None,
        confidence: float | None = None,
        event_time: object | None = _UNSET,
        temporal_type: str | None = None,
        relevance_until: object | None = _UNSET,
    ) -> Memory | None:
        """Patch one or more fields on an existing memory.

        Pass ``embedding`` alongside ``content`` to refresh the vector index;
        callers that change content without supplying an embedding silently
        keep the stale vector (used by tests). The LanceDB mirror is upserted
        whenever any field changes so retrieval stays in sync.

        ``metadata`` replaces the whole JSON bag by default. Pass
        ``metadata_merge=True`` to shallow-merge instead — used by the
        anniversary path to stamp ``last_anniversaried_at`` without losing
        the original ``vibe`` / ``when`` / ``what`` fields.

        ``tier`` may be ``"scratchpad"`` / ``"long_term"`` / ``"archive"``.
        Pinned rows are coerced back to ``"long_term"`` regardless of the
        requested tier. ``revival_score`` is clamped to ``[0, 1]``.

        ``temporal_type`` / ``event_time`` / ``relevance_until`` are the v10
        temporal-awareness fields (see :data:`VALID_TEMPORAL_TYPES`).
        ``event_time`` and ``relevance_until`` use a sentinel default
        (``_UNSET``) so callers can explicitly clear them with ``None``
        without conflating "leave as-is" and "set to NULL".

        Returns the updated :class:`Memory` snapshot, or ``None`` if the row
        doesn't exist.
        """
        with self._lock:
            mem = self._mirror.get(int(memory_id))
        if mem is None:
            return None

        new_content = mem.content
        if content is not None:
            cleaned = str(content).strip()
            if len(cleaned) < 4:
                return None
            new_content = cleaned

        new_kind = mem.kind
        if kind is not None:
            requested = str(kind).strip().lower() or "fact"
            new_kind = requested if requested in VALID_KINDS else "fact"

        new_salience = mem.salience
        if salience is not None:
            new_salience = max(0.0, min(1.0, float(salience)))

        new_embedding = mem.embedding
        if embedding is not None:
            emb = np.asarray(embedding, dtype=np.float32)
            if emb.size > 0:
                norm = float(np.linalg.norm(emb))
                if norm > 0.0:
                    emb = emb / norm
                new_embedding = emb

        new_metadata = dict(mem.metadata) if mem.metadata else {}
        metadata_changed = False
        if metadata is not None:
            if metadata_merge:
                new_metadata = {**new_metadata, **dict(metadata)}
            else:
                new_metadata = dict(metadata)
            metadata_changed = True

        new_tier = mem.tier
        if tier is not None:
            new_tier = _normalize_tier(tier, pinned=mem.pinned)
        elif mem.pinned and new_tier != "long_term":
            # Defensive: a pinned row should never be sitting in a
            # non-long_term tier. Coerce on any update touching the row.
            new_tier = "long_term"

        new_revival = mem.revival_score
        if revival_score is not None:
            new_revival = max(0.0, min(1.0, float(revival_score)))

        new_confidence = mem.confidence
        if confidence is not None:
            new_confidence = max(0.0, min(1.0, float(confidence)))
            if mem.pinned and new_confidence < 0.9:
                new_confidence = 0.9

        new_event_time = mem.event_time
        if event_time is not self._UNSET:
            if event_time is None:
                new_event_time = None
            elif isinstance(event_time, str) and event_time.strip():
                new_event_time = event_time.strip()
            else:
                new_event_time = None

        new_temporal_type = mem.temporal_type
        if temporal_type is not None:
            new_temporal_type = _coerce_temporal_type(temporal_type)

        new_relevance_until = mem.relevance_until
        if relevance_until is not self._UNSET:
            if relevance_until is None:
                new_relevance_until = None
            elif isinstance(relevance_until, str) and relevance_until.strip():
                new_relevance_until = relevance_until.strip()
            else:
                new_relevance_until = None

        conn = self._get_conn()
        conn.execute(
            "UPDATE memories SET content = ?, kind = ?, salience = ?, embedding = ?, "
            "metadata = ?, tier = ?, revival_score = ?, confidence = ?, "
            "event_time = ?, temporal_type = ?, relevance_until = ? WHERE id = ?",
            (
                new_content,
                new_kind,
                float(new_salience),
                _encode(new_embedding),
                _encode_metadata(new_metadata),
                new_tier,
                float(new_revival),
                float(new_confidence),
                new_event_time,
                new_temporal_type,
                new_relevance_until,
                int(memory_id),
            ),
        )
        conn.commit()

        with self._lock:
            mem.content = new_content
            mem.kind = new_kind
            mem.salience = new_salience
            mem.embedding = new_embedding
            if metadata_changed:
                mem.metadata = new_metadata
            mem.tier = new_tier
            mem.revival_score = new_revival
            mem.confidence = new_confidence
            mem.event_time = new_event_time
            mem.temporal_type = new_temporal_type
            mem.relevance_until = new_relevance_until
            updated = mem

        if self._rag is not None:
            try:
                # ``add_memory`` upserts on id; safe to call for plain
                # field changes too.
                self._rag.add_memory(
                    record_id=str(int(memory_id)),
                    content=updated.content,
                    kind=updated.kind,
                    embedding=updated.embedding,
                    salience=updated.salience,
                    source_session=updated.source_session,
                    source_message_id=updated.source_message_id,
                    created_at=updated.created_at,
                )
            except Exception:
                log.debug("rag update mirror failed", exc_info=True)
        return updated

    def reclassify(
        self,
        memory_id: int,
        *,
        temporal_type: str,
        event_time: object | None = _UNSET,
        relevance_until: object | None = _UNSET,
    ) -> Memory | None:
        """Flip the v10 temporal classification of a memory in-place.

        Used by :class:`MemoryDecayWorker` to convert a ``future_plan``
        whose ``event_time`` has passed into a ``past_event`` (with a
        fresh ``relevance_until = event_time + 7d`` so the row can still
        be referenced retrospectively for a week before sliding to
        ``archive``).

        Pass ``event_time=None`` / ``relevance_until=None`` explicitly to
        clear those columns; omit the arg (sentinel) to leave them as-is.
        Returns the updated :class:`Memory` snapshot, or ``None`` if the
        row doesn't exist.
        """
        return self.update(
            memory_id,
            temporal_type=temporal_type,
            event_time=event_time,
            relevance_until=relevance_until,
        )

    def set_pinned(self, memory_id: int, pinned: bool) -> Memory | None:
        """Pin or unpin a memory.

        Pinning nudges ``salience`` up to ``1.0`` so a future un-pin does not
        snap back to a stale low value. It also coerces the row's ``tier``
        to ``long_term`` so the row can never sit in ``scratchpad`` or
        ``archive`` while pinned. Un-pinning leaves the existing salience
        and tier intact -- decay + the promotion worker will manage them
        from there.
        """
        with self._lock:
            mem = self._mirror.get(int(memory_id))
        if mem is None:
            return None
        new_pinned = 1 if pinned else 0
        new_salience = mem.salience
        new_tier = mem.tier
        new_confidence = mem.confidence
        if pinned:
            new_salience = max(new_salience, 1.0)
            new_tier = "long_term"
            new_confidence = max(new_confidence, 0.9)
        conn = self._get_conn()
        conn.execute(
            "UPDATE memories SET pinned = ?, salience = ?, tier = ?, confidence = ? "
            "WHERE id = ?",
            (
                new_pinned,
                float(new_salience),
                new_tier,
                float(new_confidence),
                int(memory_id),
            ),
        )
        conn.commit()
        with self._lock:
            mem.pinned = bool(pinned)
            mem.salience = new_salience
            mem.tier = new_tier
            mem.confidence = new_confidence
            updated = mem
        if self._rag is not None and pinned:
            # Mirror the salience bump so retrieval scoring matches what
            # the SQLite store believes.
            try:
                self._rag.add_memory(
                    record_id=str(int(memory_id)),
                    content=updated.content,
                    kind=updated.kind,
                    embedding=updated.embedding,
                    salience=updated.salience,
                    source_session=updated.source_session,
                    source_message_id=updated.source_message_id,
                    created_at=updated.created_at,
                )
            except Exception:
                log.debug("rag pin mirror failed", exc_info=True)
        return updated

    def get(self, memory_id: int) -> Memory | None:
        with self._lock:
            return self._mirror.get(int(memory_id))

    # ── Schema v10 temporal-awareness helpers ────────────────────────

    def list_by_temporal_type(
        self,
        temporal_type: str,
        *,
        event_time_before: str | None = None,
        relevance_until_before: str | None = None,
        limit: int | None = None,
    ) -> list[Memory]:
        """Filtered scan over the in-memory mirror by v10 temporal columns.

        Used by :class:`MemoryDecayWorker` for the reclassification
        passes (future_plan -> past_event when ``event_time`` slips
        into the past; past_event -> archive when ``relevance_until``
        passes) and by :class:`FollowUpWorker` to find due plans.

        ``event_time_before`` / ``relevance_until_before`` are ISO-8601
        strings; rows whose corresponding column is missing or sorts
        AFTER the threshold are skipped. Lexical comparison on ISO-8601
        is correct as long as the strings are properly formatted (which
        the writer paths guarantee).
        """
        normalized = _coerce_temporal_type(temporal_type)
        with self._lock:
            mirror_snapshot = list(self._mirror.values())
        out: list[Memory] = []
        for mem in mirror_snapshot:
            if mem.temporal_type != normalized:
                continue
            if event_time_before is not None:
                et = mem.event_time
                if not et or et >= event_time_before:
                    continue
            if relevance_until_before is not None:
                ru = mem.relevance_until
                if not ru or ru >= relevance_until_before:
                    continue
            out.append(mem)
            if limit is not None and len(out) >= int(limit):
                break
        return out

    def decay(
        self,
        by: float | None = None,
        *,
        now: datetime | None = None,
        elapsed_days: float | None = None,
        decay_rates: dict[str, float] | None = None,
        revival_coefficient: float = 0.05,
        revival_decay_per_day: float = 0.02,
        max_catchup_days: float = 30.0,
    ) -> dict[str, float]:
        """Apply wall-clock-driven decay, tier-aware with a revival rebate.

        Default per-tier rates (per day): ``scratchpad=0.05``,
        ``long_term=0.02``, ``archive=0.0``. Pass ``decay_rates`` to
        override individual tiers from settings.

        The actual decay magnitude is ``rate * elapsed_days``. By default
        ``elapsed_days`` is computed from the persisted
        ``memory.last_decay_run_at`` anchor in :class:`ChatDatabase`'s
        ``kv_meta`` table, so running once an hour applies 1/24 of a
        day; coming back online after 3 days produces 3 days' worth
        (clamped to ``max_catchup_days`` so a long absence doesn't zero
        everything). Pass ``elapsed_days`` explicitly for tests.

        Each row gets a small *revival rebate* before decay applies:
        ``salience' = clamp(salience + revival_coefficient * elapsed_days *
        revival_score - rate * elapsed_days, 0, 1)``. ``revival_score``
        itself decays at ``revival_decay_per_day`` so old revivals fade.

        Pinned rows are skipped (their salience stays at 1.0).

        Legacy positional ``by``: when set, applies that flat rate to
        every tier (preserves the old daily-loop semantics for callers
        that still pass ``decay(by=0.02)``). When ``by`` is provided,
        ``elapsed_days`` defaults to 1.0 (one day) to match the old
        contract.
        """
        now_dt = now or datetime.now(timezone.utc)

        # Resolve effective per-tier rates first so the legacy ``by`` arg
        # can map onto them cleanly.
        rates = {"scratchpad": 0.05, "long_term": 0.02, "archive": 0.0}
        if decay_rates:
            for tier, rate in decay_rates.items():
                tier_norm = str(tier).strip().lower()
                if tier_norm in rates:
                    rates[tier_norm] = max(0.0, float(rate))
        legacy_by = by is not None
        if legacy_by:
            flat = max(0.0, float(by))
            rates = {t: flat for t in rates}
            # Legacy callers expect one tick = one day's worth.
            if elapsed_days is None:
                elapsed_days = 1.0

        # Compute elapsed_days from the persisted anchor if not supplied.
        if elapsed_days is None:
            last_dt = self._read_last_decay_run_at()
            if last_dt is None:
                # First-ever run: nothing to decay yet. Just persist the
                # anchor so the next tick has a baseline.
                self._write_last_decay_run_at(now_dt)
                return {"elapsed_days": 0.0, "applied": False}
            delta_seconds = max(0.0, (now_dt - last_dt).total_seconds())
            elapsed_days = min(
                float(max_catchup_days), delta_seconds / 86_400.0,
            )

        stats: dict[str, float] = {
            "elapsed_days": float(elapsed_days),
            "applied": False,
        }
        if elapsed_days <= 0.0:
            self._write_last_decay_run_at(now_dt)
            return stats

        conn = self._get_conn()
        # Per-tier salience update. ``MAX/MIN`` clamp to [0, 1].
        # ``salience + rebate * revival_score - decay`` -- the rebate
        # scales with both ``revival_score`` (per-row signal) and
        # ``elapsed_days`` (uniform), so old high-revival rows
        # actively gain salience between sweeps.
        for tier in VALID_TIERS:
            rate = rates.get(tier, 0.0)
            decay_amount = rate * float(elapsed_days)
            rebate = float(revival_coefficient) * float(elapsed_days)
            if decay_amount <= 0.0 and rebate <= 0.0:
                continue
            conn.execute(
                "UPDATE memories SET salience = "
                "MAX(0.0, MIN(1.0, salience + ? * revival_score - ?)) "
                "WHERE tier = ? AND pinned = 0",
                (rebate, decay_amount, tier),
            )

        # Decay revival_score itself so a one-time spike fades without
        # gating future rebates.
        revival_delta = float(revival_decay_per_day) * float(elapsed_days)
        if revival_delta > 0:
            conn.execute(
                "UPDATE memories SET revival_score = "
                "MAX(0.0, revival_score - ?) WHERE pinned = 0",
                (revival_delta,),
            )

        conn.commit()
        # Refresh the in-memory mirror after the bulk UPDATE so search /
        # iter helpers see the new salience values immediately.
        self._reload_mirror()
        self._write_last_decay_run_at(now_dt)
        stats["applied"] = True
        return stats

    _KV_LAST_DECAY = "memory.last_decay_run_at"

    def _read_last_decay_run_at(self) -> datetime | None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM kv_meta WHERE key = ?",
                (self._KV_LAST_DECAY,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        try:
            return datetime.fromisoformat(str(row[0]))
        except (TypeError, ValueError):
            return None

    def _write_last_decay_run_at(self, when: datetime) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO kv_meta (key, value, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (self._KV_LAST_DECAY, when.isoformat(), when.isoformat()),
            )
            conn.commit()
        except sqlite3.OperationalError:
            # kv_meta missing means a pre-v8 schema; fail silently --
            # the next ChatDatabase init will create the table.
            log.debug("kv_meta unavailable; skipped decay anchor", exc_info=True)

    def prune(self) -> int:
        """Delete the lowest-priority memories per-tier until each tier fits.

        Each tier has its own cap (see :meth:`set_tier_caps`). Within a
        tier, victims are ranked by ``salience + 0.05 * min(use_count, 20)
        + 0.1 * revival_score`` -- lowest scores die first. Pinned rows
        are never selected (and pinned rows always live in ``long_term``
        anyway). Returns total victims across all tiers.
        """
        total_victims = 0
        with self._lock:
            snapshot = list(self._mirror.values())
        for tier in VALID_TIERS:
            tier_rows = [m for m in snapshot if m.tier == tier and not m.pinned]
            cap = self._tier_caps.get(tier, self._max)
            if len(tier_rows) <= cap:
                continue
            tier_rows.sort(
                key=lambda m: (
                    m.salience
                    + 0.05 * min(m.use_count, 20)
                    + 0.1 * m.revival_score
                ),
            )
            excess = len(tier_rows) - cap
            victims = [m.id for m in tier_rows[:excess]]
            if not victims:
                continue
            conn = self._get_conn()
            placeholders = ",".join("?" * len(victims))
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})", victims,
            )
            conn.commit()
            with self._lock:
                for mid in victims:
                    self._mirror.pop(mid, None)
            if self._rag is not None:
                for mid in victims:
                    try:
                        self._rag.delete_memory(str(mid))
                    except Exception:
                        log.debug("rag delete during prune failed", exc_info=True)
            total_victims += len(victims)
            log.info(
                "pruned %d low-priority memories in tier=%s", len(victims), tier,
            )
        return total_victims

    # ── reads ─────────────────────────────────────────────────────────────

    def search(
        self,
        query_embedding: np.ndarray,
        *,
        top_k: int = 6,
        min_score: float = 0.4,
    ) -> list[SearchHit]:
        """Return the top-k memories by cosine similarity. Empty if store is empty."""
        with self._lock:
            mems = list(self._mirror.values())
        if not mems:
            return []
        q = np.asarray(query_embedding, dtype=np.float32)
        if q.size == 0:
            return []
        qn = float(np.linalg.norm(q))
        if qn > 0.0:
            q = q / qn
        scored: list[SearchHit] = []
        for mem in mems:
            score = cosine_similarity(q, mem.embedding)
            # Light salience boost so two similar memories prefer the more salient one.
            adjusted = score + 0.05 * (mem.salience - 0.5)
            if score >= min_score:
                scored.append(SearchHit(memory=mem, score=adjusted))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[: max(1, int(top_k))]

    def list_recent(
        self,
        limit: int = 50,
        *,
        offset: int = 0,
        kind: str | None = None,
    ) -> list[Memory]:
        with self._lock:
            mems = list(self._mirror.values())
        if kind:
            kind_norm = kind.strip().lower()
            mems = [m for m in mems if m.kind == kind_norm]
        mems.sort(key=lambda m: m.created_at, reverse=True)
        # Pinned rows always float to the top of the recent list so the
        # editor's default view shows curated rows first regardless of
        # creation date.
        mems.sort(key=lambda m: (0 if m.pinned else 1))
        start = max(0, int(offset))
        stop = start + max(1, int(limit))
        return mems[start:stop]

    def list_top(
        self,
        limit: int = 50,
        *,
        offset: int = 0,
        kind: str | None = None,
    ) -> list[Memory]:
        with self._lock:
            mems = list(self._mirror.values())
        if kind:
            kind_norm = kind.strip().lower()
            mems = [m for m in mems if m.kind == kind_norm]
        mems.sort(
            key=lambda m: (
                0 if m.pinned else 1,
                -m.salience,
                -m.use_count,
            ),
        )
        start = max(0, int(offset))
        stop = start + max(1, int(limit))
        return mems[start:stop]

    def iter_by_kind(self, kind: str) -> list[Memory]:
        """Snapshot of all memories of a given kind. Cheap (mirror walk)."""
        kind_norm = (kind or "").strip().lower()
        if not kind_norm:
            return []
        with self._lock:
            return [m for m in self._mirror.values() if m.kind == kind_norm]

    def iter_by_tier(self, tier: str) -> list[Memory]:
        """Snapshot of all memories in a given tier. Cheap (mirror walk).

        Used by :class:`MemoryPromotionWorker` to scan each tier on its
        own schedule (promote/delete scratchpad, demote long_term, etc.).
        """
        tier_norm = (tier or "").strip().lower()
        if tier_norm not in VALID_TIERS:
            return []
        with self._lock:
            return [m for m in self._mirror.values() if m.tier == tier_norm]

    def count_memories(
        self,
        kind: str | None = None,
        *,
        tier: str | None = None,
    ) -> int:
        with self._lock:
            mems = list(self._mirror.values())
        if kind:
            kind_norm = kind.strip().lower()
            mems = [m for m in mems if m.kind == kind_norm]
        if tier:
            tier_norm = tier.strip().lower()
            mems = [m for m in mems if m.tier == tier_norm]
        return len(mems)

    def count_by_tier(self) -> dict[str, int]:
        """Return ``{tier: count}`` covering every tier (zeros included).

        Feeds the "scratchpad N | long_term M | archive K" header on the
        Memory tab and the ``/api/memories/counts`` endpoint.
        """
        counts: dict[str, int] = {t: 0 for t in VALID_TIERS}
        with self._lock:
            for mem in self._mirror.values():
                if mem.tier in counts:
                    counts[mem.tier] += 1
                else:
                    counts.setdefault("long_term", 0)
                    counts["long_term"] += 1
        counts["total"] = sum(counts[t] for t in VALID_TIERS)
        return counts

    def count(self) -> int:
        with self._lock:
            return len(self._mirror)
