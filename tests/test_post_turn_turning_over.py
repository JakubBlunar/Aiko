"""Unit tests for the K28 post-turn arming helper.

Exercises
:meth:`PostTurnMixin._maybe_arm_turning_over_slot` -- the small
helper that decides whether a finished turn should stash a value
on ``_pending_turning_over_seconds`` for the next prompt
assembly's provider to consume. Built as a separate helper from
the post-turn orchestrator so the gate matrix can be tested in
isolation (the orchestrator itself is far too heavy to test
end-to-end without a real :class:`SessionController`).

Also covers the parallel-arm contract: K14's
``_pending_absence_seconds`` slot is set by the same engagement
record, but the two are independent — disabling K28 must not
suppress K14, and arming K28 must not consume K14.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.core.session.post_turn_mixin import PostTurnMixin


@dataclass(slots=True)
class _StubEngagement:
    """``EngagementResult``-shaped stub for arming tests."""

    mode: str = "typed"
    latency_seconds: float | None = None
    absence_seconds: float | None = None
    closeness_delta: float = 0.0
    label: str = ""


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(turning_over_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(turning_over_min_gap_minutes=90.0)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(PostTurnMixin):
    """Minimal mixin host with the attributes the helper reads."""

    def __init__(
        self,
        *,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
        pending_absence_seconds: float | None = None,
        pending_turning_over_seconds: float | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._pending_absence_seconds = pending_absence_seconds
        self._pending_turning_over_seconds = pending_turning_over_seconds


# ── Master switch ──────────────────────────────────────────────────────


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_does_not_arm(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(turning_over_enabled=False),
        )
        eng = _StubEngagement(latency_seconds=120 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        self.assertIsNone(host._pending_turning_over_seconds)


# ── Mode gate ──────────────────────────────────────────────────────────


class ModeGateTests(unittest.TestCase):
    def test_voice_mode_does_not_arm(self) -> None:
        host = _Host()
        eng = _StubEngagement(mode="voice", latency_seconds=120 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_typed_mode_arms(self) -> None:
        host = _Host()
        eng = _StubEngagement(mode="typed", latency_seconds=120 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        self.assertEqual(host._pending_turning_over_seconds, 120 * 60.0)


# ── Latency gate ───────────────────────────────────────────────────────


class LatencyGateTests(unittest.TestCase):
    def test_none_latency_does_not_arm(self) -> None:
        host = _Host()
        eng = _StubEngagement(latency_seconds=None)
        host._maybe_arm_turning_over_slot(eng)
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_below_threshold_does_not_arm(self) -> None:
        host = _Host()
        eng = _StubEngagement(latency_seconds=30 * 60.0)  # 30 min < 90 min
        host._maybe_arm_turning_over_slot(eng)
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_at_threshold_arms(self) -> None:
        host = _Host()
        eng = _StubEngagement(latency_seconds=90 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        self.assertEqual(host._pending_turning_over_seconds, 90 * 60.0)

    def test_zero_or_negative_does_not_arm(self) -> None:
        host = _Host()
        host._maybe_arm_turning_over_slot(_StubEngagement(latency_seconds=0.0))
        self.assertIsNone(host._pending_turning_over_seconds)
        host._maybe_arm_turning_over_slot(_StubEngagement(latency_seconds=-1.0))
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_custom_threshold_overrides_default(self) -> None:
        # Lower the threshold to 1 minute (after parser clamp would
        # raise it to 5; we bypass the parser here and inject directly).
        host = _Host(
            memory_settings=_make_memory_settings(
                turning_over_min_gap_minutes=1.0,
            ),
        )
        eng = _StubEngagement(latency_seconds=2 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        self.assertEqual(host._pending_turning_over_seconds, 120.0)


# ── Defensive paths ───────────────────────────────────────────────────


class DefensiveTests(unittest.TestCase):
    def test_none_engagement_is_no_op(self) -> None:
        host = _Host()
        host._maybe_arm_turning_over_slot(None)
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_non_numeric_latency_silently_skipped(self) -> None:
        host = _Host()
        # mode=typed is required to reach the float coercion path.
        eng = _StubEngagement(mode="typed", latency_seconds="garbage")  # type: ignore[arg-type]
        host._maybe_arm_turning_over_slot(eng)
        self.assertIsNone(host._pending_turning_over_seconds)


# ── Parallel-arm contract ─────────────────────────────────────────────


class ParallelArmTests(unittest.TestCase):
    """K28 arming must not disturb K14's ``_pending_absence_seconds``
    slot. The two cues stack on the 90 min - 4h overlap."""

    def test_arming_k28_does_not_clear_k14(self) -> None:
        host = _Host(pending_absence_seconds=99.0)
        eng = _StubEngagement(latency_seconds=120 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        # K14 slot untouched.
        self.assertEqual(host._pending_absence_seconds, 99.0)
        # K28 slot armed.
        self.assertEqual(host._pending_turning_over_seconds, 120 * 60.0)

    def test_k28_disabled_does_not_affect_k14(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(turning_over_enabled=False),
            pending_absence_seconds=99.0,
        )
        eng = _StubEngagement(latency_seconds=120 * 60.0)
        host._maybe_arm_turning_over_slot(eng)
        self.assertEqual(host._pending_absence_seconds, 99.0)
        self.assertIsNone(host._pending_turning_over_seconds)


if __name__ == "__main__":
    unittest.main()
