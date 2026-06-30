"""Pure-module tests for K73 shared-ritual formation."""
from __future__ import annotations

import unittest

from app.core.relationship import shared_ritual as sr


class DominantShapeTests(unittest.TestCase):
    def test_no_signal_defaults_check_in(self) -> None:
        self.assertEqual(sr.dominant_shape([None, None]), "casual_check_in")
        self.assertEqual(sr.dominant_shape([]), "casual_check_in")

    def test_modal_arc_wins(self) -> None:
        self.assertEqual(
            sr.dominant_shape(["support", "support", None, "planning"]),
            "support",
        )

    def test_single_signal_defines_shape(self) -> None:
        self.assertEqual(sr.dominant_shape([None, "reflection"]), "reflection")


class DetectRitualsTests(unittest.TestCase):
    def _slot(self, weeks: int):
        return {
            ("friday", "evening", "casual_check_in"): {
                (2026, w) for w in range(1, weeks + 1)
            }
        }

    def test_fires_when_recurring(self) -> None:
        out = sr.detect_rituals(
            self._slot(3), total_weeks=8, min_weeks=3, min_share=0.34,
        )
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.key, "friday:evening:casual_check_in")
        self.assertEqual(c.cadence, "Friday evenings")
        self.assertEqual(c.shape_label, "check-ins")
        self.assertEqual(c.label, "our Friday-evening check-ins")
        self.assertEqual(c.weeks_seen, 3)

    def test_below_min_weeks_silent(self) -> None:
        self.assertEqual(
            sr.detect_rituals(
                self._slot(2), total_weeks=8, min_weeks=3, min_share=0.0,
            ),
            [],
        )

    def test_below_min_share_silent(self) -> None:
        # 3 weeks out of 20 = 0.15 share, below 0.34.
        self.assertEqual(
            sr.detect_rituals(
                self._slot(3), total_weeks=20, min_weeks=3, min_share=0.34,
            ),
            [],
        )

    def test_late_and_support_labels(self) -> None:
        slot = {
            ("saturday", "late", "support"): {(2026, w) for w in range(1, 5)},
        }
        out = sr.detect_rituals(slot, total_weeks=8, min_weeks=3)
        self.assertEqual(out[0].label, "our late-night Saturday heart-to-hearts")
        self.assertEqual(out[0].cadence, "Saturday late nights")

    def test_sorted_and_capped(self) -> None:
        slot = {
            ("friday", "evening", "casual_check_in"): {(2026, w) for w in range(1, 4)},
            ("sunday", "morning", "casual_check_in"): {(2026, w) for w in range(1, 6)},
        }
        out = sr.detect_rituals(
            slot, total_weeks=8, min_weeks=3, min_share=0.0, max_rituals=1,
        )
        self.assertEqual(len(out), 1)
        # Sunday recurred more weeks -> wins the cap.
        self.assertEqual(out[0].weekday, "sunday")


class MergeRitualsTests(unittest.TestCase):
    def _cand(self, key="friday:evening:casual_check_in", weeks=3):
        return sr.RitualCandidate(
            key=key, weekday="friday", bucket="evening",
            shape="casual_check_in", cadence="Friday evenings",
            shape_label="check-ins", label="our Friday-evening check-ins",
            weeks_seen=weeks, share=0.4,
        )

    def test_adds_new(self) -> None:
        merged, new = sr.merge_rituals([], [self._cand()], now_date="2026-06-01")
        self.assertEqual(len(merged), 1)
        self.assertEqual(new, ["friday:evening:casual_check_in"])
        self.assertFalse(merged[0]["acknowledged"])
        self.assertEqual(merged[0]["first_seen"], "2026-06-01")

    def test_preserves_acknowledged_and_first_seen(self) -> None:
        existing = [{
            "key": "friday:evening:casual_check_in",
            "label": "old", "weeks_seen": 3, "acknowledged": True,
            "first_seen": "2026-01-01",
        }]
        merged, new = sr.merge_rituals(
            existing, [self._cand(weeks=5)], now_date="2026-06-01",
        )
        self.assertEqual(new, [])
        row = merged[0]
        self.assertTrue(row["acknowledged"])
        self.assertEqual(row["first_seen"], "2026-01-01")
        self.assertEqual(row["weeks_seen"], 5)

    def test_drops_pending_fade(self) -> None:
        existing = [{
            "key": "monday:morning:casual_check_in",
            "label": "x", "weeks_seen": 3, "acknowledged": False,
        }]
        merged, _ = sr.merge_rituals(existing, [], now_date="2026-06-01")
        self.assertEqual(merged, [])

    def test_keeps_acknowledged_fade(self) -> None:
        existing = [{
            "key": "monday:morning:casual_check_in",
            "label": "x", "weeks_seen": 3, "acknowledged": True,
        }]
        merged, _ = sr.merge_rituals(existing, [], now_date="2026-06-01")
        self.assertEqual(len(merged), 1)


class PickAndRenderTests(unittest.TestCase):
    def test_pick_unacknowledged_strongest(self) -> None:
        rituals = [
            {"key": "a", "weeks_seen": 3, "acknowledged": True},
            {"key": "b", "weeks_seen": 4, "acknowledged": False},
            {"key": "c", "weeks_seen": 6, "acknowledged": False},
        ]
        self.assertEqual(sr.pick_unacknowledged(rituals)["key"], "c")

    def test_pick_none_when_all_acknowledged(self) -> None:
        self.assertIsNone(
            sr.pick_unacknowledged([{"key": "a", "acknowledged": True}])
        )

    def test_render_warm_cue(self) -> None:
        line = sr.render_inner_life_block(
            {"label": "our Friday-evening wind-downs", "weeks_seen": 5},
            user_display_name="Jacob",
        )
        self.assertIn("our Friday-evening wind-downs", line)
        self.assertIn("Jacob", line)
        self.assertIn("our thing", line)
        self.assertIn("ONCE", line)

    def test_render_empty_without_label(self) -> None:
        self.assertEqual(sr.render_inner_life_block({"weeks_seen": 5}), "")

    def test_mark_acknowledged(self) -> None:
        rituals = [{"key": "a", "acknowledged": False}]
        out = sr.mark_acknowledged(rituals, "a")
        self.assertTrue(out[0]["acknowledged"])
        # original not mutated
        self.assertFalse(rituals[0]["acknowledged"])


class KvHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict[str, str] = {}

    def test_round_trip(self) -> None:
        sr.save_rituals(self.store.__setitem__, [{"key": "a"}])
        self.assertEqual(
            sr.load_rituals(self.store.get)[0]["key"], "a",
        )

    def test_garbage_tolerated(self) -> None:
        self.store[sr.SHARED_RITUALS_KEY] = "not json"
        self.assertEqual(sr.load_rituals(self.store.get), [])


if __name__ == "__main__":
    unittest.main()
