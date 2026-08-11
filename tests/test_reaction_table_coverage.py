"""Every canonical reaction must be present in the per-reaction tables.

The tables keyed by reaction name all look the value up with a
``.get(name, <neutral default>)``. That is deliberate — an unknown tag
from the LLM should degrade, not raise — but it also means a *canonical*
reaction that never got an entry fails completely silently. The affect
health pass found eight such names in ``_REACTION_IMPULSE``, including
the two most frequently emitted tags (``gentle`` and ``embarrassed``);
between them they covered 49% of turns, and every one of those turns
moved her mood by exactly zero.

These tests are the tripwire: add a name to ``REACTIONS`` and they fail
until the affect impulse and the TTS speed have been chosen for it.
"""
from __future__ import annotations

import unittest

from app.core.affect.affect_state import _REACTION_IMPULSE
from app.core.affect.mood_inertia import reaction_affect_target
from app.core.affect.reactions import REACTIONS
from app.tts.pocket_tts_service import _REACTION_SPEED


class ImpulseCoverageTests(unittest.TestCase):
    def test_every_canonical_reaction_has_an_impulse(self) -> None:
        missing = [r for r in REACTIONS if r not in _REACTION_IMPULSE]
        self.assertEqual(
            missing,
            [],
            "canonical reactions with no affect impulse (they would move "
            f"her mood by zero on every turn they fire): {missing}",
        )

    def test_the_impulse_table_invents_nothing(self) -> None:
        extra = [r for r in _REACTION_IMPULSE if r not in REACTIONS]
        self.assertEqual(extra, [], f"impulses for non-canonical names: {extra}")

    def test_impulses_stay_inside_the_calibrated_range(self) -> None:
        # mood_inertia normalises by these magnitudes; overshooting them
        # would just clamp, which silently flattens the distinction
        # between "strong" and "strongest".
        for name, (dv, da) in _REACTION_IMPULSE.items():
            with self.subTest(reaction=name):
                self.assertLessEqual(abs(dv), 0.18, f"{name} valence impulse")
                self.assertLessEqual(abs(da), 0.20, f"{name} arousal impulse")

    def test_only_the_near_zero_reactions_have_no_target(self) -> None:
        # reaction_affect_target() returning None means "this tag points
        # nowhere", which K45 reads as "no mismatch possible" and the
        # cluster-affect sampler reads as "skip this turn". Only the three
        # deliberately near-zero impulses belong in that bucket; anything
        # else landing here is a reaction whose feeling is being discarded.
        directionless = {
            r for r in REACTIONS if reaction_affect_target(r) is None
        }
        self.assertEqual(directionless, {"neutral", "thoughtful", "serious"})


class SpeedCoverageTests(unittest.TestCase):
    def test_every_canonical_reaction_has_a_speed(self) -> None:
        missing = [r for r in REACTIONS if r not in _REACTION_SPEED]
        self.assertEqual(
            missing,
            [],
            f"canonical reactions with no TTS speed multiplier: {missing}",
        )

    def test_speeds_stay_inside_the_safe_pitch_range(self) -> None:
        for name, speed in _REACTION_SPEED.items():
            with self.subTest(reaction=name):
                self.assertGreaterEqual(speed, 0.92, f"{name} too slow")
                self.assertLessEqual(speed, 1.08, f"{name} too fast")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
