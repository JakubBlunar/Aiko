"""Tests for the L16 deferred block: relationship modulation of plasticity,
one-way plasticity-drift, and the plasticity-scaled contradiction re-check
slowdown.

Two layers:

* pure math -- :func:`effective_plasticity` (no-op == base, positive-trust /
  duration lift, capped at the kind ceiling, negative trust = no lift) and
  :func:`drift_plasticity` (monotone down toward the floor, never below,
  ``rate == 0`` no-op), plus the registry wiring (``boundary`` opts in, other
  kinds stay the no-op default);
* worker integration -- a high-trust signal makes a boundary's confidence move
  faster and records the ``influences`` edge + a ``plasticity_shift`` event on a
  band cross; drift persists a lower plasticity for a mature active concept; a
  sticky concept skips contradiction probes on its stride; every piece no-ops
  when its flag is off.
"""
from __future__ import annotations

import unittest

from app.core.concepts.concept_kinds import (
    DEFAULT_PLASTICITY_MODULATION,
    PlasticityModulation,
    get_kind,
)
from app.core.concepts.concept_lifecycle import (
    RelationshipSignal,
    drift_plasticity,
    effective_plasticity,
)

from tests.test_concept_lifecycle_worker import (
    _FakeDetector,
    _add,
    _harness,
    _iso,
    _settings,
)


# ── pure: effective_plasticity ───────────────────────────────────────────


class EffectivePlasticityTests(unittest.TestCase):
    def test_default_modulation_is_noop(self) -> None:
        # The no-op default returns the base untouched (every non-opted kind).
        for base in (0.0, 0.3, 0.45, 1.0):
            self.assertEqual(
                effective_plasticity(
                    base,
                    signal=RelationshipSignal(trust01=1.0, duration01=1.0),
                    mod=DEFAULT_PLASTICITY_MODULATION,
                ),
                base,
            )

    def test_positive_trust_lifts(self) -> None:
        mod = PlasticityModulation(trust_gain=0.25, duration_gain=0.1, max_plasticity=0.75)
        eff = effective_plasticity(
            0.45, signal=RelationshipSignal(trust01=1.0, duration01=0.0), mod=mod
        )
        self.assertAlmostEqual(eff, 0.70, places=6)

    def test_duration_also_lifts(self) -> None:
        mod = PlasticityModulation(trust_gain=0.25, duration_gain=0.1, max_plasticity=0.9)
        eff = effective_plasticity(
            0.45, signal=RelationshipSignal(trust01=1.0, duration01=1.0), mod=mod
        )
        self.assertAlmostEqual(eff, 0.80, places=6)

    def test_capped_at_max_plasticity(self) -> None:
        mod = PlasticityModulation(trust_gain=0.5, duration_gain=0.5, max_plasticity=0.75)
        eff = effective_plasticity(
            0.6, signal=RelationshipSignal(trust01=1.0, duration01=1.0), mod=mod
        )
        self.assertEqual(eff, 0.75)

    def test_negative_trust_gives_no_lift(self) -> None:
        # Signals are clamped to [0,1]; a negative/zero trust never *lowers*
        # plasticity below the stored base.
        mod = PlasticityModulation(trust_gain=0.25, duration_gain=0.1, max_plasticity=0.75)
        eff = effective_plasticity(
            0.45, signal=RelationshipSignal(trust01=-1.0, duration01=0.0), mod=mod
        )
        self.assertEqual(eff, 0.45)


# ── pure: drift_plasticity ───────────────────────────────────────────────


