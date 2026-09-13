"""Pass 16: C9 daytime long-focus into K72 (not a companion cue)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core.activity.evidence import long_focus_dates_from_sessions
from app.core.relationship import wellbeing_concern as wc
from app.core.session.session_controller import SessionController


_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _local(day_offset: int, hour: int) -> datetime:
    base = datetime.now(_LOCAL_TZ) - timedelta(days=day_offset)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _session(
    *,
    day_offset: int,
    hour: int,
    app: str = "Cursor",
    source: str = "foreground",
    hours: float = 6.0,
) -> dict:
    start = _local(day_offset, hour)
    end = start + timedelta(hours=hours)
    return {
        "source": source,
        "app": app,
        "title": "secret.py — assistant",
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "duration_seconds": int(hours * 3600),
    }


class LongFocusEvidenceTests(unittest.TestCase):
    def test_daytime_coding_clears_the_bar(self) -> None:
        dates = long_focus_dates_from_sessions(
            [
                _session(day_offset=d, hour=10) for d in (1, 2, 3)
            ],
            now=datetime.now(_LOCAL_TZ),
            window_days=7,
            min_seconds=wc.DEFAULT_LONG_FOCUS_MIN_SECONDS,
            night_start_hour=wc.LATE_NIGHT_START_HOUR,
            night_end_hour=wc.LATE_NIGHT_END_HOUR,
        )
        self.assertEqual(len(dates), 3)
        self.assertNotIn("secret.py", str(dates))

    def test_small_hours_are_not_long_focus(self) -> None:
        dates = long_focus_dates_from_sessions(
            [
                _session(day_offset=d, hour=2, hours=2.0) for d in (1, 2, 3)
            ],
            now=datetime.now(_LOCAL_TZ),
            window_days=7,
            min_seconds=wc.DEFAULT_LONG_FOCUS_MIN_SECONDS,
            night_start_hour=wc.LATE_NIGHT_START_HOUR,
            night_end_hour=wc.LATE_NIGHT_END_HOUR,
        )
        self.assertEqual(dates, [])

    def test_media_and_idle_do_not_count(self) -> None:
        dates = long_focus_dates_from_sessions(
            [
                _session(day_offset=1, hour=10, app="YouTube"),
                _session(day_offset=2, hour=10, source="idle"),
                _session(day_offset=3, hour=10, source="lock"),
            ],
            now=datetime.now(_LOCAL_TZ),
            window_days=7,
            min_seconds=wc.DEFAULT_LONG_FOCUS_MIN_SECONDS,
            night_start_hour=wc.LATE_NIGHT_START_HOUR,
            night_end_hour=wc.LATE_NIGHT_END_HOUR,
        )
        self.assertEqual(dates, [])

    def test_short_coding_day_is_silent(self) -> None:
        dates = long_focus_dates_from_sessions(
            [_session(day_offset=1, hour=14, hours=2.0)],
            now=datetime.now(_LOCAL_TZ),
            window_days=7,
            min_seconds=wc.DEFAULT_LONG_FOCUS_MIN_SECONDS,
            night_start_hour=wc.LATE_NIGHT_START_HOUR,
            night_end_hour=wc.LATE_NIGHT_END_HOUR,
        )
        self.assertEqual(dates, [])


class GatePinTests(unittest.TestCase):
    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))


if __name__ == "__main__":
    unittest.main()
