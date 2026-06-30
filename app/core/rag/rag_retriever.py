"""Multi-source retrieval over the LanceDB :class:`RagStore`.

Supersedes :class:`app.core.memory.memory_retriever.MemoryRetriever`. Searches three
tables in parallel and merges the hits with source-aware scoring:

  - ``memories`` (durable cross-session facts; high prior weight)
  - ``messages`` (chat history embeddings; recency-aware)
  - ``documents`` (uploaded notes / PDFs)

The output is a structured ``list[RagHit]`` *plus* a ready-to-paste prompt
block. The prompt block intentionally splits "What you know about Jacob"
(memories) from "Snippets you remembered" (messages) and "From your notes"
(documents), so the LLM can use them with appropriate confidence.

Design notes:
  - Memory hits get a small salience boost inside :class:`RagStore`; we
    additionally bias message hits down so a strong memory always wins ties.
  - We dedupe by content-text after merging.
  - Recency is folded into message scores via an exponential decay on
    ``created_at``; older messages are penalized so RAG doesn't unearth a
    five-month-old line on every turn.
  - H1 + K4: when an arc-state provider and chat_db are wired, hits whose
    source ``messages`` row matches the *current* arc / dialogue_act get
    a small boost (capped at ``+0.05`` combined). Two cheap dict lookups
    per hit -- the join is a single SQL ``IN (...)`` query batched across
    all surfaced hits.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

import numpy as np

from app.core.infra import timephrase as _tp
from app.core.infra.time_expr import TimeWindow, parse_time_window
from app.core.rag.rag_store import MessageRecord, RagHit

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.conversation.conversation_arc import ArcState
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.goals.goal_store import GoalStore
    from app.core.memory.memory_store import MemoryStore
    from app.core.rag.rag_store import RagStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.rag_retriever")


# Tuning knobs. Kept small so we can iterate without breaking callers.
_MEMORY_PRIOR = 0.05
_MESSAGE_PRIOR = -0.04
_DOCUMENT_PRIOR = 0.0
# H4 — document-recall recency boost. A document chunk whose ``created_at``
# is within the last ``_DOCUMENT_RECENCY_DAYS`` days gets a flat additive
# ``_DOCUMENT_RECENCY_BONUS`` so freshly-uploaded notes/PDFs surface
# preferentially before they fade into the long-term pool. Same magnitude as
# the pinned / knowledge nudges so it nudges near-ties without overpowering
# raw cosine relevance; documents have no salience/decay of their own, so a
# flat in-window bonus is the cheapest way to give recent uploads an edge.
_DOCUMENT_RECENCY_DAYS = 7.0
_DOCUMENT_RECENCY_BONUS = 0.05
# Half-life in days for message recency decay; messages older than ~3 weeks
# get heavily penalized in the merged score.
_MESSAGE_HALFLIFE_DAYS = 21.0

# Memory recency / revival tuning. The plain cosine + salience scoring is
# stateless across turns, so a single high-salience callback would surface
# every turn until the user manually deletes it. These two adjustments fix
# that without weakening relevance:
#
#   * If a memory was last surfaced within ``_MEMORY_RECENCY_PENALTY_HOURS``
#     we subtract ``_MEMORY_RECENCY_PENALTY`` from its score so the
#     second-best candidate gets a real chance to win.
#   * If a memory was used at least once but hasn't been touched in
#     ``_MEMORY_REVIVAL_DAYS`` days, we add ``_MEMORY_REVIVAL_BONUS`` so
#     stale threads can re-emerge with an "oh, whatever happened with…"
#     framing instead of being lost forever under fresher hits.
#
# The deltas are intentionally smaller than the typical cosine gap between
# top results (~0.05-0.10) so the recency signal nudges ordering without
# overpowering relevance. ``_MEMORY_RECENCY_PENALTY`` is roughly twice the
# revival bonus because suppressing a stale repeat is more valuable than
# resurrecting a dormant one.
_MEMORY_RECENCY_PENALTY_HOURS = 6.0
_MEMORY_RECENCY_PENALTY = 0.08
_MEMORY_REVIVAL_DAYS = 7.0
_MEMORY_REVIVAL_BONUS = 0.04
# Bonus for memories the user has explicitly pinned via the Memory tab.
# Pinning is a curation signal ("I want this surfaced when relevant"); a
# small additive nudge gives pinned hits the edge over equally-similar
# unpinned siblings without overpowering raw cosine relevance.
_MEMORY_PINNED_BONUS = 0.05
# Bonus for ``shared_moment`` memories whose ``metadata.when`` matches an
# anniversary window today (1mo / 3mo / 6mo / 1yr / Nyr). The size is the
# same as the pinned bonus so a moment having its anniversary also gets
# a small leg-up against an equally-similar non-anniversary sibling.
_MEMORY_ANNIVERSARY_BONUS = 0.05
_ANNIVERSARY_WINDOW_DAYS: tuple[int, ...] = (30, 90, 180, 365, 730, 1095, 1460, 1825)
_ANNIVERSARY_TOLERANCE_DAYS = 1.0

# Schema v8 — per-tier score offset applied to memory hits.
# ``scratchpad`` rows are still probationary and pre-revival, so we
# bias them slightly down; ``archive`` rows are cold history, so they
# need a strong cosine match to surface (avoids unburying noise on
# weak queries). ``long_term`` is the neutral baseline. The deltas are
# small enough that a revived scratchpad row (revival_score > 0.5) or
# a high-salience archive row still wins against an equally-similar
# long_term sibling.
_MEMORY_TIER_OFFSET: dict[str, float] = {
    "scratchpad": -0.02,
    "long_term": 0.0,
    "archive": -0.03,
}

# Schema v9 — confidence-tier penalty for low-confidence memory hits.
# Memories with ``confidence < 0.5`` get a proportional score penalty
# capped at ``-0.15`` (when confidence == 0). Never hides — just demotes,
# so a strong cosine match on an uncertain memory still surfaces but with
# a small handicap against a high-confidence sibling. Per the F3 backlog
# spec: "never hiding things from Aiko is the simpler invariant".
_MEMORY_CONFIDENCE_PENALTY_THRESHOLD = 0.5
_MEMORY_CONFIDENCE_PENALTY_MAX = 0.15

# H1 + K4 — per-hit boost for source rows that share the current
# conversation arc (H1) or the live user dialogue-act (K4). The deltas
# are intentionally tiny: we want the alignment to nudge ordering when
# cosine scores are near-tied, never to move a weak match past a strong
# one. Combined cap is +0.05 -- a hit matching on both never gets the
# full additive +0.06.
_RAG_ARC_BOOST = 0.03
_RAG_DIALOGUE_ACT_BOOST = 0.03
_RAG_ALIGNMENT_BOOST_CAP = 0.05

# F8 — small additive bonus for ``knowledge``-kind hits (distilled,
# impersonal learned facts) when the live turn is *informational*
# (the K4 dialogue-act tag is ``question``). The point is to let an
# accumulated, queryable knowledge pool win over the model's generic
# parametric knowledge on "what are some good X?" turns, without
# boosting learned facts during emotional / support / banter turns
# where they'd just read as a non-sequitur lecture. Same magnitude as
# the pinned / anniversary nudges so it nudges ordering near-ties
# rather than overpowering raw cosine relevance.
_RAG_KNOWLEDGE_BONUS = 0.05

# K-time2 — additive score bonus for a memory/message hit whose recorded
# date (``created_at`` / ``event_time``) falls inside the relative-time
# window the user's query named ("yesterday", "last week", ...). Larger
# than the knowledge bonus because an explicit time reference is a strong,
# deliberate retrieval signal. Stays a soft boost, not a hard filter, so a
# timezone skew on a day boundary only shifts the nudge.
_RAG_TIME_WINDOW_BONUS = 0.08
# K-time2 direct recall — base score for a message pulled by the direct
# ``[start, end]`` DB lookup (rather than matched on cosine). Sits above
# the score threshold so the actual lines from the named day reliably
# surface for a recall query, but below a strong semantic memory hit
# (cosine > base) so genuine topical matches still rank first. The
# in-window time bonus + the per-message recency bonus ride on top.
_DIRECT_RECALL_BASE = 0.55
# Dialogue acts that count as "informational" for the knowledge boost.
# ``question`` is the K4 label for "asking for information / a soft
# request" (there is no separate ``info_seeking`` act).
_INFORMATIONAL_ACTS = frozenset({"question"})

# K7 — Forgetting protocol defaults. The original implementation only
# fired ``(faded)`` for ``tier=="archive"`` rows; this completion adds a
# graded predicate so long_term rows that have decayed in place
# (``salience`` below the threshold AND idle longer than ``idle_days``)
# also pick up the suffix. The 30-180 day window between "decayed" and
# "demoted to archive" was passing through with no hedge.
#
# Defaults are quiet on purpose: with the long_term decay rate of
# 0.02/day, a fresh ``salience=0.5`` row hits the 0.20 threshold around
# day 15; combined with the 30-day idle floor, only rows that genuinely
# haven't surfaced in over a month qualify. The master switch
# ``fade_hedge_enabled`` disables every ``(faded)`` suffix (including
# archive) for users who want sharp memories only.
_FADED_DEFAULT_SALIENCE_THRESHOLD = 0.20
_FADED_DEFAULT_IDLE_DAYS = 30


def _is_faded_memory(
    *,
    tier: str | None,
    salience: float | None,
    last_used_at: str | None,
    created_at: str | None,
    now: datetime,
    salience_threshold: float,
    idle_days: int,
) -> bool:
    """K7 graded fade predicate.

    Returns ``True`` when the row should pick up the ``(faded)``
    suffix. Two cases:

    - ``tier == "archive"`` — always faded (cold history that survived
      the score offset because the cosine match was strong).
    - ``tier == "long_term"`` (or missing) AND salience below the
      threshold AND last touched longer than ``idle_days`` ago — the
      slow-decay-in-place case.

    Scratchpad never fades: that tier has its own lifecycle
    (``scratchpad_ttl_days`` prunes; promotion lifts the warm ones)
    and conflating "raw new observation" with "old half-forgotten"
    muddies two different signals.
    """
    if tier == "archive":
        return True
    if tier not in (None, "long_term"):
        return False
    if salience is None:
        return False
    if float(salience) >= float(salience_threshold):
        return False
    ts = last_used_at or created_at
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    # ``timedelta.days`` floors, so a strict ``>`` reads correctly:
    # "more than N days idle" means at least N+1 calendar days.
    return (now - last).days > int(idle_days)


# ── K25: memory confidence time-decay ─────────────────────────────────
# Read-side time-decay on memory confidence with the new ``(distant)``
# suffix. Distinct from ``(uncertain)`` (which means "stored value is
# already low — claim quality was shaky at write time") and from
# ``(faded)`` (which means "low salience AND idle, so tier/use have
# decayed it"). K25's signal is **raw age**: a 6-month-old default-
# confidence claim that's been used recently is still actively
# retrieved, but Aiko should hedge with time-language ("a while back",
# "don't quote me") rather than quote it as if it were yesterday.
#
# Pure read-side derivation -- no schema change, no decay-writer.
# Each retrieval recomputes ``effective = stored * max(floor, 1 -
# days_since_created / horizon_days)``. Pinned rows bypass (return
# stored as-is) since pin == "user explicitly trusts this".
#
# Defaults at ``horizon_days=365, floor=0.3, threshold=0.5``:
#
# * default-confidence (0.7) memory hits the threshold at ~104 days
# * high-confidence (0.9) memory hits the threshold at ~165 days
# * pinned rows (>=0.9) never trigger regardless of age
#
# The storage column ``memories.confidence`` is left untouched —
# ``_confidence_penalty``, ``MemoryConflictWorker`` and
# ``BeliefGapDetector`` all keep reading the raw stored value.
_CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS = 365
_CONFIDENCE_DECAY_DEFAULT_FLOOR = 0.3
_CONFIDENCE_DECAY_DEFAULT_THRESHOLD = 0.5


def _compute_effective_confidence(
    stored: float,
    *,
    age_days: float,
    horizon_days: int,
    floor: float,
) -> float:
    """Linear-with-floor time decay on a stored confidence value.

    Multiplier ramps from ``1.0`` at age ``0`` down to ``floor`` at
    ``horizon_days``, and clamps at ``floor`` thereafter. The returned
    value is clamped to ``[0.0, 1.0]`` so downstream comparisons stay
    well-behaved regardless of how the caller stored its value.

    ``horizon_days <= 0`` short-circuits to the raw stored value so an
    accidentally-zero config never raises ZeroDivisionError. ``floor``
    is treated literally — a negative floor decays past zero (clamped),
    a floor of ``1.0`` disables decay entirely.
    """
    if horizon_days <= 0:
        return max(0.0, min(1.0, float(stored)))
    multiplier = max(float(floor), 1.0 - float(age_days) / float(horizon_days))
    return max(0.0, min(1.0, float(stored) * multiplier))


def _is_distant_memory(
    *,
    stored_confidence: float | None,
    created_at: str | None,
    now: datetime,
    horizon_days: int,
    floor: float,
    threshold: float,
    pinned: bool,
) -> bool:
    """K25 ``(distant)`` predicate.

    Returns ``True`` when the row's ``effective_confidence`` (after
    age-based decay) falls below ``threshold``. Returns ``False`` —
    no signal — when any of:

    - ``pinned`` is True (user explicitly trusts this row)
    - ``stored_confidence`` is None (cold-start / corrupted row)
    - ``created_at`` is None or unparseable (no age data to work with)

    Mirrors the defensive shape of :func:`_is_faded_memory`: every
    failure mode returns ``False`` rather than raising, since a single
    bad row should never poison the whole RAG render.
    """
    if pinned or stored_confidence is None or created_at is None:
        return False
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    age_seconds = (now - created).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    effective = _compute_effective_confidence(
        float(stored_confidence),
        age_days=age_days,
        horizon_days=horizon_days,
        floor=floor,
    )
    return effective < float(threshold)


# K1 — per-hit boost for memories that semantically align with one of
# Aiko's active long-term goals (cosine >= ``_RAG_GOAL_ALIGNMENT_THRESHOLD``).
# Same posture as the arc / dialogue-act boosts: small enough to nudge
# ordering on near-ties without ever moving a weak match past a strong
# one. We only apply it once per hit, so a hit aligned with two goals
# still gets just the single bonus.
_RAG_GOAL_ALIGNMENT_BOOST = 0.04
_RAG_GOAL_ALIGNMENT_THRESHOLD = 0.55

# K22 — per-hit boost for memories that Aiko has actually managed to
# weave back into a reply (``metadata.callback_count >= 1``). Same
# posture as the pinned / anniversary / goal-alignment bonuses: small
# enough to nudge ordering on near-ties without ever moving a weak
# match past a strong one. Single-step (no per-count scaling) because
# the salience bump applied at callback-record time already provides
# the compounding effect; doing it again here would double-count and
# let "hot-spot" memories permanently dominate the retriever. Bonus
# is always-on once a row has been stamped — the settings only gate
# the *write* side (the post-turn detector); read-side bonuses
# survive even if the user later disables the detector.
_RAG_CALLBACK_BONUS = 0.04


def _confidence_penalty(confidence: float | None) -> float:
    """Return a non-positive penalty for low-confidence memory hits.

    ``confidence >= 0.5`` -> 0.0 (no nudge).
    ``confidence == 0.0`` -> ``-_MEMORY_CONFIDENCE_PENALTY_MAX``.
    Linear ramp in between.
    """
    if confidence is None:
        return 0.0
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if value >= _MEMORY_CONFIDENCE_PENALTY_THRESHOLD:
        return 0.0
    gap = _MEMORY_CONFIDENCE_PENALTY_THRESHOLD - max(0.0, value)
    return -(gap / _MEMORY_CONFIDENCE_PENALTY_THRESHOLD) * _MEMORY_CONFIDENCE_PENALTY_MAX


# Schema v10 — humanized time annotations for retrieved memories.
# ``_humanize_past`` and ``_humanize_future`` return short, natural
# phrases ("yesterday", "3 days ago", "tonight 20:00") that
# :func:`_temporal_suffix` wraps into the bullet annotation Aiko sees
# in the prompt block. The format is intentionally informal — it's
# meant to read like a friend's note ("you mentioned this 3 days
# ago"), not a database timestamp.
_HOUR_SECONDS = 3600.0
_DAY_SECONDS = 86400.0


# K-time5: these relative-time helpers now live in
# ``app.core.infra.timephrase`` (one canonical implementation + the
# process-wide "now" seam the DT1 virtual clock plugs into). They are
# re-exported here under their historical private names so existing
# imports and tests (``from app.core.rag.rag_retriever import
# _humanize_past`` etc.) keep working byte-identically.
_to_aware = _tp.to_aware
_parse_temporal_iso = _tp.parse_iso
_humanize_past = _tp.humanize_past
_humanize_future = _tp.humanize_future


# Schema v10 — score adjustments for temporally-classified memories.
# ``future_plan`` rows whose moment is still ahead get a tiny demotion
# so they don't crowd current-relevance hits unless the cosine match
# is genuinely strong. ``past_event`` rows whose ``relevance_until``
# has already passed are filtered out entirely (they stay in DB for
# archive / reflection use, just not in the live RAG block). The
# magnitudes are deliberately small — the deltas tune ordering, not
# visibility.
_FUTURE_PLAN_PENALTY = -0.05


def _temporal_filter_drops(mem, now: datetime) -> bool:
    """True if the v10 temporal fields say this memory should be skipped.

    Currently only ``past_event`` rows whose ``relevance_until`` is in
    the past are dropped — they've outlived their freshness window
    and continuing to surface them in normal RAG produces the exact
    "asking about progress on something that already finished" bug
    this work targets. Other temporal types are kept.
    """
    temporal_type = getattr(mem, "temporal_type", None)
    if temporal_type != "past_event":
        return False
    relevance_until = getattr(mem, "relevance_until", None)
    if not relevance_until:
        return False
    until = _parse_temporal_iso(relevance_until)
    if until is None:
        return False
    return until < _to_aware(now)


def _temporal_boost(mem) -> float:
    """Score adjustment derived from the v10 temporal classification.

    ``future_plan`` rows get a small demotion so an upcoming-but-not-
    yet-arrived plan doesn't crowd a current-relevance hit. Returns 0
    for everything else so the function stays cheap and additive.
    """
    if getattr(mem, "temporal_type", None) == "future_plan":
        return _FUTURE_PLAN_PENALTY
    return 0.0


# K-time5: the memory-bullet time tag also lives in
# ``app.core.infra.timephrase`` now. Re-exported under its historical name
# so callers (this module's ``format_block`` + ``MemoryRetriever`` +
# ``test_memory_temporal``) keep working unchanged.
_temporal_suffix = _tp.temporal_suffix


def _is_anniversary_today(metadata: dict | None) -> bool:
    """True if ``metadata.when`` falls inside an anniversary window today.

    Safe to call with arbitrary dicts and on rows whose ``when`` is
    missing or malformed; returns ``False`` in those cases.
    """
    if not metadata or not isinstance(metadata, dict):
        return False
    when_raw = metadata.get("when")
    if not when_raw:
        return False
    try:
        when = datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - when).total_seconds() / 86400.0
    if delta <= 0:
        return False
    for window in _ANNIVERSARY_WINDOW_DAYS:
        if abs(delta - window) <= _ANNIVERSARY_TOLERANCE_DAYS:
            return True
    return False


class RagRetriever:
    def __init__(
        self,
        store: "RagStore",
        embedder: "Embedder",
        *,
        top_k: int = 6,
        score_threshold: float = 0.4,
        per_source_top_k: int = 6,
        include_messages: bool = True,
        include_documents: bool = True,
        memory_store: "MemoryStore | None" = None,
        chat_db: "ChatDatabase | None" = None,
        arc_state_provider: "Callable[[], ArcState | None] | None" = None,
        dialogue_act_provider: "Callable[[str], str | None] | None" = None,
        goal_store: "GoalStore | None" = None,
        fade_hedge_enabled: bool = True,
        faded_salience_threshold: float = _FADED_DEFAULT_SALIENCE_THRESHOLD,
        faded_idle_days: int = _FADED_DEFAULT_IDLE_DAYS,
        confidence_time_decay_enabled: bool = True,
        confidence_decay_horizon_days: int = _CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS,
        confidence_decay_floor: float = _CONFIDENCE_DECAY_DEFAULT_FLOOR,
        confidence_decay_distant_threshold: float = _CONFIDENCE_DECAY_DEFAULT_THRESHOLD,
        topic_graph: "TopicGraph | None" = None,
        cluster_diversity_enabled: bool = True,
        max_per_cluster: int = 3,
        topic_expansion_enabled: bool = True,
        expand_max: int = 2,
        expand_trigger_score: float = 0.55,
        expand_min_sim: float = 0.45,
        topic_digest_surface_enabled: bool = True,
        digest_sibling_cap: int = 1,
        topic_digest_provider: "Callable[[int], int | None] | None" = None,
        direct_recall_enabled: bool = True,
        direct_recall_max_messages: int = 6,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = max(0, int(top_k))
        self._score_threshold = float(score_threshold)
        self._per_source_top_k = max(1, int(per_source_top_k))
        self._include_messages = bool(include_messages)
        self._include_documents = bool(include_documents)
        # Optional handle to the canonical SQLite memory store. When set,
        # ``retrieve`` calls ``MemoryStore.mark_used`` on the memory hits
        # it surfaces so subsequent turns can apply the recency penalty
        # / revival bonus tuned above. Plumbing tolerates ``None`` so
        # tests and lean deployments can run without it; the recency
        # signal then simply stays at 0.
        self._memory_store = memory_store
        # H1 + K4: optional handles for the conversation-arc / dialogue-act
        # alignment boost. ``chat_db`` is used to fetch the per-row arc /
        # dialogue_act for the surfaced hits' source ``messages`` rows;
        # the providers return the *current* arc state and the regex-
        # tagged dialogue-act for the live query. All three are optional;
        # when any is None the boost is silently skipped so legacy /
        # test wiring stays functional.
        self._chat_db = chat_db
        self._arc_state_provider = arc_state_provider
        self._dialogue_act_provider = dialogue_act_provider
        # K1 — optional :class:`GoalStore` handle. When set, ``retrieve``
        # pulls the active-goal vector list once per call and applies
        # ``_RAG_GOAL_ALIGNMENT_BOOST`` to any hit whose own embedding
        # cosine-aligns with one of them above
        # ``_RAG_GOAL_ALIGNMENT_THRESHOLD``. Cost is O(num_goals × hits)
        # cosines — negligible since num_goals is capped at ~5 and
        # hits is bounded by ``per_source_top_k``. ``None`` keeps the
        # legacy retriever behaviour for tests and lean deployments.
        self._goal_store = goal_store
        # K7 — Forgetting protocol settings. ``fade_hedge_enabled``
        # is the master kill-switch; off disables every ``(faded)``
        # suffix (including archive). The threshold + idle-days
        # define the graded predicate for long_term rows that have
        # decayed in place. See :func:`_is_faded_memory`.
        self._fade_hedge_enabled = bool(fade_hedge_enabled)
        self._faded_salience_threshold = max(
            0.0, min(1.0, float(faded_salience_threshold)),
        )
        self._faded_idle_days = max(1, int(faded_idle_days))
        # K25 — time-decay on memory confidence. Settings are clamped
        # here as the second line of defence (the parser in
        # :func:`app.core.infra.settings.load_settings` is the first)
        # so a tester instantiating the retriever directly with
        # out-of-range values still gets a sane runtime. ``horizon_days``
        # at 0 would zero-divide in :func:`_compute_effective_confidence`
        # — the floor at 1 keeps the math safe; the helper itself also
        # short-circuits defensively, so this is belt-and-suspenders.
        self._confidence_time_decay_enabled = bool(
            confidence_time_decay_enabled,
        )
        self._confidence_decay_horizon_days = max(
            1, int(confidence_decay_horizon_days),
        )
        self._confidence_decay_floor = max(
            0.0, min(1.0, float(confidence_decay_floor)),
        )
        self._confidence_decay_distant_threshold = max(
            0.0, min(1.0, float(confidence_decay_distant_threshold)),
        )
        # F10b — cluster-aware RAG diversity. ``topic_graph`` is the
        # optional persistent :class:`TopicGraph`; when wired *and*
        # ``cluster_diversity_enabled`` is True, the final top-k selection
        # caps how many memory hits may come from a single topic cluster
        # (``max_per_cluster``) so one dense cluster can't monopolise every
        # slot. Backfill guarantees the top-k is still filled when only one
        # topic is relevant. ``None`` topic_graph (tests / lean deployments
        # / non-persistent mode) cleanly disables the re-rank: behaviour is
        # then byte-identical to the plain score-sorted top-k cut.
        self._topic_graph = topic_graph
        self._cluster_diversity_enabled = bool(cluster_diversity_enabled)
        self._max_per_cluster = max(1, int(max_per_cluster))
        # F10c — topic multi-hop expansion. When a turn's strongest memory
        # hit (score >= ``expand_trigger_score``) belongs to a cluster, up
        # to ``expand_max`` sibling members of that cluster whose cosine to
        # the query clears ``expand_min_sim`` are appended (beyond the
        # top-k) as ``expansion`` hits, rounding out the topic. Needs both
        # the topic graph and the memory store wired; no-op otherwise.
        self._topic_expansion_enabled = bool(topic_expansion_enabled)
        self._expand_max = max(0, int(expand_max))
        self._expand_trigger_score = float(expand_trigger_score)
        self._expand_min_sim = float(expand_min_sim)
        # F10g — surface a cluster's stored ``topic_digest`` memory as the
        # coarse "what I know about X" line during expansion, capping the
        # raw sibling enumeration to ``digest_sibling_cap`` so a dense
        # cluster contributes a gist + a couple of specifics instead of N
        # lines. ``topic_digest_provider`` maps an anchor cluster id to its
        # digest memory id (the :class:`TopicDigestWorker`'s live map);
        # ``None`` keeps the pre-F10g sibling-only expansion.
        self._topic_digest_surface_enabled = bool(topic_digest_surface_enabled)
        self._digest_sibling_cap = max(0, int(digest_sibling_cap))
        self._topic_digest_provider = topic_digest_provider
        # Schema v8 — IDs of memories surfaced in the last
        # :meth:`retrieve` call. ``SessionController._post_turn_inner_life``
        # reads this snapshot to run the keyword-overlap revival check
        # against Aiko's reply and bump ``revival_score`` on rows she
        # actually cited.
        self._last_surfaced_memory_ids: list[int] = []
        # K-time2 — the relative-time window parsed from the last query's
        # text (e.g. "yesterday" -> a concrete day range) and how many of
        # the surfaced hits actually fell inside it. ``block_for`` reads
        # these to drive the empty-window anti-confabulation guard.
        self._last_time_window: "TimeWindow | None" = None
        self._last_time_window_hit_count: int = 0
        # K-time2 direct recall — when a *guardable* (clearly retrospective)
        # time window is named, also pull the actual messages from that
        # window straight out of SQLite so verbatim "what did we say then"
        # recall isn't limited to the semantic top-N. Needs ``chat_db``
        # wired; ``direct_recall_max_messages`` caps the injection per turn.
        self._direct_recall_enabled = bool(direct_recall_enabled)
        self._direct_recall_max = max(0, int(direct_recall_max_messages))
        # P19: the three per-source Lance searches (memories / messages /
        # documents) are independent and each only takes RagStore's shared
        # read lock, so they overlap instead of summing when dispatched on
        # this small pool. Lance releases the GIL during the ANN query, so
        # the threads make real wall-clock progress in parallel. ``max_workers
        # = 3`` matches the source count; the pool is idle between turns.
        self._search_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="rag-search",
        )

    @property
    def top_k(self) -> int:
        return self._top_k

    def close(self) -> None:
        """Release the P19 search thread pool. Idempotent."""
        ex = self._search_executor
        if ex is not None:
            self._search_executor = None
            try:
                ex.shutdown(wait=False)
            except Exception:
                log.debug("rag retriever: executor shutdown raised", exc_info=True)

    def set_goal_store(self, store: "GoalStore | None") -> None:
        """Attach (or detach) the K1 :class:`GoalStore` after construction.

        SessionController builds the retriever before the goal store
        exists, so we wire the dependency in a second pass. Passing
        ``None`` cleanly disables the goal-alignment bonus.
        """
        self._goal_store = store

    def set_topic_graph(self, graph: "TopicGraph | None") -> None:
        """Attach (or detach) the F10b :class:`TopicGraph` after construction.

        SessionController builds the retriever before the topic graph
        exists, so the cluster-diversity dependency is wired in a second
        pass (mirroring :meth:`set_goal_store`). Passing ``None`` cleanly
        disables the cluster-aware re-rank; retrieval then falls back to
        the plain score-sorted top-k cut.
        """
        self._topic_graph = graph

    def set_topic_digest_provider(
        self, provider: "Callable[[int], int | None] | None"
    ) -> None:
        """Attach (or detach) the F10g digest lookup after construction.

        ``provider`` maps an anchor cluster id to its stored
        ``topic_digest`` memory id (the :class:`TopicDigestWorker`'s live
        ``cluster_digest_map``). SessionController builds the retriever
        before the worker exists, so this is wired in a second pass.
        Passing ``None`` reverts to pre-F10g sibling-only expansion.
        """
        self._topic_digest_provider = provider

    def update_settings(
        self,
        *,
        top_k: int | None = None,
        score_threshold: float | None = None,
        include_messages: bool | None = None,
        include_documents: bool | None = None,
        cluster_diversity_enabled: bool | None = None,
        max_per_cluster: int | None = None,
        topic_expansion_enabled: bool | None = None,
        expand_max: int | None = None,
        expand_trigger_score: float | None = None,
        expand_min_sim: float | None = None,
        topic_digest_surface_enabled: bool | None = None,
        digest_sibling_cap: int | None = None,
    ) -> None:
        if top_k is not None:
            self._top_k = max(0, int(top_k))
        if score_threshold is not None:
            self._score_threshold = max(0.0, min(1.0, float(score_threshold)))
        if include_messages is not None:
            self._include_messages = bool(include_messages)
        if include_documents is not None:
            self._include_documents = bool(include_documents)
        if cluster_diversity_enabled is not None:
            self._cluster_diversity_enabled = bool(cluster_diversity_enabled)
        if max_per_cluster is not None:
            self._max_per_cluster = max(1, int(max_per_cluster))
        if topic_expansion_enabled is not None:
            self._topic_expansion_enabled = bool(topic_expansion_enabled)
        if expand_max is not None:
            self._expand_max = max(0, int(expand_max))
        if expand_trigger_score is not None:
            self._expand_trigger_score = float(expand_trigger_score)
        if expand_min_sim is not None:
            self._expand_min_sim = float(expand_min_sim)
        if topic_digest_surface_enabled is not None:
            self._topic_digest_surface_enabled = bool(topic_digest_surface_enabled)
        if digest_sibling_cap is not None:
            self._digest_sibling_cap = max(0, int(digest_sibling_cap))

    # ── retrieval ───────────────────────────────────────────────────────

    def _direct_recall_hits(
        self, window: "TimeWindow", exclude_session_id: str | None,
    ) -> list[RagHit]:
        """Pull the actual messages recorded inside ``window`` from SQLite.

        K-time2 direct recall. Returns synthetic ``message`` :class:`RagHit`
        objects scored around :data:`_DIRECT_RECALL_BASE` (+ the in-window
        time bonus + per-message recency) so they reliably surface for a
        recall query without overpowering a strong semantic memory hit.
        The SQL bounds are widened by a day on each side and the rows are
        re-filtered through ``window.contains`` so a tz-format difference
        between the now-anchor and the stored timestamps can't drop a row.
        """
        if self._chat_db is None or self._direct_recall_max <= 0:
            return []
        day = timedelta(days=1)
        try:
            lo = (window.start - day).isoformat()
            hi = (window.end + day).isoformat()
        except Exception:
            return []
        # Fetch a little extra so the precise in-window re-filter below can
        # still return up to the cap after trimming the widened margins.
        rows = self._chat_db.messages_in_range(
            lo, hi,
            limit=self._direct_recall_max * 3,
            exclude_session_id=exclude_session_id,
        )
        hits: list[RagHit] = []
        for row in rows:
            created = row.get("created_at")
            if not window.contains(created):
                continue
            try:
                record = MessageRecord.from_row(row)
            except Exception:
                continue
            score = (
                _DIRECT_RECALL_BASE
                + _RAG_TIME_WINDOW_BONUS
                + _recency_bonus(created or "")
            )
            hits.append(RagHit(source="message", score=score, record=record))
            if len(hits) >= self._direct_recall_max:
                break
        if hits:
            log.debug(
                "rag retriever: direct recall window=%s hits=%d",
                window.label, len(hits),
            )
        return hits

    def _search_all_sources(
        self, embedding: Sequence[float],
    ) -> tuple[list[RagHit], list[RagHit], list[RagHit]]:
        """Run the (up to) three per-source Lance searches concurrently.

        P19: returns ``(mem_hits, msg_hits, doc_hits)`` — each already
        guarded so a single source raising never aborts the others, and
        disabled sources come back as ``[]`` without a search. Submits the
        enabled searches to ``_search_executor`` and joins; when the pool
        is gone (post-``close``) or only one source is active it runs
        inline so behaviour is identical minus the parallelism.
        """
        k = self._per_source_top_k
        thr = self._score_threshold

        def _mem() -> list[RagHit]:
            return self._store.search_memories(embedding, top_k=k, min_score=thr)

        def _msg() -> list[RagHit]:
            return self._store.search_messages(embedding, top_k=k, min_score=thr)

        def _doc() -> list[RagHit]:
            return self._store.search_documents(embedding, top_k=k, min_score=thr)

        tasks: list[tuple[str, Callable[[], list[RagHit]]]] = [("mem", _mem)]
        if self._include_messages:
            tasks.append(("msg", _msg))
        if self._include_documents:
            tasks.append(("doc", _doc))

        results: dict[str, list[RagHit]] = {"mem": [], "msg": [], "doc": []}
        ex = self._search_executor
        if ex is None or len(tasks) == 1:
            for name, fn in tasks:
                try:
                    results[name] = fn()
                except Exception:
                    log.debug("%s search failed", name, exc_info=True)
            return results["mem"], results["msg"], results["doc"]

        futures = {name: ex.submit(fn) for name, fn in tasks}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception:
                log.debug("%s search failed", name, exc_info=True)
        return results["mem"], results["msg"], results["doc"]

    def retrieve(
        self,
        query_text: str,
        *,
        recent_turns: Iterable[str] | None = None,
        exclude_session_id: str | None = None,
    ) -> list[RagHit]:
        """Return up to ``top_k`` merged hits across all sources.

        ``recent_turns`` is an optional list of recent message texts used to
        widen the query (concatenated). This dramatically improves retrieval
        on follow-up questions that share little surface form with the prior
        turn (e.g. "what did I say earlier?").
        """
        if self._top_k <= 0:
            return []
        query = self._build_query(query_text, recent_turns)
        if not query:
            return []
        try:
            embedding = self._embedder.embed(query)
        except Exception:
            log.debug("rag retriever: embed failed", exc_info=True)
            return []

        # K1 — pre-fetch active goal vectors once per retrieval call so
        # the per-hit alignment cosine check below is a cheap O(num_goals)
        # dot product. The goal vectors are unit-normalised by
        # ``MemoryStore.add`` so the dot product equals cosine directly.
        goal_vectors: list[np.ndarray] = []
        if self._goal_store is not None:
            try:
                goal_vectors = list(self._goal_store.active_goal_vectors())
            except Exception:
                log.debug(
                    "rag retriever: goal_store active vectors failed",
                    exc_info=True,
                )
                goal_vectors = []

        # F8 — decide once whether this turn is informational so the
        # per-hit ``knowledge`` bonus below stays a cheap branch. Reuses
        # the same regex dialogue-act provider the alignment boost uses;
        # missing provider / raise → not informational (no boost).
        informational_turn = False
        if self._dialogue_act_provider is not None and query_text:
            try:
                act = self._dialogue_act_provider(query_text)
            except Exception:
                act = None
            informational_turn = bool(act) and act in _INFORMATIONAL_ACTS

        # P19: dispatch the three independent per-source searches
        # concurrently (each only takes RagStore's shared read lock, and
        # Lance frees the GIL during the ANN query) so their latencies
        # max instead of summing. The per-source scoring below is cheap
        # CPU and stays sequential on the turn thread.
        mem_hits, msg_hits, doc_hits = self._search_all_sources(embedding)

        # K-time2 — parse a relative-time window from the *raw* user text
        # (not the recent-turns-expanded query, which could carry a stale
        # time phrase from an earlier turn). Hits recorded inside the window
        # get a score nudge below; the in-window count drives the guard.
        time_window: TimeWindow | None = None
        try:
            time_window = parse_time_window(query_text, _tp.now())
        except Exception:
            log.debug("rag retriever: time-window parse failed", exc_info=True)
        time_window_hits = 0

        merged: list[RagHit] = []
        try:
            # P4: batch the SQLite-mirror join once instead of a locked
            # ``get`` per hit. Falls back to the per-hit path for stores
            # that don't expose ``get_many`` (e.g. duck-typed test doubles).
            mem_by_id: dict[int, Any] = {}
            if self._memory_store is not None and hasattr(
                self._memory_store, "get_many"
            ):
                batch_ids: list[int] = []
                for h in mem_hits:
                    raw_id = getattr(h.record, "id", None)
                    if raw_id is None:
                        continue
                    try:
                        batch_ids.append(int(raw_id))
                    except (TypeError, ValueError):
                        continue
                if batch_ids:
                    try:
                        mem_by_id = dict(self._memory_store.get_many(batch_ids))
                    except Exception:
                        log.debug(
                            "rag retriever: get_many batch join raised",
                            exc_info=True,
                        )
                        mem_by_id = {}
            for h in mem_hits:
                h.score += _MEMORY_PRIOR + _memory_recency_adjust(
                    last_used_at=getattr(h.record, "last_used_at", None),
                    use_count=int(getattr(h.record, "use_count", 0) or 0),
                )
                # Pin status lives in the SQLite mirror, not LanceDB --
                # apply the bonus by joining against ``MemoryStore`` here.
                # Wrapped in a broad try/except because a misbehaving or
                # duck-typed memory store must not abort retrieval (the
                # outer except for the whole memory branch would drop
                # every memory hit, see test_mark_used_failure_does_not_
                # break_retrieval).
                if self._memory_store is not None:
                    try:
                        raw_id = getattr(h.record, "id", None)
                        if raw_id is not None and (
                            mem_by_id or hasattr(self._memory_store, "get")
                        ):
                            mem = (
                                mem_by_id.get(int(raw_id))
                                if mem_by_id
                                else self._memory_store.get(int(raw_id))
                            )
                            if mem is not None:
                                if getattr(mem, "pinned", False):
                                    h.score += _MEMORY_PINNED_BONUS
                                # Schema v7: anniversary nudge for
                                # ``shared_moment`` rows whose ``when``
                                # matches one of the 1mo/3mo/6mo/1yr/Nyr
                                # windows today. Keeps the rendering of
                                # this hint out of the hot path.
                                if mem.kind == "shared_moment" and _is_anniversary_today(
                                    getattr(mem, "metadata", None)
                                ):
                                    h.score += _MEMORY_ANNIVERSARY_BONUS
                                # Schema v8: tier offset. Reads from the
                                # SQLite mirror (tier is not stored in
                                # the LanceDB record). Defaults to 0
                                # when the row predates v8 / tier is
                                # missing so callers stay safe.
                                tier = getattr(mem, "tier", "long_term")
                                h.score += _MEMORY_TIER_OFFSET.get(tier, 0.0)
                                # K7 — stamp the tier on the hit so
                                # ``format_block`` can render a
                                # "(faded)" suffix for archive-tier
                                # rows without a second join.
                                h.memory_tier = tier
                                # Schema v9: confidence penalty. Same
                                # join path as the tier offset above;
                                # low-confidence hits are demoted (never
                                # hidden) so they only surface when the
                                # cosine match is strong enough to
                                # overcome the handicap. The confidence
                                # is also stamped on the hit so
                                # ``format_block`` can append the
                                # "(uncertain)" suffix without a second
                                # SQLite roundtrip.
                                mem_confidence = getattr(mem, "confidence", None)
                                h.score += _confidence_penalty(mem_confidence)
                                if mem_confidence is not None:
                                    h.confidence = float(mem_confidence)
                                # K25 — stamp the pinned flag on the
                                # hit so the ``(distant)`` time-decay
                                # suffix can bypass user-trusted rows
                                # in ``format_block`` without a second
                                # SQLite roundtrip. Pinned rows are
                                # also already coerced to confidence
                                # >= 0.9 by ``set_pinned`` but we
                                # honour the flag explicitly rather
                                # than relying on the floor.
                                h.memory_pinned = bool(
                                    getattr(mem, "pinned", False)
                                )
                                # K1 — small goal-alignment nudge.
                                # Walks the pre-fetched active goal
                                # vectors against ``mem.embedding``
                                # (already unit-normalised by
                                # MemoryStore.add). Skip goal /
                                # goal_progress hits themselves so
                                # the bonus doesn't compound on top
                                # of the cosine score those rows
                                # already win on. One bonus per hit
                                # max — early-exit on the first
                                # goal that aligns.
                                if (
                                    goal_vectors
                                    and mem.kind not in ("goal", "goal_progress")
                                    and mem.embedding is not None
                                ):
                                    try:
                                        mem_arr = np.asarray(
                                            mem.embedding, dtype=np.float32,
                                        )
                                        for gv in goal_vectors:
                                            sim = float((mem_arr * gv).sum())
                                            if sim >= _RAG_GOAL_ALIGNMENT_THRESHOLD:
                                                h.score += _RAG_GOAL_ALIGNMENT_BOOST
                                                break
                                    except Exception:
                                        log.debug(
                                            "rag retriever: goal-alignment cosine raised",
                                            exc_info=True,
                                        )
                                # K22 — callback bonus. Memories that
                                # Aiko has successfully wound back
                                # into a reply (cosine >= threshold
                                # in :mod:`app.core.conversation.callback_detector`)
                                # carry a positive
                                # ``metadata.callback_count``. A
                                # single-step bonus surfaces them
                                # ahead of equally-relevant siblings
                                # so the next reply naturally reaches
                                # for them again. Per-count scaling
                                # is intentionally absent: the
                                # compounding loop lives on the
                                # salience bump applied at
                                # record-time, which the retriever
                                # already factors in via tier
                                # offsets + RagStore's salience-
                                # aware base score.
                                mem_meta = getattr(mem, "metadata", None)
                                if mem_meta:
                                    try:
                                        cb_count = int(
                                            mem_meta.get("callback_count", 0)
                                        )
                                    except (TypeError, ValueError):
                                        cb_count = 0
                                    if cb_count >= 1:
                                        h.score += _RAG_CALLBACK_BONUS
                                # F8 — knowledge boost on informational
                                # turns only. A distilled ``knowledge``
                                # fact should win over an equally-similar
                                # personal memory when the user is
                                # asking "what are some good X?", but
                                # stay neutral on emotional / banter
                                # turns where reciting a fact reads as a
                                # lecture.
                                if (
                                    informational_turn
                                    and mem.kind == "knowledge"
                                ):
                                    h.score += _RAG_KNOWLEDGE_BONUS
                                # Schema v10: stamp the temporal
                                # fields onto the hit so format_block
                                # can render the time-tag suffix
                                # without a second SQLite roundtrip.
                                # ``temporal_type`` always lands (the
                                # SQLite column has a NOT NULL
                                # default); ``event_time`` and
                                # ``relevance_until`` are ``None`` for
                                # legacy / pre-v10 rows or when the
                                # extractor didn't anchor a timestamp.
                                h.temporal_type = getattr(
                                    mem, "temporal_type", None
                                )
                                h.event_time = getattr(mem, "event_time", None)
                                h.relevance_until = getattr(
                                    mem, "relevance_until", None
                                )
                                # K-time2 — boost when the memory's
                                # recorded date or its anchored event
                                # time lands inside the window the query
                                # named ("yesterday" etc.). Either field
                                # matching counts: a fact recorded then,
                                # or a past/future event dated then.
                                if time_window is not None and (
                                    time_window.contains(
                                        getattr(mem, "created_at", None)
                                    )
                                    or time_window.contains(
                                        getattr(mem, "event_time", None)
                                    )
                                ):
                                    h.score += _RAG_TIME_WINDOW_BONUS
                                    time_window_hits += 1
                                # Apply the relevance-window filter
                                # *and* the future-plan boost. We tag
                                # the hit with a sentinel score so
                                # the dedupe/top-k cut at the bottom
                                # of ``retrieve`` discards it, instead
                                # of scattering ``continue`` here.
                                if _temporal_filter_drops(mem, datetime.now(timezone.utc)):
                                    h.score = -1.0
                                else:
                                    h.score += _temporal_boost(mem)
                    except Exception:
                        log.debug("pinned-bonus lookup failed", exc_info=True)
                merged.append(h)
        except Exception:
            log.debug("memory search failed", exc_info=True)

        if self._include_messages:
            try:
                for h in msg_hits:
                    if exclude_session_id and h.source == "message":
                        # Don't surface lines from the *current* session --
                        # they're already in the recent-window context.
                        if getattr(h.record, "session_id", None) == exclude_session_id:
                            continue
                    h.score = h.score + _MESSAGE_PRIOR + _recency_bonus(
                        getattr(h.record, "created_at", "")
                    )
                    # K-time2 — same date-window nudge for chat snippets
                    # said inside the named window.
                    if time_window is not None and time_window.contains(
                        getattr(h.record, "created_at", None)
                    ):
                        h.score += _RAG_TIME_WINDOW_BONUS
                        time_window_hits += 1
                    merged.append(h)
            except Exception:
                log.debug("message search failed", exc_info=True)

            # K-time2 direct recall — for a clearly retrospective window
            # ("yesterday", "last Tuesday", "back in March") the semantic
            # top-N can miss the actual lines from that day entirely. Pull
            # them straight out of SQLite and inject as message hits so
            # verbatim recall is guaranteed, not luck-of-the-cosine. Bounded
            # by ``_direct_recall_max`` and gated to guardable windows so it
            # never fires on chit-chat like "how are you today". Dedup by
            # text below collapses any overlap with the semantic hits.
            if (
                self._direct_recall_enabled
                and self._direct_recall_max > 0
                and self._chat_db is not None
                and time_window is not None
                and time_window.guardable
            ):
                try:
                    direct_hits = self._direct_recall_hits(
                        time_window, exclude_session_id,
                    )
                    merged.extend(direct_hits)
                    # The injected lines are in-window by construction, so
                    # count them toward the guard (an empty semantic pass on
                    # a day we *do* have messages for shouldn't read as "I
                    # have nothing from then").
                    time_window_hits = max(time_window_hits, len(direct_hits))
                except Exception:
                    log.debug("direct-recall lookup failed", exc_info=True)

        if self._include_documents:
            try:
                for h in doc_hits:
                    h.score += _DOCUMENT_PRIOR
                    # H4: nudge freshly-uploaded documents up so newly-added
                    # knowledge surfaces before it fades into the pool.
                    h.score += _document_recency_bonus(
                        getattr(h.record, "created_at", "")
                    )
                    merged.append(h)
            except Exception:
                log.debug("document search failed", exc_info=True)

        # H1 + K4: apply the conversation-arc / dialogue-act alignment
        # boost. Combined cap is +0.05 so a hit matching on both never
        # gets the full additive +0.06; misaligned hits stay at 0. The
        # join is a single SQL ``IN (...)`` call across every surfaced
        # hit, batched in :meth:`_apply_alignment_boost`.
        try:
            self._apply_alignment_boost(merged, query_text=query_text)
        except Exception:
            log.debug("alignment boost failed", exc_info=True)

        # Dedupe by content text (case-insensitive, whitespace-stripped).
        # Schema v10: hits flagged with ``score < 0`` by the temporal
        # filter (e.g. expired past_events) are dropped here before
        # the top-k cut so they never make it into the prompt block.
        seen: set[str] = set()
        candidates: list[RagHit] = []
        for h in sorted(merged, key=lambda x: x.score, reverse=True):
            if h.score < 0:
                continue
            key = (h.text or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(h)

        # F10b — cluster-aware diversity. Cap how many of the top-k may
        # come from one topic cluster, backfilling from the deferred
        # overflow so we never return fewer hits than the plain cut would.
        unique = self._select_diverse(candidates)

        # F10c — topic multi-hop expansion. If this turn landed strongly on
        # a cluster, append a couple of its sibling members (beyond the
        # top-k) so Aiko gets the surrounding context, not just the single
        # closest line. Best-effort and bounded; a failure leaves the
        # direct top-k untouched.
        _digest_active = (
            self._topic_digest_surface_enabled
            and self._topic_digest_provider is not None
        )
        if (
            self._topic_expansion_enabled
            and (self._expand_max > 0 or _digest_active)
            and self._topic_graph is not None
            and self._memory_store is not None
        ):
            try:
                expansions = self._expand_topic(unique, embedding)
            except Exception:
                log.debug("topic expansion failed", exc_info=True)
                expansions = []
            if expansions:
                unique = unique + expansions

        # Bump ``last_used_at`` / ``use_count`` for every memory we're
        # actually surfacing this turn. Closes the loop with the recency
        # penalty above: a memory we just sent the LLM gets penalised on
        # the very next turn, breaking the "always wins" feedback loop
        # the legacy retriever suffered from. Best-effort — a broken
        # store must not abort the prompt build.
        ids: list[int] = []
        for hit in unique:
            if hit.source != "memory":
                continue
            raw = getattr(hit.record, "id", None)
            if raw is None:
                continue
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                # Lance memory ids are stringified ints; anything
                # that doesn't parse cleanly is a non-canonical row
                # we can't reach via the SQLite mirror anyway.
                continue
        # Schema v8: snapshot the surfaced memory IDs so
        # SessionController can run the post-turn revival check (keyword
        # overlap between Aiko's reply and each surfaced memory) before
        # the next retrieve() clobbers the list.
        self._last_surfaced_memory_ids = list(ids)
        # K-time2 — snapshot the parsed window + how many hits fell inside
        # it so block_for can decide whether to add the anti-confabulation
        # guard for an empty retrospective window.
        self._last_time_window = time_window
        self._last_time_window_hit_count = time_window_hits
        if self._memory_store is not None and ids:
            try:
                self._memory_store.mark_used(ids)
            except Exception:
                log.debug("mark_used failed", exc_info=True)
        return unique

    @property
    def last_surfaced_memory_ids(self) -> list[int]:
        """Snapshot of memory IDs surfaced by the most recent ``retrieve``.

        Empty list when the last call returned no memory hits or before
        the first call. Consumed by :class:`SessionController` to
        run the post-turn revival keyword-overlap check.
        """
        return list(self._last_surfaced_memory_ids)

    @property
    def last_time_window(self) -> "TimeWindow | None":
        """The relative-time window parsed from the most recent query."""
        return self._last_time_window

    def time_window_guard_note(self) -> str | None:
        """Anti-confabulation note for a retrospective query that surfaced
        nothing from the window it named (K-time2).

        Returns ``None`` unless the last query named a *guardable* (clearly
        retrospective) window AND zero surfaced hits fell inside it. Phrased
        as private guidance to Aiko rather than a hard claim — RAG only sees
        the semantic top-N, so "nothing surfaced" is not "nothing exists".
        """
        win = self._last_time_window
        if win is None or not win.guardable:
            return None
        if self._last_time_window_hit_count > 0:
            return None
        return (
            f"[Note: nothing from {win.label} surfaced in memory. If asked "
            f"about {win.label} specifically and you don't actually recall "
            f"it, say so plainly instead of guessing.]"
        )

    # ── cluster-scoped recall (F10d) ────────────────────────────────────

    def recall_topic(
        self,
        topic_query: str,
        *,
        limit: int = 8,
        min_cluster_sim: float = 0.30,
    ) -> tuple[str, list[RagHit]]:
        """Coarse cluster match, then drill into that cluster's members.

        The F10d "retrieve at the cluster level first, then drill into
        members" tier, surfaced to Aiko as the ``recall_topic`` tool. Embeds
        ``topic_query``, finds the single best-matching topic cluster by
        centroid cosine (``best_clusters_for``), then returns that cluster's
        members ranked by cosine to the query, capped at ``limit``. Unlike
        :meth:`retrieve` this is *not* a global vector search -- it answers
        "what do I actually know about X?" by enumerating one coherent
        topic. Returns ``(cluster_label, hits)``; the label may be empty
        (cluster not yet F10a-named) and ``hits`` is ``[]`` when no cluster
        clears ``min_cluster_sim`` or the graph / store isn't wired.
        """
        query = (topic_query or "").strip()
        if not query or self._topic_graph is None or self._memory_store is None:
            return "", []
        cap = max(1, int(limit))
        try:
            embedding = self._embedder.embed(query)
        except Exception:
            log.debug("recall_topic: embed failed", exc_info=True)
            return "", []
        q = np.asarray(embedding, dtype=np.float32).ravel()
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return "", []
        q = q / q_norm
        try:
            clusters = self._topic_graph.best_clusters_for(
                q, top_n=1, min_sim=float(min_cluster_sim)
            )
        except Exception:
            log.debug("recall_topic: best_clusters_for failed", exc_info=True)
            return "", []
        if not clusters:
            return "", []
        cid, label, _sim = clusters[0]
        try:
            member_ids = self._topic_graph.cluster_member_ids(cid)
        except Exception:
            log.debug("recall_topic: cluster_member_ids failed", exc_info=True)
            return "", []
        if not member_ids:
            return label, []

        scored: list[tuple[float, Any]] = []
        for mid in member_ids:
            try:
                mem = self._memory_store.get(mid)
            except Exception:
                continue
            if mem is None:
                continue
            vec = getattr(mem, "embedding", None)
            if vec is None:
                continue
            v = np.asarray(vec, dtype=np.float32).ravel()
            v_norm = float(np.linalg.norm(v))
            if v_norm == 0.0 or v.size != q.size:
                continue
            sim = float(np.dot(q, v)) / v_norm
            scored.append((sim, mem))
        scored.sort(key=lambda t: t[0], reverse=True)
        hits = [
            self._memory_to_hit(mem, score=sim)
            for sim, mem in scored[:cap]
        ]
        return label, hits

    # ── cluster-aware diversity (F10b) ──────────────────────────────────

    def _select_diverse(self, candidates: list[RagHit]) -> list[RagHit]:
        """Pick the final top-k, capping hits per topic cluster.

        ``candidates`` is the deduped, score-descending hit list. When the
        cluster-diversity re-rank is disabled or no :class:`TopicGraph` is
        wired this is a plain ``candidates[: top_k]`` cut, so behaviour is
        unchanged for tests / lean deployments.

        Otherwise we walk the candidates in score order and admit each one
        unless its cluster already holds ``max_per_cluster`` admitted hits,
        in which case it is deferred. Only ``memory`` hits with a known
        cluster id are capped -- message / document hits and unclustered
        memories are always admitted (they don't belong to a topic knot).
        If diversity leaves us short of ``top_k`` (e.g. only one cluster is
        relevant this turn), we backfill from the deferred overflow in
        score order, so the re-rank only ever *reorders* the top-k -- it
        never shrinks it.
        """
        if self._top_k <= 0:
            return []
        if not self._cluster_diversity_enabled or self._topic_graph is None:
            return candidates[: self._top_k]

        selected: list[RagHit] = []
        deferred: list[RagHit] = []
        per_cluster: dict[int, int] = {}
        cap = self._max_per_cluster
        for h in candidates:
            if len(selected) >= self._top_k:
                break
            cid = self._hit_cluster_id(h)
            if cid is not None:
                if per_cluster.get(cid, 0) >= cap:
                    deferred.append(h)
                    continue
                per_cluster[cid] = per_cluster.get(cid, 0) + 1
            selected.append(h)
        if len(selected) < self._top_k and deferred:
            for h in deferred:
                selected.append(h)
                if len(selected) >= self._top_k:
                    break
        return selected

    def _hit_cluster_id(self, hit: RagHit) -> int | None:
        """Cluster id for a memory hit via the topic graph, or ``None``.

        Non-memory hits and any lookup failure return ``None`` (treated as
        un-capped). The topic graph's :meth:`cluster_id_for` is an O(1)
        read against the warm assignment map and never forces a rebuild.
        """
        if hit.source != "memory" or self._topic_graph is None:
            return None
        raw = getattr(hit.record, "id", None)
        if raw is None:
            return None
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            return None
        try:
            return self._topic_graph.cluster_id_for(mid)
        except Exception:
            log.debug("cluster_id_for lookup failed", exc_info=True)
            return None

    # ── topic multi-hop expansion (F10c) ────────────────────────────────

    def _expand_topic(
        self, selected: list[RagHit], query_embedding: Sequence[float]
    ) -> list[RagHit]:
        """Pull up to ``expand_max`` sibling memories from the dominant cluster.

        The "dominant cluster" is the topic of the strongest memory hit in
        ``selected`` whose score clears ``expand_trigger_score``. Its member
        ids (minus the memories already surfaced this turn) are scored by
        cosine to the live query embedding; the closest few above
        ``expand_min_sim`` are returned as ``expansion``-flagged hits. Reads
        the memory mirror only -- no extra DB hit, no embed. Returns ``[]``
        when nothing clears the gates (no strong hit, no cluster, no
        sibling close enough), leaving the direct top-k unchanged.
        """
        # Find the strongest memory hit and its cluster.
        anchor_cid: int | None = None
        anchor_score: float = self._expand_trigger_score
        for hit in selected:
            if hit.source != "memory":
                continue
            if float(hit.score) < self._expand_trigger_score:
                continue
            cid = self._hit_cluster_id(hit)
            if cid is not None:
                anchor_cid = cid
                anchor_score = float(hit.score)
                break
        if anchor_cid is None:
            return []

        try:
            member_ids = self._topic_graph.cluster_member_ids(anchor_cid)
        except Exception:
            log.debug("cluster_member_ids lookup failed", exc_info=True)
            member_ids = []

        # Don't re-surface anything already in the top-k this turn.
        already: set[int] = set()
        for hit in selected:
            if hit.source != "memory":
                continue
            raw = getattr(hit.record, "id", None)
            try:
                already.add(int(raw))
            except (TypeError, ValueError):
                continue

        # F10g — surface the cluster's stored digest as the coarse "what I
        # know about X" line. When present it replaces bulk sibling
        # enumeration: only ``_digest_sibling_cap`` raw siblings follow.
        digest_mem = self._lookup_cluster_digest(anchor_cid, already)
        sibling_limit = (
            self._digest_sibling_cap if digest_mem is not None else self._expand_max
        )

        scored: list[tuple[float, "Any"]] = []
        if member_ids and sibling_limit > 0:
            q = np.asarray(query_embedding, dtype=np.float32).ravel()
            q_norm = float(np.linalg.norm(q))
            if q_norm != 0.0:
                q = q / q_norm
                for mid in member_ids:
                    if mid in already:
                        continue
                    try:
                        mem = self._memory_store.get(mid)
                    except Exception:
                        continue
                    if mem is None:
                        continue
                    vec = getattr(mem, "embedding", None)
                    if vec is None:
                        continue
                    v = np.asarray(vec, dtype=np.float32).ravel()
                    v_norm = float(np.linalg.norm(v))
                    if v_norm == 0.0 or v.size != q.size:
                        continue
                    sim = float(np.dot(q, v)) / v_norm
                    if sim < self._expand_min_sim:
                        continue
                    scored.append((sim, mem))
        scored.sort(key=lambda t: t[0], reverse=True)

        out: list[RagHit] = []
        if digest_mem is not None:
            out.append(
                self._memory_to_hit(digest_mem, score=anchor_score, expansion=True)
            )
        out.extend(
            self._memory_to_hit(mem, score=sim, expansion=True)
            for sim, mem in scored[:sibling_limit]
        )
        return out

    def _lookup_cluster_digest(
        self, cluster_id: int, already: set[int]
    ) -> "Any | None":
        """Resolve the F10g digest memory for ``cluster_id`` (or ``None``).

        Reads the injected provider (the :class:`TopicDigestWorker`'s live
        ``cluster_digest_map``), then verifies the row still exists, is a
        ``topic_digest``, and isn't already surfaced this turn. Stale
        provider entries (between a graph rebuild and the next worker tick)
        degrade gracefully to ``None`` -- the digest still surfaces through
        ordinary cosine RAG, it just doesn't get the special expansion
        treatment that turn.
        """
        if not self._topic_digest_surface_enabled:
            return None
        provider = self._topic_digest_provider
        if provider is None or self._memory_store is None:
            return None
        try:
            mem_id = provider(int(cluster_id))
        except Exception:
            log.debug("topic_digest provider raised", exc_info=True)
            return None
        if mem_id is None:
            return None
        try:
            mid = int(mem_id)
        except (TypeError, ValueError):
            return None
        if mid in already:
            return None
        try:
            mem = self._memory_store.get(mid)
        except Exception:
            return None
        if mem is None or str(getattr(mem, "kind", "")) != "topic_digest":
            return None
        return mem

    @staticmethod
    def _memory_to_hit(mem: "Any", *, score: float, expansion: bool = False) -> RagHit:
        """Build a ``RagHit`` from a :class:`MemoryStore` mirror row.

        Used by the F10c expansion and F10d cluster-scoped recall, which
        reach memories by id (via the topic graph) rather than through a
        vector search, so they have a ``Memory`` object instead of a
        ``RagHit``. Carries the canonical fields ``format_block`` reads;
        the tier / pinned suffix joins are left ``None`` (these hits render
        in their own sections, not the main "(faded)"/"(distant)" path).
        """
        from app.core.rag.rag_store import MemoryRecord

        record = MemoryRecord(
            id=str(mem.id),
            content=mem.content,
            kind=mem.kind,
            salience=float(mem.salience),
            source_session=mem.source_session,
            source_message_id=mem.source_message_id,
            created_at=mem.created_at,
            last_used_at=mem.last_used_at,
            use_count=int(mem.use_count),
        )
        return RagHit(
            source="memory",
            score=float(score),
            record=record,
            memory_tier=getattr(mem, "tier", None),
            expansion=bool(expansion),
        )

    # ── alignment boost (H1 + K4) ───────────────────────────────────────

    def _apply_alignment_boost(
        self, hits: list[RagHit], *, query_text: str,
    ) -> None:
        """Bump hits whose source row matches the live arc / dialogue_act.

        Called once per ``retrieve`` against the merged hit list; mutates
        ``hit.score`` in place. Silently noops when any of the optional
        wirings (``chat_db``, ``arc_state_provider``,
        ``dialogue_act_provider``) is missing -- the legacy retrieval
        ordering is unchanged in that case.

        Combined boost is capped at ``_RAG_ALIGNMENT_BOOST_CAP`` so a
        hit aligned on both signals never gets the full additive +0.06.
        """
        if not hits or self._chat_db is None:
            return
        current_arc: str | None = None
        if self._arc_state_provider is not None:
            try:
                state = self._arc_state_provider()
            except Exception:
                state = None
            if state is not None:
                current_arc = getattr(state, "arc", None)
        current_act: str | None = None
        if self._dialogue_act_provider is not None and query_text:
            try:
                current_act = self._dialogue_act_provider(query_text)
            except Exception:
                current_act = None
        if not current_arc and not current_act:
            return

        message_ids: list[int] = []
        for h in hits:
            mid = self._extract_message_id(h)
            if mid is not None:
                message_ids.append(mid)
        if not message_ids:
            return
        try:
            signals = self._chat_db.get_message_signals(message_ids)
        except Exception:
            log.debug("get_message_signals failed", exc_info=True)
            return
        if not signals:
            return

        for h in hits:
            mid = self._extract_message_id(h)
            if mid is None:
                continue
            row_arc, row_act = signals.get(mid, (None, None))
            bonus = 0.0
            if current_arc and row_arc and row_arc == current_arc:
                bonus += _RAG_ARC_BOOST
            if current_act and row_act and row_act == current_act:
                bonus += _RAG_DIALOGUE_ACT_BOOST
            if bonus <= 0.0:
                continue
            h.score += min(bonus, _RAG_ALIGNMENT_BOOST_CAP)

    @staticmethod
    def _extract_message_id(hit: RagHit) -> int | None:
        """Return the underlying ``messages`` row id for a hit, or ``None``."""
        record = hit.record
        if hit.source == "memory":
            mid = getattr(record, "source_message_id", None)
        elif hit.source == "message":
            mid = getattr(record, "message_id", None)
        else:
            return None
        if mid is None:
            return None
        try:
            value = int(mid)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # ── formatting ──────────────────────────────────────────────────────

    @staticmethod
    def format_block(
        hits: list[RagHit],
        *,
        user_display_name: str = "the user",
        fade_hedge_enabled: bool = True,
        faded_salience_threshold: float = _FADED_DEFAULT_SALIENCE_THRESHOLD,
        faded_idle_days: int = _FADED_DEFAULT_IDLE_DAYS,
        confidence_time_decay_enabled: bool = True,
        confidence_decay_horizon_days: int = _CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS,
        confidence_decay_floor: float = _CONFIDENCE_DECAY_DEFAULT_FLOOR,
        confidence_decay_distant_threshold: float = _CONFIDENCE_DECAY_DEFAULT_THRESHOLD,
    ) -> str:
        """Render hits into a system-prompt-ready block.

        Three sections, in this order, each only emitted when non-empty:
          - "What you know about <user> (long-term memory):" -- memories with
            ``kind`` in {fact, preference, event, relationship}.
          - "Things you've shared / decided about yourself:" -- memories with
            ``kind == "self"``.
          - "Snippets you remembered from past chats:" -- message hits.
          - "From your notes:" -- document hits.

        K7 — ``fade_hedge_enabled`` / ``faded_salience_threshold`` /
        ``faded_idle_days`` control the ``(faded)`` suffix. Defaults
        preserve the original archive-only behaviour with the graded
        long_term predicate enabled; flip ``fade_hedge_enabled=False``
        to silence every fade hedge including archive.

        K25 — ``confidence_time_decay_enabled`` plus the three
        ``confidence_decay_*`` knobs control the ``(distant)`` suffix.
        Computes ``effective_confidence = stored * max(floor, 1 -
        days_since_created / horizon_days)`` and stamps the row when
        the result drops below ``distant_threshold``. Pinned rows
        bypass. Disabled-by-default-but-on-here master switch lets a
        user kill the suffix without disabling K7.
        """
        if not hits:
            return ""
        user_lines: list[str] = []
        self_lines: list[str] = []
        message_lines: list[str] = []
        document_lines: list[str] = []
        expansion_lines: list[str] = []
        digest_lines: list[str] = []
        now = datetime.now(timezone.utc)
        for hit in hits:
            text = (hit.text or "").strip()
            if not text:
                continue
            # F10c/F10g — associative pulls (sibling members + the cluster
            # digest) render in their own sections, not the direct-recall
            # list. A digest is a paragraph "what I know about X", so it
            # gets a clearer label and a longer truncation than the bullet
            # siblings.
            if getattr(hit, "expansion", False):
                kind = (getattr(hit.record, "kind", "") or "").lower()
                if kind == "topic_digest":
                    digest_lines.append(_truncate(text, 600))
                else:
                    expansion_lines.append(f"- {_truncate(text, 240)}")
                continue
            if hit.source == "memory":
                kind = (getattr(hit.record, "kind", "") or "").lower()
                # Suffix tags. Order matters: "(uncertain)" first so
                # confidence reads before provenance.
                suffix_tags: list[str] = []
                # Schema v9: append "(uncertain)" so the LLM hedges when
                # the underlying memory has a low confidence score (the
                # F1 fact-checker may have flagged it, or it never had
                # a high-confidence source to begin with).
                confidence = getattr(hit, "confidence", None)
                if confidence is not None and float(confidence) < 0.5:
                    suffix_tags.append("(uncertain)")
                # K25 — append "(distant)" when the row's effective
                # confidence (after age-based decay) drops below the
                # threshold. Distinct from "(uncertain)" — that's the
                # "stored value was already low" hedge; "(distant)" is
                # the "raw age" hedge for actively-used rows whose
                # stored confidence is fine but they're old enough that
                # Aiko shouldn't quote them as if they were yesterday.
                # Pinned rows bypass via the helper. Order: lands
                # after "(uncertain)" so when both stack the LLM reads
                # the stored-doubt cue first, then the time cue.
                if confidence_time_decay_enabled and _is_distant_memory(
                    stored_confidence=confidence,
                    created_at=getattr(hit.record, "created_at", None),
                    now=now,
                    horizon_days=confidence_decay_horizon_days,
                    floor=confidence_decay_floor,
                    threshold=confidence_decay_distant_threshold,
                    pinned=bool(getattr(hit, "memory_pinned", False)),
                ):
                    suffix_tags.append("(distant)")
                # G3: append "(curiosity)" so the persona rule can
                # surface findings as "I was reading about X — turns
                # out…" rather than reciting them as bare facts. The
                # tag is invisible to the user; only the LLM ever
                # sees it.
                if kind == "curiosity_finding":
                    suffix_tags.append("(curiosity)")
                # F8 — append "(learned)" so the persona rule lets Aiko
                # surface a distilled fact she picked up between sessions
                # naturally ("oh — try Slowdive") rather than reciting it
                # like a textbook. Invisible to the user; only the LLM
                # sees it. Sibling of the G3 "(curiosity)" tag.
                if kind == "knowledge":
                    suffix_tags.append("(learned)")
                # K11 — pre-thought / counterfactual cache. Tag drafts
                # so the persona rule lets Aiko lean on a reply she
                # already mulled ("I actually thought about this…")
                # rather than treat it as a recalled fact. Invisible to
                # the user; only the LLM sees it.
                if kind == "pre_thought":
                    suffix_tags.append("(pre-thought)")
                # K7 — Forgetting protocol. Graded fade predicate
                # covers both archive-tier rows (cold history) AND
                # long_term rows that have decayed in place
                # (salience below threshold AND idle longer than
                # ``faded_idle_days``). The 30-180 day window between
                # "decayed" and "archive demoted" used to pass
                # through with no hedge — closing that gap is what
                # the K7 completion adds. The ``fade_hedge_enabled``
                # master switch lets a user disable every fade
                # suffix (including archive) for a sharper feel.
                if fade_hedge_enabled and _is_faded_memory(
                    tier=getattr(hit, "memory_tier", None),
                    salience=getattr(hit.record, "salience", None),
                    last_used_at=getattr(hit.record, "last_used_at", None),
                    created_at=getattr(hit.record, "created_at", None),
                    now=now,
                    salience_threshold=faded_salience_threshold,
                    idle_days=faded_idle_days,
                ):
                    suffix_tags.append("(faded)")
                suffix = (" " + " ".join(suffix_tags)) if suffix_tags else ""
                # Schema v10: append the temporal time-tag (e.g.
                # "(yesterday)", "(planned for tonight 20:00)",
                # "(ongoing)") so Aiko reads the memory at the right
                # tense. Empty for durable/preference rows so the
                # output stays identical to pre-v10 for legacy /
                # timeless memories.
                time_suffix = _temporal_suffix(
                    temporal_type=getattr(hit, "temporal_type", None),
                    event_time=getattr(hit, "event_time", None),
                    created_at=getattr(hit.record, "created_at", None),
                    now=now,
                )
                if kind in ("self", "self_tagged"):
                    self_lines.append(f"- {text}{suffix}{time_suffix}")
                else:
                    user_lines.append(f"- {text}{suffix}{time_suffix}")
            elif hit.source == "message":
                role = (getattr(hit.record, "role", "") or "").lower()
                speaker = (
                    f"{user_display_name} said" if role == "user" else "You said"
                )
                message_lines.append(f'- {speaker}: "{_truncate(text, 200)}"')
            elif hit.source == "document":
                title = getattr(hit.record, "title", "")
                head = f"({title}) " if title else ""
                document_lines.append(f"- {head}{_truncate(text, 240)}")
        sections: list[str] = []
        if user_lines:
            sections.append(
                f"What you know about {user_display_name} (long-term memory):\n"
                + "\n".join(user_lines)
            )
        if self_lines:
            sections.append(
                "Things you've shared / decided about yourself:\n"
                + "\n".join(self_lines)
            )
        if message_lines:
            sections.append(
                "Snippets you remembered from past chats:\n"
                + "\n".join(message_lines)
            )
        if document_lines:
            sections.append("From your notes:\n" + "\n".join(document_lines))
        if digest_lines:
            sections.append(
                "What you know about this topic so far (your own running "
                "sense of it — lean on it only if it fits naturally):\n"
                + "\n".join(digest_lines)
            )
        if expansion_lines:
            sections.append(
                "Related notes from the same topic (you've circled around this "
                "before — lean on them only if they fit naturally):\n"
                + "\n".join(expansion_lines)
            )
        return "\n\n".join(sections)

    def block_for(
        self,
        query_text: str,
        *,
        recent_turns: Iterable[str] | None = None,
        exclude_session_id: str | None = None,
        user_display_name: str = "the user",
    ) -> str:
        hits = self.retrieve(
            query_text,
            recent_turns=recent_turns,
            exclude_session_id=exclude_session_id,
        )
        block = self.format_block(
            hits,
            user_display_name=user_display_name,
            fade_hedge_enabled=self._fade_hedge_enabled,
            faded_salience_threshold=self._faded_salience_threshold,
            faded_idle_days=self._faded_idle_days,
            confidence_time_decay_enabled=self._confidence_time_decay_enabled,
            confidence_decay_horizon_days=self._confidence_decay_horizon_days,
            confidence_decay_floor=self._confidence_decay_floor,
            confidence_decay_distant_threshold=self._confidence_decay_distant_threshold,
        )
        # K-time2 — append the empty-window anti-confabulation guard. Fires
        # even when the block is otherwise empty (a retrospective query that
        # surfaced nothing from its window is exactly when the guard matters).
        note = self.time_window_guard_note()
        if note:
            block = f"{block}\n{note}".strip() if block else note
        return block

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _build_query(query_text: str, recent_turns: Iterable[str] | None) -> str:
        base = (query_text or "").strip()
        if not recent_turns:
            return base
        # Prepend a small recent-context snippet (last 2-3 turns) so search
        # can pick up referents like "that" / "earlier".
        chunks: list[str] = []
        for t in recent_turns:
            t = (t or "").strip()
            if not t:
                continue
            chunks.append(t)
        if not chunks:
            return base
        ctx = " | ".join(chunks[-3:])
        if not base:
            return ctx
        return f"{ctx} || {base}"


# ── helpers ─────────────────────────────────────────────────────────────────


def _truncate(s: str, limit: int) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[: max(20, limit - 1)].rstrip() + "…"


def _recency_bonus(created_at: str) -> float:
    """Tiny bonus for recent messages, penalty for ancient ones.

    Returns a value in roughly ``[-0.06, 0.06]``. Combined with the cosine
    score (which is in ``[score_threshold, 1.0]`` by then), this nudges the
    final ordering without overpowering raw similarity.
    """
    if not created_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    delta_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    if delta_days < 0:
        delta_days = 0.0
    # Exponential decay halved every _MESSAGE_HALFLIFE_DAYS; bonus shrinks
    # from 0.06 to ~0 as messages age.
    import math

    weight = math.pow(0.5, delta_days / _MESSAGE_HALFLIFE_DAYS)
    return 0.06 * (weight - 0.5)


def _document_recency_bonus(created_at: str) -> float:
    """H4: flat additive bonus for a document chunk uploaded within the
    last ``_DOCUMENT_RECENCY_DAYS`` days, else ``0.0``.

    Documents carry no salience or decay of their own, so a recently
    uploaded note would otherwise rank purely on cosine and could be
    buried under older chunks. A small in-window nudge gives fresh
    uploads a chance to feel "current". Unparseable / missing timestamps
    return ``0.0`` (no bonus, never a penalty).
    """
    hrs = _hours_since(created_at)
    if hrs is None:
        return 0.0
    if hrs <= _DOCUMENT_RECENCY_DAYS * 24.0:
        return _DOCUMENT_RECENCY_BONUS
    return 0.0


def _hours_since(iso_ts: str | None) -> float | None:
    """Hours elapsed between ``iso_ts`` (UTC ISO-8601) and now, or
    ``None`` for missing / unparseable values.

    Negative deltas (clock skew) are clamped to 0.0 so a future-dated
    timestamp doesn't accidentally trigger the revival bonus.
    """
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    return max(0.0, seconds / 3600.0)


def _memory_recency_adjust(
    *,
    last_used_at: str | None,
    use_count: int,
) -> float:
    """Per-memory score nudge based on how recently it was surfaced.

    See the constants at the top of the module for the rationale. The
    function is total: it returns 0.0 for never-used memories, for
    parse failures, and for the "in-between" zone (used some time ago,
    not yet stale enough to revive).
    """
    hours = _hours_since(last_used_at)
    if hours is None:
        # Never surfaced -> no adjustment. Fresh discovery wins on its
        # own merits.
        return 0.0
    if hours < _MEMORY_RECENCY_PENALTY_HOURS:
        return -_MEMORY_RECENCY_PENALTY
    days = hours / 24.0
    if days >= _MEMORY_REVIVAL_DAYS and use_count > 0:
        return _MEMORY_REVIVAL_BONUS
    return 0.0


