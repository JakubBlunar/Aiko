"""Unit tests for the K34 post-turn arming helper.

Exercises
:meth:`PostTurnMixin._maybe_arm_forward_curiosity_slot` -- the helper
that stashes a value on ``_pending_forward_curiosity_seconds`` for the
next prompt assembly's forward-curiosity provider to consume. Mirrors
the K28/K36 arming gate matrix with its own master switch
(``agent.forward_curiosity_enabled``) and threshold
(``memory.forward_curiosity_min_gap_hours``, default 4h).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.core.session.post_turn_mixin import PostTurnMixin


@dataclass(slots=True)
class _StubEngagement:
    mode: str = "typed"
    latency_seconds: float | None = None
    closeness_delta: float = 0.0
    label: str = ""


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(forward_curiosity_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(forward_curiosity_min_gap_hours=4.0)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(PostTurnMixin):
    def __init__(
        self,
        *,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
        pending_forward_curiosity_seconds: float | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._pending_forward_curiosity_seconds = (
            pending_forward_curiosity_seconds
        )


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_does_not_arm(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(
                forward_curiosity_enabled=False
            ),
        )
        eng = _StubEngagement(latency_seconds=5 * 3600.0)
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertIsNone(host._pending_forward_curiosity_seconds)


class ModeGateTests(unittest.TestCase):
    def test_voice_mode_does_not_arm(self) -> None:
        host = _Host()
        eng = _StubEngagement(mode="voice", latency_seconds=5 * 3600.0)
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertIsNone(host._pending_forward_curiosity_seconds)

    def test_typed_mode_arms(self) -> None:
        host = _Host()
        eng = _StubEngagement(mode="typed", latency_seconds=5 * 3600.0)
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertEqual(
            host._pending_forward_curiosity_seconds, 5 * 3600.0
        )


class GapGateTests(unittest.TestCase):
    def test_below_threshold_does_not_arm(self) -> None:
        host = _Host()
        eng = _StubEngagement(latency_seconds=2 * 3600.0)  # 2h < 4h
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertIsNone(host._pending_forward_curiosity_seconds)

    def test_at_threshold_arms(self) -> None:
        host = _Host()
        eng = _StubEngagement(latency_seconds=4 * 3600.0)
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertEqual(
            host._pending_forward_curiosity_seconds, 4 * 3600.0
        )

    def test_none_latency_does_not_arm(self) -> None:
        host = _Host()
        host._maybe_arm_forward_curiosity_slot(
            _StubEngagement(latency_seconds=None)
        )
        self.assertIsNone(host._pending_forward_curiosity_seconds)

    def test_zero_or_negative_does_not_arm(self) -> None:
        host = _Host()
        host._maybe_arm_forward_curiosity_slot(
            _StubEngagement(latency_seconds=0.0)
        )
        self.assertIsNone(host._pending_forward_curiosity_seconds)
        host._maybe_arm_forward_curiosity_slot(
            _StubEngagement(latency_seconds=-1.0)
        )
        self.assertIsNone(host._pending_forward_curiosity_seconds)

    def test_custom_threshold_overrides_default(self) -> None:
        host = _Host(
            memory_settings=_make_memory_settings(
                forward_curiosity_min_gap_hours=1.0,
            ),
        )
        eng = _StubEngagement(latency_seconds=2 * 3600.0)
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertEqual(
            host._pending_forward_curiosity_seconds, 2 * 3600.0
        )


class DefensiveTests(unittest.TestCase):
    def test_none_engagement_is_no_op(self) -> None:
        host = _Host()
        host._maybe_arm_forward_curiosity_slot(None)
        self.assertIsNone(host._pending_forward_curiosity_seconds)

    def test_non_numeric_latency_silently_skipped(self) -> None:
        host = _Host()
        eng = _StubEngagement(
            mode="typed", latency_seconds="garbage"  # type: ignore[arg-type]
        )
        host._maybe_arm_forward_curiosity_slot(eng)
        self.assertIsNone(host._pending_forward_curiosity_seconds)


if __name__ == "__main__":
    unittest.main()
