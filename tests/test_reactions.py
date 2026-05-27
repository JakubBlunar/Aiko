"""Tests for ``app.core.reactions`` — vocabulary + semantic fallbacks.

This file used to live as ``test_persona_manager_reaction_fallbacks.py``;
it was renamed when the reaction tables moved out of ``persona_manager``
into their own module as part of the Alexia avatar bundling work.
"""
from __future__ import annotations

import unittest

from app.core.reactions import (
    REACTIONS,
    _REACTION_NEIGHBOURS,
    _REACTION_SYNONYMS,
    resolve_reaction,
    resolve_reaction_stack,
    split_reaction_stack,
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
        self.assertIsNone(resolve_reaction("tired", {"angry": "rage"}))

    def test_neutral_in_amused_chain(self) -> None:
        mapping = {"neutral": "default"}
        self.assertEqual(resolve_reaction("amused", mapping), "default")

    def test_empty_inputs_handled(self) -> None:
        self.assertIsNone(resolve_reaction("", {"neutral": "x"}))
        self.assertIsNone(resolve_reaction(None, {"neutral": "x"}))
        self.assertIsNone(resolve_reaction("amused", {}))

    def test_unknown_reaction_returns_none(self) -> None:
        self.assertIsNone(resolve_reaction("ecstatic", {"cheerful": "smile"}))


class ReactionStackTests(unittest.TestCase):
    """Phase 3 stacked-reaction / overlay grammar: ``[[reaction:A+B]]``
    and ``[[overlay:A+B+C]]``. ``split_reaction_stack`` is the parser;
    ``resolve_reaction_stack`` is the dispatcher-side resolver that
    maps each component into the loaded rig's expression names."""

    def test_split_single_returns_one_token(self) -> None:
        self.assertEqual(split_reaction_stack("cheerful"), ["cheerful"])

    def test_split_lowercases_and_trims(self) -> None:
        self.assertEqual(
            split_reaction_stack(" Cheerful + BLUSH "), ["cheerful", "blush"],
        )

    def test_split_dedupes_preserving_order(self) -> None:
        # Same component twice (e.g. ``cheerful+cheerful``) folds to
        # one entry so the dispatch doesn't pulse the same overlay
        # twice. Order of first occurrence is preserved.
        self.assertEqual(
            split_reaction_stack("blush+grin+blush"), ["blush", "grin"],
        )

    def test_split_empty_inputs(self) -> None:
        self.assertEqual(split_reaction_stack(""), [])
        self.assertEqual(split_reaction_stack(None), [])
        # Bare ``+`` characters with no tokens between them yield
        # nothing — defensive against pathological grammars the regex
        # might theoretically admit.
        self.assertEqual(split_reaction_stack("++"), [])

    def test_resolve_stack_returns_expression_name_list(self) -> None:
        mapping = {"cheerful": "lzx", "warm": "lh"}
        self.assertEqual(
            resolve_reaction_stack("cheerful+warm", mapping), ["lzx", "lh"],
        )

    def test_resolve_stack_falls_back_through_neighbours(self) -> None:
        # ``embarrassed`` has no direct entry in this minimal mapping;
        # the resolver walks the Phase 5 neighbour chain
        # ``warm → tender → cheerful → friendly → neutral`` and lands
        # on ``warm → lh``. The stack therefore resolves to
        # ``[lzx, lh]`` — cheerful direct + embarrassed via warm.
        mapping = {"cheerful": "lzx", "warm": "lh"}
        self.assertEqual(
            resolve_reaction_stack("cheerful+embarrassed", mapping),
            ["lzx", "lh"],
        )

    def test_resolve_stack_drops_unresolvable_component(self) -> None:
        # A made-up reaction with no canonical entry and no neighbour
        # chain falls through completely; the stack drops it instead
        # of poisoning the rest of the dispatch.
        mapping = {"cheerful": "lzx"}
        self.assertEqual(
            resolve_reaction_stack("cheerful+ecstatic", mapping), ["lzx"],
        )

    def test_resolve_stack_dedupes_resolved_expressions(self) -> None:
        # ``cheerful`` and ``amused`` both map to ``lzx`` on Alexia;
        # the stack collapses to one entry so the channel doesn't
        # double-write the same param contributions.
        mapping = {"cheerful": "lzx", "amused": "lzx"}
        self.assertEqual(
            resolve_reaction_stack("cheerful+amused", mapping), ["lzx"],
        )

    def test_resolve_stack_empty_returns_empty(self) -> None:
        self.assertEqual(resolve_reaction_stack("", {"cheerful": "lzx"}), [])
        self.assertEqual(resolve_reaction_stack(None, {"cheerful": "lzx"}), [])


class ConfusedReactionTests(unittest.TestCase):
    """``confused`` was minted as part of the Alexia visual audit so
    the ``y`` (dizzy / spiral-eye) overlay has a semantically correct
    home. It must be a first-class canonical reaction with the right
    fallback chain so rigs without a dizzy overlay degrade to a
    thinking / curious visual rather than collapsing to neutral."""

    def test_confused_is_in_canonical_reactions(self) -> None:
        self.assertIn("confused", REACTIONS)

    def test_confused_has_synonyms(self) -> None:
        self.assertIn("confused", _REACTION_SYNONYMS)
        self.assertGreater(len(_REACTION_SYNONYMS["confused"]), 0)

    def test_confused_falls_back_through_curious_then_thoughtful(self) -> None:
        # No direct entry. Neighbour chain: curious → thoughtful →
        # surprised → neutral. A model with only ``thoughtful``
        # mapped should resolve through it.
        mapping = {"thoughtful": "ponder"}
        self.assertEqual(resolve_reaction("confused", mapping), "ponder")

    def test_confused_prefers_curious_when_both_available(self) -> None:
        mapping = {"curious": "tilt", "thoughtful": "ponder"}
        self.assertEqual(resolve_reaction("confused", mapping), "tilt")


class Phase5NewReactionsTests(unittest.TestCase):
    """Phase 5 (expression overhaul): minted three new canonical
    reactions to back the visual shades the audit surfaced.

    Each must:
      - be in :data:`REACTIONS`,
      - carry a non-empty synonym tuple (auto-default-mapping fuel),
      - carry a neighbour-chain whose entries are all canonical,
      - and degrade through that chain on a minimal-rig mapping
        instead of collapsing to ``None`` / ``neutral``."""

    NEW_REACTIONS = ("embarrassed", "nervous", "defiant")

    def test_all_phase5_reactions_are_canonical(self) -> None:
        for r in self.NEW_REACTIONS:
            self.assertIn(r, REACTIONS, f"missing canonical reaction: {r}")

    def test_all_phase5_reactions_have_synonyms(self) -> None:
        for r in self.NEW_REACTIONS:
            self.assertIn(r, _REACTION_SYNONYMS)
            self.assertGreater(len(_REACTION_SYNONYMS[r]), 0)

    def test_all_phase5_reactions_have_neighbour_chains(self) -> None:
        canonical = set(REACTIONS)
        for r in self.NEW_REACTIONS:
            self.assertIn(r, _REACTION_NEIGHBOURS, f"no neighbour chain for {r}")
            chain = _REACTION_NEIGHBOURS[r]
            self.assertGreater(len(chain), 0)
            for n in chain:
                self.assertIn(n, canonical, f"{r} → unknown {n}")

    # ── embarrassed ───────────────────────────────────────────────────

    def test_embarrassed_falls_back_through_warm(self) -> None:
        # Chain: warm → tender → cheerful → friendly → neutral.
        # A rig that only ships a "warm" smile should land there
        # instead of degrading all the way to neutral.
        mapping = {"warm": "soft_smile"}
        self.assertEqual(resolve_reaction("embarrassed", mapping), "soft_smile")

    def test_embarrassed_prefers_warm_over_cheerful(self) -> None:
        mapping = {"warm": "soft_smile", "cheerful": "grin"}
        self.assertEqual(resolve_reaction("embarrassed", mapping), "soft_smile")

    def test_embarrassed_terminal_fallback_to_neutral(self) -> None:
        mapping = {"neutral": "base"}
        self.assertEqual(resolve_reaction("embarrassed", mapping), "base")

    # ── nervous ───────────────────────────────────────────────────────

    def test_nervous_falls_back_through_concerned(self) -> None:
        # Chain: concerned → serious → thoughtful → neutral. The
        # closest semantic neighbour ``concerned`` wins when present.
        mapping = {"concerned": "worry"}
        self.assertEqual(resolve_reaction("nervous", mapping), "worry")

    def test_nervous_prefers_concerned_over_thoughtful(self) -> None:
        mapping = {"concerned": "worry", "thoughtful": "ponder"}
        self.assertEqual(resolve_reaction("nervous", mapping), "worry")

    def test_nervous_falls_back_through_serious_when_concerned_missing(self) -> None:
        mapping = {"serious": "stern", "thoughtful": "ponder"}
        self.assertEqual(resolve_reaction("nervous", mapping), "stern")

    # ── defiant ───────────────────────────────────────────────────────

    def test_defiant_falls_back_through_frustrated(self) -> None:
        # Chain: frustrated → angry → serious → neutral. ``frustrated``
        # is the softer / closer match; ``angry`` only if frustrated
        # is missing on the rig.
        mapping = {"frustrated": "pout"}
        self.assertEqual(resolve_reaction("defiant", mapping), "pout")

    def test_defiant_falls_back_through_angry_when_frustrated_missing(self) -> None:
        mapping = {"angry": "rage"}
        self.assertEqual(resolve_reaction("defiant", mapping), "rage")

    def test_defiant_prefers_frustrated_over_angry(self) -> None:
        mapping = {"frustrated": "pout", "angry": "rage"}
        self.assertEqual(resolve_reaction("defiant", mapping), "pout")


class CryCascadeGuardTests(unittest.TestCase):
    """Regression guard for the "cheerful turn, Aiko visibly cried"
    bug. The discovered failure mode: certain non-sad reactions
    (notably ``thoughtful`` and ``serious``) chained through
    ``concerned`` in :data:`_REACTION_NEIGHBOURS`, and on rigs where
    ``concerned`` paints a tear-streak overlay (Alexia's ``k`` /
    Param59) a single ``[[reaction:thoughtful]]`` emit — or the
    filler-injector's default "thoughtful" tone on a fresh-boot
    turn — silently flipped the avatar into "visibly crying".

    The fix tightens the neighbour chains so non-sad reactions never
    chain through the sad family. The sad family itself
    (``sad`` / ``melancholy`` / ``wistful`` / ``concerned`` / ``tired``
    / ``cry``) still chains within itself — that's where tear-streak
    overlays belong.

    Tests below pin the guard against ``concerned`` (the specific
    Alexia ``k`` cascade) but the principle generalises: keep these
    chains inside the same emotional family.

    Mirror lives in
    :file:`web/src/live2d/channels/ExpressionChannel.ts`
    ``_REACTION_NEIGHBOURS`` — keep in lockstep.
    """

    NON_SAD = ("thoughtful", "serious", "frustrated", "angry")
    SAD_FAMILY = {"concerned", "sad", "melancholy", "wistful", "cry", "tired"}

    def test_non_sad_chains_never_route_through_sad_family(self) -> None:
        for source in self.NON_SAD:
            chain = _REACTION_NEIGHBOURS.get(source, ())
            overlap = set(chain) & self.SAD_FAMILY
            self.assertEqual(
                overlap, set(),
                f"{source!r} neighbour chain {chain!r} routes through sad "
                f"family {overlap!r} — on Alexia this paints tears on a "
                f"thinking / serious / angry beat. Drop those entries."
            )

    def test_thoughtful_resolves_to_nothing_on_alexia_minimal_subset(self) -> None:
        # On Alexia ``thoughtful`` is intentionally empty (body language
        # carries it) and ``concerned`` / ``sad`` map to ``k`` (cry).
        # With the fixed chain, an explicit [[reaction:thoughtful]] on
        # this rig MUST resolve to ``None`` (no expression change) —
        # NEVER to the cry expression.
        alexia_minimal = {"concerned": "k", "sad": "k", "neutral": ""}
        result = resolve_reaction("thoughtful", alexia_minimal)
        self.assertNotEqual(
            result, "k",
            "thoughtful must not cascade to the cry expression on Alexia",
        )

    def test_serious_resolves_to_nothing_on_alexia_minimal_subset(self) -> None:
        # Same shape as the thoughtful test but for the other crybug
        # entrypoint discovered alongside it.
        alexia_minimal = {"concerned": "k", "sad": "k", "neutral": ""}
        result = resolve_reaction("serious", alexia_minimal)
        self.assertNotEqual(
            result, "k",
            "serious must not cascade to the cry expression on Alexia",
        )

    def test_frustrated_resolves_to_anger_not_cry_on_alexia_like_rig(self) -> None:
        # Frustration is anger-leaning. On a rig where ``frustrated`` is
        # missing but ``angry`` is mapped, the chain must hit angry
        # first — not fall through to concerned/cry.
        rig = {"angry": "sq", "concerned": "k", "neutral": ""}
        self.assertEqual(resolve_reaction("frustrated", rig), "sq")

    def test_angry_resolves_to_frustrated_not_cry_on_alexia_like_rig(self) -> None:
        # Symmetric: angry → frustrated first (then serious, then
        # neutral). Never through concerned/cry.
        rig = {"frustrated": "sq", "concerned": "k", "neutral": ""}
        self.assertEqual(resolve_reaction("angry", rig), "sq")

    def test_sad_family_still_chains_within_itself(self) -> None:
        # The fix MUST NOT break the legitimate sad cascade: a model
        # that has only ``concerned`` mapped should still surface that
        # for an explicit [[reaction:sad]] emit (intentional empathy
        # beat from the LLM).
        rig = {"concerned": "k"}
        self.assertEqual(resolve_reaction("sad", rig), "k")
        # And cry → sad → concerned still works end-to-end.
        self.assertEqual(resolve_reaction("cry", rig), "k")


class AlexiaPhase5MappingTests(unittest.TestCase):
    """Direct Alexia mappings for the Phase 5 reactions. Locked in
    here (as opposed to ``test_avatar_profile.py``) because the
    mapping table lives in ``avatar_profile`` but the *semantics*
    are owned by ``reactions.py``."""

    def test_alexia_reaction_map_has_phase5_entries(self) -> None:
        from app.core.avatar_profile import _ALEXIA_REACTION_MAP

        # Each Phase 5 reaction must have its canonical direct
        # mapping on Alexia. Empty strings are allowed elsewhere in
        # the table to defer to the neighbour chain, but for these
        # three the visual audit identified the right expression.
        self.assertEqual(_ALEXIA_REACTION_MAP.get("embarrassed"), "lh")
        self.assertEqual(_ALEXIA_REACTION_MAP.get("nervous"), "yfmz")
        self.assertEqual(_ALEXIA_REACTION_MAP.get("defiant"), "mj")


if __name__ == "__main__":
    unittest.main()
