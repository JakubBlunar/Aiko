"""Tests for :mod:`app.core.world.beat_episode` (K91 pass 3).

The planner's contract is narrow but load-bearing: chains only contain
beats the room actually affords, never repeat one, stop at terminal beats,
and join into a sentence that keeps the order of what happened.
"""
from __future__ import annotations

import random
import unittest

from app.core.world import beat_episode


ALL_KEYS = [
    "tea", "snack", "read_book", "look_outside", "tidy_desk",
    "doodle", "move_cat", "wander", "nap", "outing",
]


class ChainTests(unittest.TestCase):
    def test_chain_only_uses_available_beats(self) -> None:
        rng = random.Random(0)
        chain = beat_episode.plan_chain(
            "tea", ["tea", "read_book"], rng=rng, length=3,
        )
        self.assertEqual(chain[0], "tea")
        for key in chain:
            self.assertIn(key, ("tea", "read_book"))

    def test_chain_never_repeats_a_beat(self) -> None:
        for seed in range(30):
            chain = beat_episode.plan_chain(
                "tea", ALL_KEYS, rng=random.Random(seed), length=3,
            )
            self.assertEqual(len(chain), len(set(chain)))

    def test_chain_respects_the_successor_table(self) -> None:
        for seed in range(30):
            chain = beat_episode.plan_chain(
                "tea", ALL_KEYS, rng=random.Random(seed), length=3,
            )
            for before, after in zip(chain, chain[1:]):
                self.assertIn(after, beat_episode.SUCCESSORS[before])

    def test_terminal_beat_ends_the_chain(self) -> None:
        chain = beat_episode.plan_chain(
            "nap", ALL_KEYS, rng=random.Random(0), length=3,
        )
        self.assertEqual(chain, ["nap"])

    def test_llm_beat_does_not_chain(self) -> None:
        chain = beat_episode.plan_chain(
            "llm", ALL_KEYS, rng=random.Random(0), length=3,
        )
        self.assertEqual(chain, ["llm"])

    def test_length_one_returns_a_single_beat(self) -> None:
        chain = beat_episode.plan_chain(
            "tea", ALL_KEYS, rng=random.Random(0), length=1,
        )
        self.assertEqual(chain, ["tea"])

    def test_a_room_with_no_continuation_yields_one_beat(self) -> None:
        # ``tidy_desk`` continues into tea/doodle/snack; offer none of them.
        chain = beat_episode.plan_chain(
            "tidy_desk", ["tidy_desk", "nap"], rng=random.Random(0), length=3,
        )
        self.assertEqual(chain, ["tidy_desk"])


class ShouldChainTests(unittest.TestCase):
    def test_a_recent_beat_does_not_chain(self) -> None:
        self.assertFalse(
            beat_episode.should_chain(
                seconds_since_last_beat=600.0,
                min_gap_seconds=10800.0,
                ratio=1.0,
                rng=random.Random(0),
            )
        )

    def test_a_long_gap_chains_at_full_ratio(self) -> None:
        self.assertTrue(
            beat_episode.should_chain(
                seconds_since_last_beat=20000.0,
                min_gap_seconds=10800.0,
                ratio=1.0,
                rng=random.Random(0),
            )
        )

    def test_a_first_ever_beat_counts_as_a_long_gap(self) -> None:
        self.assertTrue(
            beat_episode.should_chain(
                seconds_since_last_beat=None,
                min_gap_seconds=10800.0,
                ratio=1.0,
                rng=random.Random(0),
            )
        )

    def test_zero_ratio_disables_episodes(self) -> None:
        self.assertFalse(
            beat_episode.should_chain(
                seconds_since_last_beat=None,
                min_gap_seconds=0.0,
                ratio=0.0,
                rng=random.Random(0),
            )
        )


class LengthTests(unittest.TestCase):
    def test_length_is_bounded_by_max_beats(self) -> None:
        for seed in range(20):
            length = beat_episode.pick_length(
                rng=random.Random(seed), max_beats=3,
            )
            self.assertIn(length, (2, 3))

    def test_max_two_never_returns_three(self) -> None:
        for seed in range(20):
            self.assertEqual(
                beat_episode.pick_length(rng=random.Random(seed), max_beats=2),
                2,
            )


class JoinTests(unittest.TestCase):
    def test_two_clauses_read_as_a_sequence(self) -> None:
        line = beat_episode.join_clauses(
            ["stretched out in the garden", "went round with the watering can"]
        )
        self.assertEqual(
            line,
            "stretched out in the garden, then went round with the watering can",
        )

    def test_three_clauses_get_a_later(self) -> None:
        line = beat_episode.join_clauses(["a", "b", "c"])
        self.assertEqual(line, "a, then b, and later c")

    def test_single_clause_is_unchanged(self) -> None:
        self.assertEqual(beat_episode.join_clauses(["just the one"]), "just the one")

    def test_blank_clauses_are_dropped(self) -> None:
        self.assertEqual(beat_episode.join_clauses(["", "  ", "real"]), "real")
        self.assertEqual(beat_episode.join_clauses([]), "")

    def test_trailing_full_stops_do_not_survive_the_join(self) -> None:
        self.assertEqual(
            beat_episode.join_clauses(["made tea.", "read a bit."]),
            "made tea, then read a bit",
        )


if __name__ == "__main__":
    unittest.main()
