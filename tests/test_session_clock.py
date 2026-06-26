"""K-time4 — session-clock pure module + provider plumbing tests."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.conversation import session_clock as sc
from app.core.infra import timephrase
from app.core.session.inner_life_part4 import InnerLifePart4Mixin


_NOW = datetime(2026, 6, 26, 22, 0, 0, tzinfo=timezone.utc)


def _t(minutes_before: float) -> datetime:
    return _NOW - timedelta(minutes=minutes_before)


# ── pure module ──────────────────────────────────────────────────────────


class ContinuousBurstTests(unittest.TestCase):
    def test_contiguous_run_within_break(self) -> None:
        # 70-min run, every step <= 20 min (< 30-min break).
        times = [_t(0), _t(2), _t(12), _t(30), _t(50), _t(70)]
        elapsed, start = sc.continuous_burst(times, _NOW, break_seconds=1800)
        self.assertAlmostEqual(elapsed, 70 * 60, delta=1)
        self.assertEqual(start, _t(70))

    def test_break_ends_the_sitting(self) -> None:
        # A 90-min gap between -10 and -100 splits the burst.
        times = [_t(0), _t(10), _t(100), _t(120)]
        elapsed, start = sc.continuous_burst(times, _NOW, break_seconds=1800)
        self.assertAlmostEqual(elapsed, 10 * 60, delta=1)
        self.assertEqual(start, _t(10))

    def test_empty_is_zero(self) -> None:
        elapsed, start = sc.continuous_burst([], _NOW, break_seconds=1800)
        self.assertEqual(elapsed, 0.0)
        self.assertEqual(start, _NOW)


class ClassifyTests(unittest.TestCase):
    def _classify(self, times: list[datetime]) -> sc.SessionClockSignal:
        return sc.classify(
            times, _NOW,
            long_seconds=60 * 60,
            very_long_seconds=150 * 60,
            break_seconds=30 * 60,
            gap_min_seconds=10 * 60,
            gap_max_seconds=30 * 60,
        )

    def test_long_band(self) -> None:
        sig = self._classify([_t(0), _t(2), _t(30), _t(50), _t(70)])
        self.assertEqual(sig.elapsed_band, "long")
        self.assertFalse(sig.gap_notable)  # last gap 2 min

    def test_very_long_band(self) -> None:
        times = [_t(m) for m in (0, 20, 40, 60, 80, 100, 120, 140, 160)]
        sig = self._classify(times)
        self.assertEqual(sig.elapsed_band, "very_long")

    def test_short_sitting_no_band(self) -> None:
        sig = self._classify([_t(0), _t(5), _t(20)])
        self.assertIsNone(sig.elapsed_band)

    def test_notable_pause(self) -> None:
        # 20-min pause before the latest message, short sitting otherwise.
        sig = self._classify([_t(0), _t(20), _t(40)])
        self.assertTrue(sig.gap_notable)
        self.assertAlmostEqual(sig.gap_seconds, 20 * 60, delta=1)

    def test_pause_below_band_not_notable(self) -> None:
        sig = self._classify([_t(0), _t(5), _t(25)])
        self.assertFalse(sig.gap_notable)

    def test_pause_at_or_above_cap_not_notable(self) -> None:
        # 35-min pause is the gap-return family's territory, not K-time4.
        sig = self._classify([_t(0), _t(35), _t(60)])
        self.assertFalse(sig.gap_notable)


class HumanizeTests(unittest.TestCase):
    def test_elapsed_phrases(self) -> None:
        self.assertEqual(sc.humanize_elapsed(65 * 60), "about an hour")
        self.assertEqual(sc.humanize_elapsed(120 * 60), "an hour and a half or so")
        self.assertEqual(sc.humanize_elapsed(160 * 60), "a couple of hours")
        self.assertEqual(sc.humanize_elapsed(4 * 3600), "4 hours")

    def test_pause_rounds_to_five(self) -> None:
        self.assertEqual(sc.humanize_pause(17 * 60), "about 15 minutes")
        self.assertEqual(sc.humanize_pause(23 * 60), "about 25 minutes")


class RenderTests(unittest.TestCase):
    def test_elapsed_line_has_guard(self) -> None:
        sig = sc.SessionClockSignal(
            elapsed_seconds=70 * 60, elapsed_band="long",
            burst_start_iso="x", gap_seconds=0.0, gap_notable=False,
        )
        out = sc.render_block(sig, "Jacob")
        self.assertIn("Jacob", out)
        self.assertIn("about an hour", out)
        self.assertIn("never police", out.lower())

    def test_pause_line(self) -> None:
        sig = sc.SessionClockSignal(
            elapsed_seconds=0.0, elapsed_band=None,
            burst_start_iso="x", gap_seconds=20 * 60, gap_notable=True,
        )
        out = sc.render_block(sig, "Jacob")
        self.assertIn("away about 20 minutes", out)
        self.assertIn("never make them explain", out.lower())

    def test_nothing_renders_empty(self) -> None:
        sig = sc.SessionClockSignal(
            elapsed_seconds=0.0, elapsed_band=None,
            burst_start_iso="x", gap_seconds=0.0, gap_notable=False,
        )
        self.assertEqual(sc.render_block(sig, "Jacob"), "")


# ── provider plumbing ─────────────────────────────────────────────────────


@dataclass
class _Row:
    created_at: str
    role: str = "user"
    content: str = "msg"


class _Agent:
    session_clock_enabled = True
    session_clock_long_minutes = 60.0
    session_clock_very_long_minutes = 150.0
    session_clock_break_minutes = 30.0
    session_clock_gap_min_minutes = 10.0
    session_clock_gap_max_minutes = 30.0


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart4Mixin):
    def __init__(self, rows: list[_Row]) -> None:
        self._settings = _Settings()
        self._rows = rows

    def _inner_life_recent_messages(self, limit: int) -> list[_Row]:
        return list(self._rows)  # already oldest-first

    @property
    def user_display_name(self) -> str:
        return "Jacob"


def _rows(*minutes_before: float) -> list[_Row]:
    # Args given newest-last (e.g. 70, 50, ..., 0) so the produced list is
    # oldest-first, matching chat_db.get_messages order.
    return [_Row(created_at=_t(m).isoformat()) for m in minutes_before]


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        timephrase.set_now_provider(lambda: _NOW)
        self.addCleanup(timephrase.set_now_provider, None)

    def test_disabled_blank(self) -> None:
        host = _Host(_rows(70, 50, 30, 2, 0))
        host._settings.agent.session_clock_enabled = False
        self.assertEqual(host._render_session_clock_block(), "")

    def test_elapsed_surfaces_once_then_suppressed(self) -> None:
        host = _Host(_rows(70, 50, 30, 12, 2, 0))
        first = host._render_session_clock_block()
        self.assertIn("been talking for about an hour", first)
        self.assertEqual(host._session_clock_fired_band, "long")
        # Same sitting + same band -> suppressed.
        self.assertEqual(host._render_session_clock_block(), "")

    def test_stronger_band_resurfaces(self) -> None:
        host = _Host(_rows(70, 50, 30, 12, 2, 0))
        self.assertTrue(host._render_session_clock_block())
        # The sitting grows past the very-long threshold (new burst start).
        host._rows = _rows(160, 140, 120, 100, 80, 60, 40, 20, 2, 0)
        out = host._render_session_clock_block()
        self.assertIn("a couple of hours", out)
        self.assertEqual(host._session_clock_fired_band, "very_long")

    def test_pause_surfaces_once_then_suppressed(self) -> None:
        host = _Host(_rows(40, 20, 0))  # 20-min pause, short sitting
        first = host._render_session_clock_block()
        self.assertIn("away about 20 minutes", first)
        self.assertEqual(host._render_session_clock_block(), "")

    def test_no_signal_blank(self) -> None:
        host = _Host(_rows(20, 5, 0))  # short sitting, tiny pause
        self.assertEqual(host._render_session_clock_block(), "")

    def test_force_bypasses_watermark(self) -> None:
        host = _Host(_rows(70, 50, 30, 12, 2, 0))
        self.assertTrue(host._render_session_clock_block())
        self.assertEqual(host._render_session_clock_block(), "")  # suppressed
        host._session_clock_force_next = True
        out = host._render_session_clock_block()
        self.assertIn("been talking for", out)
        self.assertFalse(host._session_clock_force_next)


if __name__ == "__main__":
    unittest.main()
