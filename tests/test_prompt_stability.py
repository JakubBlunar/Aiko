"""Tests for ``app.core.infra.prompt_stability``.

These helpers exist for one reason: a running count printed into an early
prompt tier changes that tier's bytes every turn, and GPT-5.6+ only reads
the cache from a byte-identical prefix. So the property under test is not
"is the number right" -- it is "does the string hold still", which is the
one thing no other test in the suite would notice breaking.
"""
from __future__ import annotations

import unittest

from app.core.infra.prompt_stability import coarse_count, coarse_elapsed_turns


class CoarseCountTests(unittest.TestCase):
    def test_small_counts_are_exact(self) -> None:
        """Under ten there are few turns and each one is meaningful."""
        for n in range(10):
            self.assertEqual(coarse_count(n), n)

    def test_it_rounds_down_never_up(self) -> None:
        """Rounding up would claim more history than there is."""
        for n in (10, 47, 199, 999, 1487, 12345):
            self.assertLessEqual(coarse_count(n), n)

    def test_the_step_widens_with_magnitude(self) -> None:
        self.assertEqual(coarse_count(47), 45)
        self.assertEqual(coarse_count(147), 140)
        self.assertEqual(coarse_count(947), 925)
        self.assertEqual(coarse_count(1487), 1400)
        self.assertEqual(coarse_count(12_487), 12_250)

    def test_a_long_run_of_turns_yields_one_value(self) -> None:
        """The whole point -- 100 consecutive turns, one prefix."""
        self.assertEqual(
            len({coarse_count(n) for n in range(1400, 1500)}), 1,
        )

    def test_non_positive_is_zero(self) -> None:
        self.assertEqual(coarse_count(0), 0)
        self.assertEqual(coarse_count(-5), 0)


class CoarseElapsedTurnsTests(unittest.TestCase):
    def test_the_bands_are_ordered_and_few(self) -> None:
        phrases = [coarse_elapsed_turns(n) for n in range(0, 60)]
        # Five distinct phrases at most, and each one contiguous, so the
        # block changes a handful of times per conversation rather than
        # once per turn.
        self.assertLessEqual(len(set(phrases)), 5)
        runs = [p for i, p in enumerate(phrases) if i == 0 or p != phrases[i - 1]]
        self.assertEqual(len(runs), len(set(runs)))

    def test_a_stretch_of_turns_reads_the_same(self) -> None:
        self.assertEqual(
            len({coarse_elapsed_turns(n) for n in range(25, 200)}), 1,
        )

    def test_it_never_returns_a_bare_number(self) -> None:
        """A digit here is the bug this replaced."""
        for n in range(0, 200):
            self.assertFalse(
                any(ch.isdigit() for ch in coarse_elapsed_turns(n)),
                f"{n}: {coarse_elapsed_turns(n)!r}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
