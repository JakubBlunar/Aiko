"""Tests for H18 weighted idle-activity selection (pure functions)."""
from __future__ import annotations

import random
import unittest

from app.core.world.activity_selection import compute_weights, weighted_pick


_KEYS = ["snack", "read_book", "move_cat", "look_outside", "tidy_desk", "doodle", "wander"]


class ComputeWeightsTests(unittest.TestCase):
    def test_flat_weights_with_no_context(self) -> None:
        w = compute_weights(_KEYS)
        self.assertTrue(all(v == 1.0 for v in w.values()))

    def test_recency_penalises_recent_keys(self) -> None:
        # "doodle" fired most recently -> heavily down-weighted vs a key
        # that never fired.
        w = compute_weights(_KEYS, recent_keys=["wander", "snack", "doodle"])
        self.assertLess(w["doodle"], w["read_book"])
        self.assertLess(w["doodle"], 0.5)

    def test_recency_penalty_fades_with_distance(self) -> None:
        # Same key at distance 0 vs distance 3 -> the older one is penalised
        # less (closer to 1.0).
        recent_now = ["doodle"]
        recent_old = ["doodle", "a", "b", "c"]
        w_now = compute_weights(["doodle"], recent_keys=recent_now)
        w_old = compute_weights(["doodle"], recent_keys=recent_old)
        self.assertLess(w_now["doodle"], w_old["doodle"])

    def test_floor_keeps_weights_positive(self) -> None:
        # Hammer a key with many repeats; it must still stay above the floor.
        recent = ["doodle"] * 6
        w = compute_weights(["doodle"], recent_keys=recent)
        self.assertGreaterEqual(w["doodle"], 0.05)

    def test_circadian_bias_favours_nap_at_night(self) -> None:
        w = compute_weights(["nap", "tidy_desk"], period="late_night")
        self.assertGreater(w["nap"], w["tidy_desk"])

    def test_circadian_bias_favours_tidy_in_morning(self) -> None:
        w = compute_weights(["nap", "tidy_desk"], period="morning")
        self.assertGreater(w["tidy_desk"], w["nap"])

    def test_low_valence_favours_cozy(self) -> None:
        w = compute_weights(["read_book", "tidy_desk"], valence=-0.5)
        self.assertGreater(w["read_book"], w["tidy_desk"])

    def test_high_valence_favours_active(self) -> None:
        w = compute_weights(["read_book", "doodle"], valence=0.6)
        self.assertGreater(w["doodle"], w["read_book"])

    def test_day_color_cozy_favours_reading(self) -> None:
        w = compute_weights(["read_book", "tidy_desk"], day_color="cozy")
        self.assertGreater(w["read_book"], w["tidy_desk"])

    def test_unknown_period_and_color_are_neutral(self) -> None:
        w = compute_weights(_KEYS, period="zzz", day_color="zzz")
        self.assertTrue(all(v == 1.0 for v in w.values()))


class WeightedPickTests(unittest.TestCase):
    def test_returns_none_on_empty(self) -> None:
        self.assertIsNone(weighted_pick([], rng=random.Random(0)))

    def test_avoids_recent_over_many_draws(self) -> None:
        # Over many draws, the most-recent key should be picked notably less
        # than a fresh one.
        rng = random.Random(1234)
        counts = {"doodle": 0, "read_book": 0}
        for _ in range(2000):
            k = weighted_pick(
                ["doodle", "read_book"],
                rng=rng,
                recent_keys=["doodle"],
            )
            counts[k] += 1
        self.assertLess(counts["doodle"], counts["read_book"])

    def test_single_candidate_always_returned(self) -> None:
        k = weighted_pick(["wander"], rng=random.Random(0), recent_keys=["wander"] * 5)
        self.assertEqual(k, "wander")


if __name__ == "__main__":
    unittest.main()
