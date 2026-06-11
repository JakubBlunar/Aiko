"""Tests for the K45 mood-inertia pure module.

Covers target derivation from the reaction-impulse table, mismatch
monotonicity, whiplash detection, band thresholds, and the rendered
cue's K44 contract (felt-language, no digits).
"""
from __future__ import annotations

import unittest

from app.core.affect.affect_state import felt_phrase
from app.core.affect.mood_inertia import (
    DEFAULT_STRONG_THRESHOLD,
    InertiaResult,
    assess,
    detect_whiplash,
    reaction_affect_target,
    reaction_affect_targets,
    render_cue,
)


class ReactionAffectTargetTests(unittest.TestCase):
    def test_excited_implies_bright_high_energy(self) -> None:
        target = reaction_affect_target("excited")
        assert target is not None
        valence, arousal = target
        self.assertGreaterEqual(valence, 0.9)
        self.assertGreaterEqual(arousal, 0.9)

    def test_sad_implies_dark_low_energy(self) -> None:
        target = reaction_affect_target("sad")
        assert target is not None
        valence, arousal = target
        self.assertLessEqual(valence, -0.9)
        self.assertLess(arousal, 0.4)

    def test_directionless_reactions_return_none(self) -> None:
        self.assertIsNone(reaction_affect_target("neutral"))
        self.assertIsNone(reaction_affect_target("thoughtful"))

    def test_unknown_reaction_returns_none(self) -> None:
        self.assertIsNone(reaction_affect_target("zebra"))
        self.assertIsNone(reaction_affect_target(""))

    def test_case_and_whitespace_tolerant(self) -> None:
        self.assertEqual(
            reaction_affect_target(" Excited "),
            reaction_affect_target("excited"),
        )

    def test_targets_clamped_into_range(self) -> None:
        for valence, arousal in reaction_affect_targets().values():
            self.assertGreaterEqual(valence, -1.0)
            self.assertLessEqual(valence, 1.0)
            self.assertGreaterEqual(arousal, 0.0)
            self.assertLessEqual(arousal, 1.0)

    def test_manifest_map_excludes_directionless(self) -> None:
        targets = reaction_affect_targets()
        self.assertNotIn("neutral", targets)
        self.assertNotIn("thoughtful", targets)
        self.assertIn("excited", targets)
        self.assertIn("sad", targets)
        self.assertIn("angry", targets)


class DetectWhiplashTests(unittest.TestCase):
    def test_opposite_consecutive_signs_is_whiplash(self) -> None:
        self.assertTrue(detect_whiplash(["excited", "sad"]))
        self.assertTrue(detect_whiplash(["sad", "cheerful", "calm"]))

    def test_same_direction_is_not_whiplash(self) -> None:
        self.assertFalse(detect_whiplash(["cheerful", "excited", "warm"]))
        self.assertFalse(detect_whiplash(["sad", "melancholy"]))

    def test_neutral_breaks_the_chain(self) -> None:
        # A pause through a directionless tag is not a flip.
        self.assertFalse(detect_whiplash(["excited", "neutral", "sad"]))

    def test_short_or_empty_history_is_not_whiplash(self) -> None:
        self.assertFalse(detect_whiplash([]))
        self.assertFalse(detect_whiplash(["excited"]))


