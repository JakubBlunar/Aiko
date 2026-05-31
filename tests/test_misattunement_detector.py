"""Tests for the K23 misattunement detector.

Covers both trigger paths (shrink + pivot), the cooldown gate, the
``prev_aiko_words=None`` cold-start path, and the render output's
key invariants (no apology language, contains ``user_display_name``).
"""
from __future__ import annotations

import unittest

from app.core.affect import misattunement_detector
from app.core.affect.misattunement_detector import (
    DEFAULT_PIVOT_MAX_USER_WORDS,
    DEFAULT_SHRINK_MAX_USER_WORDS,
    DEFAULT_SHRINK_MIN_PREV_WORDS,
    MisattunementResult,
    detect,
    render_inner_life_block,
)


class ShrinkTriggerTests(unittest.TestCase):
    """``shrink`` fires when prev_aiko_words is substantial AND
    this_user_words is very short."""

    def test_fires_at_thresholds(self) -> None:
        # prev=30, this=8: both exactly at threshold; this is the
        # boundary-inclusive case (>= min, <= max). Should fire.
        result = detect(
            prev_aiko_words=DEFAULT_SHRINK_MIN_PREV_WORDS,
            this_user_words=DEFAULT_SHRINK_MAX_USER_WORDS,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNotNone(result)
        assert result is not None  # narrow for mypy
        self.assertEqual(result.trigger, "shrink")
        self.assertEqual(result.band, "mild_disengagement")
        self.assertEqual(result.prev_aiko_words, DEFAULT_SHRINK_MIN_PREV_WORDS)
        self.assertEqual(result.this_user_words, DEFAULT_SHRINK_MAX_USER_WORDS)

    def test_does_not_fire_when_prev_too_short(self) -> None:
        # prev=29 < min(30) -> shrink doesn't fire.
        result = detect(
            prev_aiko_words=DEFAULT_SHRINK_MIN_PREV_WORDS - 1,
            this_user_words=DEFAULT_SHRINK_MAX_USER_WORDS,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)

    def test_does_not_fire_when_user_too_long(self) -> None:
        # this=9 > max(8) -> shrink doesn't fire.
        result = detect(
            prev_aiko_words=DEFAULT_SHRINK_MIN_PREV_WORDS,
            this_user_words=DEFAULT_SHRINK_MAX_USER_WORDS + 1,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)

    def test_does_not_fire_when_user_zero(self) -> None:
        # Empty user input -> no signal.
        result = detect(
            prev_aiko_words=DEFAULT_SHRINK_MIN_PREV_WORDS,
            this_user_words=0,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)

    def test_does_not_fire_at_cold_start(self) -> None:
        # No prior assistant turn -> shrink can't fire (no length to
        # compare against). Pivot path is also disabled here because
        # novelty_band is None.
        result = detect(
            prev_aiko_words=None,
            this_user_words=2,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)


class PivotTriggerTests(unittest.TestCase):
    """``pivot`` fires when K6 flagged strong_novelty AND this_user_words
    is short."""

    def test_fires_on_strong_novelty(self) -> None:
        result = detect(
            prev_aiko_words=20,  # below shrink_min_prev_words; shrink can't fire
            this_user_words=5,
            novelty_band="strong_novelty",
            novelty_distance=0.62,
            cooldown_remaining=0,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "pivot")
        self.assertEqual(result.this_user_words, 5)
        self.assertEqual(result.novelty_distance, 0.62)

    def test_does_not_fire_on_mild_shift(self) -> None:
        # mild_shift band must not trigger; only strong_novelty does.
        result = detect(
            prev_aiko_words=20,
            this_user_words=5,
            novelty_band="mild_shift",
            novelty_distance=0.40,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)

    def test_does_not_fire_on_empty_band(self) -> None:
        result = detect(
            prev_aiko_words=20,
            this_user_words=5,
            novelty_band="",
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)

    def test_does_not_fire_when_pivot_too_long(self) -> None:
        # Long pivot is "engaging the new topic", not "drifting away".
        result = detect(
            prev_aiko_words=20,
            this_user_words=DEFAULT_PIVOT_MAX_USER_WORDS + 5,
            novelty_band="strong_novelty",
            novelty_distance=0.62,
            cooldown_remaining=0,
        )
        self.assertIsNone(result)

    def test_shrink_wins_when_both_apply(self) -> None:
        # Substantial prev + short user + strong_novelty -> shrink
        # comes first in the detect() check, so the trigger reads
        # as "shrink". The render text doesn't depend on trigger
        # label, so the user-visible cue is identical either way --
        # this assertion is for the MCP diagnostic only.
        result = detect(
            prev_aiko_words=40,
            this_user_words=3,
            novelty_band="strong_novelty",
            novelty_distance=0.65,
            cooldown_remaining=0,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "shrink")


class CooldownGateTests(unittest.TestCase):
    """``cooldown_remaining > 0`` short-circuits both triggers."""

    def test_cooldown_blocks_shrink(self) -> None:
        result = detect(
            prev_aiko_words=DEFAULT_SHRINK_MIN_PREV_WORDS,
            this_user_words=DEFAULT_SHRINK_MAX_USER_WORDS,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=2,
        )
        self.assertIsNone(result)

    def test_cooldown_blocks_pivot(self) -> None:
        result = detect(
            prev_aiko_words=20,
            this_user_words=5,
            novelty_band="strong_novelty",
            novelty_distance=0.62,
            cooldown_remaining=1,
        )
        self.assertIsNone(result)

    def test_cooldown_zero_allows_fire(self) -> None:
        result = detect(
            prev_aiko_words=DEFAULT_SHRINK_MIN_PREV_WORDS,
            this_user_words=DEFAULT_SHRINK_MAX_USER_WORDS,
            novelty_band=None,
            novelty_distance=None,
            cooldown_remaining=0,
        )
        self.assertIsNotNone(result)


class RenderTests(unittest.TestCase):
    """``render_inner_life_block`` output invariants."""

    @staticmethod
    def _make_result() -> MisattunementResult:
        return MisattunementResult(
            band="mild_disengagement",
            trigger="shrink",
            prev_aiko_words=42,
            this_user_words=2,
            novelty_distance=None,
        )

    def test_render_contains_user_display_name(self) -> None:
        text = render_inner_life_block(
            self._make_result(), user_display_name="Jacob",
        )
        self.assertIn("Jacob", text)

    def test_render_uses_default_name_when_omitted(self) -> None:
        text = render_inner_life_block(self._make_result())
        self.assertIn("the user", text)

    def test_render_contains_steering_verbs(self) -> None:
        # The cue must tell Aiko to pull back and lighten -- those
        # are the persona's hooks for the "what to do" guidance.
        text = render_inner_life_block(self._make_result()).lower()
        self.assertIn("pull back", text)
        self.assertIn("lighten", text)

    def test_render_forbids_apology_spiral_language(self) -> None:
        # The single biggest failure mode for short-reply cues is
        # over-correction ("are you ok?" / "I'm sorry"). The render
        # text must explicitly steer Aiko AWAY from those.
        text = render_inner_life_block(self._make_result())
        self.assertIn('"are you ok?"', text)
        self.assertIn("apologise", text)


class PublicSurfaceTests(unittest.TestCase):
    """Smoke tests on the module's public exports."""

    def test_defaults_are_positive_ints(self) -> None:
        self.assertGreater(DEFAULT_SHRINK_MIN_PREV_WORDS, 0)
        self.assertGreater(DEFAULT_SHRINK_MAX_USER_WORDS, 0)
        self.assertGreater(DEFAULT_PIVOT_MAX_USER_WORDS, 0)

    def test_pivot_band_default(self) -> None:
        self.assertEqual(
            misattunement_detector.DEFAULT_PIVOT_BAND, "strong_novelty",
        )


if __name__ == "__main__":
    unittest.main()
