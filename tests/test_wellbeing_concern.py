"""Pure-module tests for K72 wellbeing concern."""
from __future__ import annotations

import unittest

from app.core.affect.mood_drift import DriftSample
from app.core.relationship import wellbeing_concern as wc


def _drift(valence: float, *, day: str = "2026-01-01") -> DriftSample:
    return DriftSample(
        date=day, valence=valence, closeness=0.0, humor=0.0, trust=0.0,
        comfort=0.0,
    )


class ClassifyNeglectTests(unittest.TestCase):
    def test_sleep_negations(self) -> None:
        for txt in (
            "ugh I haven't slept at all",
            "didn't sleep last night",
            "can't sleep again",
            "running on no sleep honestly",
            "pulled an all-nighter for this",
            "only got 3 hours of sleep",
        ):
            self.assertIn(wc.CATEGORY_SLEEP, wc.classify_neglect_text(txt), txt)

    def test_food_negations(self) -> None:
        for txt in (
            "haven't eaten all day",
            "didn't eat lunch",
            "skipped dinner again",
            "forgot to eat honestly",
            "no time to eat today",
        ):
            self.assertIn(wc.CATEGORY_FOOD, wc.classify_neglect_text(txt), txt)

    def test_positive_sleep_not_flagged(self) -> None:
        # The negative-lookahead keeps "slept great" out.
        self.assertEqual(wc.classify_neglect_text("I haven't slept this well in ages"), set())
        self.assertEqual(wc.classify_neglect_text("slept great, had a big breakfast"), set())

    def test_plain_text_empty(self) -> None:
        self.assertEqual(wc.classify_neglect_text("the weather is nice today"), set())
        self.assertEqual(wc.classify_neglect_text(""), set())

    def test_both_categories(self) -> None:
        cats = wc.classify_neglect_text("haven't slept and haven't eaten")
        self.assertEqual(cats, {wc.CATEGORY_SLEEP, wc.CATEGORY_FOOD})


class DetectLateNightsTests(unittest.TestCase):
    def test_fires_at_threshold(self) -> None:
        f = wc.detect_late_nights(
            ["2026-01-01", "2026-01-02", "2026-01-03"], min_nights=3,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, wc.KIND_LATE_NIGHTS)
        self.assertEqual(f.signature, "late_nights:3")

    def test_below_threshold_silent(self) -> None:
        self.assertIsNone(
            wc.detect_late_nights(["2026-01-01", "2026-01-02"], min_nights=3)
        )

    def test_dedupes_dates(self) -> None:
        self.assertIsNone(
            wc.detect_late_nights(
                ["2026-01-01", "2026-01-01", "2026-01-01"], min_nights=3,
            )
        )

    def test_escalation_changes_signature(self) -> None:
        f3 = wc.detect_late_nights(["a", "b", "c"], min_nights=3)
        f4 = wc.detect_late_nights(["a", "b", "c", "d"], min_nights=3)
        self.assertNotEqual(f3.signature, f4.signature)


class DetectSelfNeglectTests(unittest.TestCase):
    def test_fires_with_days_and_category(self) -> None:
        f = wc.detect_self_neglect(
            ["2026-01-01", "2026-01-02"], ["sleep"], min_days=2,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, wc.KIND_SELF_NEGLECT)
        self.assertEqual(f.detail, "sleeping")

    def test_food_detail(self) -> None:
        f = wc.detect_self_neglect(["a", "b"], ["food"], min_days=2)
        self.assertEqual(f.detail, "eating")

    def test_both_detail(self) -> None:
        f = wc.detect_self_neglect(["a", "b"], ["sleep", "food"], min_days=2)
        self.assertEqual(f.detail, "sleep and meals")
        self.assertEqual(f.signature, "self_neglect:food+sleep")

    def test_below_min_days_silent(self) -> None:
        self.assertIsNone(
            wc.detect_self_neglect(["a"], ["sleep"], min_days=2)
        )

    def test_no_category_silent(self) -> None:
        self.assertIsNone(
            wc.detect_self_neglect(["a", "b"], [], min_days=2)
        )