class DriftPlasticityTests(unittest.TestCase):
    def test_drifts_down_toward_floor(self) -> None:
        new = drift_plasticity(
            0.5, confidence=0.9, age_days=60.0, floor=0.15, rate=0.05
        )
        self.assertLess(new, 0.5)
        self.assertGreater(new, 0.15)

    def test_monotone_non_increasing_over_repeats(self) -> None:
        p = 0.5
        prev = p
        for _ in range(20):
            p = drift_plasticity(
                p, confidence=0.9, age_days=90.0, floor=0.15, rate=0.05
            )
            self.assertLessEqual(p, prev)
            prev = p
        self.assertGreaterEqual(p, 0.15)

    def test_never_below_floor(self) -> None:
        new = drift_plasticity(
            0.16, confidence=1.0, age_days=10_000.0, floor=0.15, rate=1.0
        )
        self.assertGreaterEqual(new, 0.15)

    def test_at_or_below_floor_is_noop(self) -> None:
        self.assertEqual(
            drift_plasticity(0.15, confidence=1.0, age_days=100.0, floor=0.15, rate=0.5),
            0.15,
        )
        self.assertEqual(
            drift_plasticity(0.10, confidence=1.0, age_days=100.0, floor=0.15, rate=0.5),
            0.10,
        )

    def test_rate_zero_is_noop(self) -> None:
        self.assertEqual(
            drift_plasticity(0.5, confidence=0.9, age_days=60.0, floor=0.15, rate=0.0),
            0.5,
        )

    def test_young_concept_barely_moves(self) -> None:
        new = drift_plasticity(
            0.5, confidence=0.9, age_days=0.0, floor=0.15, rate=0.05
        )
        self.assertEqual(new, 0.5)


# ── registry wiring ──────────────────────────────────────────────────────


class RegistryModulationTests(unittest.TestCase):
    def test_boundary_opts_in(self) -> None:
        mod = get_kind("boundary").plasticity_modulation
        self.assertIsNot(mod, DEFAULT_PLASTICITY_MODULATION)
        self.assertGreater(mod.trust_gain, 0.0)
        self.assertGreater(mod.duration_gain, 0.0)
        self.assertLess(mod.max_plasticity, 1.0)

    def test_other_kinds_stay_default(self) -> None:
        for name in ("identity", "value", "affective", "narrative"):
            self.assertIs(
                get_kind(name).plasticity_modulation,
                DEFAULT_PLASTICITY_MODULATION,
                name,
            )


# ── worker: modulation (piece 1) ─────────────────────────────────────────


def _boundary(store, **over):
    base = dict(
        label="Go gentler about work with him",
        kind="boundary",
        subject="user",
        status="active",
        confidence=0.5,
        plasticity=0.45,
        distinct_source_count=6,
        first_evidence_at=_iso(30),
        last_lifecycle_at=_iso(1),
        last_reinforced_at=_iso(0.5),
        promoted_at=_iso(20),
    )
    base.update(over)
    return _add(store, **base)


class WorkerModulationTests(unittest.TestCase):
    def test_high_trust_moves_confidence_faster(self) -> None:
        signal = lambda: RelationshipSignal(trust01=1.0, duration01=1.0)
        h_mod = _harness(
            with_clock=False, relationship_signal_provider=signal
        )
        h_base = _harness(with_clock=False)
        c_mod = _boundary(h_mod.store)
        c_base = _boundary(h_base.store)
        h_mod.worker.run()
        h_base.worker.run()
        conf_mod = h_mod.store.get(c_mod.concept_id).confidence
        conf_base = h_base.store.get(c_base.concept_id).confidence
        # Same reinforced eval, higher effective plasticity => bigger accrual
        # step toward the (higher) target.
        self.assertGreater(conf_mod, conf_base)

    def test_influences_edge_and_shift_event_on_band_cross(self) -> None:
        signal = lambda: RelationshipSignal(trust01=1.0, duration01=1.0)
        h = _harness(with_clock=False, relationship_signal_provider=signal)
        c = _boundary(h.store)
        stats = h.worker.run()
        edges = [
            e
            for e in h.store.edges_into("concept", c.concept_id)
            if e.relation == "influences" and e.src_type == "signal"
        ]
        self.assertEqual(len(edges), 1)
        # Raw gain 0.25*1 + 0.1*1 = 0.35 would lift 0.45 -> 0.80, but the 0.75
        # ceiling caps eff at 0.75, so the *recorded* lift is 0.75 - 0.45 = 0.30.
        self.assertAlmostEqual(edges[0].strength, 0.30, places=5)
        self.assertEqual(edges[0].src_id, "relationship_trust")
        types = [e.event_type for e in h.events.list()]
        self.assertIn("plasticity_shift", types)
        self.assertEqual(stats["plasticity_shifts"], 1)
        shift = next(
            e for e in h.events.list() if e.event_type == "plasticity_shift"
        )
        self.assertIn("loosening", shift.reason)

    def test_disabled_modulation_is_noop(self) -> None:
        signal = lambda: RelationshipSignal(trust01=1.0, duration01=1.0)
        h = _harness(
            _settings(concept_plasticity_modulation_enabled=False),
            with_clock=False,
            relationship_signal_provider=signal,
        )
        c = _boundary(h.store)
        h.worker.run()
        edges = [
            e
            for e in h.store.edges_into("concept", c.concept_id)
            if e.relation == "influences"
        ]
        self.assertEqual(edges, [])
        self.assertNotIn(
            "plasticity_shift", [e.event_type for e in h.events.list()]
        )

    def test_no_provider_is_noop(self) -> None:
        # Lean deployment: no signal provider => no edge, no event.
        h = _harness(with_clock=False)
        c = _boundary(h.store)
        h.worker.run()
        self.assertEqual(
            [
                e
                for e in h.store.edges_into("concept", c.concept_id)
                if e.relation == "influences"
            ],
            [],
        )


