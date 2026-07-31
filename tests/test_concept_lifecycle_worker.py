"""Tests for the L3 :class:`ConceptLifecycleWorker` (single writer).

Covers promotion (+ no-promotion gates), saturating/bounded confidence,
the engagement-driven downtime guard + catch-up clamp, dormancy/retire
by confidence floor, stale-candidate retirement, revival of a retired
concept on fresh evidence, single-writer discipline, the disabled
short-circuit, idempotency, batching (rolling round-robin), per-concept
anchor correctness (no double-decay), and the set-evidence gate.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.concepts.concept_lifecycle import (
    accrual_alpha,
    identity_evidence_gate,
    next_confidence,
    set_evidence_gate,
    value_evidence_gate,
)
from app.core.concepts.concept_lifecycle_worker import ConceptLifecycleWorker
from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.engagement_clock import EngagementClock

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _settings(**over) -> SimpleNamespace:
    base = dict(
        concept_lifecycle_enabled=True,
        concept_lifecycle_interval_seconds=300,
        concept_lifecycle_batch_size=100,
        concept_promote_min_sources=2,
        concept_promote_min_age_days=2.0,
        concept_promote_min_confidence=0.6,
        concept_confidence_halflife_days=45.0,
        concept_decay_max_catchup_days=3.0,
        concept_dormant_confidence_floor=0.35,
        concept_retire_confidence_floor=0.15,
        concept_candidate_ttl_days=21.0,
        concept_promote_young_min_sources=3,
        concept_promote_young_min_confidence=0.72,
        # L16 plasticity modulation / drift / re-check slowdown. Drift +
        # slowdown default OFF in the shared stub so the existing confidence /
        # contradiction assertions stay behaviour-neutral; the dedicated L16
        # suite enables them explicitly. Modulation is a no-op here anyway (the
        # harness wires no relationship_signal_provider).
        concept_plasticity_modulation_enabled=True,
        concept_plasticity_duration_days_full=180.0,
        concept_plasticity_shift_event_delta=0.1,
        concept_plasticity_drift_enabled=False,
        concept_plasticity_drift_rate=0.05,
        concept_plasticity_drift_floor=0.15,
        concept_plasticity_recheck_slowdown_enabled=False,
        concept_plasticity_recheck_stride_k=3.0,
        # engagement clock knobs (for the shared clock instance)
        engagement_clock_enabled=True,
        engagement_seconds_per_day=3600.0,
        engagement_idle_cap_seconds=300.0,
        engagement_min_turn_seconds=15.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _KV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = str(value)


def _harness(
    settings=None,
    *,
    with_clock=True,
    concepts_enabled=True,
    graph_mature=None,
    detector=None,
    belief_reviser=None,
    relationship_signal_provider=None,
):
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "test.db")
    store = ConceptStore(db)
    events = ConceptEventStore(db)
    settings = settings or _settings()
    kv = _KV()
    clock = (
        EngagementClock(
            kv_get=kv.get, kv_set=kv.set, settings=settings,
            clock=lambda: _NOW,
        )
        if with_clock
        else None
    )
    worker = ConceptLifecycleWorker(
        concept_store=store,
        concept_event_store=events,
        engagement_clock=clock,
        graph_mature_provider=graph_mature,
        contradiction_detector=detector,
        belief_reviser=belief_reviser,
        relationship_signal_provider=relationship_signal_provider,
        memory_settings=settings,
        agent_settings=SimpleNamespace(concepts_enabled=concepts_enabled),
        clock=lambda: _NOW,
    )
    return SimpleNamespace(
        db=db, store=store, events=events, kv=kv, clock=clock, worker=worker,
        settings=settings,
    )


def _add(store: ConceptStore, **over) -> Concept:
    base = dict(
        label="Jacob values understanding systems",
        kind="identity",
        subject="user",
        status="candidate",
        confidence=0.5,
        plasticity=0.5,
        evidence_count=3,
        distinct_source_count=2,
        first_evidence_at=_iso(3),
        last_reinforced_at=None,
        last_lifecycle_at=None,
        last_lifecycle_engagement=None,
    )
    base.update(over)
    c = Concept(**base)
    store.add(c)
    return c


def _seed_engaged_days(h, days: float) -> None:
    """Put ``days`` of engaged time on the shared clock.

    ``engagement_seconds_per_day=3600`` => 1 engaged day == 3600 units. A
    concept anchored at ``first_evidence_engagement=0.0`` then reads exactly
    ``days`` as its age. Promotion tests need this because the age floor is
    measured in *engaged* days whenever the clock is live -- an old
    ``first_evidence_at`` alone buys nothing.
    """
    h.kv.set("engagement.total_units", str(days * 3600.0))


class PromotionTests(unittest.TestCase):
    def test_promotes_when_gate_clears(self) -> None:
        h = _harness()
        _seed_engaged_days(h, 3)
        # Seed at the confidence bar: with L16's plasticity-damped accrual
        # a reinforced first eval keeps the seed (accrual never lowers), so
        # this isolates the gate from the accrual ramp.
        c = _add(
            h.store, distinct_source_count=3, first_evidence_at=_iso(3),
            first_evidence_engagement=0.0, confidence=0.7,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")
        self.assertTrue(got.promoted_at)
        self.assertGreaterEqual(got.confidence, 0.6)
        evs = h.events.list(limit=10)
        self.assertEqual([e.event_type for e in evs], ["promoted"])

    def test_no_promotion_single_source(self) -> None:
        h = _harness()
        c = _add(h.store, distinct_source_count=1, first_evidence_at=_iso(5))
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")
        self.assertEqual(h.events.count(), 0)

    def test_no_promotion_too_young(self) -> None:
        h = _harness()
        c = _add(h.store, distinct_source_count=3, first_evidence_at=_iso(0.5))
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")
        self.assertEqual(h.events.count(), 0)

    def test_confidence_is_bounded(self) -> None:
        h = _harness()
        c = _add(h.store, distinct_source_count=50, first_evidence_at=_iso(9))
        h.worker.run()
        self.assertLessEqual(h.store.get(c.concept_id).confidence, 0.97)


class EngagementAgeTests(unittest.TestCase):
    """Promotion / TTL age is engaged (active-conversation) time, not
    wall-clock, when the shared clock is on (via ``first_evidence_engagement``).
    ``engagement_seconds_per_day=3600`` => 1 engaged day == 3600 units."""

    def test_engaged_age_promotes_despite_young_wallclock(self) -> None:
        # Anchored at clock start; the clock shows 3 engaged days even
        # though the concept is only ~2.4h old on the calendar. Engaged
        # age (3) clears the 2-day floor -> promotes.
        h = _harness()
        _seed_engaged_days(h, 3)
        c = _add(
            h.store,
            distinct_source_count=3,
            first_evidence_at=_iso(0.1),
            first_evidence_engagement=0.0,
            confidence=0.7,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_wallclock_age_ignored_when_engaged_age_too_low(self) -> None:
        # 30 calendar days old, but only 1 engaged day accrued. The old
        # wall-clock gate would have promoted long ago; the engaged gate
        # (1 < 2) holds it as a candidate.
        h = _harness()
        h.kv.set("engagement.total_units", str(1 * 3600.0))
        c = _add(
            h.store,
            distinct_source_count=2,
            first_evidence_at=_iso(30),
            first_evidence_engagement=0.0,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")
        self.assertEqual(h.events.count(), 0)

    def test_new_concept_anchors_on_first_eval(self) -> None:
        # Un-anchored (brand-new): the worker stamps the engagement anchor at
        # the current total, so age accrues in engaged time from here on.
        h = _harness()
        _seed_engaged_days(h, 5)
        c = _add(
            h.store,
            distinct_source_count=2,
            first_evidence_at=_iso(0.1),
            first_evidence_engagement=None,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "candidate")
        self.assertAlmostEqual(got.first_evidence_engagement, 5 * 3600.0)

    def test_first_eval_age_is_zero_not_wallclock(self) -> None:
        # The offline-gap case. A candidate minted just before a long
        # shutdown is 30 *calendar* days old on its first evaluation but has
        # lived through no engaged time at all. The anchor is stamped before
        # the gate reads it, so age is 0 and the 2-day floor holds -- were it
        # stamped afterwards the gate would fall back to wall-clock and let
        # idle downtime mature the candidate.
        h = _harness()
        _seed_engaged_days(h, 9)
        c = _add(
            h.store,
            distinct_source_count=5,
            first_evidence_at=_iso(30),
            first_evidence_engagement=None,
            confidence=0.9,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "candidate")
        self.assertAlmostEqual(got.first_evidence_engagement, 9 * 3600.0)
        # Two engaged days later the same candidate clears the floor, so it
        # was the age gate holding it, not the source or confidence bars.
        _seed_engaged_days(h, 11)
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_unanchored_reeval_keeps_wallclock_age(self) -> None:
        # A concept that predates the v24 anchor backfill: already evaluated
        # (``last_lifecycle_at`` set) but still un-anchored. Re-anchoring it
        # would silently reset its accrued age to zero, so the wall-clock
        # fallback stays in force and its 30 days of age still count.
        h = _harness()
        _seed_engaged_days(h, 0)
        c = _add(
            h.store,
            distinct_source_count=3,
            first_evidence_at=_iso(30),
            first_evidence_engagement=None,
            last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0,
            confidence=0.7,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")
        self.assertIsNone(got.first_evidence_engagement)

    def test_wallclock_fallback_when_clock_disabled(self) -> None:
        # Clock unwired -> age is wall-clock regardless of the anchor, so a
        # 3-day-old candidate promotes exactly as before.
        h = _harness(with_clock=False)
        c = _add(
            h.store,
            distinct_source_count=3,
            first_evidence_at=_iso(3),
            first_evidence_engagement=0.0,
            confidence=0.7,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")


class DecayTests(unittest.TestCase):
    def test_downtime_guard_clamps_decay(self) -> None:
        # Active concept, no fresh evidence, but the engagement clock
        # jumped by 30 "days" of active time -> only the catch-up clamp
        # worth of decay applies, staying well above the dormant floor.
        h = _harness(_settings(concept_confidence_halflife_days=45.0))
        c = _add(
            h.store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(10),
        )
        h.kv.set("engagement.total_units", str(30 * 3600.0))  # 30 engaged days
        h.worker.run()
        got = h.store.get(c.concept_id)
        # clamp = 3 days; eff halflife = 45*(2-0.5)=67.5 -> 0.9*0.5^(3/67.5)
        self.assertAlmostEqual(got.confidence, 0.9 * (0.5 ** (3 / 67.5)), places=4)
        self.assertEqual(got.status, "active")
        # Anchor advanced to the current clock total.
        self.assertAlmostEqual(got.last_lifecycle_engagement, 30 * 3600.0)

    def test_reinforcement_snaps_confidence_up(self) -> None:
        # High plasticity (1.0) keeps the pre-L16 full snap straight to
        # target on reinforcement; the damped-approach case is covered in
        # AccrualPlasticityTests.
        h = _harness()
        c = _add(
            h.store, status="active", confidence=0.2, plasticity=1.0,
            distinct_source_count=4, last_lifecycle_at=_iso(2),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(1),
            promoted_at=_iso(5), first_evidence_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")  # no decay
        h.worker.run()
        # reinforced (last_reinforced newer than last eval) -> snap to target
        self.assertGreaterEqual(h.store.get(c.concept_id).confidence, 0.9)


class TransitionTests(unittest.TestCase):
    def test_active_to_dormant(self) -> None:
        h = _harness()
        c = _add(
            h.store, status="active", confidence=0.30, distinct_source_count=1,
            last_lifecycle_at=_iso(1), last_lifecycle_engagement=0.0,
            last_reinforced_at=_iso(5), promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")  # no decay; conf stays 0.30
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "dormant")
        self.assertIn("dormant", [e.event_type for e in h.events.list()])

    def test_dormant_to_retired(self) -> None:
        h = _harness()
        c = _add(
            h.store, status="dormant", confidence=0.10, distinct_source_count=1,
            last_lifecycle_at=_iso(1), last_lifecycle_engagement=0.0,
            last_reinforced_at=_iso(5), promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "retired")
        self.assertIn("retired", [e.event_type for e in h.events.list()])

    def test_stale_candidate_retired(self) -> None:
        h = _harness()
        c = _add(
            h.store, status="candidate", distinct_source_count=1,
            first_evidence_at=_iso(30), last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0,
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "retired")
        self.assertIn("retired", [e.event_type for e in h.events.list()])

    def test_active_with_no_evidence_left_is_demoted(self) -> None:
        # L25 reconciles a concept's edges away when its supporting
        # memories are deleted. The status floors read confidence only, so
        # this belief would otherwise have stayed active at 0.9 for the
        # tens of engaged days decay needs to reach the dormant floor.
        h = _harness()
        c = _add(
            h.store, status="active", confidence=0.9, distinct_source_count=0,
            evidence_count=0, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        stats = h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "candidate")
        self.assertEqual(stats["demoted"], 1)
        self.assertIn("demoted", [e.event_type for e in h.events.list()])

    def test_demotion_does_not_touch_beliefs_with_a_single_source(self) -> None:
        # The source bar is not uniform across kinds (boundary and
        # communication_style accept a single deliberate anchor), so the
        # re-gate fires only at zero -- anything else is a threshold call.
        h = _harness()
        c = _add(
            h.store, status="active", confidence=0.9, distinct_source_count=1,
            last_lifecycle_at=_iso(1), last_lifecycle_engagement=0.0,
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_demoted_concept_can_repromote_when_evidence_returns(self) -> None:
        # Demotion is a return to the funnel, not a verdict: re-evidence
        # it and the ordinary promotion gate lets it back through.
        h = _harness(with_clock=False)  # wall-clock, so age already clears
        c = _add(
            h.store, status="active", confidence=0.9, distinct_source_count=0,
            first_evidence_at=_iso(9), last_lifecycle_at=_iso(2),
            promoted_at=_iso(9),
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")

        restored = h.store.get(c.concept_id)
        restored.distinct_source_count = 3
        restored.last_reinforced_at = _iso(0)
        restored.last_lifecycle_at = _iso(1)
        h.store.update(restored)
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_candidate_with_no_evidence_is_not_demoted_again(self) -> None:
        # The re-gate is an active-only concern; a candidate is already in
        # the funnel and the TTL is what removes it.
        h = _harness()
        c = _add(
            h.store, status="candidate", distinct_source_count=0,
            first_evidence_at=_iso(1), last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0,
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")
        self.assertEqual(h.events.count(), 0)

    def test_retired_revives_on_fresh_evidence(self) -> None:
        h = _harness(with_clock=False)  # wall-clock fallback
        c = _add(
            h.store, status="retired", confidence=0.10, distinct_source_count=4,
            first_evidence_at=_iso(20), last_lifecycle_at=_iso(2),
            last_reinforced_at=_iso(1), promoted_at=_iso(15),
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")
        self.assertIn("revived", [e.event_type for e in h.events.list()])


class DisciplineTests(unittest.TestCase):
    def test_single_writer_leaves_evidence_counts(self) -> None:
        h = _harness()
        c = _add(
            h.store, status="active", confidence=0.8, distinct_source_count=3,
            evidence_count=7, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, promoted_at=_iso(5),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.distinct_source_count, 3)
        self.assertEqual(got.evidence_count, 7)

    def test_disabled_short_circuit(self) -> None:
        h = _harness(concepts_enabled=False)
        _add(h.store)
        self.assertEqual(h.worker.run().get("skipped"), True)
        h2 = _harness(_settings(concept_lifecycle_enabled=False))
        _add(h2.store)
        self.assertEqual(h2.worker.run().get("skipped"), True)

    def test_idempotent_second_pass(self) -> None:
        h = _harness()
        _add(h.store, distinct_source_count=2, first_evidence_at=_iso(3))
        h.worker.run()
        n = h.events.count()
        h.worker.run()
        self.assertEqual(h.events.count(), n)


class BatchingTests(unittest.TestCase):
    def test_rolling_round_robin(self) -> None:
        h = _harness(_settings(concept_lifecycle_batch_size=2))
        h.kv.set("engagement.total_units", "0.0")
        cs = [_add(h.store, distinct_source_count=1, label=f"c{i}") for i in range(5)]
        r1 = h.worker.run()
        self.assertEqual(r1["scanned"], 2)
        stamped = sum(
            1 for c in cs if h.store.get(c.concept_id).last_lifecycle_at
        )
        self.assertEqual(stamped, 2)
        h.worker.run()
        stamped = sum(
            1 for c in cs if h.store.get(c.concept_id).last_lifecycle_at
        )
        self.assertEqual(stamped, 4)

    def test_per_concept_anchor_no_double_decay(self) -> None:
        settings = _settings(concept_confidence_halflife_days=2.0)
        # A: processed twice (1 engaged day each). B: once (2 engaged days).
        ha = _harness(settings)
        a = _add(
            ha.store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(9),
            promoted_at=_iso(9),
        )
        ha.kv.set("engagement.total_units", str(3600.0))
        ha.worker.run()
        ha.kv.set("engagement.total_units", str(2 * 3600.0))
        ha.worker.run()
        conf_a = ha.store.get(a.concept_id).confidence

        hb = _harness(settings)
        b = _add(
            hb.store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(9),
            promoted_at=_iso(9),
        )
        hb.kv.set("engagement.total_units", str(2 * 3600.0))
        hb.worker.run()
        conf_b = hb.store.get(b.concept_id).confidence
        self.assertAlmostEqual(conf_a, conf_b, places=6)


class _FakeDetector:
    """Stand-in for :class:`ConceptContradictionDetector`. Fires for the
    given concept ids (or all when ``target_ids`` is None), counting how
    many times it was consulted so batch bounding can be asserted."""

    def __init__(self, *, target_ids=None) -> None:
        self._target_ids = target_ids
        self.calls = 0

    def detect(self, concept):
        from app.core.concepts.concept_contradiction import (
            ContradictionVerdict,
        )

        self.calls += 1
        if (
            self._target_ids is not None
            and concept.concept_id not in self._target_ids
        ):
            return None
        return ContradictionVerdict(
            memory_id=99,
            similarity=0.8,
            heuristic_label="definite",
            llm_verdict=None,
            reason="loves/hates",
            snippet="Jacob no longer cares about systems",
        )


class ContradictionTests(unittest.TestCase):
    def test_active_to_contradicted_when_penalty_crosses_floor(self) -> None:
        h = _harness(detector=_FakeDetector())
        c = _add(
            h.store, status="active", confidence=0.5, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")  # no decay; conf stays 0.5
        stats = h.worker.run()
        got = h.store.get(c.concept_id)
        # 0.5 - 0.25*(0.5+0.5*0.5) = 0.3125 < contradicted_floor (0.4).
        self.assertEqual(got.status, "contradicted")
        self.assertAlmostEqual(got.confidence, 0.3125, places=4)
        types = [e.event_type for e in h.events.list()]
        self.assertIn("contradicted", types)
        self.assertEqual(stats["contradiction_hits"], 1)

    def test_high_confidence_belief_only_weakens(self) -> None:
        h = _harness(detector=_FakeDetector())
        c = _add(
            h.store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        got = h.store.get(c.concept_id)
        # 0.9 - 0.1875 = 0.7125 -> still active, but a disproof event fires.
        self.assertEqual(got.status, "active")
        self.assertAlmostEqual(got.confidence, 0.7125, places=4)
        self.assertIn(
            "contradicted", [e.event_type for e in h.events.list()]
        )

    def test_contradicted_revives_on_fresh_evidence(self) -> None:
        h = _harness(with_clock=False, detector=_FakeDetector())
        c = _add(
            h.store, status="contradicted", confidence=0.2,
            distinct_source_count=4, first_evidence_at=_iso(20),
            last_lifecycle_at=_iso(2), last_reinforced_at=_iso(1),
            promoted_at=_iso(15),
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")
        self.assertIn("revived", [e.event_type for e in h.events.list()])

    def test_contradicted_retires_on_decay(self) -> None:
        h = _harness(detector=_FakeDetector())
        c = _add(
            h.store, status="contradicted", confidence=0.10,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "retired")

    def test_detector_not_consulted_for_non_active(self) -> None:
        det = _FakeDetector()
        h = _harness(detector=det)
        _add(
            h.store, status="dormant", confidence=0.5, distinct_source_count=1,
            last_lifecycle_at=_iso(1), last_lifecycle_engagement=0.0,
            last_reinforced_at=_iso(5), promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        self.assertEqual(det.calls, 0)

    def test_batch_cap_bounds_checks_per_tick(self) -> None:
        det = _FakeDetector()
        h = _harness(
            _settings(concept_contradiction_batch_size=1),
            detector=det,
        )
        for i in range(3):
            _add(
                h.store, status="active", confidence=0.9,
                distinct_source_count=1, label=f"c{i}",
                last_lifecycle_at=_iso(1), last_lifecycle_engagement=0.0,
                last_reinforced_at=_iso(5), promoted_at=_iso(9),
            )
        h.kv.set("engagement.total_units", "0.0")
        stats = h.worker.run()
        self.assertEqual(stats["contradiction_checks"], 1)
        self.assertEqual(det.calls, 1)


class _FakeReviser:
    """Stand-in for :class:`ConceptBeliefReviser`. Records every concept
    it was asked to revise so the L3 trigger + batch cap can be asserted."""

    def __init__(self) -> None:
        self.revised: list[int] = []

    def revise(self, concept, verdict, *, now=None):
        self.revised.append(concept.concept_id)
        return SimpleNamespace(lowered=1, superseded=0)


class BeliefRevisionTriggerTests(unittest.TestCase):
    def test_edge_persisted_on_confirmed_contradiction(self) -> None:
        h = _harness(detector=_FakeDetector())
        c = _add(
            h.store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        edges = h.store.edges_from("concept", c.concept_id)
        contradicts = [e for e in edges if e.relation == "contradicts"]
        self.assertEqual(len(contradicts), 1)
        self.assertEqual(contradicts[0].dst_type, "memory")
        self.assertEqual(contradicts[0].dst_id, "99")
        self.assertEqual(contradicts[0].polarity, -1)

    def test_reviser_invoked_on_flip_to_contradicted(self) -> None:
        rev = _FakeReviser()
        h = _harness(detector=_FakeDetector(), belief_reviser=rev)
        c = _add(
            h.store, status="active", confidence=0.5, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        stats = h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "contradicted")
        self.assertEqual(rev.revised, [c.concept_id])
        self.assertEqual(stats["belief_revisions"], 1)
        self.assertEqual(stats["memories_lowered"], 1)

    def test_reviser_not_invoked_when_only_weakened(self) -> None:
        rev = _FakeReviser()
        h = _harness(detector=_FakeDetector(), belief_reviser=rev)
        _add(
            h.store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=1, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )
        h.kv.set("engagement.total_units", "0.0")
        h.worker.run()
        # Confidence only dented (0.7125) -> stays active -> no revision.
        self.assertEqual(rev.revised, [])

    def test_batch_cap_bounds_revisions_per_tick(self) -> None:
        rev = _FakeReviser()
        h = _harness(
            _settings(concept_belief_revision_batch_size=1),
            detector=_FakeDetector(),
            belief_reviser=rev,
        )
        for i in range(3):
            _add(
                h.store, status="active", confidence=0.5, plasticity=0.5,
                distinct_source_count=1, label=f"c{i}",
                last_lifecycle_at=_iso(1), last_lifecycle_engagement=0.0,
                last_reinforced_at=_iso(5), promoted_at=_iso(9),
            )
        h.kv.set("engagement.total_units", "0.0")
        stats = h.worker.run()
        self.assertEqual(stats["belief_revisions"], 1)
        self.assertEqual(len(rev.revised), 1)


class AccrualPlasticityTests(unittest.TestCase):
    """L16: plasticity damps confidence *accrual* symmetrically with decay.
    High plasticity keeps the pre-L16 full snap; low plasticity approaches
    the evidence target in partial steps."""

    def test_alpha_endpoints(self) -> None:
        self.assertAlmostEqual(accrual_alpha(1.0), 1.0, places=6)
        self.assertAlmostEqual(accrual_alpha(0.0), 0.5, places=6)
        self.assertAlmostEqual(accrual_alpha(0.5), 0.75, places=6)

    def test_high_plasticity_full_snap(self) -> None:
        got = next_confidence(
            0.2, engaged_days=0.0, halflife_days=45.0,
            plasticity=1.0, target=0.9, reinforced=True,
        )
        self.assertAlmostEqual(got, 0.9, places=6)

    def test_low_plasticity_partial_approach(self) -> None:
        # p=0 -> half-step toward the target: 0.2 + (0.9-0.2)*0.5 = 0.55.
        got = next_confidence(
            0.2, engaged_days=0.0, halflife_days=45.0,
            plasticity=0.0, target=0.9, reinforced=True,
        )
        self.assertAlmostEqual(got, 0.55, places=6)

    def test_low_plasticity_builds_slower_than_high(self) -> None:
        low = next_confidence(
            0.2, engaged_days=0.0, halflife_days=45.0,
            plasticity=0.2, target=0.9, reinforced=True,
        )
        high = next_confidence(
            0.2, engaged_days=0.0, halflife_days=45.0,
            plasticity=0.9, target=0.9, reinforced=True,
        )
        self.assertLess(low, high)

    def test_reinforcement_never_lowers(self) -> None:
        # Already above the target -> the up-move can't drag it down.
        got = next_confidence(
            0.8, engaged_days=0.0, halflife_days=45.0,
            plasticity=0.0, target=0.5, reinforced=True,
        )
        self.assertAlmostEqual(got, 0.8, places=6)


class KindPlasticityTests(unittest.TestCase):
    """L16: the worker stamps a kind's default plasticity on first eval."""

    def test_identity_uses_identity_setting(self) -> None:
        h = _harness(_settings(concept_identity_plasticity=0.25))
        c = _add(h.store, plasticity=0.9)  # seed differs from the default
        h.worker.run()
        self.assertAlmostEqual(h.store.get(c.concept_id).plasticity, 0.25)

    def test_unknown_kind_falls_back_to_default_setting(self) -> None:
        h = _harness(_settings(concept_default_plasticity=0.6))
        c = _add(h.store, kind="taste", plasticity=0.9)
        h.worker.run()
        self.assertAlmostEqual(h.store.get(c.concept_id).plasticity, 0.6)

    def test_registered_kind_default_beats_general_setting(self) -> None:
        from app.core.concepts.concept_kinds import (
            CONCEPT_KINDS,
            ConceptKind,
            register_kind,
        )

        register_kind(ConceptKind(name="l16_taste", plasticity_default=0.85))
        self.addCleanup(lambda: CONCEPT_KINDS.pop("l16_taste", None))
        h = _harness(_settings(concept_default_plasticity=0.6))
        c = _add(h.store, kind="l16_taste", plasticity=0.9)
        h.worker.run()
        self.assertAlmostEqual(h.store.get(c.concept_id).plasticity, 0.85)


