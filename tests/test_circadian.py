"""Tests for the circadian state computation."""
from __future__ import annotations

import unittest
from datetime import datetime

from app.core.affect.circadian import CircadianState, compute


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


class WeekdayAwarenessTests(unittest.TestCase):
    """Phase 4a — weekday + is_weekend on the CircadianState."""

    def test_weekday_name_matches_python(self) -> None:
        # 2026-05-24 was a Sunday; 2026-05-25 was a Monday, etc.
        cases = [
            (datetime(2026, 5, 25, 10), "Monday", False),
            (datetime(2026, 5, 26, 10), "Tuesday", False),
            (datetime(2026, 5, 29, 18), "Friday", False),
            (datetime(2026, 5, 30, 14), "Saturday", True),
            (datetime(2026, 5, 31, 14), "Sunday", True),
        ]
        for moment, name, weekend in cases:
            with self.subTest(day=name):
                state = compute(moment)
                self.assertEqual(state.weekday, name)
                self.assertEqual(state.is_weekend, weekend)

    def test_friday_evening_phrasing(self) -> None:
        state = compute(datetime(2026, 5, 29, 19, 30))  # Friday evening.
        line = state.ambient_line()
        self.assertIn("Friday", line)
        self.assertIn("evening", line)

    def test_lazy_sunday_afternoon_phrasing(self) -> None:
        state = compute(datetime(2026, 5, 31, 14, 30))  # Sunday afternoon.
        line = state.ambient_line()
        self.assertIn("Sunday", line)
        self.assertIn("lazy", line)

    def test_monday_morning_phrasing(self) -> None:
        state = compute(datetime(2026, 5, 25, 8, 30))  # Monday morning.
        line = state.ambient_line()
        self.assertIn("Monday", line)
        self.assertIn("morning", line)

    def test_neutral_day_period_combo_still_includes_weekday(self) -> None:
        # Wednesday midday — no special phrasing, but weekday is named.
        state = compute(datetime(2026, 5, 27, 12, 30))
        line = state.ambient_line()
        self.assertIn("Wednesday", line)


if __name__ == "__main__":
    unittest.main()
