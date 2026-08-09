"""Tests for :mod:`app.core.world.day_intention` (K91 pass 4).

The intention's job is to give a quiet day a spine: it comes from what her
world is actually asking for, it survives the day but not the night, and
the beat that satisfies it says so.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone

from app.core.world import day_intention


NOW = datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc)


class _Item:
    def __init__(self, name: str, *, kind: str = "other", state=None) -> None:
        self.id = 1
        self.name = name
        self.kind = kind
        self.quantity = 1
        self.state = state if state is not None else {}
        self.slug = name.lower().replace(" ", "_")


def _plant(name: str, *, days_dry: float = 0.0, stage: str = "growing") -> _Item:
    watered = NOW - timedelta(days=days_dry)
    return _Item(
        name,
        kind="plant",
        state={"stage": stage, "last_watered_at": watered.isoformat()},
    )


def _book(progress: int, total: int = 16) -> _Item:
    return _Item(
        "The Glasshouse Letters",
        kind="book",
        state={
            "title": "The Glasshouse Letters",
            "progress": progress,
            "total": total,
        },
    )


class ProposeTests(unittest.TestCase):
    def test_ripe_produce_outranks_everything(self) -> None:
        intention = day_intention.propose(
            [_plant("tomato", stage="mature"), _plant("basil", days_dry=6.0)],
            now=NOW,
            rng=random.Random(0),
        )
        self.assertIn("pick the tomato", intention.text)
        self.assertEqual(intention.beat_key, "garden")

    def test_a_thirsty_plant_becomes_the_intention(self) -> None:
        intention = day_intention.propose(
            [_plant("lettuce", days_dry=3.0)], now=NOW, rng=random.Random(0),
        )
        self.assertIn("lettuce", intention.text)
        self.assertEqual(intention.beat_key, "garden")

    def test_a_nearly_finished_book_becomes_the_intention(self) -> None:
        intention = day_intention.propose(
            [_book(14)], now=NOW, rng=random.Random(0),
        )
        self.assertEqual(intention.text, "finish The Glasshouse Letters")
        self.assertEqual(intention.beat_key, "read_book")

    def test_a_barely_started_book_is_not_urgent(self) -> None:
        intention = day_intention.propose(
            [_book(2)], now=NOW, hobby="learning Welsh", rng=random.Random(0),
        )
        self.assertIn("Welsh", intention.text)

    def test_hobby_is_used_when_the_room_is_content(self) -> None:
        intention = day_intention.propose(
            [_plant("basil", days_dry=0.1)],
            now=NOW,
            hobby="mapping the constellations",
            rng=random.Random(0),
        )
        self.assertEqual(
            intention.text, "put a proper hour into mapping the constellations"
        )

    def test_an_empty_room_still_gets_an_intention(self) -> None:
        intention = day_intention.propose([], now=NOW, rng=random.Random(0))
        self.assertTrue(intention.text)
        self.assertTrue(intention.beat_key)

    def test_the_day_is_stamped_locally(self) -> None:
        intention = day_intention.propose([], now=NOW, rng=random.Random(0))
        self.assertEqual(intention.day, day_intention.local_day(NOW))


class RoundTripTests(unittest.TestCase):
    def test_dump_and_load_round_trip(self) -> None:
        original = day_intention.DayIntention(
            day="2026-08-09", text="finish the book", beat_key="read_book",
        )
        restored = day_intention.load(day_intention.dump(original))
        self.assertEqual(restored, original)

    def test_satisfy_flips_the_flag_without_mutating(self) -> None:
        original = day_intention.DayIntention(
            day="2026-08-09", text="x", beat_key="read_book",
        )
        done = original.satisfy()
        self.assertFalse(original.satisfied)
        self.assertTrue(done.satisfied)

    def test_garbage_loads_as_none(self) -> None:
        self.assertIsNone(day_intention.load(None))
        self.assertIsNone(day_intention.load(""))
        self.assertIsNone(day_intention.load("not json"))
        self.assertIsNone(day_intention.load("[1, 2]"))
        self.assertIsNone(day_intention.load('{"day": "", "text": ""}'))


class CloseOutTests(unittest.TestCase):
    def test_close_out_appends_an_admission(self) -> None:
        line = day_intention.close_out(
            "curled up with the book, one chapter left", random.Random(0),
        )
        self.assertTrue(line.startswith("curled up with the book"))
        self.assertIn(" — ", line)

    def test_close_out_drops_a_trailing_stop(self) -> None:
        line = day_intention.close_out("read a bit.", random.Random(0))
        self.assertNotIn(".", line.split(" — ")[0])

    def test_close_out_of_nothing_is_nothing(self) -> None:
        self.assertEqual(day_intention.close_out("", random.Random(0)), "")


if __name__ == "__main__":
    unittest.main()
