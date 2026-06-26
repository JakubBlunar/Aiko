"""Tests for the K-time2 relative-time-expression parser."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.core.infra.time_expr import (
    DIR_FUTURE,
    DIR_PAST,
    TimeWindow,
    parse_time_window,
)

# Wednesday, 2026-06-17 14:00 UTC. June 1 2026 is a Monday, so the 17th
# is a Wednesday (weekday() == 2). All expected windows are derived from
# this fixed anchor so the tests never drift with the real clock.
NOW = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)


def _w(text: str) -> TimeWindow | None:
    return parse_time_window(text, NOW)


class NoMatchTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertIsNone(parse_time_window("", NOW))

    def test_no_time_phrase(self) -> None:
        self.assertIsNone(_w("how are you doing with the dashboard?"))

    def test_bare_in_is_not_a_month(self) -> None:
        self.assertIsNone(_w("I'm interested in the project"))


class DayAnchorTests(unittest.TestCase):
    def test_yesterday(self) -> None:
        win = _w("what did I tell you yesterday about the dashboard?")
        assert win is not None
        self.assertEqual(win.label, "yesterday")
        self.assertEqual(win.direction, DIR_PAST)
        self.assertTrue(win.guardable)
        self.assertEqual(win.start, datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(win.start.day, 16)
        self.assertEqual(win.end.day, 16)
        self.assertEqual(win.end.hour, 23)

    def test_today_not_guardable(self) -> None:
        win = _w("how are you today?")
        assert win is not None
        self.assertEqual(win.label, "today")
        self.assertEqual(win.direction, DIR_PAST)
        self.assertFalse(win.guardable)
        self.assertEqual(win.start.day, 17)
        self.assertEqual(win.end.day, 17)

    def test_tomorrow_is_future(self) -> None:
        win = _w("are we still on for tomorrow?")
        assert win is not None
        self.assertEqual(win.direction, DIR_FUTURE)
        self.assertFalse(win.guardable)
        self.assertEqual(win.start.day, 18)

    def test_last_night(self) -> None:
        win = _w("that thing from last night")
        assert win is not None
        self.assertEqual(win.label, "last night")
        self.assertEqual(win.start, datetime(2026, 6, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(win.end.day, 16)

    def test_this_morning(self) -> None:
        win = _w("you said this morning that...")
        assert win is not None
        self.assertEqual(win.start.hour, 5)
        self.assertEqual(win.start.day, 17)
        self.assertEqual(win.end.hour, 11)


class AgoTests(unittest.TestCase):
    def test_three_days_ago(self) -> None:
        win = _w("3 days ago we talked about it")
        assert win is not None
        self.assertEqual(win.start.day, 14)
        self.assertEqual(win.end.day, 14)
        self.assertTrue(win.guardable)

    def test_couple_days_ago(self) -> None:
        win = _w("a couple days ago")
        assert win is not None
        self.assertEqual(win.start.day, 15)  # 2 days before the 17th

    def test_two_weeks_ago(self) -> None:
        # anchor - 14d = 2026-06-03 (Wed); that ISO week starts Mon 06-01.
        win = _w("two weeks ago")
        assert win is not None
        self.assertEqual(win.start, datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(win.end.day, 7)

    def test_one_month_ago(self) -> None:
        win = _w("about a month ago")
        assert win is not None
        self.assertEqual(win.start.month, 5)
        self.assertEqual(win.start.day, 1)
        self.assertEqual(win.end.month, 5)
        self.assertEqual(win.end.day, 31)


class SpanTests(unittest.TestCase):
    def test_last_week_is_previous_iso_week(self) -> None:
        # This week's Monday is 2026-06-15; last week = 06-08 .. 06-14.
        win = _w("remember that thing from last week?")
        assert win is not None
        self.assertEqual(win.start, datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(win.end.day, 14)
        self.assertTrue(win.guardable)

    def test_this_week(self) -> None:
        win = _w("earlier this week")
        assert win is not None
        self.assertEqual(win.start.day, 15)  # Monday
        self.assertEqual(win.end.day, 17)

    def test_last_month(self) -> None:
        win = _w("last month I mentioned")
        assert win is not None
        self.assertEqual(win.start.month, 5)
        self.assertEqual(win.end.month, 5)
        self.assertEqual(win.end.day, 31)

    def test_this_month(self) -> None:
        win = _w("this month has been busy")
        assert win is not None
        self.assertEqual(win.start.month, 6)
        self.assertEqual(win.start.day, 1)

    def test_last_3_days(self) -> None:
        win = _w("anything from the last 3 days?")
        assert win is not None
        self.assertEqual(win.start.day, 14)
        self.assertEqual(win.end.day, 17)


class WeekdayAndMonthTests(unittest.TestCase):
    def test_on_monday(self) -> None:
        # Anchor is Wednesday; most recent past Monday is 06-15.
        win = _w("you mentioned on Monday")
        assert win is not None
        self.assertEqual(win.start.day, 15)
        self.assertTrue(win.guardable)

    def test_back_in_march(self) -> None:
        win = _w("back in March we discussed")
        assert win is not None
        self.assertEqual(win.start.month, 3)
        self.assertEqual(win.start.year, 2026)
        self.assertEqual(win.end.month, 3)
        self.assertEqual(win.end.day, 31)

    def test_in_future_month_rolls_back_a_year(self) -> None:
        # "in December" said in June → last December (2025).
        win = _w("back in December")
        assert win is not None
        self.assertEqual(win.start.month, 12)
        self.assertEqual(win.start.year, 2025)


class ContainsTests(unittest.TestCase):
    def test_contains_inside_and_outside(self) -> None:
        win = _w("yesterday")
        assert win is not None
        self.assertTrue(win.contains(datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)))
        self.assertFalse(win.contains(datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc)))
        self.assertFalse(win.contains(None))

    def test_contains_naive_treated_as_utc(self) -> None:
        win = _w("yesterday")
        assert win is not None
        self.assertTrue(win.contains(datetime(2026, 6, 16, 9, 0)))


if __name__ == "__main__":
    unittest.main()
