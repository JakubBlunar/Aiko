"""Table-driven tests for :mod:`app.core.memory.conflict_heuristics`."""
from __future__ import annotations

import unittest

from app.core.memory.conflict_heuristics import (
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    HEURISTIC_NO,
    classify_pair,
)


class ClassifyPairTests(unittest.TestCase):
    """Each row is ``(a, b, expected_label, expected_signals_subset)``.

    ``expected_signals_subset`` is checked with ``issubset`` so a row
    that matches multiple signals (e.g. negation_flip + antonym) just
    needs to assert at least one of them.
    """

    CASES = (
        # ── definite by negation flip ──
        (
            "I love pizza.",
            "I do not love pizza.",
            HEURISTIC_DEFINITE,
            {"negation_flip"},
        ),
        (
            "I love pizza.",
            "I dont love pizza.",  # missing apostrophe
            HEURISTIC_DEFINITE,
            {"negation_flip"},
        ),
        (
            "Aiko has a sister named Mei.",
            "Aiko does not have a sister.",
            HEURISTIC_DEFINITE,
            {"negation_flip"},
        ),
        # ── definite by antonym table ──
        (
            "Bea loves spicy food.",
            "Bea hates spicy food.",
            HEURISTIC_DEFINITE,
            {"antonym:loves/hates"},
        ),
        (
            "She is married to Tom.",
            "She is single.",
            HEURISTIC_DEFINITE,
            {"antonym:married/single"},
        ),
        (
            "User is vegetarian.",
            "User is a carnivore.",
            HEURISTIC_DEFINITE,
            {"antonym:vegetarian/carnivore"},
        ),
        # ── borderline by numerical mismatch ──
        (
            "Bob is 35 years old.",
            "Bob is 60 years old.",
            HEURISTIC_BORDERLINE,
            {"number_mismatch:35.0!=60.0"},
        ),
        # ── no contradiction: same idea, paraphrased ──
        (
            "User likes spicy food.",
            "User likes ice cream.",
            HEURISTIC_NO,
            set(),
        ),
        # ── no contradiction: same number ──
        (
            "Bob is 35 years old.",
            "Bob turned 35 last spring.",
            HEURISTIC_NO,
            set(),
        ),
        # ── no contradiction: numbers differ but anchors don't overlap ──
        # "Born in 1990" vs "About 30 years old" — both can be true.
        (
            "Born in 1990.",
            "About 30 years old.",
            HEURISTIC_NO,
            set(),
        ),
        # ── no contradiction: lives in different cities (out of antonym
        # table; needs LLM tier — heuristic correctly says "no") ──
        (
            "User lives in Berlin.",
            "User lives in Munich.",
            HEURISTIC_NO,
            set(),
        ),
    )

    def test_table(self) -> None:
        for a, b, expected_label, expected_signals in self.CASES:
            with self.subTest(a=a, b=b):
                result = classify_pair(a, b)
                self.assertEqual(
                    result.label,
                    expected_label,
                    f"label mismatch for ({a!r}, {b!r}): "
                    f"got {result.label!r}, signals={result.signals!r}",
                )
                self.assertTrue(
                    expected_signals.issubset(set(result.signals)),
                    f"missing signal for ({a!r}, {b!r}): "
                    f"expected subset {expected_signals!r}, "
                    f"got {result.signals!r}",
                )


class NegationFlipEdgeCases(unittest.TestCase):
    def test_both_negate_is_not_a_flip(self) -> None:
        result = classify_pair("I do not eat meat.", "I never eat meat.")
        self.assertEqual(result.label, HEURISTIC_NO)

    def test_negation_without_overlap_is_not_a_flip(self) -> None:
        # Different topics: one negates pizza, the other affirms anchovies.
        result = classify_pair(
            "I do not like pizza.",
            "I love anchovies on toast.",
        )
        self.assertEqual(result.label, HEURISTIC_NO)


class AntonymEdgeCases(unittest.TestCase):
    def test_same_side_with_both_words_does_not_count(self) -> None:
        # "loves and hates" is ambivalence on one side, not a contradiction
        # across rows.
        result = classify_pair(
            "Bea loves and hates spicy food.",
            "Bea is fine with spicy food.",
        )
        # The first row contains both ``loves`` and ``hates`` so the
        # antonym hit short-circuits. We don't care which exact label
        # falls out; we only assert no antonym signal is emitted.
        self.assertNotIn("antonym:loves/hates", result.signals)


class NumericalEdgeCases(unittest.TestCase):
    def test_within_10_percent_no_mismatch(self) -> None:
        result = classify_pair(
            "Bob is 35 years old.",
            "Bob is 36 years old.",
        )
        self.assertEqual(result.label, HEURISTIC_NO)

    def test_zero_zero_no_mismatch(self) -> None:
        result = classify_pair(
            "Aiko has 0 siblings.",
            "Aiko has 0 siblings to speak of.",
        )
        self.assertEqual(result.label, HEURISTIC_NO)


if __name__ == "__main__":
    unittest.main()
