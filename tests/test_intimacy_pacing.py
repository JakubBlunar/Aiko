"""Unit tests for the J12 intimacy-pacing pure module.

Pure math + serde + cue rendering only — no controller, no I/O.
Mirrors the ``tests/test_affection_style.py`` shape.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.relationship.intimacy_pacing import (
    BAND_AFFECTIONATE,
    BAND_RESERVED,
    BAND_WARM,
    IntimacyPacingState,
    NEUTRAL,
    ceiling_band,
    clamp01,
    decay_pace,
    deserialize,
    disclosure_factor,
    effective_forwardness,
    neutral_state,
    render_pacing_block,
    score_user_message,
    score_user_reaction,
    serialize,
    stage_base_forwardness,
    update_pace,
)


_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


class ClampAndBandTests(unittest.TestCase):
    def test_clamp01(self) -> None:
        self.assertEqual(clamp01(-1.0), 0.0)
        self.assertEqual(clamp01(2.0), 1.0)
        self.assertAlmostEqual(clamp01(0.42), 0.42)

    def test_ceiling_band_thresholds(self) -> None:
        self.assertEqual(ceiling_band(0.0), BAND_RESERVED)
        self.assertEqual(ceiling_band(0.39), BAND_RESERVED)
        self.assertEqual(ceiling_band(0.4), BAND_WARM)
        self.assertEqual(ceiling_band(0.7), BAND_WARM)
        self.assertEqual(ceiling_band(0.74), BAND_WARM)
        self.assertEqual(ceiling_band(0.75), BAND_AFFECTIONATE)
        self.assertEqual(ceiling_band(1.0), BAND_AFFECTIONATE)


class DisclosureFactorTests(unittest.TestCase):
    def test_default_ceiling_is_neutral(self) -> None:
        # The whole point of 0.7 default: the K15 budget is untouched.
        self.assertAlmostEqual(disclosure_factor(0.7), 1.0)
        self.assertAlmostEqual(disclosure_factor(1.0), 1.0)

    def test_reserved_shrinks_budget_but_keeps_a_floor(self) -> None:
        self.assertAlmostEqual(disclosure_factor(0.5), 0.8)
        self.assertAlmostEqual(disclosure_factor(0.2), 0.5)
        self.assertAlmostEqual(disclosure_factor(0.0), 0.4)  # floor
        self.assertGreaterEqual(disclosure_factor(0.0), 0.4)


class StageForwardnessTests(unittest.TestCase):
    def test_monotonic_in_stage(self) -> None:
        vals = [stage_base_forwardness(r) for r in range(4)]
        self.assertEqual(vals, sorted(vals))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in vals))

    def test_rank_clamped(self) -> None:
        self.assertEqual(stage_base_forwardness(-5), stage_base_forwardness(0))
        self.assertEqual(stage_base_forwardness(99), stage_base_forwardness(3))


class EffectiveForwardnessTests(unittest.TestCase):
    def test_ceiling_hard_caps(self) -> None:
        # Intimate stage base 0.85, neutral pace, but ceiling 0.3 caps it.
        eff = effective_forwardness(3, NEUTRAL, 0.3, follow_strength=0.5)
        self.assertLessEqual(eff, 0.3 + 1e-9)

    def test_forward_user_lifts_within_ceiling(self) -> None:
        low = effective_forwardness(2, 0.5, 1.0, follow_strength=0.5)
        high = effective_forwardness(2, 1.0, 1.0, follow_strength=0.5)
        self.assertGreater(high, low)

    def test_reserved_user_lowers(self) -> None:
        neutral = effective_forwardness(2, 0.5, 1.0, follow_strength=0.5)
        cold = effective_forwardness(2, 0.0, 1.0, follow_strength=0.5)
        self.assertLess(cold, neutral)

    def test_zero_follow_strength_ignores_user(self) -> None:
        a = effective_forwardness(2, 0.0, 1.0, follow_strength=0.0)
        b = effective_forwardness(2, 1.0, 1.0, follow_strength=0.0)
        self.assertAlmostEqual(a, b)
        self.assertAlmostEqual(a, stage_base_forwardness(2))


class ScoreUserMessageTests(unittest.TestCase):
    def test_strong_forward(self) -> None:
        self.assertEqual(score_user_message("I love you Aiko"), 0.85)
        self.assertEqual(score_user_message("miss you so much"), 0.85)
        self.assertEqual(score_user_message("hey cutie"), 0.85)

    def test_heart_emoji_reads_forward(self) -> None:
        self.assertEqual(score_user_message("you're great \u2764"), 0.85)

    def test_mild_warmth(self) -> None:
        self.assertEqual(score_user_message("thank you so much"), 0.65)
        self.assertEqual(score_user_message("you're so sweet"), 0.65)

    def test_cooling_wins_over_warm(self) -> None:
        # A leftover pet name doesn't override a clear brake.
        self.assertEqual(
            score_user_message("babe this is too much, slow down"), 0.15,
        )
        self.assertEqual(score_user_message("back off"), 0.15)

    def test_neutral_returns_none(self) -> None:
        self.assertIsNone(score_user_message("what's the weather today"))
        self.assertIsNone(score_user_message(""))
        self.assertIsNone(score_user_message("can you read this file"))


class ScoreUserReactionTests(unittest.TestCase):
    def test_affectionate_reactions(self) -> None:
        self.assertEqual(score_user_reaction("heart"), 0.85)
        self.assertEqual(score_user_reaction("hug"), 0.85)
        self.assertIsNotNone(score_user_reaction("grateful"))

    def test_non_affectionate_returns_none(self) -> None:
        self.assertIsNone(score_user_reaction("surprise"))
        self.assertIsNone(score_user_reaction("eyeroll"))
        self.assertIsNone(score_user_reaction(""))


class UpdateAndDecayTests(unittest.TestCase):
    def test_update_moves_toward_score(self) -> None:
        s = neutral_state(_NOW)
        s2 = update_pace(s, 1.0, _NOW, learning_rate=0.5)
        self.assertAlmostEqual(s2.user_pace, 0.75)
        s3 = update_pace(s2, 1.0, _NOW, learning_rate=0.5)
        self.assertGreater(s3.user_pace, s2.user_pace)

    def test_update_clamps(self) -> None:
        s = IntimacyPacingState(user_pace=0.95, updated_at=_NOW.isoformat())
        s2 = update_pace(s, 1.0, _NOW, learning_rate=1.0)
        self.assertLessEqual(s2.user_pace, 1.0)

    def test_zero_learning_rate_noop(self) -> None:
        s = IntimacyPacingState(user_pace=0.8, updated_at=_NOW.isoformat())
        s2 = update_pace(s, 0.0, _NOW, learning_rate=0.0)
        self.assertAlmostEqual(s2.user_pace, 0.8)

    def test_decay_pulls_toward_neutral(self) -> None:
        s = IntimacyPacingState(user_pace=0.9, updated_at=_NOW.isoformat())
        later = _NOW + timedelta(days=14)  # one half-life
        s2 = decay_pace(s, later, half_life_days=14.0)
        self.assertAlmostEqual(s2.user_pace, NEUTRAL + (0.9 - NEUTRAL) * 0.5)

    def test_decay_noop_without_elapsed(self) -> None:
        s = IntimacyPacingState(user_pace=0.9, updated_at=_NOW.isoformat())
        s2 = decay_pace(s, _NOW, half_life_days=14.0)
        self.assertAlmostEqual(s2.user_pace, 0.9)

    def test_decay_zero_half_life_noop(self) -> None:
        s = IntimacyPacingState(user_pace=0.9, updated_at=_NOW.isoformat())
        later = _NOW + timedelta(days=14)
        s2 = decay_pace(s, later, half_life_days=0.0)
        self.assertAlmostEqual(s2.user_pace, 0.9)


class SerdeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        s = IntimacyPacingState(user_pace=0.42, updated_at=_NOW.isoformat())
        back = deserialize(serialize(s))
        self.assertAlmostEqual(back.user_pace, 0.42)
        self.assertEqual(back.updated_at, _NOW.isoformat())

    def test_corrupt_returns_neutral(self) -> None:
        self.assertAlmostEqual(deserialize(None).user_pace, NEUTRAL)
        self.assertAlmostEqual(deserialize("not json").user_pace, NEUTRAL)
        self.assertAlmostEqual(deserialize("[1,2,3]").user_pace, NEUTRAL)

    def test_missing_pace_falls_back(self) -> None:
        self.assertAlmostEqual(deserialize("{}").user_pace, NEUTRAL)


class RenderPacingBlockTests(unittest.TestCase):
    def test_reserved_always_fires_consent_cue(self) -> None:
        block = render_pacing_block(
            ceiling=0.2, user_pace=NEUTRAL, stage_rank=0,
            follow_strength=0.5, pacing_enabled=True, user_display_name="Jacob",
        )
        self.assertIn("reserved", block.lower())
        self.assertIn("Jacob", block)

    def test_reserved_fires_even_with_pacing_disabled(self) -> None:
        # Consent dial is independent of the learned-half switch.
        block = render_pacing_block(
            ceiling=0.2, user_pace=NEUTRAL, stage_rank=0,
            follow_strength=0.5, pacing_enabled=False, user_display_name="Jacob",
        )
        self.assertTrue(block)

    def test_default_warm_neutral_is_silent_for_shallow_bond(self) -> None:
        # Behaviour-neutral at default: new/familiar stage, neutral pace.
        block = render_pacing_block(
            ceiling=0.7, user_pace=NEUTRAL, stage_rank=1,
            follow_strength=0.5, pacing_enabled=True, user_display_name="Jacob",
        )
        self.assertEqual(block, "")

    def test_warm_caps_intimate_bond(self) -> None:
        # Warm dial but intimate stage base (0.85) exceeds 0.7 -> cue.
        block = render_pacing_block(
            ceiling=0.7, user_pace=NEUTRAL, stage_rank=3,
            follow_strength=0.5, pacing_enabled=True, user_display_name="Jacob",
        )
        self.assertIn("warm", block.lower())

    def test_follow_cue_fires_for_cool_user(self) -> None:
        block = render_pacing_block(
            ceiling=1.0, user_pace=0.2, stage_rank=2,
            follow_strength=0.5, pacing_enabled=True, user_display_name="Jacob",
        )
        self.assertIn("pace", block.lower())

    def test_follow_cue_gated_by_master_switch(self) -> None:
        block = render_pacing_block(
            ceiling=1.0, user_pace=0.2, stage_rank=2,
            follow_strength=0.5, pacing_enabled=False, user_display_name="Jacob",
        )
        self.assertEqual(block, "")

    def test_forward_user_high_ceiling_silent(self) -> None:
        block = render_pacing_block(
            ceiling=1.0, user_pace=0.9, stage_rank=3,
            follow_strength=0.5, pacing_enabled=True, user_display_name="Jacob",
        )
        self.assertEqual(block, "")


if __name__ == "__main__":
    unittest.main()
