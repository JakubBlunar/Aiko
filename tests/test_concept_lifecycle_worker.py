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

from app.core.concepts.concept_lifecycle import set_evidence_gate
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


class PromotionTests(unittest.TestCase):
    def test_promotes_when_gate_clears(self) -> None:
        h = _harness()
        c = _add(h.store, distinct_source_count=2, first_evidence_at=_iso(3))
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
        h = _harness()
        c = _add(
            h.store, status="active", confidence=0.2, plasticity=0.5,
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
        c = _add(h.store, distinct_source_count=2, first_evidence_at=_iso(3))
        h.worker.run()
        got = h.store.get(c.concept_id)
        self.assertEqual(got.status, "active")

    def test_no_provider_uses_normal_bar(self) -> None:
        # Default (no provider) treats the graph as mature.
        h = _harness()
        c = _add(h.store, distinct_source_count=2, first_evidence_at=_iso(3))
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")

    def test_immature_graph_still_promotes_with_enough_sources(self) -> None:
        # 3 distinct sources clears even the stricter young bar.
        h = _harness(graph_mature=lambda: False)
        c = _add(h.store, distinct_source_count=3, first_evidence_at=_iso(3))
        h.worker.run()
        self.assertEqual(h.store.get(c.concept_id).status, "active")


if __name__ == "__main__":
    unittest.main()
