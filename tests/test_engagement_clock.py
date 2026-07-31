"""Tests for the shared :class:`EngagementClock` primitive.

Covers turn crediting (floor + idle cap), monotonicity + kv_meta
round-trip (survives a "reload"), the ``engaged_days_since`` conversion
and clamp, and the disabled short-circuit.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.infra.engagement_clock import EngagementClock


@dataclass
class _Settings:
    engagement_clock_enabled: bool = True
    engagement_seconds_per_day: float = 3600.0
    engagement_idle_cap_seconds: float = 300.0
    engagement_min_turn_seconds: float = 15.0


class _KV:
    """Minimal in-memory kv_meta stand-in."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = str(value)


def _clock(settings: _Settings, kv: _KV, now_fn):
    return EngagementClock(
        kv_get=kv.get, kv_set=kv.set, settings=settings, clock=now_fn,
    )


_T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


class RecordTurnTests(unittest.TestCase):
    def test_first_turn_credits_floor(self) -> None:
        kv = _KV()
        clock = _clock(_Settings(), kv, lambda: _T0)
        total = clock.record_turn()
        self.assertAlmostEqual(total, 15.0)

    def test_gap_credited_between_floor_and_cap(self) -> None:
        kv = _KV()
        settings = _Settings()
        t = {"now": _T0}
        clock = _clock(settings, kv, lambda: t["now"])
        clock.record_turn()  # first turn: floor, sets anchor at _T0
        # 120s later -> credits the full 120s (between floor and cap).
        t["now"] = _T0 + timedelta(seconds=120)
        total = clock.record_turn()
        self.assertAlmostEqual(total, 15.0 + 120.0)

    def test_long_gap_capped_to_idle_cap(self) -> None:
        kv = _KV()
        settings = _Settings(engagement_idle_cap_seconds=300.0)
        t = {"now": _T0}
        clock = _clock(settings, kv, lambda: t["now"])
        clock.record_turn()  # floor
        # A week away -> only the idle cap is credited, not the week.
        t["now"] = _T0 + timedelta(days=7)
        total = clock.record_turn()
        self.assertAlmostEqual(total, 15.0 + 300.0)

    def test_rapid_turn_credits_at_least_floor(self) -> None:
        kv = _KV()
        settings = _Settings(engagement_min_turn_seconds=15.0)
        t = {"now": _T0}
        clock = _clock(settings, kv, lambda: t["now"])
        clock.record_turn()
        t["now"] = _T0 + timedelta(seconds=2)  # 2s apart, below floor
        total = clock.record_turn()
        self.assertAlmostEqual(total, 15.0 + 15.0)

    def test_monotonic_and_survives_reload(self) -> None:
        kv = _KV()
        settings = _Settings()
        t = {"now": _T0}
        clock = _clock(settings, kv, lambda: t["now"])
        clock.record_turn()
        t["now"] = _T0 + timedelta(seconds=60)
        first = clock.record_turn()
        # A brand-new instance over the same kv sees the persisted total.
        reloaded = _clock(settings, kv, lambda: t["now"])
        self.assertAlmostEqual(reloaded.total(), first)
        t["now"] = _T0 + timedelta(seconds=120)
        second = reloaded.record_turn()
        self.assertGreater(second, first)


class EngagedDaysSinceTests(unittest.TestCase):
    def test_converts_via_seconds_per_day(self) -> None:
        kv = _KV()
        kv.set("engagement.total_units", "7200.0")  # 2 hours
        settings = _Settings(engagement_seconds_per_day=3600.0)
        clock = _clock(settings, kv, lambda: _T0)
        # anchor at 0 units, 7200s accumulated, 3600s/day -> 2.0 days
        self.assertAlmostEqual(clock.engaged_days_since(0.0), 2.0)

    def test_clamps_to_clamp_days(self) -> None:
        kv = _KV()
        kv.set("engagement.total_units", str(3600.0 * 100))  # 100 days
        settings = _Settings(engagement_seconds_per_day=3600.0)
        clock = _clock(settings, kv, lambda: _T0)
        self.assertAlmostEqual(
            clock.engaged_days_since(0.0, clamp_days=3.0), 3.0,
        )

    def test_negative_delta_reads_zero(self) -> None:
        kv = _KV()
        kv.set("engagement.total_units", "100.0")
        clock = _clock(_Settings(), kv, lambda: _T0)
        # anchor ahead of the current total (e.g. after a reset) -> 0
        self.assertEqual(clock.engaged_days_since(500.0), 0.0)


class DisabledTests(unittest.TestCase):
    def test_disabled_record_turn_is_noop(self) -> None:
        kv = _KV()
        settings = _Settings(engagement_clock_enabled=False)
        clock = _clock(settings, kv, lambda: _T0)
        self.assertEqual(clock.record_turn(), 0.0)
        self.assertEqual(clock.total(), 0.0)
        self.assertNotIn("engagement.total_units", kv.store)


if __name__ == "__main__":
    unittest.main()
