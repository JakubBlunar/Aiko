"""Unit tests for the K38 post-turn arming helper.

Exercises :meth:`PostTurnMixin._maybe_arm_self_correction` -- the helper
that runs the self-correction detector over Aiko's just-finished reply
and queues a cue for the next prompt assembly to claim. Covers the master
switch, the per-fire cooldown, and the hit / no-hit arming paths.

The cue goes to ``cue_pool`` rather than to an in-memory slot, so these
run against a real :class:`CueStore` on a throwaway file. That is not
incidental: the point of the move is that an owed correction outlives the
process, and a stub would be exactly as forgetful as the slot it replaced.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
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


class _Host(PostTurnMixin, CuePoolMixin):
    def __init__(
        self,
        store: CueStore | None,
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
        self._self_correction_cooldown_remaining = cooldown_remaining
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = None


# A reply that contradicts the canonical preference memory below.
_CONTRADICT_REPLY = "Honestly, these days I actually hate hiking in the mountains."
_PREF_MEM = _Mem(
    id=7,
    content="I really love hiking in the mountains.",
    kind="preference",
    confidence=0.85,
)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _host(self, **kwargs: Any) -> _Host:
        return _Host(self.store, **kwargs)

    def _queued(self) -> list:
        return self.store.pending("self_correction")


class MasterSwitchTests(_Fixture):
    def test_disabled_does_not_arm(self) -> None:
        host = self._host(
            agent_settings=_make_agent_settings(self_correction_enabled=False),
            memories=[_PREF_MEM],
        )
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertEqual(self._queued(), [])


class CooldownTests(_Fixture):
    def test_cooldown_blocks_and_decrements(self) -> None:
        host = self._host(memories=[_PREF_MEM], cooldown_remaining=2)
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertEqual(self._queued(), [])
        self.assertEqual(host._self_correction_cooldown_remaining, 1)

    def test_cooldown_zero_runs_detector(self) -> None:
        host = self._host(memories=[_PREF_MEM], cooldown_remaining=0)
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertEqual(len(self._queued()), 1)


class ArmingTests(_Fixture):
    def test_hit_queues_a_cue_and_resets_cooldown(self) -> None:
        host = self._host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        rows = self._queued()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload["memory_id"], 7)
        self.assertEqual(host._self_correction_cooldown_remaining, 3)

    def test_the_subject_is_what_she_should_correct_to(self) -> None:
        """Not the snippet she got wrong.

        She is likely to quote her own slip while owning it ("earlier I
        said X..."), so matching on the snippet would read repeating the
        mistake as having fixed it.
        """
        host = self._host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        subject = self._queued()[0].subject
        self.assertIn("love hiking", subject)
        self.assertNotIn("hate hiking", subject)

    def test_the_cue_text_carries_both_halves(self) -> None:
        host = self._host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        text = self._queued()[0].text
        self.assertIn("hate hiking in the mountains", text)
        self.assertIn("love hiking in the mountains", text)

    def test_no_hit_queues_nothing(self) -> None:
        host = self._host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction(
            "I had a wonderful relaxed afternoon today."
        )
        self.assertEqual(self._queued(), [])
        self.assertEqual(host._self_correction_cooldown_remaining, 0)

    def test_empty_reply_no_op(self) -> None:
        host = self._host(memories=[_PREF_MEM])
        host._maybe_arm_self_correction("   ")
        self.assertEqual(self._queued(), [])

    def test_no_memory_store_no_op(self) -> None:
        host = self._host(memories=[_PREF_MEM])
        host._memory_store = None
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertEqual(self._queued(), [])

    def test_a_refused_write_leaves_the_cooldown_alone(self) -> None:
        """No pool, no cue -- and so no reason to sit out three turns.

        The cooldown exists to stop one slip nagging, which presupposes
        the slip was actually raised. Spending it on a write that never
        landed would silence the next real correction too.
        """
        host = _Host(None, memories=[_PREF_MEM])
        host._maybe_arm_self_correction(_CONTRADICT_REPLY)
        self.assertEqual(host._self_correction_cooldown_remaining, 0)


class RenderCueTests(unittest.TestCase):
    """The pure renderer, which is also the write-time validation gate."""

    def test_a_hit_missing_either_half_renders_nothing(self) -> None:
        from app.core.conversation.self_correction_detector import (
            SelfCorrectionHit,
            render_cue,
        )

        base = dict(memory_id=1, label="definite", overlap=3)
        self.assertEqual(
            render_cue(
                SelfCorrectionHit(
                    reply_snippet="", memory_content="a thing", **base,
                )
            ),
            "",
        )
        self.assertEqual(
            render_cue(
                SelfCorrectionHit(
                    reply_snippet="said this", memory_content="  ", **base,
                )
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
