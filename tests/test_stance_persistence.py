"""Tests for the K46 stance-persistence pure module + provider.

Two layers:

* :class:`EvaluateTests` / :class:`RenderTests` cover the pure
  :mod:`app.core.conversation.stance_persistence` gate + cue copy.
* :class:`ProviderTests` exercises
  :meth:`InnerLifePart3Mixin._render_stance_persistence_block` through a
  minimal mixin host -- the recent-stance window gate, the mild-band
  filter (a strong correction must NOT fire), the force-next bypass, and
  the master switch.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from app.core.conversation import stance_persistence
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


# ── Pure module ──────────────────────────────────────────────────────


class EvaluateTests(unittest.TestCase):
    def test_holds_on_recent_stance_plus_mild(self) -> None:
        v = stance_persistence.evaluate(
            recent_stance=True, pushback_band="pushback_mild",
        )
        self.assertTrue(v.hold)
        self.assertEqual(v.reason, "mild_taste_pushback")

    def test_silent_without_recent_stance(self) -> None:
        v = stance_persistence.evaluate(
            recent_stance=False, pushback_band="pushback_mild",
        )
        self.assertFalse(v.hold)
        self.assertEqual(v.reason, "no_recent_stance")

    def test_silent_on_strong_correction(self) -> None:
        # A strong correction is a factual signal even mid-taste-talk;
        # K46 leaves it to K20.
        v = stance_persistence.evaluate(
            recent_stance=True, pushback_band="pushback_strong",
        )
        self.assertFalse(v.hold)
        self.assertEqual(v.reason, "band:pushback_strong")

    def test_silent_on_affirmation_and_none(self) -> None:
        self.assertFalse(
            stance_persistence.evaluate(
                recent_stance=True, pushback_band="affirmation",
            ).hold
        )
        self.assertFalse(
            stance_persistence.evaluate(
                recent_stance=True, pushback_band=None,
            ).hold
        )


class RenderTests(unittest.TestCase):
    def test_renders_with_stance_anchor(self) -> None:
        block = stance_persistence.render_block(
            "I prefer cozy stories over horror",
            user_display_name="Jacob",
        )
        self.assertIn("Jacob", block)
        self.assertIn("preference", block)
        self.assertIn("cozy stories over horror", block)

    def test_renders_without_stance_text(self) -> None:
        block = stance_persistence.render_block("", user_display_name="Jacob")
        self.assertNotEqual(block, "")
        self.assertNotIn("you noted", block)

    def test_long_stance_is_trimmed(self) -> None:
        long_text = "I really don't like " + ("x" * 400)
        block = stance_persistence.render_block(long_text, user_display_name="J")
        self.assertIn("\u2026", block)
        # The snippet portion is capped at STANCE_SNIPPET_MAXLEN chars.
        self.assertLessEqual(
            len(long_text[: stance_persistence.STANCE_SNIPPET_MAXLEN]),
            stance_persistence.STANCE_SNIPPET_MAXLEN,
        )


# ── Provider ─────────────────────────────────────────────────────────


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(stance_persistence_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        window: int = 0,
        stance_text: str = "I prefer cozy stories over horror",
        force_next: bool = False,
        agent: SimpleNamespace | None = None,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent or _agent())
        self._memory_settings = SimpleNamespace(stance_persistence_window=3)
        self._stance_recent_window = window
        self._stance_recent_text = stance_text
        self._stance_persistence_force_next = force_next
        self._last_stance_persistence: Any = None
        self.user_display_name = "Jacob"


MILD_MSG = "really? you don't like that?"
STRONG_MSG = "wait, no, that's not right"
NEUTRAL_MSG = "I think I'll cook pasta for dinner tonight"


class ProviderTests(unittest.TestCase):
    def test_fires_on_recent_stance_plus_mild(self) -> None:
        host = _Host(window=2)
        block = host._render_stance_persistence_block(MILD_MSG)
        self.assertNotEqual(block, "")
        self.assertIn("Jacob", block)
        self.assertIsNotNone(host._last_stance_persistence)
        self.assertEqual(host._last_stance_persistence["band"], "pushback_mild")

    def test_silent_without_window(self) -> None:
        host = _Host(window=0)
        self.assertEqual(host._render_stance_persistence_block(MILD_MSG), "")

    def test_silent_on_strong_correction(self) -> None:
        host = _Host(window=2)
        self.assertEqual(host._render_stance_persistence_block(STRONG_MSG), "")

    def test_silent_on_neutral_message(self) -> None:
        host = _Host(window=2)
        self.assertEqual(host._render_stance_persistence_block(NEUTRAL_MSG), "")

    def test_master_switch_off(self) -> None:
        host = _Host(window=2, agent=_agent(stance_persistence_enabled=False))
        self.assertEqual(host._render_stance_persistence_block(MILD_MSG), "")

    def test_force_next_bypasses_window_but_needs_mild(self) -> None:
        host = _Host(window=0, force_next=True)
        # Forced + mild -> fires even with an empty window.
        block = host._render_stance_persistence_block(MILD_MSG)
        self.assertNotEqual(block, "")
        # One-shot consumed.
        self.assertFalse(host._stance_persistence_force_next)

    def test_force_next_consumed_even_on_miss(self) -> None:
        host = _Host(window=0, force_next=True)
        # Forced but the band is neutral -> no fire, flag still consumed.
        self.assertEqual(host._render_stance_persistence_block(NEUTRAL_MSG), "")
        self.assertFalse(host._stance_persistence_force_next)


if __name__ == "__main__":
    unittest.main()
