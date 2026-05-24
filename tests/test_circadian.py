"""Tests for the circadian state computation."""
from __future__ import annotations

import unittest
from datetime import datetime

from app.core.circadian import CircadianState, compute


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 24, hour, minute, 0)


class CircadianTests(unittest.TestCase):
    def test_period_classification(self) -> None:
        cases = [
            (2, "late_night"),
            (6, "early_morning"),
            (10, "morning"),
            (13, "midday"),
            (15, "afternoon"),
            (19, "evening"),
            (23, "night"),
        ]
        for hour, expected in cases:
            with self.subTest(hour=hour):
                state = compute(_at(hour))
                self.assertEqual(state.period, expected)

    def test_energy_curve_continuous(self) -> None:
        """Energy should rise from late night through afternoon."""
        prev = compute(_at(3)).energy
        for hour in range(4, 14):
            state = compute(_at(hour))
            self.assertGreaterEqual(
                state.energy + 0.05, prev,
                msg=f"energy non-monotone at hour {hour}",
            )
            prev = state.energy

    def test_drowsy_flag_only_at_night_with_low_energy(self) -> None:
        # Late night (3am) with no drift -> drowsy.
        state = compute(_at(3))
        self.assertTrue(state.drowsy)
        # Mid-afternoon -> never drowsy regardless of drift.
        state = compute(_at(15))
        self.assertFalse(state.drowsy)

    def test_sociability_bias_in_range(self) -> None:
        for hour in range(0, 24):
            state = compute(_at(hour))
            self.assertGreaterEqual(state.sociability_bias, -0.3)
            self.assertLessEqual(state.sociability_bias, 0.3)

    def test_drift_shifts_peak_later(self) -> None:
        """positive drift = night-owl baseline -> later energy peak."""
        morning_person = compute(_at(11), baseline_drift=-1.0)
        night_owl = compute(_at(11), baseline_drift=+1.0)
        # At 11am the morning person should have higher energy.
        self.assertGreater(morning_person.energy, night_owl.energy)
        evening_morning_person = compute(_at(20), baseline_drift=-1.0)
        evening_night_owl = compute(_at(20), baseline_drift=+1.0)
        self.assertGreater(evening_night_owl.energy, evening_morning_person.energy)

    def test_ambient_line_includes_period_and_time(self) -> None:
        state = compute(_at(23, 14))
        line = state.ambient_line()
        self.assertIn("11:14 PM", line)
        self.assertIn("night", line)

    def test_ambient_line_drowsy_message(self) -> None:
        state = compute(_at(3, 0))
        line = state.ambient_line()
        self.assertIn("drowsy", line)


if __name__ == "__main__":
    unittest.main()
