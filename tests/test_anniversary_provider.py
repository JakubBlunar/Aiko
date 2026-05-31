"""Tests for ``app.core.relationship.anniversary.pick_anniversary``.

Calendar-windowed matching, ±1-day tolerance, longest-window precedence,
6h rate-limit, pinning + recency tiebreaker.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.relationship.anniversary import (
    AnniversaryMatch,
    pick_anniversary,
    render_anniversary_block,
)
from app.core.relationship.shared_moments import SharedMomentRow


def _row(
    moment_id: int,
    when: datetime,
    *,
    summary: str = "a moment",
    vibe: str = "warm",
    pinned: bool = False,
    salience: float = 0.7,
    last_anniversaried_at: str | None = None,
) -> SharedMomentRow:
    return SharedMomentRow(
        id=moment_id,
        summary=summary,
        vibe=vibe,
        when=when.isoformat(),
        created_at=when.isoformat(),
        salience=salience,
        pinned=pinned,
        source="manual",
        confidence=1.0,
        source_message_ids=[],
        last_anniversaried_at=last_anniversaried_at,
    )


class TestPickAnniversary(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(pick_anniversary([], now=self.now))

    def test_no_match_returns_none(self) -> None:
        moment = _row(1, self.now - timedelta(days=17))
        self.assertIsNone(pick_anniversary([moment], now=self.now))

    def test_one_month_match_within_tolerance(self) -> None:
        # 29 days back falls inside the ±1 day tolerance for the 30d window.
        moment = _row(1, self.now - timedelta(days=29))
        match = pick_anniversary([moment], now=self.now)
        self.assertIsNotNone(match)
        self.assertEqual(match.moment_id, 1)
        self.assertEqual(match.window_label, "a month ago today")
        self.assertEqual(match.days_ago, 29)

    def test_one_year_beats_one_month(self) -> None:
        old = _row(1, self.now - timedelta(days=365))
        recent = _row(2, self.now - timedelta(days=30))
        match = pick_anniversary([old, recent], now=self.now)
        self.assertIsNotNone(match)
        self.assertEqual(match.moment_id, 1)
        self.assertEqual(match.window_label, "a year ago today")

    def test_pinned_wins_tie_inside_a_window(self) -> None:
        a = _row(1, self.now - timedelta(days=30), pinned=False, salience=0.9)
        b = _row(2, self.now - timedelta(days=30), pinned=True, salience=0.5)
        match = pick_anniversary([a, b], now=self.now)
        self.assertIsNotNone(match)
        self.assertEqual(match.moment_id, 2)

    def test_rate_limited_moment_skipped(self) -> None:
        recent_stamp = (self.now - timedelta(hours=1)).isoformat()
        moment = _row(
            1,
            self.now - timedelta(days=30),
            last_anniversaried_at=recent_stamp,
        )
        self.assertIsNone(pick_anniversary([moment], now=self.now))

    def test_stale_stamp_does_not_skip(self) -> None:
        old_stamp = (self.now - timedelta(days=2)).isoformat()
        moment = _row(
            1,
            self.now - timedelta(days=30),
            last_anniversaried_at=old_stamp,
        )
        match = pick_anniversary([moment], now=self.now)
        self.assertIsNotNone(match)
        self.assertEqual(match.moment_id, 1)

    def test_future_moments_ignored(self) -> None:
        moment = _row(1, self.now + timedelta(days=30))
        self.assertIsNone(pick_anniversary([moment], now=self.now))

    def test_unparseable_when_ignored(self) -> None:
        bad = SharedMomentRow(
            id=99,
            summary="??",
            vibe="warm",
            when="not a date",
            created_at="not a date",
            salience=0.5,
            pinned=False,
            source="manual",
            confidence=1.0,
            source_message_ids=[],
            last_anniversaried_at=None,
        )
        self.assertIsNone(pick_anniversary([bad], now=self.now))


class TestRenderBlock(unittest.TestCase):
    def test_render_none_returns_empty(self) -> None:
        self.assertEqual(render_anniversary_block(None), "")

    def test_render_includes_label_and_summary(self) -> None:
        match = AnniversaryMatch(
            moment_id=1,
            summary="we debugged the proactive bug together",
            vibe="focused",
            days_ago=30,
            window_label="a month ago today",
            when_iso="2026-04-27T12:00:00+00:00",
        )
        block = render_anniversary_block(match)
        self.assertIn("a month ago today", block)
        self.assertIn("debugged the proactive bug", block)
        self.assertIn("Acknowledge naturally", block)


if __name__ == "__main__":
    unittest.main()