class GateUnitTests(unittest.TestCase):
    def test_set_evidence_gate(self) -> None:
        kw = dict(min_sources=2, min_age_days=2.0, min_confidence=0.6)
        self.assertTrue(
            set_evidence_gate(distinct_source_count=2, age_days=3, confidence=0.7, **kw)
        )
        self.assertFalse(
            set_evidence_gate(distinct_source_count=1, age_days=3, confidence=0.7, **kw)
        )
        self.assertFalse(
            set_evidence_gate(distinct_source_count=2, age_days=1, confidence=0.7, **kw)
        )
        self.assertFalse(
            set_evidence_gate(distinct_source_count=2, age_days=3, confidence=0.5, **kw)
        )

    def test_value_evidence_gate_is_stricter_than_set(self) -> None:
        # Thresholds that pass the plain ``set`` gate (identity's world):
        kw = dict(min_sources=2, min_age_days=0.5, min_confidence=0.6)
        # Value floors them to >=3 sources / >=1.0 day / >=0.72 confidence.
        self.assertTrue(
            set_evidence_gate(
                distinct_source_count=2, age_days=0.5, confidence=0.6, **kw
            )
        )
        self.assertFalse(  # too few sources for a value
            value_evidence_gate(
                distinct_source_count=2, age_days=3.0, confidence=0.9, **kw
            )
        )
        self.assertFalse(  # too young for a value
            value_evidence_gate(
                distinct_source_count=3, age_days=0.5, confidence=0.9, **kw
            )
        )
        self.assertFalse(  # confidence below the value bar
            value_evidence_gate(
                distinct_source_count=3, age_days=3.0, confidence=0.65, **kw
            )
        )
        self.assertTrue(  # clears every value floor
            value_evidence_gate(
                distinct_source_count=3, age_days=3.0, confidence=0.75, **kw
            )
        )

    def test_value_gate_honours_higher_caller_thresholds(self) -> None:
        # The caller's bar wins when it's stricter than the value floor
        # (e.g. the L21 young-graph tightening).
        self.assertFalse(
            value_evidence_gate(
                distinct_source_count=3, age_days=3.0, confidence=0.75,
                min_sources=4, min_age_days=1.0, min_confidence=0.72,
            )
        )

    def test_identity_gate_floors_sources_and_age(self) -> None:
        # The live global settings identity used to ride alone: a 2-source
        # bar and *no* stability delay at all, which is what let 70% of
        # identity concepts promote within an hour of first evidence.
        kw = dict(min_sources=2, min_age_days=0.0, min_confidence=0.6)
        self.assertTrue(  # the bare gate waves this through
            set_evidence_gate(
                distinct_source_count=2, age_days=0.0, confidence=0.7, **kw
            )
        )
        self.assertFalse(  # identity now needs a third source
            identity_evidence_gate(
                distinct_source_count=2, age_days=3.0, confidence=0.7, **kw
            )
        )
        self.assertFalse(  # ... and a real stability delay
            identity_evidence_gate(
                distinct_source_count=3, age_days=0.0, confidence=0.7, **kw
            )
        )
        self.assertTrue(
            identity_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.7, **kw
            )
        )

    def test_identity_gate_keeps_the_ordinary_confidence_bar(self) -> None:
        # Deliberately *not* raised to value's 0.72: the live histogram put
        # these rows at 0.773 mean confidence, so the leak was structural
        # (sources and age), and a higher bar would only suppress good
        # concepts without touching the mechanism at fault.
        kw = dict(min_sources=2, min_age_days=0.0, min_confidence=0.6)
        self.assertTrue(
            identity_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.6, **kw
            )
        )
        self.assertFalse(
            value_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.6, **kw
            )
        )
        self.assertFalse(  # still refuses confidence under the global bar
            identity_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.59, **kw
            )
        )

    def test_identity_gate_honours_higher_caller_thresholds(self) -> None:
        # L21's young-graph bar wins via max() when it is the stricter one.
        self.assertFalse(
            identity_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.7,
                min_sources=4, min_age_days=1.0, min_confidence=0.6,
            )
        )
        self.assertFalse(
            identity_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.7,
                min_sources=2, min_age_days=1.0, min_confidence=0.72,
            )
        )
        self.assertFalse(
            identity_evidence_gate(
                distinct_source_count=3, age_days=1.5, confidence=0.7,
                min_sources=2, min_age_days=2.0, min_confidence=0.6,
            )
        )


