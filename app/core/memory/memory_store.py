"""SQLite-backed long-term memory store with cosine search.

One row per durable fact about the user. Embeddings live alongside the row
as a packed float32 BLOB. The store keeps an in-memory mirror of every row
so cosine search runs in pure NumPy without a per-query SQL roundtrip.

Capacity is unbounded by default; setting a per-tier cap makes ``prune()``
evict the least-used / lowest-salience rows of that tier once it is hit.
Cross-session by design: there's exactly one memory store for the assistant.

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np

from app.core.infra import timephrase
from app.core.infra.text_query import compile_query
from app.core.memory.conflict_heuristics import HEURISTIC_NO, classify_pair
from app.core.memory.vector_index import VectorIndex

if TYPE_CHECKING:
    from app.core.rag.rag_store import RagStore


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


# How long retrieval keeps surfacing a row in normal RAG, per temporal
# type. ``None`` means no expiry (the two timeless types).
#
# This lives beside the writer rather than in :class:`MemoryExtractor`
# (where it started) because ``add`` can now *change* a row's temporal
# type, and a type carries an expiry rule -- so whoever changes the one
# has to be able to recompute the other. Leaving the derivation upstream
# is what made the H40 near-miss possible: the store reclassified a row
# and left it holding ``durable``'s ``relevance_until``, which is
# ``None``, and ``list_by_temporal_type`` skips rows with no
# ``relevance_until`` -- so the reclassified row became unretirable.
_RELEVANCE_WINDOW: dict[str, timedelta | None] = {
    "durable": None,
    "preference": None,
    "ongoing": timedelta(days=30),
    "past_event": timedelta(days=7),
    # ``future_plan`` derives from ``event_time`` instead; the entry is
    # its clockless fallback.
    "future_plan": timedelta(days=2),
}


def derive_relevance_until(
    temporal_type: str,
    *,
    event_time: datetime | None,
    created_at: datetime,
) -> str | None:
    """When retrieval should stop surfacing a row of this type.

    ``past_event`` / ``ongoing`` measure from ``created_at`` (when we
    learned of it). ``future_plan`` measures from ``event_time`` + 1 day,
    so there is still a window afterwards in which Aiko can ask how it
    went; a plan with no clock falls back to ``created_at``, which is
    what gives the decay worker something to retire it on.
    ``durable`` / ``preference`` never expire.
    """
    if temporal_type == "future_plan":
        anchor = event_time if event_time is not None else created_at
        return (anchor + timedelta(days=1)).isoformat()
    window = _RELEVANCE_WINDOW.get(temporal_type)
    if window is None:
        return None
    return (created_at + window).isoformat()


# Schema v30 (F16): testimony vs. inference. ``stated`` = the user said it
# outright; ``inferred`` = Aiko concluded it. The default is ``inferred``
# on purpose -- over-claiming testimony ("you told me X" when he didn't) is
# the failure this fixes, so anything unsure lands here, and only the
# deliberate write paths mark ``stated``.
VALID_PROVENANCE = (
    "stated",
    "inferred",
)
_DEFAULT_PROVENANCE = "inferred"


def _coerce_provenance(value: str | None) -> str:
    """Normalize and validate a provenance string.

    Falls back to ``'inferred'`` (the safe baseline) for unknown or missing
    values so legacy callers and bad LLM output never crash an insert.
    Raises only on completely non-string input.
    """
    if value is None:
        return _DEFAULT_PROVENANCE
    if not isinstance(value, str):
        raise TypeError(f"provenance must be a string, got {type(value).__name__}")
    cleaned = value.strip().lower()
    if cleaned in VALID_PROVENANCE:
        return cleaned
    return _DEFAULT_PROVENANCE


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
    # :class:`app.core.proactive.idle_curiosity_worker.IdleCuriosityWorker`.
    # Carries ``{source_open_question_id, source_query, discovered_at}``
    # in the ``metadata`` JSON column. The persona file tells Aiko to
    # surface these as "I was reading about X — turns out..." rather
    # than recite them as bare facts.
    "curiosity_finding",
    # (``curiosity_seed`` lived here until schema v29. A seed is a topic
    # Aiko has *not* raised yet, which is the opposite of a memory, and
    # keeping it in this table gave it three behaviours nobody chose:
    # RAG could surface it as a remembered fact, the topic graph
    # clustered it into the graph it was derived from, and the
    # scratchpad TTL applied. It now lives in ``cue_pool`` with the other
    # six cue types -- see
    # :mod:`app.core.proactive.curiosity_seed_worker`.)
    # K1 personality backlog — Aiko's own long-term personal goals
    # (the things she wants to grow into / explore / become better
    # at over time). Distinct from ``agenda`` (short-term follow-ups
    # about the user) and ``self`` (one-shot self-memories). Written
    # by the ``[[goal:summary]]`` self-tag (``GoalStore.add_goal``),
    # the cold-start bootstrap inside :class:`GoalWorker`, manual
    # REST/UI/MCP/tool adds, and seeded onto durable long_term tier.
    # Carries ``{summary, added_at, last_reflected_at,
    # last_reflection_id, last_progress_note, reflection_count,
    # archived_at, source}`` in the ``metadata`` JSON column.
    # Surfaced as an inner-life "Aiko's quiet long-term goals" block
    # in the prompt assembler and rewards goal-aligned RAG hits with
    # a small score bonus. Archived rows (``metadata.archived_at``
    # set, ``tier=archive``) are kept for audit but excluded from
    # the active prompt block.
    "goal",
    # K1 personality backlog — a single reflection moment Aiko had
    # about one of her goals (worker tick or self-tag). Owned by
    # :class:`GoalStore`; carries ``{goal_id, note, noted_at, source}``
    # in the ``metadata`` JSON column. Kept on the ``long_term``
    # tier (capped per-goal via :meth:`GoalStore.prune_progress`)
    # so the history survives across decay sweeps. The most recent
    # entry's text is mirrored into the parent goal's
    # ``metadata.last_progress_note`` so prompt assembly can render
    # one line without scanning the progress tail.
    "goal_progress",
    # K11 personality backlog — a "pre-thought": Aiko's drafted reply to
    # a plausible near-future user question, written ahead of time by
    # :class:`app.core.proactive.pre_thought_worker.PreThoughtWorker`
    # during idle windows. The two-stage worker first asks the local LLM
    # for likely upcoming questions (grounded in the rolling summary +
    # persona), then drafts Aiko's in-persona reply to each via the K10
    # minimal-persona eval prompt. The row's ``content`` carries the
    # combined "If asked X, I'd say Y" text but the EMBEDDING is computed
    # on the hypothetical question alone, so the pre-thought surfaces
    # through normal cosine RAG when the user later asks something
    # similar -- smoothing the first response without any web access.
    # Carries ``{question, thought, generated_at, source}`` in the
    # ``metadata`` JSON column; lands on the ``scratchpad`` tier so it
    # ages out naturally if it never gets used. Rendered with a
    # ``(pre-thought)`` suffix in the RAG block so Aiko reads it as a
    # draft she already mulled, not a fact.
    "pre_thought",
    # F8 personality backlog — a distilled, impersonal, non-time-
    # sensitive fact Aiko learned (band names in a genre, a studio's
    # filmography, how a thing works), distinct from personal
    # ``fact``/``event`` memory about the user. Written by
    # :class:`app.core.proactive.idle_knowledge_worker.IdleKnowledgeWorker`
    # (F9) from the topic graph during idle windows, and by future
    # F1/F7 writers. Carries ``{topic, source_query, source_url,
    # source_urls, learned_at, cluster_key}`` in the ``metadata`` JSON
    # column (F4 — every knowledge row is source-cited). Surfaced
    # through ordinary cosine RAG with a ``(learned)`` suffix tag and a
    # small retrieval boost on informational (K4 dialogue-act
    # ``question``) turns; the K61 ``knowledge_grounding`` inner-life
    # block then nudges Aiko to commit to the specifics instead of
    # survey-hedging. The persona tells her to surface these naturally
    # ("oh -- try Slowdive"), never as a lecture.
    "knowledge",
    # F10g personality backlog — a rolling, worker-LLM one-paragraph
    # digest of a *dense topic cluster* ("what I know about X"). Written
    # and refreshed by
    # :class:`app.core.conversation.topic_digest_worker.TopicDigestWorker`
    # during idle windows, one per cluster, cached by the cluster's
    # representative id and only regenerated on material size drift.
    # Carries ``{cluster_representative_id, member_count, refreshed_at,
    # source_ids}`` in the ``metadata`` JSON column. Lives in the normal
    # pool (decays, pinnable, shows in the Memory tab) but is EXCLUDED
    # from topic-graph clustering (it's a derived artifact, not a raw
    # memory — see ``topic_graph._NON_CLUSTERING_KINDS``) and from the
    # F5/K35 hygiene allow-lists. Surfaces through ordinary cosine RAG as
    # the coarse answer, and the F10c expansion path prefers it over raw
    # sibling enumeration to cap prompt size on big clusters.
    "topic_digest",
    # H9 — an intentional first-person diary entry Aiko chooses to write
    # via an inline ``[[diary:...]]`` tag (as opposed to the reflections /
    # dreams / moments she produces as a side effect). Written by
    # :class:`app.core.session.turn_runner.TurnRunner` straight from the
    # tag body, ``skip_dedupe=True`` so each entry is preserved as its own
    # journal moment, on the durable ``long_term`` tier. Surfaced read-only
    # in the "Diary" UI tab alongside the other journal-flavoured kinds;
    # otherwise a normal pool member (decays slowly, pinnable, retrievable
    # through ordinary cosine RAG).
    "diary",
    # K85b personality backlog — a durable trace of something Aiko did in
    # her own time that she has an angle on: a hobby milestone or wrap-up
    # (:class:`app.core.proactive.hobby_worker.HobbyWorker`) or a
    # substantive away beat -- one that changed her room, ran as a
    # multi-beat episode, or closed the day's intention
    # (:class:`app.core.world.idle_activity_worker.IdleAwayActivityWorker`).
    # Everything her inner life produced before this was written to a
    # ring or a blob that overwrites itself: the away journal keeps 8
    # entries, ``_rotate_hobby`` drops the finished thread and starts the
    # counter at zero. Nothing survived long enough to become a concept,
    # which is why ``taste`` has two rows. This kind is the supply line
    # the ``pursuit`` concept kind mines. Carries ``{source, topic,
    # noted_at}`` in the ``metadata`` JSON column; long_term tier, since
    # the whole point is that it outlives the ring it came from.
    "pursuit_note",
}

# L13 — the first-person kinds whose writes get a ``metadata.affect`` stamp
# (Aiko's self-narrative population; mirrors the concept layer's
# ``AIKO_SELF_KINDS``).
_AFFECT_STAMP_KINDS = ("self", "reflection", "diary")


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
    # Schema v30 (F16) — testimony vs. inference (see :data:`VALID_PROVENANCE`).
    # ``inferred`` (default) = Aiko concluded it, so retrieval demotes it a
    # hair at equal cosine and the prompt tags it ``(inferred)`` to phrase
    # it as an impression; ``stated`` = the user said it outright (an
    # explicit ``[[remember:]]`` tag, a manual UI add, or a fact confirmed
    # by an F13 correction). Legacy rows default to ``inferred``.
    provenance: str = _DEFAULT_PROVENANCE

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
            "provenance": str(self.provenance),
        }


@dataclass(slots=True)
class SearchHit:
    memory: Memory
    score: float


# Kinds the conversational extractor writes, and the only ones the
# narrow restatement gate applies to. These are the rows that get
# re-derived from the transcript every turn the subject comes up, so
# "the same thing again, worded differently" is their characteristic
# failure -- and nothing downstream depends on ``add`` handing back a
# row for them.
#
# Worker-written kinds stay out on purpose. ``knowledge_gap``,
# ``open_question``, ``callback`` and friends are minted deliberately,
# one per distinct subject, by producers that run their own inventory
# caps and read the returned row to know the write landed. Merging two
# of those would both lose a distinct item and look like a failed write.
_RESTATE_KINDS: frozenset[str] = frozenset({
    "fact", "event", "preference", "self", "relationship",
})


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


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


def _normalize_cap(value: int | None) -> int | None:
    """Return the effective tier cap: ``None`` means no cap at all.

    ``0`` (or anything below it) switches eviction off for that tier;
    every other value keeps the old floor of 50, so a mistyped ``5``
    still can't empty the store on the next prune.

    Uncapped is the shipped default. The relational store is nowhere
    near being the limit -- an aggregate over 50k rows is ~2.5 ms --
    but the in-process mirror is linear in row count, so growing the
    corpus buys startup time and RSS rather than query time. See P30
    for the measured curve and the three fixes that flatten it.
    """
    if value is None:
        return None
    n = int(value)
    return None if n <= 0 else max(50, n)


def _apply_text_query(mems: list["Memory"], q: str | None) -> list["Memory"]:
    """Narrow ``mems`` to rows whose content matches ``q``.

    Searches ``content`` only. Not ``kind`` or ``tier``: those have their
    own filters, and folding them into the text match would make
    ``preference`` match every row of that kind and quietly swamp the
    result the user was actually looking for.

    Returns the input list unchanged for a blank query, so the common
    case costs one ``None`` check rather than a full walk.
    """
    query = compile_query(q)
    if query is None:
        return mems
    return [m for m in mems if query.matches(m.content)]


class MemoryStore:
    """Thread-safe long-term memory backed by the ``memories`` SQLite table.

    The ``memories`` table is created by :class:`ChatDatabase` (schema v3).
    This class is a focused lens on that one table -- no foreign-key joins.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_memories: int = 0,
        scratchpad_cap: int = 0,
        archive_cap: int = 0,
        dedupe_threshold: float = 0.92,
        restate_threshold: float = 0.85,
        restate_window_hours: float = 6.0,
    ) -> None:
        self._db_path = db_path
        self._max = _normalize_cap(max_memories)
        # Per-tier caps (schema v8). The long_term cap reuses ``_max``
        # for backward compat with the existing ``max_memories``
        # setting. ``prune()`` enforces these independently per tier so
        # scratchpad churn never crowds verified long-term anchors.
        # ``None`` for a tier means it is never evicted from.
        self._tier_caps: dict[str, int | None] = {
            "scratchpad": _normalize_cap(scratchpad_cap),
            "long_term": self._max,
            "archive": _normalize_cap(archive_cap),
        }
        self._dedupe_threshold = float(dedupe_threshold)
        # The narrow second gate for a fact restated minutes later. See
        # ``_is_restatement`` for why it needs a window as well as a
        # floor, and set the window to 0 to switch it off entirely.
        self._restate_threshold = float(restate_threshold)
        self._restate_window_hours = max(0.0, float(restate_window_hours))
        self._local = threading.local()
        self._lock = threading.Lock()
        # In-memory mirror so cosine search is a single NumPy pass.
        self._mirror: dict[int, Memory] = {}
        # The same rows' embeddings as one contiguous matrix. Kept beside
        # the mirror rather than derived from it on demand, because the
        # two callers that need it (``search`` and ``add``'s dedupe) run
        # often enough that rebuilding per call would cost more than the
        # loop it replaces. Mutated at exactly the four places ``_mirror``
        # is, always under ``_lock``.
        self._vectors = VectorIndex()
        self._rag: "RagStore | None" = None
        # Listeners notified after each successful ``delete``. Used by
        # the F5 :class:`app.core.memory.memory_conflict_store.MemoryConflictStore`
        # to cascade-clean any conflict pair that referenced the
        # deleted row. Listeners run synchronously on the caller
        # thread and any exception is swallowed so a buggy listener
        # cannot break a legit delete.
        self._delete_listeners: list[Any] = []
        # Symmetric add listeners: ``callback(memory: Memory)`` fired
        # AFTER a genuinely new row is inserted (not on a dedupe-bump).
        # Used by the topic graph for incremental cluster assignment so
        # a new memory never triggers a full re-cluster. Fired outside
        # the store lock (same discipline as delete listeners).
        self._added_listeners: list[Any] = []
        # K76 flashbulb encoding — optional affect hook. ``_flashbulb_
        # provider`` returns the live ``(arousal, episode_intensity)`` at
        # write time; when enabled, ``add`` boosts a new row's salience by
        # the emotional charge. Off by default; wired by SessionController
        # via ``set_flashbulb``. Reading affect is the caller's concern, so
        # MemoryStore stays decoupled from AffectState / K57.
        self._flashbulb_provider: Any = None
        self._flashbulb_enabled = False
        self._flashbulb_max_boost = 0.35
        self._flashbulb_arousal_weight = 0.6
        self._flashbulb_episode_weight = 0.7
        self._flashbulb_arousal_neutral = 0.4
        self._flashbulb_min_charge = 0.05
        # L13 affect stamping — optional ``() -> (valence, arousal)`` hook.
        # When enabled, ``self`` / ``reflection`` / ``diary`` writes stamp
        # ``metadata.affect`` with the tone of the moment (the self-narrative
        # signal the aiko affective pass reads). Off by default; wired by
        # SessionController via ``set_affect_provider``.
        self._affect_provider: Any = None
        self._affect_provider_enabled = False
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

    def set_flashbulb(
        self,
        provider: Any,
        *,
        enabled: bool = True,
        max_boost: float = 0.35,
        arousal_weight: float = 0.6,
        episode_weight: float = 0.7,
        arousal_neutral: float = 0.4,
        min_charge: float = 0.05,
    ) -> None:
        """Wire K76 flashbulb encoding.

        ``provider()`` must return ``(arousal, episode_intensity)`` floats
        (the live affect at write time). When ``enabled``, every non-pinned
        ``add`` boosts the new row's salience by the emotional charge and
        stamps ``metadata.affect_at_encoding``. Pass ``enabled=False`` to
        disable without dropping the provider.
        """
        self._flashbulb_provider = provider
        self._flashbulb_enabled = bool(enabled)
        self._flashbulb_max_boost = float(max_boost)
        self._flashbulb_arousal_weight = float(arousal_weight)
        self._flashbulb_episode_weight = float(episode_weight)
        self._flashbulb_arousal_neutral = float(arousal_neutral)
        self._flashbulb_min_charge = float(min_charge)

    def set_affect_provider(self, provider: Any, *, enabled: bool = True) -> None:
        """Wire L13 self-memory affect stamping.

        ``provider()`` must return ``(valence, arousal)`` floats (Aiko's live
        affect at write time). When ``enabled``, every ``self`` /
        ``reflection`` / ``diary`` ``add`` stamps ``metadata.affect`` so the
        aiko affective pass can aggregate a self-theme's typical tone. Pass
        ``enabled=False`` to disable without dropping the provider.
        """
        self._affect_provider = provider
        self._affect_provider_enabled = bool(enabled)

    def add_memory_listener(self, callback: Any) -> None:
        """Register ``callback(memory: Memory)`` invoked after a new insert."""
        if callback is not None and callback not in self._added_listeners:
            self._added_listeners.append(callback)

    def remove_memory_listener(self, callback: Any) -> None:
        try:
            self._added_listeners.remove(callback)
        except ValueError:
            pass

    def set_tier_caps(
        self,
        *,
        scratchpad: int | None = None,
        long_term: int | None = None,
        archive: int | None = None,
    ) -> None:
        """Update tier caps at runtime (e.g. when settings change).

        ``0`` lifts the cap on that tier; ``None`` leaves it as it is.
        """
        if scratchpad is not None:
            self._tier_caps["scratchpad"] = _normalize_cap(scratchpad)
        if long_term is not None:
            self._tier_caps["long_term"] = _normalize_cap(long_term)
            self._max = self._tier_caps["long_term"]
        if archive is not None:
            self._tier_caps["archive"] = _normalize_cap(archive)

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
        # Try the v30 shape first (… + provenance). Fall back through v10
        # (no provenance), v9 (no temporal), v8 (no confidence), v7 (no
        # tier/revival), v6 (no metadata). Pre-v6 databases land in the
        # bottom-most ``except`` and start with an empty mirror.
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned, "
                "metadata, tier, revival_score, confidence, "
                "event_time, temporal_type, relevance_until, provenance FROM memories"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self._reload_mirror_pre_v30(conn)
            if rows is None:
                with self._lock:
                    self._mirror = {}
                    self._vectors.rebuild(())
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
                    provenance=_coerce_provenance(r[18]),
                )
                for r in rows
            }
            self._vectors.rebuild(
                (m.id, m.embedding) for m in self._mirror.values()
            )
        log.info("memory store loaded with %d memories", len(self._mirror))

    def _reload_mirror_pre_v30(
        self, conn: sqlite3.Connection,
    ) -> list[tuple] | None:
        """Read a pre-v30 ``memories`` table and pad every row to the v30
        column shape (19 tuple slots, provenance last).

        Walks the same descending fallback ladder ``_reload_mirror`` used
        before F16: v10 (no provenance), v9 (no temporal), v8 (no
        confidence), v7 (no tier/revival), v6 (no metadata). Each older
        shape appends the columns added since, ending with the v30
        ``provenance`` default so the caller's ``Memory(...)`` construction
        is shape-agnostic. Returns ``None`` for a pre-v6 database that has
        no readable memory columns at all (the caller empties the mirror).
        """
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned, "
                "metadata, tier, revival_score, confidence, "
                "event_time, temporal_type, relevance_until FROM memories"
            ).fetchall()
            # Append default provenance for pre-v30 rows.
            return [(*r, _DEFAULT_PROVENANCE) for r in rows]
        except sqlite3.OperationalError:
            pass
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned, "
                "metadata, tier, revival_score, confidence FROM memories"
            ).fetchall()
            # Append default temporal + provenance fields for pre-v10 rows.
            return [
                (*r, None, _DEFAULT_TEMPORAL_TYPE, None, _DEFAULT_PROVENANCE)
                for r in rows
            ]
        except sqlite3.OperationalError:
            pass
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned, "
                "metadata, tier, revival_score FROM memories"
            ).fetchall()
            # Append default confidence + temporal + provenance for pre-v9 rows.
            return [
                (*r, 0.7, None, _DEFAULT_TEMPORAL_TYPE, None, _DEFAULT_PROVENANCE)
                for r in rows
            ]
        except sqlite3.OperationalError:
            pass
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned, "
                "metadata FROM memories"
            ).fetchall()
            # Append default (tier, revival_score, confidence, event_time,
            # temporal_type, relevance_until, provenance) for pre-v8 rows.
            return [
                (
                    *r,
                    _DEFAULT_TIER,
                    0.0,
                    0.7,
                    None,
                    _DEFAULT_TEMPORAL_TYPE,
                    None,
                    _DEFAULT_PROVENANCE,
                )
                for r in rows
            ]
        except sqlite3.OperationalError:
            pass
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count, pinned "
                "FROM memories"
            ).fetchall()
            return [
                (
                    *r,
                    None,
                    _DEFAULT_TIER,
                    0.0,
                    0.7,
                    None,
                    _DEFAULT_TEMPORAL_TYPE,
                    None,
                    _DEFAULT_PROVENANCE,
                )
                for r in rows
            ]
        except sqlite3.OperationalError:
            return None

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
        provenance: str | None = None,
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

        ``provenance`` is the v30 F16 testimony-vs-inference label (see
        :data:`VALID_PROVENANCE`). ``None`` (and anything unknown) coerces
        to ``'inferred'`` — the safe default, since over-claiming testimony
        is the failure this fixes. The deliberate write paths (explicit
        ``[[remember:]]`` tags, manual UI adds, F13-confirmed corrections)
        pass ``'stated'`` explicitly.
        """
        cleaned = (content or "").strip()
        if not cleaned or len(cleaned) < 4:
            return None
        kind = kind.strip().lower() or "fact"
        if kind not in VALID_KINDS:
            kind = "fact"
        salience_clipped = max(0.0, min(1.0, float(salience)))
        # K76 flashbulb encoding: boost salience by the live emotional
        # charge at write time. Best-effort — a broken provider must never
        # break a memory write. Pinned rows are skipped (already special).
        if (
            self._flashbulb_enabled
            and self._flashbulb_provider is not None
            and not pinned
        ):
            try:
                from app.core.memory import flashbulb as _fb

                arousal, episode_intensity = self._flashbulb_provider()
                result = _fb.apply_flashbulb(
                    salience_clipped,
                    arousal=float(arousal),
                    episode_intensity=float(episode_intensity),
                    max_boost=self._flashbulb_max_boost,
                    arousal_weight=self._flashbulb_arousal_weight,
                    episode_weight=self._flashbulb_episode_weight,
                    arousal_neutral=self._flashbulb_arousal_neutral,
                )
                salience_clipped = result.salience
                if result.charge >= self._flashbulb_min_charge:
                    metadata = {
                        **(metadata or {}),
                        "affect_at_encoding": {
                            "arousal": round(float(arousal), 3),
                            "episode_intensity": round(
                                float(episode_intensity), 3
                            ),
                            "charge": round(result.charge, 3),
                            "boost": round(result.boost, 3),
                        },
                    }
            except Exception:
                log.debug("flashbulb salience hook failed", exc_info=True)
        # L13 self-memory affect stamping — record Aiko's live (valence,
        # arousal) on her first-person memories so the aiko affective pass
        # can aggregate a self-theme's typical tone. Best-effort.
        if (
            self._affect_provider_enabled
            and self._affect_provider is not None
            and kind in _AFFECT_STAMP_KINDS
        ):
            try:
                valence, arousal = self._affect_provider()
                metadata = {
                    **(metadata or {}),
                    "affect": {
                        "valence": round(float(valence), 3),
                        "arousal": round(float(arousal), 3),
                    },
                }
            except Exception:
                log.debug("affect stamp hook failed", exc_info=True)
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
        provenance_normalized = _coerce_provenance(provenance)
        event_time_clean = (
            event_time.strip()
            if isinstance(event_time, str) and event_time.strip()
            else None
        )
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
            written_at = timephrase.utcnow()
            # Neither gate can fire below the lower of the two thresholds
            # (``_is_restatement`` returns early under ``_restate_threshold``),
            # so one matmul plus a walk of whatever cleared that floor
            # replaces a cosine call per row. Usually nothing clears it.
            floor = min(self._dedupe_threshold, self._restate_threshold)
            with self._lock:
                # Descending score rather than insertion order: where the
                # old loop merged into the first row it happened to visit,
                # this merges into the closest one.
                for mid, score in self._vectors.above(emb, floor):
                    mem = self._mirror.get(mid)
                    if mem is None:
                        continue
                    if score >= self._dedupe_threshold or self._is_restatement(
                        mem,
                        score,
                        content=cleaned,
                        kind=kind,
                        temporal_type=temporal_type_normalized,
                        written_at=written_at,
                    ):
                        dup_id = mem.id
                        break
        if dup_id is not None:
            self._touch_existing(dup_id, salience_clipped)
            return None

        # Real insert.
        conn = self._get_conn()
        now = _now_iso()
        # K-time10 backstop. A note worded "today" / "currently" is only
        # true on the day it was written, but ``durable`` is the *default*
        # temporal type and renders with no time tag at all -- so such a
        # row keeps reaching the prompt months later still asserting the
        # present. Re-reading it as an event anchored at write time is the
        # honest interpretation, and it makes retrieval tag the bullet
        # "(N days ago)" instead of leaving it bare.
        #
        # This lives at the store rather than in each producer's prompt on
        # purpose: every long-term write funnels through here, so one
        # branch covers all ~35 producers and anything added later.
        # Prompts should still be taught not to write the phrase (that is
        # the K-time worker toolkit's job); this only catches what slips
        # past. The text itself is never rewritten -- editing what was
        # recorded to satisfy a regex would be worse than mis-tagging it.
        #
        # H40 split it by direction. The original read *any* deictic as
        # evidence of a past event, but five of the eighteen words point
        # the other way, so "the courier comes tomorrow" was filed as
        # something that had already happened and stamped at write time.
        # That is the worst available outcome: no upkeep pass looks at
        # ``past_event``, the upcoming-horizon block only reads
        # ``future_plan``, and the bullet rendered as though the courier
        # had just been. Note the asymmetry in what each branch may
        # invent -- a past deictic licenses "it happened when this was
        # written", which is true by construction, while a future one
        # licenses no timestamp at all: "soon" and "next week" do not
        # name a moment, and guessing one is the same fabrication in a
        # different costume. A clockless ``future_plan`` is handled: the
        # decay worker retires it on ``relevance_until``.
        type_before = temporal_type_normalized
        direction = timephrase.deictic_direction(cleaned)
        if temporal_type_normalized in ("durable", "preference") and direction:
            if direction == timephrase.FUTURE:
                temporal_type_normalized = "future_plan"
            else:
                temporal_type_normalized = "past_event"
                if event_time_clean is None:
                    event_time_clean = now
            log.debug(
                "memory reclassified %s -> %s (%s wording): %s",
                temporal_type,
                temporal_type_normalized,
                direction,
                cleaned[:80],
            )

        # Direction validation, which nothing did before H40: a
        # ``past_event`` dated after the moment it was recorded is not a
        # tolerable rounding error, it is two fields disagreeing about
        # whether the thing has happened. Believe ``event_time`` -- it is
        # the more specific claim, and the label is the field producers
        # get wrong (only 17 of 2,095 rows ever reached ``future_plan``,
        # while 54 plans sat in ``past_event`` dated into their own
        # future). Deliberately compares against the write time and not
        # against "now", so replaying an old row cannot re-decide it.
        if (
            temporal_type_normalized == "past_event"
            and event_time_clean is not None
        ):
            stamped = timephrase.parse_iso(event_time_clean)
            written = timephrase.parse_iso(now)
            if stamped is not None and written is not None and stamped > written:
                log.info(
                    "memory past_event dated after its own write -> "
                    "future_plan (event_time=%s written=%s): %s",
                    event_time_clean, now, cleaned[:80],
                )
                temporal_type_normalized = "future_plan"

        # A type carries an expiry rule, so changing the type invalidates
        # whatever the caller derived from the old one. Recomputing is not
        # optional bookkeeping: ``list_by_temporal_type`` skips rows whose
        # ``relevance_until`` is NULL, and ``durable`` derives NULL -- so a
        # row promoted out of ``durable`` without this would be invisible
        # to every upkeep pass that could ever retire it.
        if temporal_type_normalized != type_before:
            written = timephrase.parse_iso(now)
            if written is not None:
                relevance_until_clean = derive_relevance_until(
                    temporal_type_normalized,
                    event_time=timephrase.parse_iso(event_time_clean),
                    created_at=written,
                )
        meta_json = _encode_metadata(metadata)
        pinned_int = 1 if pinned else 0
        cursor = conn.execute(
            "INSERT INTO memories ("
            "  content, kind, salience, embedding, source_session, "
            "  source_message_id, created_at, last_used_at, use_count, pinned, "
            "  metadata, tier, revival_score, confidence, "
            "  event_time, temporal_type, relevance_until, provenance"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0.0, ?, ?, ?, ?, ?)",
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
                provenance_normalized,
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
            provenance=provenance_normalized,
        )
        with self._lock:
            self._mirror[new_id] = memory
            self._vectors.add(new_id, emb)
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
        # Notify add listeners (topic-graph incremental assignment). Fired
        # before the opportunistic prune so the new row is settled in both
        # the mirror and the LanceDB ANN table when the listener runs.
        if self._added_listeners:
            for listener in list(self._added_listeners):
                try:
                    listener(memory)
                except Exception:
                    log.debug(
                        "memory add listener raised for id=%s",
                        new_id,
                        exc_info=True,
                    )
        # Per-tier opportunistic prune. Cheaper to check the just-grown
        # tier than to walk every row -- and when the tier is uncapped,
        # cheaper still to skip the count: this runs on every single
        # write, so an O(n) scan here is the one place where a larger
        # corpus would slow down the turn itself.
        cap = self._tier_caps.get(tier_normalized, self._max)
        if cap is not None:
            with self._lock:
                tier_count = sum(
                    1 for m in self._mirror.values() if m.tier == tier_normalized
                )
            if tier_count > cap:
                self.prune()
        return memory

    def _is_restatement(
        self,
        mem: "Memory",
        score: float,
        *,
        content: str,
        kind: str,
        temporal_type: str,
        written_at: datetime,
    ) -> bool:
        """Is this the same thing said again a few minutes later?

        The global :attr:`_dedupe_threshold` has to stay high because it
        compares across every kind and every age, where a false merge
        silently destroys a distinct memory. That leaves a gap the
        extractor drives a truck through: the same fact restated with
        slightly different wording minutes apart lands at 0.85-0.90 and
        gets its own row, so one plan became six ("Jacob's cookies will
        arrive in a few days" / "Jacob expects his cookie order to
        arrive in a few days"). Downstream that is not merely bloat --
        every consumer keyed on memory id treats them as separate
        subjects, which is how a single plan produced three identical
        forward-curiosity questions an hour apart.

        Four conditions narrow the lower threshold to where a merge is
        safe. The kind is one the extractor writes (see
        ``_RESTATE_KINDS``). Same ``kind`` and same ``temporal_type``,
        because a plan and the past event it became are genuinely
        different rows even when they are worded almost identically. And
        written inside ``_restate_window_hours``, which is the condition
        that does the real work: measured over the live store, same-kind
        pairs inside a few hours are overwhelmingly restatements, while
        the similarly-scoring pairs a day or more apart are distinct
        facts that happen to share a frame ("allergies make breathing
        hard outdoors" and "allergies improve after rain" sit at 0.82).

        The fifth condition is a **contradiction guard**, and it is the
        one that makes the other four safe to lower a threshold behind.
        F5's conflict detector reads the ``[0.80, 0.92)`` band precisely
        because that is where a contradiction lives -- a negation flip
        barely moves an embedding, so "Jacob loves spicy food" and
        "Jacob does not like spicy food" sit *above* this gate's floor.
        Merging those keeps the older row and silently discards the
        correction, which is the worst outcome available here and the
        exact opposite of what the newer statement means. So the same
        pure-Python heuristic F5 uses adjudicates the pair, and anything
        it does not label ``no`` is left as two rows for F5 to resolve
        properly. It runs last because it is the only string-level check
        in the chain, and by this point at most a handful of rows per
        write have got past the vector, kind and window gates.
        """
        if score < self._restate_threshold or self._restate_window_hours <= 0.0:
            return False
        if kind not in _RESTATE_KINDS:
            return False
        if mem.kind != kind or mem.temporal_type != temporal_type:
            return False
        created = timephrase.parse_iso(mem.created_at)
        if created is None:
            return False
        gap_hours = abs((written_at - created).total_seconds()) / 3600.0
        if gap_hours > self._restate_window_hours:
            return False
        return classify_pair(content, mem.content).label == HEURISTIC_NO

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
            self._vectors.remove(int(memory_id))
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
            # Unconditional: overwriting one row costs a dim-length copy,
            # which is cheaper than working out whether it changed.
            self._vectors.add(int(memory_id), new_embedding)
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

    def get_many(self, memory_ids: Iterable[int]) -> dict[int, Memory]:
        """Batch variant of :meth:`get`: one lock acquisition, ``{id: Memory}``.

        The RAG hot loop (:meth:`RagRetriever.retrieve`) calls this once per
        turn instead of one locked ``get`` per Lance hit (P4). Ids that don't
        resolve (missing / non-integer) are simply absent from the result.
        """
        out: dict[int, Memory] = {}
        with self._lock:
            for raw in memory_ids:
                try:
                    mid = int(raw)
                except (TypeError, ValueError):
                    continue
                mem = self._mirror.get(mid)
                if mem is not None:
                    out[mid] = mem
        return out

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
        now_dt = now or timephrase.utcnow()

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

        A tier whose cap is ``None`` is never pruned, and when every
        tier is uncapped this returns 0 without touching the mirror.
        """
        if all(self._tier_caps.get(t, self._max) is None for t in VALID_TIERS):
            return 0
        total_victims = 0
        with self._lock:
            snapshot = list(self._mirror.values())
        for tier in VALID_TIERS:
            cap = self._tier_caps.get(tier, self._max)
            if cap is None:
                continue
            tier_rows = [m for m in snapshot if m.tier == tier and not m.pinned]
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
                    self._vectors.remove(mid)
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
            ids, raw = self._vectors.scores(query_embedding)
            if ids.size == 0:
                return []
            # Threshold on the raw cosine, exactly as the per-row loop
            # this replaces did, so the salience boost can reorder the
            # survivors but never admit a row that did not qualify.
            keep = np.flatnonzero(raw >= float(min_score))
            if keep.size == 0:
                return []
            scored: list[SearchHit] = []
            for i in keep:
                mem = self._mirror.get(int(ids[i]))
                if mem is None:
                    continue
                # Light salience boost so two similar memories prefer the
                # more salient one.
                scored.append(
                    SearchHit(
                        memory=mem,
                        score=float(raw[i]) + 0.05 * (mem.salience - 0.5),
                    )
                )
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[: max(1, int(top_k))]

    def list_recent(
        self,
        limit: int = 50,
        *,
        offset: int = 0,
        kind: str | None = None,
        tier: str | None = None,
        q: str | None = None,
    ) -> list[Memory]:
        # P33: filter by kind *inside* the lock so a kind-scoped call
        # neither copies the whole mirror nor sorts rows it will discard.
        # At the ~16k-row ceiling an unfiltered copy + two sorts to return
        # 3 catchphrases was the shape this replaces.
        kind_norm = kind.strip().lower() if kind else ""
        with self._lock:
            if kind_norm:
                mems = [m for m in self._mirror.values() if m.kind == kind_norm]
            else:
                mems = list(self._mirror.values())
        # Tier filter must run BEFORE the offset/limit slice so pagination
        # matches ``count_memories(tier=...)`` — otherwise a tier filter
        # applied to an already-paginated page only catches the rows of
        # that tier that happen to fall in the current window (archive
        # rows sort to the bottom, so the first page showed ~none).
        if tier:
            tier_norm = tier.strip().lower()
            mems = [m for m in mems if m.tier == tier_norm]
        mems = _apply_text_query(mems, q)
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
        tier: str | None = None,
        q: str | None = None,
    ) -> list[Memory]:
        # P33: see ``list_recent`` — kind filtering happens under the lock,
        # before the sort.
        kind_norm = kind.strip().lower() if kind else ""
        with self._lock:
            if kind_norm:
                mems = [m for m in self._mirror.values() if m.kind == kind_norm]
            else:
                mems = list(self._mirror.values())
        # See ``list_recent``: filter tier before the slice so pagination
        # is consistent with ``count_memories``.
        if tier:
            tier_norm = tier.strip().lower()
            mems = [m for m in mems if m.tier == tier_norm]
        mems = _apply_text_query(mems, q)
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

    def iter_by_kinds(self, kinds: Iterable[str]) -> list[Memory]:
        """Snapshot of all memories whose kind is in ``kinds``.

        One locked mirror walk, no sort -- the plural sibling of
        :meth:`iter_by_kind`. The K22 callback detector (P17) calls this
        instead of ``list_recent(limit=10_000)``: that path copied the
        *entire* mirror and paid two O(n log n) sorts before the detector
        discarded every non-callback-kind row anyway. Filtering to the
        allow-list here means the per-turn cosine walk only ever touches
        eligible rows (facts / preferences / shared moments / …), not the
        high-volume observation / knowledge-gap / scratchpad bulk.

        Empty / falsy ``kinds`` returns ``[]`` (no implicit "all").
        """
        kind_set = {
            k.strip().lower() for k in kinds if k and str(k).strip()
        }
        if not kind_set:
            return []
        with self._lock:
            return [m for m in self._mirror.values() if m.kind in kind_set]

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
        q: str | None = None,
    ) -> int:
        """Rows matching the filters. Must accept every filter the list
        methods do, or the pager lies: ``total`` drives the page count, so
        a filter the count ignores produces pages that render empty.
        """
        with self._lock:
            mems = list(self._mirror.values())
        if kind:
            kind_norm = kind.strip().lower()
            mems = [m for m in mems if m.kind == kind_norm]
        if tier:
            tier_norm = tier.strip().lower()
            mems = [m for m in mems if m.tier == tier_norm]
        mems = _apply_text_query(mems, q)
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

    def earliest_created_at(self) -> str | None:
        """ISO timestamp of the oldest memory (``None`` when empty).

        A cheap "how much history exists" signal for cold-start gates
        (L21). Scans the in-process mirror, so it costs O(n) but only
        gets called on slow idle paths, never the per-turn hot path.
        """
        with self._lock:
            if not self._mirror:
                return None
            return min(
                (m.created_at for m in self._mirror.values() if m.created_at),
                default=None,
            )