# ── worker: drift (piece 2) ──────────────────────────────────────────────


class WorkerDriftTests(unittest.TestCase):
    def test_mature_active_concept_gets_stickier(self) -> None:
        h = _harness(
            _settings(concept_plasticity_drift_enabled=True), with_clock=False
        )
        c = _add(
            h.store, kind="identity", status="active", confidence=0.9,
            plasticity=0.5, distinct_source_count=4, first_evidence_at=_iso(60),
            last_lifecycle_at=_iso(1), last_reinforced_at=_iso(5),
            promoted_at=_iso(40),
        )
        h.worker.run()
        got = h.store.get(c.concept_id).plasticity
        self.assertLess(got, 0.5)
        self.assertGreaterEqual(got, 0.15)

    def test_disabled_drift_leaves_plasticity(self) -> None:
        # Shared stub has drift OFF; plasticity stays at the seed.
        h = _harness(with_clock=False)
        c = _add(
            h.store, kind="identity", status="active", confidence=0.9,
            plasticity=0.5, distinct_source_count=4, first_evidence_at=_iso(60),
            last_lifecycle_at=_iso(1), last_reinforced_at=_iso(5),
            promoted_at=_iso(40),
        )
        h.worker.run()
        self.assertAlmostEqual(h.store.get(c.concept_id).plasticity, 0.5)

    def test_first_eval_stamps_before_drift(self) -> None:
        # First eval stamps the kind band; drift is skipped that tick.
        h = _harness(
            _settings(concept_plasticity_drift_enabled=True), with_clock=False
        )
        c = _add(
            h.store, kind="value", status="active", confidence=0.9,
            plasticity=0.9, distinct_source_count=4, first_evidence_at=_iso(60),
            last_lifecycle_at=None, promoted_at=_iso(40),
        )
        h.worker.run()
        # Stamped to the value band (0.2), not drifted below it on first eval.
        self.assertAlmostEqual(h.store.get(c.concept_id).plasticity, 0.2)


# ── worker: re-check slowdown (piece 3) ──────────────────────────────────


class WorkerRecheckSlowdownTests(unittest.TestCase):
    def _sticky_concept(self, store):
        return _add(
            store, status="active", confidence=0.9, plasticity=0.5,
            distinct_source_count=4, last_lifecycle_at=_iso(1),
            last_lifecycle_engagement=0.0, last_reinforced_at=_iso(5),
            promoted_at=_iso(9),
        )

    def test_sticky_concept_skips_probes_on_stride(self) -> None:
        det = _FakeDetector(target_ids=set())  # always returns None, counts calls
        h = _harness(
            _settings(
                concept_plasticity_recheck_slowdown_enabled=True,
                concept_plasticity_recheck_stride_k=3.0,
            ),
            detector=det,
        )
        self._sticky_concept(h.store)
        h.kv.set("engagement.total_units", "0.0")
        # eff_plast 0.5 => stride = 1 + round(3*0.5) = 3: only the 3rd tick probes.
        for _ in range(3):
            h.worker.run()
        self.assertEqual(det.calls, 1)

    def test_disabled_slowdown_probes_every_tick(self) -> None:
        det = _FakeDetector(target_ids=set())
        h = _harness(detector=det)  # shared stub has slowdown OFF
        self._sticky_concept(h.store)
        h.kv.set("engagement.total_units", "0.0")
        for _ in range(3):
            h.worker.run()
        self.assertEqual(det.calls, 3)


if __name__ == "__main__":
    unittest.main()
