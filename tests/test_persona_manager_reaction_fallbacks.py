"""Tests for Phase 3b reaction-vocabulary alignment + semantic fallbacks."""
from __future__ import annotations

import unittest

from app.core.persona_manager import (
    REACTIONS,
    _REACTION_NEIGHBOURS,
    _REACTION_SYNONYMS,
    resolve_reaction,
)


class ReactionsCoverageTests(unittest.TestCase):
    def test_all_17_affect_reactions_in_canonical_set(self) -> None:
        affect_reactions = {
            "neutral", "cheerful", "excited", "enthusiastic", "amused",
            "warm", "tender", "friendly", "calm", "thoughtful", "serious",
            "concerned", "sad", "melancholy", "angry", "frustrated",
            "surprised",
        }
        missing = affect_reactions - set(REACTIONS)
        self.assertEqual(missing, set(), f"reactions missing from REACTIONS: {missing}")

    def test_every_reaction_has_synonyms(self) -> None:
        for r in REACTIONS:
            self.assertIn(r, _REACTION_SYNONYMS, f"no synonym entry for {r!r}")
            self.assertGreater(len(_REACTION_SYNONYMS[r]), 0)

    def test_every_neighbour_is_a_known_reaction(self) -> None:
        # Sanity: don't put fallbacks for reactions that don't exist in
        # the canonical set (silently ignored, but it would be a typo bug).
        canonical = set(REACTIONS)
        for src, neighbours in _REACTION_NEIGHBOURS.items():
            self.assertIn(src, canonical, f"unknown source reaction {src!r}")
            for n in neighbours:
                self.assertIn(
                    n, canonical,
                    f"reaction {src!r} falls back to unknown {n!r}",
                )


class ResolveReactionTests(unittest.TestCase):
    def test_direct_mapping_wins(self) -> None:
        mapping = {"amused": "smile", "neutral": "default"}
        self.assertEqual(resolve_reaction("amused", mapping), "smile")

    def test_falls_back_to_semantic_neighbour(self) -> None:
        # No direct entry for "wistful"; should pick the first mapped
        # neighbour. Order: sad → melancholy → thoughtful → calm → gentle.
        mapping = {"thoughtful": "ponder"}
        self.assertEqual(resolve_reaction("wistful", mapping), "ponder")

    def test_returns_none_when_no_neighbour_is_mapped(self) -> None:
        # "tired" → calm → melancholy → neutral → sad. None of those
        # are mapped, so we return None instead of a wrong fallback.
        self.assertIsNone(resolve_reaction("tired", {"angry": "rage"}))

    def test_neutral_in_amused_chain(self) -> None:
        # "amused" → cheerful → playful → friendly → warm → neutral.
        mapping = {"neutral": "default"}
        self.assertEqual(resolve_reaction("amused", mapping), "default")

    def test_empty_inputs_handled(self) -> None:
        self.assertIsNone(resolve_reaction("", {"neutral": "x"}))
        self.assertIsNone(resolve_reaction(None, {"neutral": "x"}))
        self.assertIsNone(resolve_reaction("amused", {}))

    def test_unknown_reaction_returns_none(self) -> None:
        # No fallback chain for completely unknown reactions.
        self.assertIsNone(resolve_reaction("ecstatic", {"cheerful": "smile"}))


if __name__ == "__main__":
    unittest.main()