class AssessTests(unittest.TestCase):
    def test_aligned_reaction_scores_low(self) -> None:
        # Excited tag while already bright + buzzing: tiny mismatch.
        result = assess("excited", 0.8, 0.9)
        self.assertEqual(result.band, "none")
        self.assertLess(result.mismatch, 0.2)

    def test_opposed_reaction_scores_strong(self) -> None:
        # Excited tag while heavy-hearted and flat: big mismatch.
        result = assess("excited", -0.7, 0.2)
        self.assertEqual(result.band, "strong")
        self.assertGreaterEqual(result.mismatch, DEFAULT_STRONG_THRESHOLD)

    def test_mismatch_monotone_in_valence_gap(self) -> None:
        near = assess("excited", 0.5, 0.8).raw_mismatch
        mid = assess("excited", 0.0, 0.8).raw_mismatch
        far = assess("excited", -0.8, 0.8).raw_mismatch
        self.assertLess(near, mid)
        self.assertLess(mid, far)

    def test_directionless_reaction_never_fires(self) -> None:
        result = assess("neutral", -0.9, 0.95)
        self.assertEqual(result.band, "none")
        self.assertEqual(result.mismatch, 0.0)

    def test_whiplash_bumps_effective_mismatch(self) -> None:
        plain = assess("excited", -0.2, 0.4)
        whip = assess("excited", -0.2, 0.4, ["sad", "excited"])
        self.assertTrue(whip.whiplash)
        self.assertGreater(whip.mismatch, plain.mismatch)

    def test_threshold_floor_clamps(self) -> None:
        # A pathological threshold of 0 is floored to 0.1, so an
        # aligned reaction still doesn't fire.
        result = assess("excited", 0.95, 0.95, strong_threshold=0.0)
        self.assertEqual(result.band, "none")

    def test_mild_band_between_thresholds(self) -> None:
        # Find a point that lands between mild (0.297) and strong
        # (0.45) at default thresholds.
        result = assess("cheerful", -0.1, 0.4)
        self.assertEqual(result.band, "mild")

    def test_garbage_scalars_fall_back(self) -> None:
        result = assess("excited", float("nan"), float("nan"))
        self.assertIsInstance(result, InertiaResult)
        self.assertGreaterEqual(result.mismatch, 0.0)


class RenderCueTests(unittest.TestCase):
    def _strong(self, whiplash: bool = False) -> InertiaResult:
        return InertiaResult(
            mismatch=0.8, raw_mismatch=0.8, whiplash=whiplash, band="strong",
        )

    def test_non_strong_band_renders_empty(self) -> None:
        for band in ("none", "mild"):
            result = InertiaResult(
                mismatch=0.3, raw_mismatch=0.3, whiplash=False, band=band,
            )
            self.assertEqual(render_cue(result, "excited", -0.5, 0.2), "")

    def test_cue_contains_reaction_and_felt_phrase(self) -> None:
        cue = render_cue(self._strong(), "excited", -0.7, 0.2)
        self.assertIn("excited", cue)
        self.assertIn(felt_phrase(-0.7, 0.2), cue)
        self.assertIn("let the words catch up", cue)

    def test_brightening_direction_copy(self) -> None:
        cue = render_cue(self._strong(), "excited", -0.7, 0.2)
        self.assertIn("don't snap fully bright", cue)

    def test_darkening_direction_copy(self) -> None:
        cue = render_cue(self._strong(), "sad", 0.8, 0.6)
        self.assertIn("don't plunge", cue)

    def test_whiplash_adds_settling_line(self) -> None:
        plain = render_cue(self._strong(False), "excited", -0.5, 0.3)
        whip = render_cue(self._strong(True), "excited", -0.5, 0.3)
        self.assertNotIn("swinging", plain)
        self.assertIn("swinging", whip)

    def test_cue_contains_no_digits(self) -> None:
        # K44 contract: felt-language only, never numeric coordinates.
        for reaction, valence, arousal in (
            ("excited", -0.7, 0.2),
            ("sad", 0.8, 0.6),
            ("angry", 0.5, 0.1),
            ("cheerful", -0.9, 0.95),
        ):
            cue = render_cue(self._strong(True), reaction, valence, arousal)
            self.assertFalse(
                any(ch.isdigit() for ch in cue),
                msg=f"digits leaked for {reaction}: {cue!r}",
            )

    def test_underscored_reaction_reads_as_words(self) -> None:
        cue = render_cue(self._strong(), "excited", -0.5, 0.2)
        self.assertNotIn("_", cue)


if __name__ == "__main__":
    unittest.main()