class ValueKindWorkerTests(unittest.TestCase):
    """L10: a value concept uses the stricter registry gate + low plasticity
    without any worker-side special casing."""

    def test_value_does_not_promote_at_identity_thresholds(self) -> None:
        h = _harness()
        _seed_engaged_days(h, 3)
        # Identity and value now share their structural floors (3 sources, a
        # real stability delay); what still separates them is confidence. Seed
        # sticky so the accrual half-step lands this candidate *between* the
        # two bars: above identity's 0.6, below value's 0.72.
        c = _add(
            h.store, kind="value", distinct_source_count=3, confidence=0.62,
            plasticity=0.0, first_evidence_engagement=0.0,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "candidate")
        # Assert the setup still straddles the bars, so a retuned accrual
        # curve fails here instead of quietly making the test vacuous.
        self.assertGreater(got.confidence, 0.6)
        self.assertLess(got.confidence, 0.72)

    def test_value_promotes_once_value_bar_clears(self) -> None:
        h = _harness()
        _seed_engaged_days(h, 3)
        c = _add(
            h.store, kind="value", distinct_source_count=3, confidence=0.75,
            first_evidence_engagement=0.0,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_value_stamps_low_plasticity_on_first_eval(self) -> None:
        h = _harness()
        c = _add(h.store, kind="value", plasticity=0.9)
        h.worker.run()
        self.assertAlmostEqual(h.store.get(c.concept_id).plasticity, 0.2)


class IdentityKindWorkerTests(unittest.TestCase):
    """The identity intake tightening, end to end through the worker.

    Identity was the only kind with no floors of its own, riding the global
    promote settings (2 sources, zero age). It is also the largest kind, so
    that combination produced most of the never-reinforced backlog.
    """

    def test_two_source_identity_no_longer_promotes(self) -> None:
        # The exact live pattern: two distinct sources, comfortable
        # confidence, plenty of engaged age. Used to promote on sight.
        h = _harness()
        _seed_engaged_days(h, 5)
        c = _add(
            h.store, distinct_source_count=2, confidence=0.8,
            first_evidence_engagement=0.0,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")
        self.assertEqual(h.events.count(), 0)

    def test_third_source_lets_it_through(self) -> None:
        h = _harness()
        _seed_engaged_days(h, 5)
        c = _add(
            h.store, distinct_source_count=3, confidence=0.8,
            first_evidence_engagement=0.0,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_zero_age_identity_waits_even_with_enough_sources(self) -> None:
        # The stability delay, which identity previously had none of: the
        # global floor was 0.0, so a well-sourced trait promoted instantly.
        h = _harness(_settings(concept_promote_min_age_days=0.0))
        _seed_engaged_days(h, 0)
        c = _add(
            h.store, distinct_source_count=4, confidence=0.8,
            first_evidence_engagement=0.0,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "candidate")
        # One engaged day (~an hour of conversation) later it clears.
        _seed_engaged_days(h, 1)
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_existing_actives_are_not_retroactively_demoted(self) -> None:
        # The tightening must only gate *future* promotions. A concept that
        # already promoted under the old two-source bar stays active: the
        # worker re-gates only candidate/retired rows, and demotion is driven
        # by confidence, not by the promotion gate.
        h = _harness()
        _seed_engaged_days(h, 5)
        c = _add(
            h.store,
            status="active",
            distinct_source_count=2,
            confidence=0.8,
            promoted_at=_iso(10),
            last_reinforced_at=_iso(9),
            last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0,
            first_evidence_engagement=0.0,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")
        self.assertEqual(
            [e.event_type for e in h.events.list(limit=10)], []
        )


class YoungGraphGateTests(unittest.TestCase):
    """L21: promotion uses a stricter bar until the topic graph matures."""

    def test_immature_graph_blocks_promotion(self) -> None:
        # 2 distinct sources clears the normal bar but not the young bar
        # (which needs 3). The provider reports the graph as immature.
        h = _harness(graph_mature=lambda: False)
        c = _add(h.store, distinct_source_count=2, first_evidence_at=_iso(3))
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "candidate")

    def test_mature_graph_allows_promotion(self) -> None:
        h = _harness(graph_mature=lambda: True)
        _seed_engaged_days(h, 3)
        c = _add(
            h.store, distinct_source_count=3, first_evidence_at=_iso(3),
            first_evidence_engagement=0.0, confidence=0.7,
        )
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")

    def test_no_provider_uses_normal_bar(self) -> None:
        # Default (no provider) treats the graph as mature.
        h = _harness()
        _seed_engaged_days(h, 3)
        c = _add(
            h.store, distinct_source_count=3, first_evidence_at=_iso(3),
            first_evidence_engagement=0.0, confidence=0.7,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_immature_graph_still_promotes_with_enough_sources(self) -> None:
        # 3 distinct sources clears even the stricter young bar. Seeded at
        # the young confidence bar so the plasticity-damped accrual ramp
        # isn't the binding factor (this test is about the source count).
        h = _harness(graph_mature=lambda: False)
        _seed_engaged_days(h, 3)
        c = _add(
            h.store, distinct_source_count=3, first_evidence_at=_iso(3),
            first_evidence_engagement=0.0, confidence=0.8,
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")


class ReinforcedEventTests(unittest.TestCase):
    """A fresh reinforcement on an already-active concept (no status change)
    leaves a ``reinforced`` beat on the timeline; nothing fires without new
    evidence, and a first evaluation never counts as a reinforcement beat."""

    def _active(self, h, **over):
        base = dict(
            status="active",
            promoted_at=_iso(10),
            confidence=0.85,
            distinct_source_count=3,
            first_evidence_at=_iso(20),
            first_evidence_engagement=0.0,
            last_lifecycle_engagement=0.0,
        )
        base.update(over)
        return _add(h.store, **base)

    def test_reinforced_active_concept_emits_event(self) -> None:
        h = _harness()
        # New evidence landed (last_reinforced) after the last eval, no clock
        # advance -> no decay -> stays active -> a reinforced beat.
        c = self._active(
            h, last_lifecycle_at=_iso(5), last_reinforced_at=_iso(1)
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")
        self.assertEqual(
            [e.event_type for e in h.events.list(limit=10)], ["reinforced"]
        )

    def test_no_event_without_fresh_evidence(self) -> None:
        h = _harness()
        # last_reinforced predates the last eval -> not reinforced -> silent.
        c = self._active(
            h, last_lifecycle_at=_iso(1), last_reinforced_at=_iso(9)
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")
        self.assertEqual(h.events.count(), 0)

    def test_first_eval_does_not_emit_reinforced(self) -> None:
        h = _harness()
        # last_lifecycle_at is None -> first eval. ``reinforced`` is True by
        # convention there, but the beat is reserved for later ticks.
        c = self._active(
            h, last_lifecycle_at=None, last_reinforced_at=_iso(1)
        )
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")
        self.assertEqual(h.events.count(), 0)


if __name__ == "__main__":
    unittest.main()
