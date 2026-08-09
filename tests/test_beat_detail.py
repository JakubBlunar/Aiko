"""Tests for :mod:`app.core.world.beat_detail` (K91 pass 1).

The module's whole job is to turn live item state into the clause an idle
beat journals, so these tests pin the distinctions that were invisible
before: two reading beats read differently because the chapter moved, a
thirsty pot gets named, and garbage state degrades to the old generic
line instead of raising.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.world import beat_detail


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class _Item:
    def __init__(
        self,
        name: str,
        *,
        kind: str = "other",
        quantity: int = 1,
        state: Any = None,
        slug: str = "",
    ) -> None:
        self.id = 1
        self.name = name
        self.kind = kind
        self.quantity = quantity
        self.state = state if state is not None else {}
        self.slug = slug


def _plant(name: str, *, days_dry: float, stage: str = "growing") -> _Item:
    watered = NOW - timedelta(days=days_dry)
    return _Item(
        name,
        kind="plant",
        state={"stage": stage, "last_watered_at": watered.isoformat()},
    )


class ReadBookTests(unittest.TestCase):
    def test_progress_is_named_so_two_sessions_differ(self) -> None:
        early = beat_detail.read_book_summary(
            _Item(
                "The Glasshouse Letters",
                kind="book",
                state={"title": "The Glasshouse Letters", "progress": 3, "total": 16},
            )
        )
        later = beat_detail.read_book_summary(
            _Item(
                "The Glasshouse Letters",
                kind="book",
                state={"title": "The Glasshouse Letters", "progress": 9, "total": 16},
            )
        )
        self.assertIn("three chapters in", early)
        self.assertIn("nine chapters in", later)
        self.assertNotEqual(early, later)

    def test_first_and_last_chapters_read_specially(self) -> None:
        start = beat_detail.read_book_summary(
            _Item("X", kind="book", state={"title": "X", "progress": 0, "total": 12})
        )
        self.assertIn("started the first chapter", start)
        nearly = beat_detail.read_book_summary(
            _Item("X", kind="book", state={"title": "X", "progress": 11, "total": 12})
        )
        self.assertIn("one chapter left", nearly)

    def test_stateless_book_falls_back_to_the_old_line(self) -> None:
        line = beat_detail.read_book_summary(_Item("some paperback", kind="book"))
        self.assertEqual(line, "curled up with some paperback and read for a while")

    def test_garbage_state_does_not_raise(self) -> None:
        item = _Item("X", kind="book", state="not a dict")
        self.assertIn("X", beat_detail.read_book_summary(item))
        item2 = _Item("Y", kind="book", state={"progress": "?", "total": "?"})
        self.assertIn("Y", beat_detail.read_book_summary(item2))


class TeaTests(unittest.TestCase):
    def test_full_pot_pours_a_cup(self) -> None:
        line = beat_detail.tea_summary(
            _Item("tea pot", state={"fullness": "full", "flavor": "genmaicha"})
        )
        assert line is not None
        self.assertIn("genmaicha tea", line)

    def test_half_pot_is_the_last_of_it(self) -> None:
        line = beat_detail.tea_summary(
            _Item("tea pot", state={"fullness": "half", "flavor": "jasmine"})
        )
        assert line is not None
        self.assertIn("what was left", line)

    def test_empty_pot_is_not_a_beat(self) -> None:
        self.assertIsNone(
            beat_detail.tea_summary(_Item("tea pot", state={"fullness": "empty"}))
        )


class SnackTests(unittest.TestCase):
    def test_last_one_is_noted(self) -> None:
        line = beat_detail.snack_summary(
            _Item("cookies", kind="food", quantity=1)
        )
        self.assertIn("last of the", line)

    def test_flavor_is_folded_in_without_repeating(self) -> None:
        line = beat_detail.snack_summary(
            _Item(
                "cookies",
                kind="food",
                quantity=5,
                state={"flavor": "chocolate chip"},
            )
        )
        self.assertIn("chocolate chip cookies", line)
        self.assertEqual(line.count("cookies"), 1)


class PlantTests(unittest.TestCase):
    def test_thirstiest_plant_is_the_driest_one(self) -> None:
        plants = [
            _plant("lavender pot", days_dry=0.2),
            _plant("basil seedling", days_dry=3.0),
            _plant("tomato seedling", days_dry=1.0),
        ]
        pick = beat_detail.thirstiest_plant(plants, now=NOW)
        assert pick is not None
        self.assertEqual(pick.name, "basil seedling")

    def test_a_freshly_watered_garden_has_no_standout(self) -> None:
        plants = [_plant("lavender pot", days_dry=0.1)]
        self.assertIsNone(beat_detail.thirstiest_plant(plants, now=NOW))

    def test_notes_escalate_with_dryness(self) -> None:
        self.assertIn(
            "really needed the water", beat_detail.plant_note("basil", 2.0) or ""
        )
        self.assertIn("bone dry", beat_detail.plant_note("basil", 6.0) or "")

    def test_a_watered_plant_reports_its_stage_instead(self) -> None:
        note = beat_detail.plant_note("lavender pot", 0.1, "flowering")
        self.assertEqual(note, "lavender pot is in flower")
        self.assertIsNone(beat_detail.plant_note("lavender pot", 0.1, "sprout"))

    def test_missing_watering_stamp_falls_back_to_days_dry(self) -> None:
        item = _Item("basil", kind="plant", state={"days_dry": 5.0})
        self.assertEqual(beat_detail.dryness_days(item, now=NOW), 5.0)

    def test_unparseable_timestamp_reads_as_not_dry(self) -> None:
        item = _Item("basil", kind="plant", state={"last_watered_at": "nonsense"})
        self.assertEqual(beat_detail.dryness_days(item, now=NOW), 0.0)


class GardenSummaryTests(unittest.TestCase):
    def test_thirsty_plant_is_named(self) -> None:
        line = beat_detail.garden_tend_summary(
            [_plant("lettuce", days_dry=3.0), _plant("lavender pot", days_dry=0.1)],
            now=NOW,
        )
        self.assertIn("lettuce", line)
        self.assertIn("really needed the water", line)

    def test_harvest_is_mentioned(self) -> None:
        line = beat_detail.garden_tend_summary(
            [_plant("tomato", days_dry=0.1)], now=NOW, harvested=["ripe tomatoes"],
        )
        self.assertIn("picked ripe tomatoes", line)

    def test_empty_garden_still_reads(self) -> None:
        self.assertTrue(beat_detail.garden_tend_summary([], now=NOW))


class PromptHintTests(unittest.TestCase):
    def test_book_hint_carries_chapter_and_blurb(self) -> None:
        hint = beat_detail.item_state_hint(
            _Item(
                "The Glasshouse Letters",
                kind="book",
                state={"progress": 3, "total": 16, "blurb": "two botanists"},
            ),
            now=NOW,
        )
        assert hint is not None
        self.assertIn("chapter 3 of 16", hint)
        self.assertIn("two botanists", hint)

    def test_plant_hint_says_whether_it_wants_water(self) -> None:
        dry = beat_detail.item_state_hint(_plant("basil", days_dry=5.0), now=NOW)
        assert dry is not None
        self.assertIn("badly needs water", dry)
        wet = beat_detail.item_state_hint(_plant("basil", days_dry=0.1), now=NOW)
        assert wet is not None
        self.assertIn("watered recently", wet)

    def test_prompt_line_pairs_names_with_state(self) -> None:
        line = beat_detail.describe_items_for_prompt(
            [
                _Item("tea pot", state={"fullness": "full", "flavor": "genmaicha"}),
                _Item("retro keyboard", kind="gadget"),
            ],
            now=NOW,
        )
        self.assertIn("tea pot (full of genmaicha)", line)
        self.assertIn("retro keyboard", line)
        self.assertNotIn("retro keyboard (", line)

    def test_empty_inventory_has_a_placeholder(self) -> None:
        self.assertEqual(
            beat_detail.describe_items_for_prompt([], now=NOW), "(nothing notable)"
        )


if __name__ == "__main__":
    unittest.main()
