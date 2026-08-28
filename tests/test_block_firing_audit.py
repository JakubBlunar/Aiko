"""H53: what a block's zero means, and the guard that keeps it honest.

The classification itself is pure arithmetic and cheap to pin. The test
that earns its keep is :class:`SuppressionDriftTests` -- it reads the
suppression region out of the assembler's own source and fails if the
published constant stops matching what the code blanks, because the whole
point of the constant is that H53 spent a day rediscovering a set that
only existed as a comment.
"""
from __future__ import annotations

import inspect
import re
import unittest

from app.core.session.block_firing_audit import (
    BENIGN,
    CADENCES,
    DISABLED,
    FIRES,
    SILENT,
    SUPPRESSED,
    UNOBSERVABLE,
    classify_all,
    classify_block,
    summarise,
)
from app.core.session.prompt_assembler import (
    _BLOCK_TIER_OF,
    GROUNDING_SUPPRESSED_REPLACE,
    GROUNDING_SUPPRESSED_SPLIT,
    PromptAssembler,
    grounding_suppressed,
)


def _classify(block: str = "b", **kw: object):
    base = dict(tier="T6_detectors", fired=0, turns=100, window_days=20.0)
    base.update(kw)
    return classify_block(block, **base)  # type: ignore[arg-type]


class VerdictOrderTests(unittest.TestCase):
    def test_firing_beats_every_structural_explanation(self) -> None:
        # If a block rendered, the config reading that says it cannot is
        # the thing that is wrong -- never the observation.
        v = _classify(fired=5, disabled={"b"}, suppressed={"b"})
        self.assertEqual(v.verdict, FIRES)
        self.assertAlmostEqual(v.rate or 0.0, 0.05)

    def test_disabled_beats_suppressed(self) -> None:
        v = _classify(disabled={"b"}, suppressed={"b"})
        self.assertEqual(v.verdict, DISABLED)

    def test_suppressed_beats_the_cadence_refusal(self) -> None:
        # A suppressed block's zero is fully explained; calling it
        # "unobservable" would invite someone to wait for a longer window
        # that can never change the answer.
        v = _classify(
            suppressed={"b"},
            window_days=1.0,
            cadences={"b": (30.0, "x")},
        )
        self.assertEqual(v.verdict, SUPPRESSED)


class CadenceRefusalTests(unittest.TestCase):
    def test_a_window_shorter_than_the_cadence_quotes_no_rate(self) -> None:
        v = _classify(window_days=18.0, cadences={"b": (30.0, "some.knob")})
        self.assertEqual(v.verdict, UNOBSERVABLE)
        # The refusal is the feature: a 0.0 here is what H53 misread.
        self.assertIsNone(v.rate)
        self.assertIn("30d", v.reason)
        self.assertIn("18.0d", v.reason)
        self.assertIn("some.knob", v.reason)

    def test_a_window_past_the_cadence_is_a_real_zero(self) -> None:
        v = _classify(window_days=45.0, cadences={"b": (30.0, "k")})
        self.assertEqual(v.verdict, SILENT)
        self.assertEqual(v.rate, 0.0)

    def test_an_uncadenced_block_is_never_excused(self) -> None:
        v = _classify(window_days=0.5, cadences={})
        self.assertEqual(v.verdict, SILENT)

    def test_only_findings_are_findings(self) -> None:
        self.assertTrue(_classify().is_finding)
        self.assertFalse(_classify(fired=1).is_finding)
        self.assertFalse(_classify(disabled={"b"}).is_finding)
        self.assertFalse(
            _classify(window_days=1.0, cadences={"b": (9.0, "k")}).is_finding
        )


class CadenceTableTests(unittest.TestCase):
    def test_every_cadenced_name_is_a_real_block(self) -> None:
        # A typo here silences a block forever by excusing a zero that
        # belongs to nothing.
        unknown = sorted(set(CADENCES) - set(_BLOCK_TIER_OF))
        self.assertEqual(unknown, [])

    def test_cadences_are_positive_and_sourced(self) -> None:
        for name, (days, source) in CADENCES.items():
            self.assertGreater(days, 0.0, name)
            self.assertTrue(source.strip(), name)


class BatchTests(unittest.TestCase):
    def test_it_classifies_the_whole_ladder(self) -> None:
        out = classify_all(
            tier_of=_BLOCK_TIER_OF,
            fired={"persona": 10},
            turns=10,
            window_days=20.0,
            suppressed=GROUNDING_SUPPRESSED_REPLACE,
        )
        self.assertEqual(len(out), len(_BLOCK_TIER_OF))
        by_name = {v.block: v for v in out}
        self.assertEqual(by_name["persona"].verdict, FIRES)
        self.assertEqual(by_name["affect_block"].verdict, SUPPRESSED)

    def test_summarise_counts_each_verdict(self) -> None:
        counts = summarise(
            [_classify(fired=1), _classify(fired=1), _classify()]
        )
        self.assertEqual(counts, {FIRES: 2, SILENT: 1})

    def test_benign_is_everything_except_silent(self) -> None:
        self.assertNotIn(SILENT, BENIGN)
        self.assertIn(UNOBSERVABLE, BENIGN)


class GroundingModeTests(unittest.TestCase):
    def test_off_suppresses_nothing(self) -> None:
        self.assertEqual(grounding_suppressed("off"), frozenset())
        self.assertEqual(grounding_suppressed("anything-else"), frozenset())

    def test_replace_is_a_superset_of_split(self) -> None:
        self.assertTrue(
            GROUNDING_SUPPRESSED_SPLIT < GROUNDING_SUPPRESSED_REPLACE
        )
        self.assertEqual(grounding_suppressed("split"), GROUNDING_SUPPRESSED_SPLIT)
        self.assertEqual(
            grounding_suppressed("replace"), GROUNDING_SUPPRESSED_REPLACE
        )

    def test_every_suppressed_name_is_a_registered_block(self) -> None:
        unknown = sorted(GROUNDING_SUPPRESSED_REPLACE - set(_BLOCK_TIER_OF))
        self.assertEqual(unknown, [])


class SuppressionDriftTests(unittest.TestCase):
    """The constant must keep matching the assignments it describes.

    Parsing the source beats asserting against a rendered prompt: the
    suppression only bites when a grounding line is present *and* the mode
    is set, so a test that wired neither would pass while measuring
    nothing. This reads the region directly and cannot be fooled that way.
    """

    def _suppressed_in_source(self) -> tuple[set[str], set[str]]:
        src = inspect.getsource(PromptAssembler.assemble_with_budget)
        head = 'grounding_mode in ("split", "replace")'
        self.assertIn(head, src, "the K16 suppression region moved or was renamed")
        region = src.split(head, 1)[1].split("# Alexia bundle", 1)[0]
        split_part, _, replace_part = region.partition(
            'if grounding_mode == "replace":'
        )
        self.assertTrue(
            replace_part, "the replace-only arm of the suppression is gone"
        )
        assigned = re.compile(r'^\s+(\w+) = ""$', re.MULTILINE)
        return (
            set(assigned.findall(split_part)),
            set(assigned.findall(replace_part)),
        )

    def test_split_matches_the_published_constant(self) -> None:
        split_names, _ = self._suppressed_in_source()
        self.assertEqual(split_names, set(GROUNDING_SUPPRESSED_SPLIT))

    def test_replace_matches_the_published_constant(self) -> None:
        split_names, replace_only = self._suppressed_in_source()
        self.assertEqual(
            split_names | replace_only, set(GROUNDING_SUPPRESSED_REPLACE)
        )


if __name__ == "__main__":
    unittest.main()
