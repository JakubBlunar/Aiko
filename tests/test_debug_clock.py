"""Tests for the DT1 virtual clock (`app/core/infra/debug_clock.py`).

Covers the offset arithmetic, the seam installation, the env gate, and
the engaged-time lever -- including its undo anchor, which is the only
part of DT1 that touches persisted state.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.infra import timephrase as tp
from app.core.infra.debug_clock import (
    ENV_FLAG,
    DebugClock,
    debug_clock_enabled,
)
from app.core.infra.engagement_clock import EngagementClock


def _engagement(seconds_per_day: float = 3600.0) -> EngagementClock:
    """An engagement clock over a plain dict, so tests need no database."""
    store: dict[str, str] = {}
    clock = EngagementClock(
        kv_get=store.get,
        kv_set=lambda k, v: store.__setitem__(k, v),
        settings=SimpleNamespace(engagement_seconds_per_day=seconds_per_day),
    )
    clock._test_store = store  # type: ignore[attr-defined]
    return clock


class EnvGateTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        self.assertFalse(debug_clock_enabled({}))

    def test_truthy_spellings(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(
                debug_clock_enabled({ENV_FLAG: value}), msg=value,
            )

    def test_falsy_spellings(self) -> None:
        for value in ("", "0", "false", "no", "off", "maybe"):
            self.assertFalse(
                debug_clock_enabled({ENV_FLAG: value}), msg=value,
            )


class DisabledClockTests(unittest.TestCase):
    """A disabled clock must be completely inert -- the whole safety story."""

    def setUp(self) -> None:
        self.clock = DebugClock(enabled=False)

    def tearDown(self) -> None:
        tp.set_now_provider(None)

    def test_install_is_a_noop(self) -> None:
        self.assertFalse(self.clock.install())
        before = tp.now()
        self.assertLess(abs((tp.now() - before).total_seconds()), 5)

    def test_mutators_refuse_and_explain(self) -> None:
        for result in (
            self.clock.advance(days=5),
            self.clock.set_to("2030-01-01T00:00:00Z"),
            self.clock.advance_engaged(5),
            self.clock.reset(),
        ):
            self.assertFalse(result["ok"])
            self.assertIn(ENV_FLAG, result["error"])

    def test_never_reports_active(self) -> None:
        self.clock.advance(days=99)
        self.assertFalse(self.clock.active)
        self.assertEqual(self.clock.offset, timedelta(0))


class OffsetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = DebugClock(enabled=True)

    def tearDown(self) -> None:
        self.clock.uninstall()
        tp.set_now_provider(None)

    def test_advance_shifts_now(self) -> None:
        before = self.clock.now()
        self.clock.advance(days=7)
        delta = (self.clock.now() - before).total_seconds()
        self.assertAlmostEqual(delta, 7 * 86400, delta=5)

    def test_advances_accumulate(self) -> None:
        self.clock.advance(days=2)
        self.clock.advance(hours=12)
        self.assertAlmostEqual(
            self.clock.offset.total_seconds(), 2.5 * 86400, delta=1,
        )

    def test_negative_advance_goes_back(self) -> None:
        self.clock.advance(days=-3)
        self.assertLess(self.clock.offset.total_seconds(), 0)

    def test_set_to_absolute_instant(self) -> None:
        target = datetime(2031, 3, 4, 5, 6, tzinfo=timezone.utc)
        self.clock.set_to(target.isoformat())
        self.assertAlmostEqual(
            (self.clock.now() - target).total_seconds(), 0, delta=5,
        )

    def test_set_to_rejects_garbage(self) -> None:
        result = self.clock.set_to("not-a-date")
        self.assertFalse(result["ok"])
        self.assertEqual(self.clock.offset, timedelta(0))

    def test_reset_returns_to_real_time(self) -> None:
        self.clock.advance(days=30)
        self.clock.reset()
        self.assertFalse(self.clock.active)
        self.assertAlmostEqual(
            (self.clock.now() - tp.real_now()).total_seconds(), 0, delta=5,
        )

    def test_active_only_once_shifted(self) -> None:
        self.assertFalse(self.clock.active)
        self.clock.advance(hours=1)
        self.assertTrue(self.clock.active)


class SeamInstallTests(unittest.TestCase):
    """The point of the whole exercise: the seam moves the whole app."""

    def tearDown(self) -> None:
        tp.set_now_provider(None)

    def test_install_moves_timephrase_now(self) -> None:
        clock = DebugClock(enabled=True)
        self.assertTrue(clock.install())
        clock.advance(days=10)
        delta = (tp.now() - tp.real_now()).total_seconds()
        self.assertAlmostEqual(delta, 10 * 86400, delta=5)

    def test_install_moves_utcnow_too(self) -> None:
        clock = DebugClock(enabled=True)
        clock.install()
        clock.advance(days=3)
        delta = (tp.utcnow() - tp.real_now()).total_seconds()
        self.assertAlmostEqual(delta, 3 * 86400, delta=5)

    def test_utcnow_is_utc_even_when_shifted(self) -> None:
        clock = DebugClock(enabled=True)
        clock.install()
        clock.advance(days=3)
        self.assertEqual(tp.utcnow().utcoffset(), timedelta(0))

    def test_real_now_bypasses_the_offset(self) -> None:
        clock = DebugClock(enabled=True)
        clock.install()
        clock.advance(days=365)
        # Would recurse or drift if real_now went through the provider.
        self.assertLess(
            abs((tp.real_now() - datetime.now(timezone.utc)).total_seconds()),
            5,
        )

    def test_uninstall_restores_the_real_provider(self) -> None:
        clock = DebugClock(enabled=True)
        clock.install()
        clock.advance(days=10)
        clock.uninstall()
        self.assertAlmostEqual(
            (tp.now() - tp.real_now()).total_seconds(), 0, delta=5,
        )

    def test_double_install_is_idempotent(self) -> None:
        clock = DebugClock(enabled=True)
        self.assertTrue(clock.install())
        self.assertFalse(clock.install())


class EngagedTimeTests(unittest.TestCase):
    """The lever that actually reaches concept + memory decay."""

    def setUp(self) -> None:
        self.engagement = _engagement(seconds_per_day=3600.0)
        self.clock = DebugClock(
            enabled=True, engagement_clock=self.engagement,
        )

    def tearDown(self) -> None:
        tp.set_now_provider(None)

    def test_wall_clock_advance_does_not_touch_engagement(self) -> None:
        # The motivating fact: shifting wall time leaves decay untouched.
        self.clock.advance(days=60)
        self.assertEqual(self.engagement.total(), 0.0)

    def test_advance_engaged_credits_units(self) -> None:
        self.clock.advance_engaged(60)
        self.assertAlmostEqual(self.engagement.total(), 60 * 3600.0, delta=1)

    def test_engaged_days_since_reads_the_credit(self) -> None:
        self.clock.advance_engaged(45)
        self.assertAlmostEqual(
            self.engagement.engaged_days_since(0.0), 45.0, delta=0.01,
        )

    def test_reset_restores_the_original_total(self) -> None:
        self.engagement.debug_advance(0)  # anchor at 0
        self.engagement._safe_set("engagement.total_units", repr(1000.0))
        self.clock.advance_engaged(50)
        self.assertGreater(self.engagement.total(), 1000.0)
        self.clock.reset()
        self.assertAlmostEqual(self.engagement.total(), 0.0, delta=1)

    def test_repeated_advances_share_one_anchor(self) -> None:
        self.engagement._safe_set("engagement.total_units", repr(500.0))
        self.clock.advance_engaged(10)
        self.clock.advance_engaged(10)
        self.assertAlmostEqual(self.engagement.debug_anchor(), 500.0, delta=1)
        self.clock.reset()
        self.assertAlmostEqual(self.engagement.total(), 500.0, delta=1)

    def test_restore_without_an_advance_is_none(self) -> None:
        self.assertIsNone(self.engagement.debug_restore())

    def test_advance_after_restore_re_anchors(self) -> None:
        self.engagement._safe_set("engagement.total_units", repr(100.0))
        self.clock.advance_engaged(5)
        self.clock.reset()
        self.engagement._safe_set("engagement.total_units", repr(200.0))
        self.clock.advance_engaged(5)
        self.assertAlmostEqual(self.engagement.debug_anchor(), 200.0, delta=1)

    def test_missing_engagement_clock_is_reported(self) -> None:
        bare = DebugClock(enabled=True)
        result = bare.advance_engaged(10)
        self.assertFalse(result["ok"])
        self.assertIn("engagement", result["error"])


class StatusTests(unittest.TestCase):
    def tearDown(self) -> None:
        tp.set_now_provider(None)

    def test_status_shows_the_shift(self) -> None:
        clock = DebugClock(enabled=True, engagement_clock=_engagement())
        clock.install()
        clock.advance(days=3, hours=4)
        status = clock.status()
        self.assertTrue(status["active"])
        self.assertTrue(status["installed"])
        self.assertEqual(status["offset"], "+3d 4h")
        self.assertAlmostEqual(
            status["offset_seconds"], 3 * 86400 + 4 * 3600, delta=1,
        )
        self.assertNotEqual(status["real_now"], status["virtual_now"])

    def test_status_reads_real_time_when_idle(self) -> None:
        status = DebugClock(enabled=True).status()
        self.assertFalse(status["active"])
        self.assertEqual(status["offset"], "real time")
        self.assertEqual(status["real_now"], status["virtual_now"])

    def test_status_surfaces_synthetic_engagement(self) -> None:
        engagement = _engagement()
        clock = DebugClock(enabled=True, engagement_clock=engagement)
        clock.advance_engaged(12)
        block = clock.status()["engagement"]
        self.assertAlmostEqual(block["synthetic_units"], 12 * 3600.0, delta=1)
        self.assertEqual(block["debug_anchor"], 0.0)


if __name__ == "__main__":
    unittest.main()
