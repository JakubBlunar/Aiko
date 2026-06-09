"""Unit tests for the K38 post-turn arming helper.

Exercises :meth:`PostTurnMixin._maybe_arm_self_correction` -- the
helper that runs the self-correction detector over Aiko's just-finished
reply and stashes a :class:`SelfCorrectionHit` on
``_pending_self_correction`` for the next prompt assembly to surface.
Covers the master switch, the per-fire cooldown, and the hit / no-hit
arming paths.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.core.session.post_turn_mixin import PostTurnMixin


@dataclass(frozen=True)
class _Mem:
    id: int
    content: str
    kind: str = "fact"
    confidence: float = 0.8


class _StubMemoryStore:
    def __init__(self, memories: list[_Mem]) -> None:
        self._mem = memories

    def iter_by_kind(self, kind: str) -> list[_Mem]:
        return [m for m in self._mem if m.kind == kind]


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(self_correction_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        self_correction_min_confidence=0.6,
        self_correction_min_overlap=2,
        self_correction_max_candidates=50,
        self_correction_cooldown_turns=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(PostTurnMixin):
    def __init__(
        self,
        *,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
        memories: list[_Mem] | None = None,
        cooldown_remaining: int = 0,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._memory_store = _StubMemoryStore(memories or [])
        self._pending_self_correction = None
        self._self_correction_cooldown_remaining = cooldown_remaining


# A reply that contradicts the canonical preference memory below.
_CONTRADICT_REPLY = "Honestly, these days I actually hate hiking in the mountains."
_PREF_MEM = _Mem(
    id=7,
    content="I really love hiking in the mountains.",
    kind="preference",
    confidence=0.85,
)


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_does_not_arm(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(self_correction_enabled=False),
            memories=[_PREF_MEM],
        )
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertIsNone(host._pending_self_correction)


class CooldownTests(unittest.TestCase):
    def test_cooldown_blocks_and_decrements(self) -> None:
        host = _Host(memories=[_PREF_MEM], cooldown_remaining=2)
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertIsNone(host._pending_self_correction)
        self.assertEqual(host._self_correction_cooldown_remaining, 1)

    def test_cooldown_zero_runs_detector(self) -> None:
        host = _Host(memories=[_PREF_MEM], cooldown_remaining=0)
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertIsNotNone(host._pending_self_correction)


class ArmingTests(unittest.TestCase):
    def test_hit_sets_pending_and_resets_cooldown(self) -> None:
        host = _Host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertIsNotNone(host._pending_self_correction)
        self.assertEqual(host._pending_self_correction.memory_id, 7)
        self.assertEqual(host._self_correction_cooldown_remaining, 3)

    def test_no_hit_stays_none(self) -> None:
        host = _Host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction(
            "I had a wonderful relaxed afternoon today."
        )
        self.assertIsNone(host._pending_self_correction)
        self.assertEqual(host._self_correction_cooldown_remaining, 0)

    def test_empty_reply_no_op(self) -> None:
        host = _Host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction("   ")
        self.assertIsNone(host._pending_self_correction)

    def test_no_memory_store_no_op(self) -> None:
        host = _Host(memories=[_PREF_MEM])
        host._memory_store = None
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertIsNone(host._pending_self_correction)


if __name__ == "__main__":
    unittest.main()
