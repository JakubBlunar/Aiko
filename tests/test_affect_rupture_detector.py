"""Tests for the K8 affect rupture-and-repair detector."""
from __future__ import annotations

import unittest

from app.core.affect.affect_rupture_detector import (
    DEFAULT_EXCLUDED_REACTIONS,
    RuptureResult,
    detect,
    render_inner_life_block,
)


class DetectFiringCasesTests(unittest.TestCase):
    """Cases where the detector SHOULD fire: a real valence drop with
    a non-empathetic prior reaction.
    """

    def test_neutral_reaction_with_drop_fires(self) -> None:
        # 0.20 drop with a neutral reaction is a clean rupture beat.
        result = detect(
            prior_valence=0.30,
            current_valence=0.10,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result.valence_drop, 0.20, places=3)
        self.assertEqual(result.prior_reaction, "neutral")

    def test_excited_reaction_with_drop_fires(self) -> None:
        # An excited reaction landing wrong reads as "she got the
        # vibe wrong" -- exactly the rupture beat we want to catch.
        result = detect(
            prior_valence=0.40,
            current_valence=0.20,
            prior_reaction="excited",
            threshold=0.12,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.prior_reaction, "excited")

    def test_playful_reaction_with_drop_fires(self) -> None:
        # Playful banter that landed flat is a rupture cue.
        result = detect(
            prior_valence=0.10,
            current_valence=-0.05,
            prior_reaction="playful",
            threshold=0.12,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.prior_reaction, "playful")

    def test_drop_well_above_threshold(self) -> None:
        # A 0.50 drop with a neutral reaction is a textbook rupture.
        result = detect(
            prior_valence=0.40,
            current_valence=-0.10,
            prior_reaction="curious",
            threshold=0.12,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result.valence_drop, 0.50, places=3)

    def test_just_above_threshold(self) -> None:
        # Boundary: drop equals threshold exactly. The check is
        # ``drop < threshold`` so equality fires.
        result = detect(
            prior_valence=0.20,
            current_valence=0.08,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNotNone(result)


class DetectExcludedReactionsTests(unittest.TestCase):
    """Cases where Aiko's prior reaction was already empathetic --
    the drop is more likely the user's existing state than a
    rupture beat. These should NOT fire.
    """

    def test_concerned_reaction_does_not_fire(self) -> None:
        result = detect(
            prior_valence=0.10,
            current_valence=-0.10,
            prior_reaction="concerned",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_gentle_reaction_does_not_fire(self) -> None:
        result = detect(
            prior_valence=0.20,
            current_valence=-0.10,
            prior_reaction="gentle",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_sad_reaction_does_not_fire(self) -> None:
        result = detect(
            prior_valence=-0.10,
            current_valence=-0.30,
            prior_reaction="sad",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_calm_reaction_does_not_fire(self) -> None:
        result = detect(
            prior_valence=0.20,
            current_valence=0.00,
            prior_reaction="calm",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_thoughtful_reaction_does_not_fire(self) -> None:
        result = detect(
            prior_valence=0.10,
            current_valence=-0.10,
            prior_reaction="thoughtful",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_reaction_uppercased_still_excluded(self) -> None:
        # The detector lowercases / strips the reaction so a
        # raw "  CONCERNED " from the upstream parser still gates.
        result = detect(
            prior_valence=0.10,
            current_valence=-0.10,
            prior_reaction="  CONCERNED ",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_custom_excluded_reactions_override(self) -> None:
        # Caller can pass their own excluded set. Here, "playful"
        # joins the excluded list while "concerned" is no longer
        # excluded -- inverted from the default.
        result = detect(
            prior_valence=0.20,
            current_valence=0.00,
            prior_reaction="playful",
            threshold=0.12,
            excluded_reactions={"playful"},
        )
        self.assertIsNone(result)
        # Same call with the default set would fire (playful is not
        # default-excluded) -- sanity check.
        result_default = detect(
            prior_valence=0.20,
            current_valence=0.00,
            prior_reaction="playful",
            threshold=0.12,
        )
        self.assertIsNotNone(result_default)


class DetectNoFireCasesTests(unittest.TestCase):
    """Cases where the detector SHOULD NOT fire because the input
    doesn't constitute a rupture beat.
    """

    def test_no_drop(self) -> None:
        result = detect(
            prior_valence=0.10,
            current_valence=0.10,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_valence_rose(self) -> None:
        # Mood went up -- definitely not a rupture.
        result = detect(
            prior_valence=-0.10,
            current_valence=0.20,
            prior_reaction="warm",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_drop_below_threshold(self) -> None:
        # Small drop within the smoothing-noise band.
        result = detect(
            prior_valence=0.10,
            current_valence=0.02,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_prior_valence_none(self) -> None:
        # Defensive: missing snapshot returns None, never synthesises.
        result = detect(
            prior_valence=None,
            current_valence=-0.30,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_current_valence_none(self) -> None:
        result = detect(
            prior_valence=0.30,
            current_valence=None,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNone(result)

    def test_zero_threshold_disables(self) -> None:
        # A zero or negative threshold turns the detector off.
        result = detect(
            prior_valence=0.30,
            current_valence=-0.30,
            prior_reaction="neutral",
            threshold=0.0,
        )
        self.assertIsNone(result)

    def test_negative_threshold_disables(self) -> None:
        result = detect(
            prior_valence=0.30,
            current_valence=-0.30,
            prior_reaction="neutral",
            threshold=-0.05,
        )
        self.assertIsNone(result)


class DetectBaselineGateTests(unittest.TestCase):
    """A mood cooling back toward normal is not a rupture.

    Falling from unusually warm to merely fine crosses the 0.12 drop
    threshold easily, but nothing was wounded -- the turn has to leave
    them below their own resting valence to count.
    """

    def test_cooling_toward_baseline_does_not_fire(self) -> None:
        result = detect(
            prior_valence=0.30,
            current_valence=0.05,
            prior_reaction="neutral",
            threshold=0.12,
            baseline_valence=0.0,
        )
        self.assertIsNone(result)

    def test_same_drop_below_baseline_fires(self) -> None:
        result = detect(
            prior_valence=0.10,
            current_valence=-0.15,
            prior_reaction="neutral",
            threshold=0.12,
            baseline_valence=0.0,
        )
        self.assertIsNotNone(result)

    def test_landing_exactly_on_baseline_fires(self) -> None:
        # The gate is ``current > baseline`` -- at the resting point the
        # drop still counts, so a dip to flat-neutral isn't swallowed.
        result = detect(
            prior_valence=0.20,
            current_valence=0.0,
            prior_reaction="neutral",
            threshold=0.12,
            baseline_valence=0.0,
        )
        self.assertIsNotNone(result)

    def test_gate_respects_a_nonzero_baseline(self) -> None:
        # Someone whose resting valence is genuinely warm: cooling to
        # +0.10 is below *their* normal, so it fires.
        result = detect(
            prior_valence=0.45,
            current_valence=0.10,
            prior_reaction="neutral",
            threshold=0.12,
            baseline_valence=0.25,
        )
        self.assertIsNotNone(result)

    def test_omitting_baseline_skips_the_gate(self) -> None:
        result = detect(
            prior_valence=0.30,
            current_valence=0.05,
            prior_reaction="neutral",
            threshold=0.12,
        )
        self.assertIsNotNone(result)


class ElapsedTimeIsNotARuptureTests(unittest.TestCase):
    """Regression: the reunion greeting that invented a "tense patch".

    ``AffectUpdater.apply_turn`` decays valence toward baseline for the
    elapsed gap *before* applying the reaction impulse, while
    ``AffectStore.get`` is a raw row read. Subtracting the stored value
    from the post-turn value therefore measured how long the user had
    been away: a warm goodbye at +0.266, two hours and forty-three
    minutes of silence, then "Where is my love?" produced a 0.221 "drop"
    with a *positive* impulse, and J6 turned it into a durable
    "you and Jacob hit a tense patch" memory.
    """

    # The real numbers from the 2026-08-15 21:02 misfire.
    STORED_PRIOR = 0.266
    GAP_SECONDS = 2 * 3600 + 43 * 60
    OBSERVED_CURRENT = 0.045
    BASELINE = 0.0

    def test_the_raw_snapshot_would_have_fired(self) -> None:
        result = detect(
            prior_valence=self.STORED_PRIOR,
            current_valence=self.OBSERVED_CURRENT,
            prior_reaction="wistful",
            threshold=0.12,
        )
        self.assertIsNotNone(result, "this is the bug being fixed")

    def test_the_decayed_prior_stays_quiet(self) -> None:
        from app.core.affect.affect_state import decay_toward

        decayed = decay_toward(self.STORED_PRIOR, self.BASELINE, self.GAP_SECONDS)
        # Time alone ate almost the whole "drop".
        self.assertLess(decayed, 0.02)
        result = detect(
            prior_valence=decayed,
            current_valence=self.OBSERVED_CURRENT,
            prior_reaction="wistful",
            threshold=0.12,
            baseline_valence=self.BASELINE,
        )
        self.assertIsNone(result)

    def test_a_real_dip_in_the_same_turn_still_fires(self) -> None:
        from app.core.affect.affect_state import decay_toward

        # No gap: the reply itself moved valence, and it went negative.
        decayed = decay_toward(0.10, self.BASELINE, 30)
        result = detect(
            prior_valence=decayed,
            current_valence=-0.18,
            prior_reaction="playful",
            threshold=0.12,
            baseline_valence=self.BASELINE,
        )
        self.assertIsNotNone(result)


class AffectStateDecayedTests(unittest.TestCase):
    def test_decayed_reports_the_value_a_returning_user_walks_into(self) -> None:
        from datetime import timedelta

        from app.core.affect.affect_state import AffectState
        from app.core.infra import timephrase

        stale = (timephrase.utcnow() - timedelta(hours=3)).isoformat()
        state = AffectState(
            user_id="u",
            valence=0.60,
            arousal=0.80,
            baseline_valence=0.0,
            baseline_arousal=0.4,
            updated_at=stale,
        )
        valence, arousal = state.decayed()
        # Three hours is six half-lives; both axes are near baseline.
        self.assertLess(valence, 0.02)
        self.assertAlmostEqual(arousal, 0.4, delta=0.01)
        # The stored snapshot is untouched -- this is a read, not a write.
        self.assertAlmostEqual(state.valence, 0.60, places=6)

    def test_decayed_is_a_noop_on_a_fresh_snapshot(self) -> None:
        from app.core.affect.affect_state import AffectState

        state = AffectState(user_id="u", valence=0.42, arousal=0.71)
        valence, arousal = state.decayed()
        self.assertAlmostEqual(valence, 0.42, places=3)
        self.assertAlmostEqual(arousal, 0.71, places=3)

    def test_decayed_survives_a_junk_timestamp(self) -> None:
        from app.core.affect.affect_state import AffectState

        state = AffectState(user_id="u", valence=0.42, updated_at="not-a-date")
        valence, _ = state.decayed()
        self.assertAlmostEqual(valence, 0.42, places=6)


class DefaultExcludedReactionsContentTests(unittest.TestCase):
    """Sanity-check the default excluded set so that breaking it
    requires intent (and a test update). The persona contract
    depends on these specific values."""

    def test_default_set_contents(self) -> None:
        self.assertIn("concerned", DEFAULT_EXCLUDED_REACTIONS)
        self.assertIn("gentle", DEFAULT_EXCLUDED_REACTIONS)
        self.assertIn("sad", DEFAULT_EXCLUDED_REACTIONS)
        self.assertIn("calm", DEFAULT_EXCLUDED_REACTIONS)
        self.assertIn("thoughtful", DEFAULT_EXCLUDED_REACTIONS)
        self.assertIn("quiet", DEFAULT_EXCLUDED_REACTIONS)


class RenderInnerLifeBlockTests(unittest.TestCase):
    """The cue rendered into the prompt is a single soft voicing --
    "soften, check in once, don't camp on it"."""

    def test_basic_render(self) -> None:
        result = RuptureResult(
            valence_drop=0.20,
            prior_reaction="excited",
            prior_valence=0.30,
            current_valence=0.10,
        )
        block = render_inner_life_block(result, user_display_name="Jacob")
        self.assertIn("Heads-up", block)
        self.assertIn("Jacob", block)
        self.assertIn("dipped", block)
        # Reaction context surfaces so the LLM knows what tone Aiko had.
        self.assertIn("excited", block)
        # The repair instructions land.
        self.assertIn("Soften", block)
        # And the anti-camping rail.
        self.assertIn("camp", block.lower())

    def test_neutral_reaction_does_not_quote_reaction(self) -> None:
        # A "neutral" prior reaction is the default; no need to
        # surface "your last reaction was neutral" -- it's the
        # absence of a tone, not a tone.
        result = RuptureResult(
            valence_drop=0.15,
            prior_reaction="neutral",
            prior_valence=0.20,
            current_valence=0.05,
        )
        block = render_inner_life_block(result, user_display_name="Jacob")
        self.assertNotIn("\"neutral\"", block)
        self.assertNotIn("(your last reaction was \"neutral\")", block)


if __name__ == "__main__":
    unittest.main()
