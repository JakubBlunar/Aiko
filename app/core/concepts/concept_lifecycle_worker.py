"""L3 concept lifecycle engine -- the single writer of concept state.

An :class:`~app.core.proactive.idle_worker.IdleWorker` that owns every
concept's ``confidence`` / ``plasticity`` / ``status`` (+ ``promoted_at``
and the L3 engagement anchor). L2 only ever *creates* candidates and
reinforces evidence counts / ``last_reinforced_at``; L3 derives the rest.
No LLM calls -- pure arithmetic over a bounded batch.

**Batched + incremental.** Aiko runs intermittently and the concept set
grows, so instead of one big sweep this runs often (default 5 min) over
a small rolling batch: each tick pulls the ``batch_size`` *stalest*
concepts (``last_lifecycle_at`` ascending, NULLs first) and processes
only those. Correctness under batching comes from the **per-concept
engagement anchor**: each concept's decay is measured from *its own*
``last_lifecycle_engagement``, so it doesn't matter how often or in what
order it's visited -- it always accounts exactly the engaged time since
it was last stamped.

**Engagement-driven decay.** Elapsed time is active-conversation time
from the shared :class:`EngagementClock` (falling back to wall-clock when
the clock is absent/disabled), so being away or quiet doesn't crater
confidence. Status keys off the (engagement-robust) confidence rather
than raw idle days; the only wall-clock reads are the promotion
*stability* age and the candidate TTL, which merely *delay* actions.

Transitions (each emits one :class:`ConceptEvent` to the discovery
timeline): ``candidate -> active`` (promotion gate), ``candidate ->
retired`` (stale), ``active -> dormant`` / ``dormant -> retired``
(confidence floors), and revival of a ``dormant`` / ``retired`` concept
when fresh evidence lifts confidence back up. ``retired`` is revivable,
not terminal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.concept_kinds import get_kind
from app.core.concepts.concept_lifecycle import (
    confidence_target,
    next_confidence,
    set_evidence_gate,
)
from app.core.concepts.concept_event_store import ConceptEvent
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.infra.engagement_clock import EngagementClock

log = logging.getLogger("app.concept_lifecycle_worker")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


# Kind-default plasticity applied on a concept's first lifecycle
# evaluation. Only identity is known today (sticky); other kinds keep
# whatever L2 seeded until their own default is registered here.
_KIND_PLASTICITY: dict[str, float] = {}


class ConceptLifecycleWorker:
    """IdleWorker: single writer of concept confidence / plasticity /
    status, processed one rolling batch per tick."""

    name = "concept_lifecycle"

    def __init__(
        self,
        *,
        concept_store: "ConceptStore",
        memory_settings: Any,
        agent_settings: Any,
        concept_event_store: "ConceptEventStore | None" = None,
        engagement_clock: "EngagementClock | None" = None,
        graph_mature_provider: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = concept_store
        self._events = concept_event_store
        self._engagement_clock = engagement_clock
        self._graph_mature_provider = graph_mature_provider
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "concept_lifecycle_interval_seconds",
                300,
            )
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    def _enabled(self) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        if not bool(
            getattr(self._memory_settings, "concept_lifecycle_enabled", True)
        ):
            return False
        return True

    # ── config knobs ──────────────────────────────────────────────────

    def _f(self, name: str, default: float) -> float:
        return float(getattr(self._memory_settings, name, default))

    def _i(self, name: str, default: int) -> int:
        return int(getattr(self._memory_settings, name, default))

    # ── run ────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        batch_size = max(1, self._i("concept_lifecycle_batch_size", 100))
        batch = self._store.list_stalest(batch_size)
        now = self._clock()
        stats = {
            "scanned": 0,
            "promoted": 0,
            "dormant": 0,
            "retired": 0,
            "revived": 0,
            "events": 0,
        }
        for concept in batch:
            try:
                self._process(concept, now, stats)
            except Exception:
                log.debug(
                    "concept lifecycle process failed (id=%s)",
                    getattr(concept, "concept_id", "?"),
                    exc_info=True,
                )
        if stats["scanned"]:
            log.info("concept_lifecycle sweep: %s", stats)
        return stats

    # ── per-concept processing ─────────────────────────────────────────

    def _process(
        self, concept: "Concept", now: datetime, stats: dict[str, Any]
    ) -> None:
        stats["scanned"] += 1
        first_eval = concept.last_lifecycle_at is None

        # 1. Accrual + decay (engagement-driven, per-concept anchor).
        engaged_days = self._engaged_days(concept, now)
        target = confidence_target(concept.distinct_source_count)
        reinforced = self._reinforced_since_last(concept, first_eval)
        new_conf = next_confidence(
            concept.confidence,
            engaged_days=engaged_days,
            halflife_days=self._f("concept_confidence_halflife_days", 45.0),
            plasticity=concept.plasticity,
            target=target,
            reinforced=reinforced,
        )
        concept.confidence = new_conf
        # Kind-default plasticity on first evaluation (identity => sticky).
        if first_eval:
            kind_plast = self._kind_plasticity(concept.kind)
            if kind_plast is not None:
                concept.plasticity = kind_plast

        # 2-5. Status transition (confidence-driven; age only gates/ delays).
        old_status = concept.status
        new_status, event_type = self._transition(concept, now, new_conf)
        if new_status != old_status:
            concept.status = new_status
            if new_status == "active" and not concept.promoted_at:
                concept.promoted_at = now.isoformat()
            self._tally(stats, event_type)

        # 6. Stamp the per-concept engagement anchor + persist.
        concept.last_lifecycle_at = now.isoformat()
        if self._clock_active():
            concept.last_lifecycle_engagement = float(
                self._engagement_clock.total()  # type: ignore[union-attr]
            )
        self._store.update(concept)

        # 7. Emit a lifecycle event + cascade to dependents on a change.
        if new_status != old_status:
            self._emit(concept, event_type, new_conf, now)
            stats["events"] += 1
            self._mark_dependents_stale(concept)

    # ── transition logic ───────────────────────────────────────────────

    def _transition(
        self, concept: "Concept", now: datetime, conf: float
    ) -> tuple[str, str]:
        """Return ``(new_status, event_type)``. ``event_type`` is only
        meaningful when the status actually changes."""
        status = concept.status
        dormant_floor = self._f("concept_dormant_confidence_floor", 0.35)
        retire_floor = self._f("concept_retire_confidence_floor", 0.15)

        if status == "candidate":
            if self._gate(concept, now, conf):
                return "active", "promoted"
            if self._is_stale_candidate(concept, now):
                return "retired", "retired"
            return "candidate", ""

        if status == "active":
            if conf < dormant_floor:
                return "dormant", "dormant"
            return "active", ""

        if status == "dormant":
            promote_min_conf = self._f("concept_promote_min_confidence", 0.6)
            if conf >= promote_min_conf:
                return "active", "revived"
            if conf < retire_floor:
                return "retired", "retired"
            return "dormant", ""

        if status == "retired":
            # Revivable, but only on fresh evidence (confidence recovered).
            if self._reinforced_since_last(concept, False):
                if self._gate(concept, now, conf):
                    return "active", "revived"
                if conf >= dormant_floor:
                    # Was active before => back to dormant; never promoted
                    # => re-enter the candidate funnel.
                    if concept.promoted_at:
                        return "dormant", "revived"
                    return "candidate", "revived"
            return "retired", ""

        # Unknown status: leave it be.
        return status, ""

    def _gate(self, concept: "Concept", now: datetime, conf: float) -> bool:
        kind = get_kind(concept.kind)
        gate = getattr(kind, "promotion_gate", None) or set_evidence_gate
        # L21: until the topic graph matures, promote against a stricter
        # bar (more distinct sources + higher confidence) so early,
        # thinly-evidenced candidates don't lock in as beliefs. Age is
        # left untouched (it only ever delays). When no provider is wired,
        # treat the graph as mature (normal thresholds).
        mature = self._graph_mature()
        min_sources = self._i("concept_promote_min_sources", 2)
        min_confidence = self._f("concept_promote_min_confidence", 0.6)
        if not mature:
            min_sources = max(
                min_sources, self._i("concept_promote_young_min_sources", 3)
            )
            min_confidence = max(
                min_confidence,
                self._f("concept_promote_young_min_confidence", 0.72),
            )
        return bool(
            gate(
                distinct_source_count=concept.distinct_source_count,
                age_days=self._age_days(concept, now),
                confidence=conf,
                min_sources=min_sources,
                min_age_days=self._f("concept_promote_min_age_days", 2.0),
                min_confidence=min_confidence,
            )
        )

    def _graph_mature(self) -> bool:
        """Whether the topic graph has cleared the L21 maturity floor.
        Defaults to ``True`` when no provider is wired so lean / test
        deployments keep the normal promotion bar."""
        provider = self._graph_mature_provider
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:
            log.debug("graph_mature_provider raised", exc_info=True)
            return True

    def _is_stale_candidate(self, concept: "Concept", now: datetime) -> bool:
        ttl = self._f("concept_candidate_ttl_days", 21.0)
        min_sources = self._i("concept_promote_min_sources", 2)
        return (
            self._age_days(concept, now) >= ttl
            and concept.distinct_source_count < min_sources
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _engaged_days(self, concept: "Concept", now: datetime) -> float:
        clamp = self._f("concept_decay_max_catchup_days", 3.0)
        if concept.last_lifecycle_at is None:
            # First evaluation: no elapsed time to decay over yet.
            return 0.0
        if self._clock_active():
            anchor = concept.last_lifecycle_engagement
            if anchor is None:
                return 0.0
            return self._engagement_clock.engaged_days_since(  # type: ignore[union-attr]
                float(anchor), clamp_days=clamp
            )
        # Wall-clock fallback.
        last = _parse_iso(concept.last_lifecycle_at)
        if last is None:
            return 0.0
        days = max(0.0, (now - last).total_seconds() / 86400.0)
        return min(days, clamp)

    def _reinforced_since_last(
        self, concept: "Concept", first_eval: bool
    ) -> bool:
        if first_eval:
            # First time we look: treat as reinforced so confidence
            # reflects the current evidence (target), not the L2 seed.
            return True
        last_reinforced = _parse_iso(concept.last_reinforced_at)
        last_eval = _parse_iso(concept.last_lifecycle_at)
        if last_reinforced is None:
            return False
        if last_eval is None:
            return True
        return last_reinforced > last_eval

    def _age_days(self, concept: "Concept", now: datetime) -> float:
        anchor = _parse_iso(concept.first_evidence_at) or _parse_iso(
            concept.created_at
        )
        if anchor is None:
            return 0.0
        return max(0.0, (now - anchor).total_seconds() / 86400.0)

    def _clock_active(self) -> bool:
        return (
            self._engagement_clock is not None
            and bool(getattr(self._engagement_clock, "enabled", False))
        )

    @staticmethod
    def _kind_plasticity(kind: str) -> float | None:
        return _KIND_PLASTICITY.get(kind)

    def _mark_dependents_stale(self, concept: "Concept") -> None:
        """Meta cascade: when a base concept changes status, mark its
        dependents stale so the next tick re-evaluates them (rather than
        recursing inline). A no-op today -- only ``set``/identity concepts
        exist, which have no dependents -- but this is the batch-safe hook
        for later meta kinds."""
        try:
            dep_ids = self._store.dependents_of(concept.concept_id)
        except Exception:
            return
        for dep_id in dep_ids:
            dep = self._store.get(dep_id)
            if dep is None:
                continue
            dep.last_lifecycle_at = None
            dep.last_lifecycle_engagement = None
            try:
                self._store.update(dep)
            except Exception:
                log.debug(
                    "cascade stale-mark failed (id=%s)", dep_id, exc_info=True
                )

    # ── events ──────────────────────────────────────────────────────────

    @staticmethod
    def _tally(stats: dict[str, Any], event_type: str) -> None:
        if event_type == "promoted":
            stats["promoted"] += 1
        elif event_type == "dormant":
            stats["dormant"] += 1
        elif event_type == "retired":
            stats["retired"] += 1
        elif event_type == "revived":
            stats["revived"] += 1

    def _emit(
        self,
        concept: "Concept",
        event_type: str,
        conf: float,
        now: datetime,
    ) -> None:
        if self._events is None or not event_type:
            return
        reason = self._reason(concept, event_type, conf, now)
        try:
            self._events.add(
                ConceptEvent(
                    event_type=event_type,
                    kind=concept.kind,
                    subject=concept.subject,
                    label=concept.label,
                    confidence=float(conf),
                    novelty=0.0,
                    evidence_count=concept.evidence_count,
                    distinct_source_count=concept.distinct_source_count,
                    source_kinds="",
                    reason=reason,
                    concept_id=concept.concept_id,
                    created_at=now.isoformat(),
                )
            )
        except Exception:
            log.debug("lifecycle event emit failed", exc_info=True)

    def _reason(
        self,
        concept: "Concept",
        event_type: str,
        conf: float,
        now: datetime,
    ) -> str:
        distinct = concept.distinct_source_count
        age = self._age_days(concept, now)
        if event_type == "promoted":
            return (
                f"Promoted to active: {distinct} distinct source(s), "
                f"stable {age:.0f}d, confidence {conf:.2f}."
            )
        if event_type == "dormant":
            return (
                f"Slid to dormant: confidence fell to {conf:.2f} "
                "without fresh evidence."
            )
        if event_type == "retired":
            if concept.promoted_at:
                return (
                    f"Retired: confidence {conf:.2f} after a long quiet "
                    "stretch."
                )
            return (
                f"Retired stale candidate: only {distinct} source(s) after "
                f"{age:.0f}d."
            )
        if event_type == "revived":
            return (
                f"Revived: fresh evidence lifted confidence to {conf:.2f}."
            )
        return ""


__all__ = ["ConceptLifecycleWorker"]
