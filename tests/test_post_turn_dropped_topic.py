"""Unit tests for the K82 post-turn arming helper.

Exercises :meth:`PostTurnHelpersMixin._maybe_arm_dropped_topic` -- the
helper that runs the dropped-topic detector over the just-finished turn
and queues a cue for the next prompt assembly to claim. Covers the master
switch, the per-fire cooldown, the hit / no-hit arming paths, and the
K96 second_thought overlap skip.

The cue goes to ``cue_pool`` rather than to an in-memory slot, so these
run against a real :class:`CueStore` on a throwaway file.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.post_turn_mixin import PostTurnMixin


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(dropped_topic_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        dropped_topic_min_asks=2,
        dropped_topic_min_overlap=2,
        dropped_topic_require_question=True,
        dropped_topic_cooldown_turns=3,
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
        cooldown_remaining: int = 0,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._dropped_topic_cooldown_remaining = cooldown_remaining
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = None


_TWO_ASKS = "I went to the store and got milk, how was your day?"
_MISS_REPLY = "Nice, milk from the store is the good stuff."
_COVER_REPLY = (
    "Nice, milk from the store is the good stuff. My day was quiet."
)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _host(self, **kwargs: Any) -> _Host:
        return _Host(self.store, **kwargs)

    def _queued(self) -> list:
        return self.store.pending("dropped_topic")


class MasterSwitchTests(_Fixture):
    def test_disabled_does_not_arm(self) -> None:
        host = self._host(
            agent_settings=_make_agent_settings(dropped_topic_enabled=False),
        )
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        self.assertEqual(self._queued(), [])


class CooldownTests(_Fixture):
    def test_cooldown_blocks_and_decrements(self) -> None:
        host = self._host(cooldown_remaining=2)
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        self.assertEqual(self._queued(), [])
        self.assertEqual(host._dropped_topic_cooldown_remaining, 1)

    def test_cooldown_zero_runs_detector(self) -> None:
        host = self._host(cooldown_remaining=0)
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        self.assertEqual(len(self._queued()), 1)


class ArmingTests(_Fixture):
    def test_hit_queues_a_cue_and_resets_cooldown(self) -> None:
        host = self._host()
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        rows = self._queued()
        self.assertEqual(len(rows), 1)
        self.assertIn("day", (rows[0].payload.get("skipped_ask") or "").lower())
        self.assertEqual(host._dropped_topic_cooldown_remaining, 3)

    def test_the_subject_is_the_skipped_ask(self) -> None:
        host = self._host()
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        rows = self._queued()
        self.assertEqual(len(rows), 1)
        self.assertIn("day", rows[0].subject.lower())

    def test_covered_reply_does_not_arm(self) -> None:
        host = self._host()
        host._maybe_arm_dropped_topic(_TWO_ASKS, _COVER_REPLY)
        self.assertEqual(self._queued(), [])
        self.assertEqual(host._dropped_topic_cooldown_remaining, 0)

    def test_one_intent_does_not_arm(self) -> None:
        host = self._host()
        host._maybe_arm_dropped_topic(
            "it was long and tiring and I need tea",
            "yeah sit down",
        )
        self.assertEqual(self._queued(), [])


class SecondThoughtSkipTests(_Fixture):
    def test_overlapping_second_thought_skips_arming(self) -> None:
        self.store.add(
            "second_thought",
            "how was your day",
            "Heads-up: you keep thinking you talked past how their day went.",
        )
        host = self._host()
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        self.assertEqual(self._queued(), [])
        self.assertEqual(host._dropped_topic_cooldown_remaining, 0)

    def test_unrelated_second_thought_still_arms(self) -> None:
        self.store.add(
            "second_thought",
            "espresso machine",
            "Heads-up: you keep thinking about the espresso machine.",
        )
        host = self._host()
        host._maybe_arm_dropped_topic(_TWO_ASKS, _MISS_REPLY)
        self.assertEqual(len(self._queued()), 1)
