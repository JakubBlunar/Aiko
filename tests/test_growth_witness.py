"""Pure-module tests for K70 longitudinal growth witness."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.core.affect.mood_drift import DriftSample
from app.core.relationship import growth_witness as gw


def _sample(day_offset: int, *, valence=0.0, comfort=0.0, trust=0.0):
    d = (date(2026, 1, 1) + timedelta(days=day_offset)).isoformat()
    return DriftSample(
        date=d,
        valence=valence,
        closeness=0.0,
        humor=0.0,
        trust=trust,
        comfort=comfort,
    )


def _ramp(attr: str, low: float, high: float, n: int = 15):
    """A ring of ``n`` samples ramping ``attr`` linearly low -> high."""
    out = []
    for i in range(n):
        frac = i / (n - 1)
        val = low + (high - low) * frac
        out.append(_sample(i, **{attr: val}))
    return out


class DetectGrowthTests(unittest.TestCase):
    def test_durable_valence_rise_fires_lighter(self) -> None:
        finding = gw.detect_growth(_ramp("valence", -0.5, 0.5))
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.kind, gw.KIND_LIGHTER)
        self.assertGreater(finding.magnitude, 0.25)
        self.assertGreater(finding.span_days, 0)

    def test_comfort_rise_fires_comfort(self) -> None:
        finding = gw.detect_growth(_ramp("comfort", -0.6, 0.6))
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.kind, gw.KIND_COMFORT)

    def test_trust_rise_fires_open(self) -> None:
        finding = gw.detect_growth(_ramp("trust", -0.6, 0.6))
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.kind, gw.KIND_OPEN)

    def test_too_few_samples_silent(self) -> None:
        ring = _ramp("valence", -0.5, 0.5, n=6)
        self.assertIsNone(gw.detect_growth(ring, min_samples=10))

    def test_flat_ring_silent(self) -> None:
        ring = [_sample(i, valence=0.1) for i in range(15)]
        self.assertIsNone(gw.detect_growth(ring))

    def test_downturn_never_fires(self) -> None:
        # A durable *fall* is K70's anti-case: it only witnesses growth.
        ring = _ramp("valence", 0.5, -0.5)
        self.assertIsNone(gw.detect_growth(ring))

    def test_small_rise_below_threshold_silent(self) -> None:
        ring = _ramp("valence", 0.0, 0.15)  # delta < 0.25
        self.assertIsNone(gw.detect_growth(ring))

    def test_mood_wins_tie_over_axis(self) -> None:
        # Both valence and comfort ramp by the same amount; mood wins.
        ring = []
        for i in range(15):
            frac = i / 14
            v = -0.5 + 1.0 * frac
            ring.append(_sample(i, valence=v, comfort=v))
        finding = gw.detect_growth(ring)
        assert finding is not None
        self.assertEqual(finding.kind, gw.KIND_LIGHTER)

    def test_signature_buckets_magnitude(self) -> None:
        finding = gw.detect_growth(_ramp("valence", -0.5, 0.5))
        assert finding is not None
        self.assertTrue(finding.signature.startswith("lighter:"))

    def test_detail_passthrough(self) -> None:
        finding = gw.detect_growth(
            _ramp("valence", -0.5, 0.5), detail="his guitar practice",
        )
        assert finding is not None
        self.assertEqual(finding.detail, "his guitar practice")


class RenderTests(unittest.TestCase):
    def test_lighter_long_span(self) -> None:
        line = gw.render_inner_life_block(
            gw.KIND_LIGHTER, user_display_name="Jacob", span_days=18,
        )
        self.assertIn("Jacob", line)
        self.assertIn("lighter", line)
        self.assertIn("these past couple of weeks", line)

    def test_lighter_short_span_uses_lately(self) -> None:
        line = gw.render_inner_life_block(
            gw.KIND_LIGHTER, user_display_name="Jacob", span_days=4,
        )
        self.assertIn("lately", line)
        self.assertNotIn("these past couple of weeks", line)

    def test_comfort_and_open_render(self) -> None:
        c = gw.render_inner_life_block(gw.KIND_COMFORT, span_days=14)
        o = gw.render_inner_life_block(gw.KIND_OPEN, span_days=14)
        self.assertIn("at ease", c)
        self.assertIn("trusting", o)

    def test_detail_woven_in(self) -> None:
        line = gw.render_inner_life_block(
            gw.KIND_LIGHTER, span_days=14, detail="his new job",
        )
        self.assertIn("his new job", line)

    def test_unknown_kind_empty(self) -> None:
        self.assertEqual(gw.render_inner_life_block("bogus"), "")


class RingHelperTests(unittest.TestCase):
    def test_round_trip_and_cap(self) -> None:
        store: dict[str, str] = {}

        def kv_get(k: str):
            return store.get(k)

        def kv_set(k: str, v: str):
            store[k] = v

        for i in range(6):
            gw.append_finding(
                kv_get, kv_set,
                {"at": f"t{i}", "kind": "lighter"},
                max_entries=4,
            )
        ring = gw.load_findings(kv_get)
        self.assertEqual(len(ring), 4)
        self.assertEqual(ring[-1]["at"], "t5")

    def test_load_tolerates_garbage(self) -> None:
        self.assertEqual(gw.load_findings(lambda _k: "not json"), [])
        self.assertEqual(gw.load_findings(lambda _k: None), [])


if __name__ == "__main__":
    unittest.main()
