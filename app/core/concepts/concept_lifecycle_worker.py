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

**Engagement-driven decay *and* age.** Elapsed time is active-conversation
time from the shared :class:`EngagementClock` (falling back to wall-clock
when the clock is absent/disabled), so being away or quiet doesn't crater
confidence. The same clock now drives *age*: promotion's stability floor
(``concept_promote_min_age_days``) and the candidate TTL
(``concept_candidate_ttl_days``) are measured in *engaged* days via the
per-concept ``first_evidence_engagement`` anchor, not wall-clock calendar
days -- so a concept matures with real interaction (~1h active convo per
"day" at the default ``engagement_seconds_per_day``) instead of idling to
maturity on the calendar. Age only ever *gates*; confidence still drives
every status floor. Un-anchored concepts (brand-new, pre-first-stamp) and
clock-disabled deployments fall back to wall-clock age.

Transitions (each emits one :class:`ConceptEvent` to the discovery
timeline): ``candidate -> active`` (promotion gate), ``candidate ->
retired`` (stale), ``active -> dormant`` / ``dormant -> retired``
(confidence floors), and revival of a ``dormant`` / ``retired`` concept
when fresh evidence lifts confidence back up. ``retired`` is revivable,
not terminal.

**L9 living beliefs.** When a :class:`ConceptContradictionDetector` is
injected, each tick also checks a bounded rolling sub-batch of *active*
concepts for counter-evidence (a memory that disproves the belief). A
confirmed contradiction applies a plasticity-damped confidence penalty
(:func:`~app.core.concepts.concept_lifecycle.apply_contradiction_penalty`)
and, once confidence falls below ``concept_contradicted_confidence_floor``,
steps the concept ``active -> contradicted`` (a revivable "actively
disproven" status, distinct from a faded ``dormant``). A disproof moment
always emits a ``contradicted`` event, even when the belief only
weakened. Detection rides the same ``list_stalest`` round-robin (capped
at ``concept_contradiction_batch_size`` checks per tick), so the LLM /
memory-search cost never sweeps the whole active set in one tick. The
detector only *reads*; L3 remains the single writer.

**L15 belief revision.** Every confirmed contradiction also persists a
``concept --contradicts--> memory`` edge (polarity -1) so the disproof
relation lives in the graph. When a
:class:`~app.core.concepts.concept_belief_reviser.ConceptBeliefReviser`
is injected, the tick that flips a concept ``-> contradicted`` also lets
the doubt flow *back down*: the reviser walks the concept's ``evidence``
memories and arbitrates, per memory, one of three resolutions --
(a) inaccurate -> lower its confidence; (b) superseded -> reclassify to
a ``past_event`` with a fresh ``relevance_until``; (c) fine (the concept
was a bad inference) -> no memory write. This is a **trigger, not a blind
write**: a concept's confidence never directly overwrites a memory's, and
pinned observations are never touched. Revision rides the same rolling
batch (at most ``concept_belief_revision_batch_size`` concepts per tick,
each up to ``concept_belief_revision_max_evidence`` memories), with its
own rate-limited maintenance-LLM budget. L3 stays the single writer of
*concept* state; the reviser only writes *memory* state, like F1 / F5.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.concept_kinds import (
    DEFAULT_PLASTICITY_MODULATION,
    get_kind,
)
from app.core.concepts.concept_lifecycle import (
    RelationshipSignal,
    apply_contradiction_penalty,
    confidence_target,
    drift_plasticity,
    effective_plasticity,
    next_confidence,
    set_evidence_gate,
)
from app.core.concepts.concept_event_store import ConceptEvent
from app.core.concepts.concept_store import ConceptEdge
from app.core.infra import timephrase
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.concepts.concept_belief_reviser import ConceptBeliefReviser
    from app.core.concepts.concept_contradiction import (
        ConceptContradictionDetector,
        ContradictionVerdict,
    )
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
        contradiction_detector: "ConceptContradictionDetector | None" = None,
        belief_reviser: "ConceptBeliefReviser | None" = None,
        relationship_signal_provider: (
            Callable[[], "RelationshipSignal | None"] | None
        ) = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = concept_store
        self._events = concept_event_store
        self._engagement_clock = engagement_clock
        self._graph_mature_provider = graph_mature_provider
        self._contradiction_detector = contradiction_detector
        self._belief_reviser = belief_reviser
        # L16: live trust/duration signal for relationship modulation of
        # plasticity. ``None`` (or a provider returning ``None``) => modulation
        # no-ops, so lean/test deployments keep the stored plasticity.
        self._relationship_signal_provider = relationship_signal_provider
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._clock = clock or timephrase.utcnow
        # L16 re-check slowdown: per-concept probe counter (in-memory, resets on
        # restart) used to skip the contradiction probe on a plasticity-scaled
        # stride so sticky concepts are re-examined less often.
        self._probe_counter: dict[int, int] = {}

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

    def _b(self, name: str, default: bool) -> bool:
        return bool(getattr(self._memory_settings, name, default))

    # L16 gates -- each of the three deferred pieces is independently
    # switchable; all default on.
    def _modulation_enabled(self) -> bool:
        return self._b("concept_plasticity_modulation_enabled", True)

    def _drift_enabled(self) -> bool:
        return self._b("concept_plasticity_drift_enabled", True)

    def _recheck_slowdown_enabled(self) -> bool:
        return self._b("concept_plasticity_recheck_slowdown_enabled", True)

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
            "demoted": 0,
            "dormant": 0,
            "retired": 0,
            "revived": 0,
            "contradicted": 0,
            # L9: work bounding -- how many active concepts got a
            # contradiction check this tick, and how many actually fired.
            "contradiction_checks": 0,
            "contradiction_hits": 0,
            # L16 piece 3: probes skipped this tick because the concept is
            # sticky (low effective plasticity) and off its re-check stride.
            "contradiction_skipped_sticky": 0,
            # L16 piece 1: relationship-modulation band crossings emitted as
            # ``plasticity_shift`` events this tick.
            "plasticity_shifts": 0,
            # L15 belief revision: concepts whose supporting memories got
            # re-examined this tick, and the resolutions applied.
            "belief_revisions": 0,
            "memories_lowered": 0,
            "memories_superseded": 0,
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

        # L16: the *effective* plasticity for this eval. For a kind that opts
        # into relationship modulation (boundary), the live trust/duration
        # signal raises the stored base toward its ceiling; for every other
        # kind this is exactly ``concept.plasticity`` (no behaviour change). The
        # stored base is never mutated here -- only piece-2 drift below does
        # that. Captured before any stamping so it matches the value the
        # confidence math and penalty actually use.
        base_plast = float(concept.plasticity)
        eff_plast = self._effective_plasticity(concept, base_plast)

        # 1. Accrual + decay (engagement-driven, per-concept anchor).
        engaged_days = self._engaged_days(concept, now)
        target = confidence_target(concept.distinct_source_count)
        reinforced = self._reinforced_since_last(concept, first_eval)
        new_conf = next_confidence(
            concept.confidence,
            engaged_days=engaged_days,
            halflife_days=self._f("concept_confidence_halflife_days", 45.0),
            plasticity=eff_plast,
            target=target,
            reinforced=reinforced,
        )
        concept.confidence = new_conf
        # Kind-default plasticity on first evaluation (identity => sticky).
        if first_eval:
            kind_plast = self._kind_plasticity(concept.kind)
            if kind_plast is not None:
                concept.plasticity = kind_plast

        # L9: counter-evidence probe (active concepts only, bounded per
        # tick). A confirmed contradiction knocks confidence down by a
        # plasticity-damped penalty *after* accrual/decay, so fresh
        # disproof can undo a reinforcement in the same pass. The probe cadence
        # + penalty both use the effective (modulated) plasticity (L16).
        verdict = self._maybe_detect_contradiction(concept, stats, eff_plast)
        if verdict is not None:
            new_conf = apply_contradiction_penalty(
                concept.confidence,
                penalty=self._f("concept_contradiction_penalty", 0.25),
                plasticity=eff_plast,
            )
            concept.confidence = new_conf
            # L15: record the disproof relation in the graph so belief
            # revision (and later graph consumers) can walk it. Upsert is
            # idempotent, so a repeated hit on the same memory is a no-op.
            self._persist_contradiction_edge(concept, verdict)

        # L16 observability ("never silently"): record the modulation as a
        # persisted ``influences`` edge and emit a ``plasticity_shift`` event
        # when the lift crosses a band -- so a boundary loosening is a visible
        # beat, not silent drift.
        self._record_modulation(concept, base_plast, eff_plast, now, stats)

        # L16 piece 2: one-way plasticity drift -- a settled *active* belief
        # gets stickier with age + confidence. Mutates the stored base (piece 1
        # modulation is read-live on top and never persisted). Skipped on first
        # eval so the kind band lands cleanly before any drift.
        if (
            self._drift_enabled()
            and concept.status == "active"
            and not first_eval
        ):
            concept.plasticity = drift_plasticity(
                concept.plasticity,
                confidence=concept.confidence,
                age_days=self._age_days(concept, now),
                floor=self._f("concept_plasticity_drift_floor", 0.15),
                rate=self._f("concept_plasticity_drift_rate", 0.05),
            )

        # L12 meta rule 3 (confidence bounding): a meta concept can't be more
        # certain than the shakiest concept it's built on. Applied after
        # accrual/penalty so a strong tension riding a wobbling base is reined
        # in *before* the gate sees its confidence. ``meta_moot`` (rule 2) is
        # carried down to the transition override below. No-op for base kinds.
        meta_moot = False
        if concept.evidence_model == "meta":
            new_conf, meta_moot = self._apply_meta_rules(concept, new_conf)
            concept.confidence = new_conf

        # 2-5. Status transition (confidence-driven; age only gates/ delays).
        old_status = concept.status
        new_status, event_type = self._transition(
            concept, now, new_conf, contradicted=verdict is not None
        )
        # L12 meta rule 2 (cascade): a tension whose base is no longer active is
        # moot -- never let it be (or become) active. Demote a promoted one to
        # dormant (revivable when the base returns and the tension is
        # re-proposed); hold a candidate/dormant/retired one back from
        # promotion/revival. The base's own status change already marked this
        # dependent stale, so it is being re-evaluated here.
        if (
            concept.evidence_model == "meta"
            and meta_moot
            and new_status == "active"
        ):
            if old_status == "active":
                new_status, event_type = "dormant", "dormant"
            else:
                new_status, event_type = old_status, ""
        status_changed = new_status != old_status
        if status_changed:
            concept.status = new_status
            if new_status == "active" and not concept.promoted_at:
                concept.promoted_at = now.isoformat()
            self._tally(stats, event_type)

        # L15: when a belief tips into ``contradicted``, let the doubt
        # flow back down to its supporting memories (bounded per tick).
        # Runs before the persist so a same-tick memory write and the
        # concept write land together; the reviser only writes *memory*
        # state, never concept state.
        if status_changed and new_status == "contradicted":
            self._maybe_revise_beliefs(concept, verdict, now, stats)

        # 6. Stamp the per-concept engagement anchors + persist.
        concept.last_lifecycle_at = now.isoformat()
        if self._clock_active():
            total = float(
                self._engagement_clock.total()  # type: ignore[union-attr]
            )
            concept.last_lifecycle_engagement = total
            # Anchor *age* to engaged time on first evaluation. A brand-new
            # concept is seconds old here, so this ~= its creation engagement
            # total; from now on promotion / candidate-TTL age accrues in
            # engaged (active-conversation) time, symmetric with decay. Only
            # set when unanchored so existing concepts (backfilled to 0.0 by
            # the v24 migration) keep their accrued engaged age.
            if concept.first_evidence_engagement is None:
                concept.first_evidence_engagement = total
        self._store.update(concept)

        # 7. Emit a lifecycle event + cascade to dependents. A confirmed
        # contradiction always hits the timeline (disproof is worth
        # recording even when the belief only weakened, status unchanged);
        # otherwise emit only on a status change.
        if verdict is not None:
            self._emit(
                concept, "contradicted", new_conf, now,
                reason_override=self._contradiction_reason(verdict, new_conf),
            )
            stats["events"] += 1
            if status_changed:
                self._mark_dependents_stale(concept)
        elif status_changed:
            self._emit(concept, event_type, new_conf, now)
            stats["events"] += 1
            self._mark_dependents_stale(concept)
        elif (
            not first_eval
            and reinforced
            and concept.status == "active"
        ):
            # Fresh distinct evidence landed on an already-active belief
            # without shifting its status -- a genuine reinforcement beat worth
            # a timeline row. Bounded: ``reinforced`` only trips when new
            # evidence arrived since the last eval, so this fires at most once
            # per tick per concept that was actually reinforced.
            self._emit(concept, "reinforced", new_conf, now)
            stats["events"] += 1

    # ── L9 contradiction probe ─────────────────────────────────────────

    def _maybe_detect_contradiction(
        self, concept: "Concept", stats: dict[str, Any], eff_plast: float
    ) -> "ContradictionVerdict | None":
        """Run the read-only detector for one active concept, bounded by
        the per-tick contradiction batch. Counts every *check* (a memory
        search, the real work unit) toward the batch so a single tick
        never sweeps the whole active set; the ``list_stalest`` ordering
        rotates which concepts are checked across ticks.

        L16 piece 3: a sticky (low effective-plasticity) concept is a settled
        belief, so it earns being re-examined *less often*. We skip the probe on
        a plasticity-scaled deterministic stride (in-memory, resets on restart)
        -- a fluid concept (``eff_plast ~ 1``) keeps ``stride == 1`` (unchanged),
        a sticky core belief is probed ~1/stride as often. The skip happens
        *before* the per-tick budget is consumed, so slowing sticky concepts
        frees the budget for concepts that actually need checking."""
        if self._contradiction_detector is None:
            return None
        if concept.status != "active":
            return None
        if self._recheck_slowdown_enabled():
            k = self._f("concept_plasticity_recheck_stride_k", 3.0)
            stride = 1 + round(max(0.0, k) * (1.0 - min(1.0, max(0.0, eff_plast))))
            cid = int(getattr(concept, "concept_id", 0) or 0)
            n = self._probe_counter.get(cid, 0) + 1
            self._probe_counter[cid] = n
            if stride > 1 and (n % stride) != 0:
                stats["contradiction_skipped_sticky"] = (
                    stats.get("contradiction_skipped_sticky", 0) + 1
                )
                return None
        batch = max(0, self._i("concept_contradiction_batch_size", 20))
        if stats["contradiction_checks"] >= batch:
            return None
        stats["contradiction_checks"] += 1
        try:
            verdict = self._contradiction_detector.detect(concept)
        except Exception:
            log.debug(
                "contradiction detect failed (id=%s)",
                getattr(concept, "concept_id", "?"),
                exc_info=True,
            )
            return None
        if verdict is not None:
            stats["contradiction_hits"] += 1
        return verdict

    def _persist_contradiction_edge(
        self, concept: "Concept", verdict: "ContradictionVerdict"
    ) -> None:
        """L15: upsert the ``concept --contradicts--> memory`` edge
        (polarity -1) for a confirmed disproof. Best-effort: a failure
        here must never break the lifecycle pass (edge-only bookkeeping).
        """
        memory_id = int(getattr(verdict, "memory_id", 0) or 0)
        if memory_id <= 0:
            return
        try:
            self._store.add_edge(
                ConceptEdge(
                    src_type="concept",
                    src_id=str(concept.concept_id),
                    dst_type="memory",
                    dst_id=str(memory_id),
                    relation="contradicts",
                    polarity=-1,
                    strength=float(getattr(verdict, "similarity", 1.0) or 1.0),
                )
            )
        except Exception:
            log.debug(
                "contradicts edge upsert failed (concept_id=%s mem_id=%s)",
                getattr(concept, "concept_id", "?"),
                memory_id,
                exc_info=True,
            )

    # ── L15 belief revision ─────────────────────────────────────────────

    def _maybe_revise_beliefs(
        self,
        concept: "Concept",
        verdict: "ContradictionVerdict | None",
        now: datetime,
        stats: dict[str, Any],
    ) -> None:
        """Run the belief reviser for one just-``contradicted`` concept,
        bounded by the per-tick revision batch. The reviser re-examines
        the concept's supporting memories (a memory search / LLM per
        borderline pair, the real work unit) so the count is capped like
        the L9 detector; ``list_stalest`` rotates which concepts get
        revised across ticks. The reviser writes only *memory* state."""
        if self._belief_reviser is None or verdict is None:
            return
        batch = max(0, self._i("concept_belief_revision_batch_size", 5))
        if stats["belief_revisions"] >= batch:
            return
        stats["belief_revisions"] += 1
        try:
            outcome = self._belief_reviser.revise(concept, verdict, now=now)
        except Exception:
            log.debug(
                "belief revision failed (id=%s)",
                getattr(concept, "concept_id", "?"),
                exc_info=True,
            )
            return
        stats["memories_lowered"] += int(getattr(outcome, "lowered", 0))
        stats["memories_superseded"] += int(getattr(outcome, "superseded", 0))

    def _contradiction_reason(
        self, verdict: "ContradictionVerdict", conf: float
    ) -> str:
        snippet = (getattr(verdict, "snippet", "") or "").strip()
        base = f"Counter-evidence lowered confidence to {conf:.2f}"
        if snippet:
            if len(snippet) > 120:
                snippet = snippet[:119] + "\u2026"
            return f'{base}: "{snippet}".'
        return base + "."

    # ── transition logic ───────────────────────────────────────────────

    def _transition(
        self,
        concept: "Concept",
        now: datetime,
        conf: float,
        *,
        contradicted: bool = False,
    ) -> tuple[str, str]:
        """Return ``(new_status, event_type)``. ``event_type`` is only
        meaningful when the status actually changes. ``contradicted`` is
        set when the detector confirmed counter-evidence this tick."""
        status = concept.status
        dormant_floor = self._f("concept_dormant_confidence_floor", 0.35)
        retire_floor = self._f("concept_retire_confidence_floor", 0.15)
        contradicted_floor = self._f(
            "concept_contradicted_confidence_floor", 0.4
        )

        if status == "candidate":
            if self._gate(concept, now, conf):
                return "active", "promoted"
            if self._is_stale_candidate(concept, now):
                return "retired", "retired"
            return "candidate", ""

        if status == "active":
            # L9: confirmed disproof that drove confidence below the
            # contradicted floor flips to the "actively disproven" status
            # (distinct from a faded dormant). A contradiction that only
            # dented confidence leaves it active-but-weakened.
            if contradicted and conf < contradicted_floor:
                return "contradicted", "contradicted"
            # L22/L25: the evidence a belief was promoted on can be taken
            # away afterwards -- memories get deleted, pruned or merged,
            # and the reconciler drops the edges. Nothing used to notice:
            # the status floors read *confidence* only, so a concept whose
            # support had vanished entirely stayed active at 0.85 for as
            # long as decay took to reach the dormant floor (tens of
            # engaged days). An unsupported belief goes back to the
            # candidate funnel, where the TTL retires it if the evidence
            # never returns.
            if not self._has_any_evidence(concept):
                return "candidate", "demoted"
            if conf < dormant_floor:
                return "dormant", "dormant"
            return "active", ""

        if status == "contradicted":
            # Revivable: fresh reinforcing evidence that clears the
            # promote bar brings the belief back; sustained decay retires
            # it. Otherwise it stays disproven and quiet (never surfaced).
            promote_min_conf = self._f("concept_promote_min_confidence", 0.6)
            if (
                self._reinforced_since_last(concept, False)
                and conf >= promote_min_conf
            ):
                return "active", "revived"
            if conf < retire_floor:
                return "retired", "retired"
            return "contradicted", ""

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
        # thinly-evidenced candidates don't lock in as beliefs. The age
        # floor is left untouched (it only ever delays) and is now measured
        # in engaged days (see ``_age_days``). When no provider is wired,
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

    @staticmethod
    def _has_any_evidence(concept: "Concept") -> bool:
        """Whether an active belief still rests on anything at all.

        Deliberately the weakest possible re-gate: zero distinct sources
        fails *every* kind's promotion gate, so demoting on it needs no
        per-kind reasoning and cannot misfire. That matters because the
        source bar is not uniform -- ``boundary`` and
        ``communication_style`` override it down to 1, since a single
        deliberate anchor is enough for them -- so re-applying the full
        bar here would strip legitimately single-anchored beliefs.

        Re-gating against each kind's real source bar would catch more
        (32 actives currently sit below it, against 6 at zero), but that
        is a threshold decision to make against the quality scoreboard
        rather than a bug fix, so it stays out of this path for now.
        """
        return int(concept.distinct_source_count) > 0

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
        """Concept age in *engaged* (active-conversation) days when the
        shared clock is active and the concept has been anchored --
        symmetric with the engagement-driven decay -- so promotion and the
        candidate TTL advance with real interaction rather than wall-clock
        calendar days. Falls back to wall-clock for un-anchored concepts
        (brand-new, before their first lifecycle stamp) and whenever the
        engagement clock is disabled / unwired. Unlike ``_engaged_days``
        this is intentionally *not* catch-up-clamped: age must accumulate
        without bound, it only ever gates promotion."""
        if self._clock_active():
            anchor = concept.first_evidence_engagement
            if anchor is not None:
                return self._engagement_clock.engaged_days_since(  # type: ignore[union-attr]
                    float(anchor)
                )
        wall_anchor = _parse_iso(concept.first_evidence_at) or _parse_iso(
            concept.created_at
        )
        if wall_anchor is None:
            return 0.0
        return max(0.0, (now - wall_anchor).total_seconds() / 86400.0)

    def _clock_active(self) -> bool:
        return (
            self._engagement_clock is not None
            and bool(getattr(self._engagement_clock, "enabled", False))
        )

    def _kind_plasticity(self, kind: str) -> float | None:
        """Kind-default plasticity applied on a concept's first lifecycle
        evaluation (L16). This is the per-concept learning rate the engine
        damps every confidence move by -- accrual, decay, L9 disproof, and
        the L15 revision delta -- so a sticky (low-plasticity) core trait
        resists change in both directions.

        Resolution order: the ``identity`` kind keeps its tunable
        ``concept_identity_plasticity`` setting override; otherwise the
        kind's registered ``plasticity_default`` band; otherwise the
        general ``concept_default_plasticity`` setting."""
        if kind == "identity":
            return self._f("concept_identity_plasticity", 0.3)
        registered = get_kind(kind)
        if registered is not None and registered.plasticity_default is not None:
            return float(registered.plasticity_default)
        return self._f("concept_default_plasticity", 0.5)

    def _effective_plasticity(self, concept: "Concept", base: float) -> float:
        """L16 piece 1: the relationship-modulated plasticity for this eval.

        For a kind that opts into modulation (only ``boundary`` today) with a
        live relationship signal available, this raises ``base`` toward the
        kind's ceiling as trust + duration grow; otherwise it returns ``base``
        unchanged -- so every non-opted kind (and every lean/test deployment
        without a signal provider) keeps exactly its stored plasticity."""
        if not self._modulation_enabled() or self._relationship_signal_provider is None:
            return base
        registered = get_kind(concept.kind)
        mod = getattr(
            registered, "plasticity_modulation", DEFAULT_PLASTICITY_MODULATION
        )
        if mod is DEFAULT_PLASTICITY_MODULATION:
            return base
        try:
            signal = self._relationship_signal_provider() or RelationshipSignal()
        except Exception:
            log.debug("relationship_signal_provider raised", exc_info=True)
            return base
        return effective_plasticity(base, signal=signal, mod=mod)

    def _record_modulation(
        self,
        concept: "Concept",
        base_plast: float,
        eff_plast: float,
        now: datetime,
        stats: dict[str, Any],
    ) -> None:
        """L16 "never silently": persist the modulation as one ``influences``
        edge (``signal:relationship_trust --influences--> concept``) and emit a
        ``plasticity_shift`` event when the lift crosses a band vs. the last
        recorded strength. Best-effort -- edge/event bookkeeping must never break
        the lifecycle pass. No-op when modulation is off or produced no lift."""
        if not self._modulation_enabled():
            return
        lift = float(eff_plast) - float(base_plast)
        if lift <= 0.0:
            return
        cid = int(getattr(concept, "concept_id", 0) or 0)
        if cid <= 0:
            return
        try:
            prior = 0.0
            for edge in self._store.edges_into("concept", cid):
                if edge.relation == "influences" and edge.src_type == "signal":
                    prior = float(edge.strength or 0.0)
                    break
            delta = self._f("concept_plasticity_shift_event_delta", 0.1)
            if abs(lift - prior) < delta:
                return
            self._store.add_edge(
                ConceptEdge(
                    src_type="signal",
                    src_id="relationship_trust",
                    dst_type="concept",
                    dst_id=str(cid),
                    relation="influences",
                    polarity=1,
                    strength=lift,
                )
            )
            loosening = lift > prior
            reason = (
                f"Boundary loosening as trust deepens (plasticity "
                f"{base_plast:.2f} -> {eff_plast:.2f})."
                if loosening
                else (
                    f"Boundary tightening as the signal eases (plasticity "
                    f"{base_plast:.2f} -> {eff_plast:.2f})."
                )
            )
            self._emit(
                concept,
                "plasticity_shift",
                concept.confidence,
                now,
                reason_override=reason,
            )
            stats["plasticity_shifts"] = stats.get("plasticity_shifts", 0) + 1
        except Exception:
            log.debug(
                "modulation record failed (concept_id=%s)", cid, exc_info=True
            )

    def _mark_dependents_stale(self, concept: "Concept") -> None:
        """Meta cascade (L12 rule 2): when a base concept changes status, mark
        its dependents stale so the next tick re-evaluates them (rather than
        recursing inline). Live for tension concepts (L12) -- a base that
        promotes / retires / is disproven walks to the tensions built on it via
        ``dependents_of`` (the ``concept -> concept`` evidence edges), forcing
        :meth:`_apply_meta_rules` to re-check them on the next round-robin."""
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

    def _apply_meta_rules(
        self, concept: "Concept", conf: float
    ) -> tuple[float, bool]:
        """Meta rules 2 + 3 for a ``evidence_model=="meta"`` concept.

        Resolve the base concepts it references (its ``("concept", ...)``
        evidence edges) and:

        - **Rule 3 (confidence bounding):** return ``min(conf, min(active base
          confidences))`` -- a meta can be no more certain than the shakiest
          concept it is *still* built on.
        - **Rule 2 (moot):** flag ``moot`` when the meta no longer stands on
          enough active bases. The floor is **arity-aware**: a tension (L12)
          holds exactly two concepts in friction, so losing EITHER side makes it
          moot; a generalization (L20) abstracts several concepts, so it stays
          live as long as at least TWO children remain active and only goes moot
          when fewer than two survive. With no resolvable base at all it is moot
          by definition.

        Returns ``(bounded_confidence, moot)``. Base kinds never call this."""
        base_ids: list[int] = []
        try:
            for e in self._store.evidence_of(concept.concept_id):
                if e.src_type == "concept":
                    try:
                        base_ids.append(int(e.src_id))
                    except (TypeError, ValueError):
                        continue
        except Exception:
            log.debug(
                "meta evidence resolve failed (id=%s)",
                concept.concept_id, exc_info=True,
            )
            return conf, True
        if not base_ids:
            return conf, True

        base_confs: list[float] = []
        missing_any = False
        for bid in base_ids:
            base = self._store.get(bid)
            if base is None or base.status != "active":
                missing_any = True
                continue
            base_confs.append(float(base.confidence))
        bounded = min(conf, min(base_confs)) if base_confs else conf
        # Arity-aware moot: a generalization survives losing a child as long as
        # >= 2 remain (it abstracts many); every other meta (tension) needs ALL
        # of its bases, so any missing base makes it moot.
        if concept.kind == "generalization":
            moot = len(base_confs) < 2
        else:
            moot = missing_any or not base_confs
        return bounded, moot

    # ── events ──────────────────────────────────────────────────────────

    @staticmethod
    def _tally(stats: dict[str, Any], event_type: str) -> None:
        if event_type == "promoted":
            stats["promoted"] += 1
        elif event_type == "demoted":
            stats["demoted"] += 1
        elif event_type == "dormant":
            stats["dormant"] += 1
        elif event_type == "retired":
            stats["retired"] += 1
        elif event_type == "revived":
            stats["revived"] += 1
        elif event_type == "contradicted":
            stats["contradicted"] += 1

    def _emit(
        self,
        concept: "Concept",
        event_type: str,
        conf: float,
        now: datetime,
        *,
        reason_override: str | None = None,
    ) -> None:
        if self._events is None or not event_type:
            return
        reason = (
            reason_override
            if reason_override is not None
            else self._reason(concept, event_type, conf, now)
        )
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
        if event_type == "demoted":
            return (
                "Demoted to candidate: all supporting evidence was removed, "
                "so the belief no longer rests on anything."
            )
        if event_type == "revived":
            return (
                f"Revived: fresh evidence lifted confidence to {conf:.2f}."
            )
        if event_type == "reinforced":
            return (
                f"Reinforced: {distinct} distinct source(s), confidence "
                f"now {conf:.2f}."
            )
        if event_type == "contradicted":
            # Normally supplied via reason_override with the disproving
            # snippet; this is the bare fallback.
            return (
                f"Contradicted: counter-evidence lowered confidence to "
                f"{conf:.2f}."
            )
        return ""


__all__ = ["ConceptLifecycleWorker"]
