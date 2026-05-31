"""Controller-level tests for the K23 misattunement provider.

Exercises ``InnerLifeProvidersMixin._render_misattunement_block`` by
building a minimal stub that simulates the controller surface it
reads from (``_settings`` / ``_chat_db`` / ``_novelty_detector`` /
``_misattunement_cooldown`` / ``_misattunement_force_next``). Avoids
spinning up the full :class:`SessionController` which would import
half the world.

The detector itself is covered exhaustively in
``tests/test_misattunement_detector.py``; this module focuses on
the provider plumbing -- cooldown decrement / arming, K6 dependency,
force-bypass, master-switch gate, and the chat_db read.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


@dataclass(frozen=True, slots=True)
class _FakeMessageRow:
    role: str
    content: str


class _FakeChatDb:
    def __init__(self, rows: list[_FakeMessageRow], message_count: int = 0) -> None:
        self._rows = rows
        self._message_count = message_count

    def get_messages(self, session_id: str, *, limit: int | None = None) -> list[_FakeMessageRow]:  # noqa: ARG002
        if limit is None:
            return list(self._rows)
        return list(self._rows[-limit:])

    def get_message_count(self, session_id: str) -> int:  # noqa: ARG002
        return self._message_count


@dataclass
class _FakeNoveltyDetector:
    last_band: str | None = None
    last_distance: float | None = None


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        misattunement_detection_enabled=True,
        misattunement_shrink_min_prev_words=30,
        misattunement_shrink_max_user_words=8,
        misattunement_pivot_max_user_words=8,
        misattunement_cooldown_turns=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    """Minimal mixin host with the attributes the provider reads."""

    def __init__(
        self,
        *,
        history: list[_FakeMessageRow],
        novelty: _FakeNoveltyDetector | None = None,
        cooldown: int = 0,
        force_next: bool = False,
        agent_settings: SimpleNamespace | None = None,
        message_count: int = 0,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._chat_db = _FakeChatDb(history, message_count=message_count)
        self.session_key = "stub-session"
        self._novelty_detector = novelty
        self._misattunement_cooldown = cooldown
        self._misattunement_force_next = force_next
        self._last_misattunement_trigger: str | None = None
        self._last_misattunement_fire_turn: int | None = None
        self.user_display_name = "Jacob"


def _long_aiko_reply() -> _FakeMessageRow:
    # 40 words, comfortably above the 30-word shrink threshold.
    return _FakeMessageRow(
        role="assistant",
        content=" ".join(["word"] * 40),
    )


class ShrinkPathTests(unittest.TestCase):
    def test_fires_on_short_reply_after_long_aiko(self) -> None:
        host = _Host(history=[_long_aiko_reply()], message_count=2)
        block = host._render_misattunement_block("ok")
        self.assertIn("Jacob", block)
        self.assertIn("pull back", block.lower())
        # Cooldown was armed to the configured 3.
        self.assertEqual(host._misattunement_cooldown, 3)
        self.assertEqual(host._last_misattunement_trigger, "shrink")
        self.assertEqual(host._last_misattunement_fire_turn, 2)

    def test_silent_on_substantive_reply(self) -> None:
        host = _Host(history=[_long_aiko_reply()])
        long_reply = " ".join(["thinking"] * 40)  # well above the 8-word ceiling
        self.assertEqual(host._render_misattunement_block(long_reply), "")
        # Cooldown stays at 0 (already there); no arming.
        self.assertEqual(host._misattunement_cooldown, 0)
        self.assertIsNone(host._last_misattunement_trigger)


class PivotPathTests(unittest.TestCase):
    def test_fires_on_short_pivot_after_short_aiko(self) -> None:
        # prev=2 words (below shrink_min_prev_words=30) means the
        # shrink path can't fire; the pivot path should pick it up.
        short_aiko = _FakeMessageRow(role="assistant", content="ok then")
        host = _Host(
            history=[short_aiko],
            novelty=_FakeNoveltyDetector(
                last_band="strong_novelty", last_distance=0.62,
            ),
        )
        block = host._render_misattunement_block("but bees")
        self.assertNotEqual(block, "")
        self.assertEqual(host._last_misattunement_trigger, "pivot")
        self.assertEqual(host._misattunement_cooldown, 3)

    def test_silent_on_mild_shift(self) -> None:
        short_aiko = _FakeMessageRow(role="assistant", content="ok then")
        host = _Host(
            history=[short_aiko],
            novelty=_FakeNoveltyDetector(
                last_band="mild_shift", last_distance=0.40,
            ),
        )
        self.assertEqual(host._render_misattunement_block("but bees"), "")


class CooldownPlumbingTests(unittest.TestCase):
    def test_cooldown_decrements_each_call(self) -> None:
        # Substantive reply on every call so the detector never
        # fires; cooldown should monotonically count down. Each user
        # reply MUST be > shrink_max_user_words (8) words so the
        # detector stays silent and the cooldown countdown is the
        # only thing we observe.
        substantive = (
            "this is a deliberately long reply with enough words to "
            "stay above the shrink threshold safely"
        )
        host = _Host(history=[_long_aiko_reply()], cooldown=3)
        host._render_misattunement_block(substantive)
        self.assertEqual(host._misattunement_cooldown, 2)
        host._render_misattunement_block(substantive)
        self.assertEqual(host._misattunement_cooldown, 1)
        host._render_misattunement_block(substantive)
        self.assertEqual(host._misattunement_cooldown, 0)
        # Already at floor; further calls stay at 0 (with the trigger
        # still silent because user_words >> shrink_max_user_words).
        host._render_misattunement_block(substantive)
        self.assertEqual(host._misattunement_cooldown, 0)

    def test_cooldown_blocks_fire(self) -> None:
        # Trigger conditions are met BUT cooldown_remaining > 0 -> no cue.
        host = _Host(history=[_long_aiko_reply()], cooldown=2)
        block = host._render_misattunement_block("ok")
        self.assertEqual(block, "")
        # Cooldown decremented by 1 (the per-call decrement); no
        # re-arming because the detector didn't fire.
        self.assertEqual(host._misattunement_cooldown, 1)

    def test_force_next_bypasses_cooldown(self) -> None:
        # force_next is the MCP debug bypass -- it must fire even
        # when the cooldown counter would otherwise block.
        host = _Host(
            history=[_long_aiko_reply()],
            cooldown=2,
            force_next=True,
        )
        block = host._render_misattunement_block("ok")
        self.assertNotEqual(block, "")
        self.assertEqual(host._last_misattunement_trigger, "shrink")
        # Force flag is consumed after the call (one-shot).
        self.assertFalse(host._misattunement_force_next)

    def test_force_next_consumed_even_when_trigger_misses(self) -> None:
        # The force flag is strictly one-turn: even if the next
        # message doesn't actually satisfy the trigger, the flag is
        # cleared so it doesn't stick.
        host = _Host(
            history=[_FakeMessageRow(role="assistant", content="short")],
            cooldown=2,
            force_next=True,
        )
        # No prior long Aiko, no K6 strong_novelty -> nothing fires.
        block = host._render_misattunement_block(
            "a perfectly substantive multi-word reply"
        )
        self.assertEqual(block, "")
        self.assertFalse(host._misattunement_force_next)


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty_without_touching_cooldown(self) -> None:
        # When the master switch is off, the provider must short-
        # circuit BEFORE the cooldown decrement so an off switch
        # doesn't quietly drain any pending cooldown.
        agent = _make_agent_settings(misattunement_detection_enabled=False)
        host = _Host(
            history=[_long_aiko_reply()],
            cooldown=2,
            agent_settings=agent,
        )
        self.assertEqual(host._render_misattunement_block("ok"), "")
        self.assertEqual(host._misattunement_cooldown, 2)


class ColdStartTests(unittest.TestCase):
    def test_empty_history_no_fire(self) -> None:
        # No prior assistant turn -> shrink path can't fire; K6
        # band is None so pivot can't fire either. Provider returns
        # empty without raising.
        host = _Host(history=[])
        self.assertEqual(host._render_misattunement_block("ok"), "")

    def test_empty_user_text_short_circuits(self) -> None:
        host = _Host(history=[_long_aiko_reply()])
        self.assertEqual(host._render_misattunement_block(""), "")
        # Cooldown still decrements when called -- but we started at 0.
        self.assertEqual(host._misattunement_cooldown, 0)


if __name__ == "__main__":
    unittest.main()