class DetectRoughStretchTests(unittest.TestCase):
    def test_fires_on_sustained_low(self) -> None:
        samples = [_drift(-0.3) for _ in range(5)]
        f = wc.detect_rough_stretch(samples, min_run=5, threshold=-0.25)
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, wc.KIND_ROUGH_STRETCH)

    def test_one_good_day_breaks_run(self) -> None:
        samples = [_drift(-0.3), _drift(-0.3), _drift(0.2), _drift(-0.3), _drift(-0.3)]
        self.assertIsNone(
            wc.detect_rough_stretch(samples, min_run=5, threshold=-0.25)
        )

    def test_too_few_samples_silent(self) -> None:
        self.assertIsNone(
            wc.detect_rough_stretch([_drift(-0.5)], min_run=5, threshold=-0.25)
        )

    def test_mild_low_below_bar_silent(self) -> None:
        # -0.2 doesn't clear the deeper -0.25 K72 bar (H3 would catch it).
        samples = [_drift(-0.2) for _ in range(5)]
        self.assertIsNone(
            wc.detect_rough_stretch(samples, min_run=5, threshold=-0.25)
        )


class PickConcernTests(unittest.TestCase):
    def test_self_neglect_outranks_late_nights(self) -> None:
        f = wc.pick_concern(
            late_night_dates=["a", "b", "c"],
            neglect_days=["x", "y"],
            neglect_categories=["food"],
            late_night_min=3,
            neglect_min_days=2,
        )
        self.assertEqual(f.kind, wc.KIND_SELF_NEGLECT)

    def test_late_nights_outranks_rough(self) -> None:
        samples = [_drift(-0.3) for _ in range(5)]
        f = wc.pick_concern(
            late_night_dates=["a", "b", "c"],
            drift_samples=samples,
            late_night_min=3,
        )
        self.assertEqual(f.kind, wc.KIND_LATE_NIGHTS)

    def test_rough_only(self) -> None:
        samples = [_drift(-0.3) for _ in range(5)]
        f = wc.pick_concern(drift_samples=samples)
        self.assertEqual(f.kind, wc.KIND_ROUGH_STRETCH)

    def test_nothing(self) -> None:
        self.assertIsNone(wc.pick_concern())


class RenderTests(unittest.TestCase):
    def test_late_nights_render(self) -> None:
        line = wc.render_inner_life_block(
            wc.KIND_LATE_NIGHTS, user_display_name="Jacob",
        )
        self.assertIn("Jacob", line)
        self.assertIn("small hours", line)
        self.assertIn("ONCE", line)

    def test_self_neglect_render_uses_detail(self) -> None:
        line = wc.render_inner_life_block(
            wc.KIND_SELF_NEGLECT, user_display_name="Jacob",
            detail="eating",
        )
        self.assertIn("eating", line)

    def test_rough_render(self) -> None:
        line = wc.render_inner_life_block(wc.KIND_ROUGH_STRETCH)
        self.assertIn("heavy", line)

    def test_unknown_kind_empty(self) -> None:
        self.assertEqual(wc.render_inner_life_block("bogus"), "")

    def test_never_lecture_language(self) -> None:
        for kind in (wc.KIND_LATE_NIGHTS, wc.KIND_SELF_NEGLECT, wc.KIND_ROUGH_STRETCH):
            line = wc.render_inner_life_block(kind)
            self.assertIn("never", line.lower())


class RingHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict[str, str] = {}

    def _get(self, k: str):
        return self.store.get(k)

    def _set(self, k: str, v: str) -> None:
        self.store[k] = v

    def test_round_trip(self) -> None:
        wc.append_finding(
            self._get, self._set, {"kind": "late_nights"}, max_entries=4,
        )
        ring = wc.load_findings(self._get)
        self.assertEqual(len(ring), 1)
        self.assertEqual(ring[-1]["kind"], "late_nights")

    def test_cap(self) -> None:
        for i in range(6):
            wc.append_finding(
                self._get, self._set, {"kind": str(i)}, max_entries=3,
            )
        ring = wc.load_findings(self._get)
        self.assertEqual(len(ring), 3)
        self.assertEqual(ring[0]["kind"], "3")

    def test_garbage_tolerated(self) -> None:
        self.store[wc.WELLBEING_CONCERN_JOURNAL_KEY] = "not json"
        self.assertEqual(wc.load_findings(self._get), [])


if __name__ == "__main__":
    unittest.main()
