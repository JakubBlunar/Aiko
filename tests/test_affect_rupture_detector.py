"""Tests for the K8 affect rupture-and-repair detector."""
from __future__ import annotations

import unittest

from app.core import affect_rupture_detector
from app.core.affect_rupture_detector import (
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
