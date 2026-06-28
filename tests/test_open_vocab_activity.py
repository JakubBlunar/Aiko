"""Tests for H14 open-vocab activities + the ``[[activity:...]]`` self-tag."""
from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    extract_activity_tag,
    strip_all_meta_tags,
)
from app.core.world.world_store import (
    VALID_ACTIVITIES,
    canonical_activity,
    normalize_activity,
)


class NormalizeActivityTests(unittest.TestCase):
    def test_snake_cases_and_lowers(self) -> None:
        self.assertEqual(
            normalize_activity("Repotting The Basil"), "repotting_the_basil"
        )

    def test_collapses_punctuation(self) -> None:
        self.assertEqual(
            normalize_activity("sketching -- the skyline!"),
            "sketching_the_skyline",
        )

    def test_length_capped(self) -> None:
        out = normalize_activity("x" * 200)
        self.assertLessEqual(len(out), 40)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(normalize_activity("   "))
        self.assertIsNone(normalize_activity("!!!"))
        self.assertIsNone(normalize_activity(None))


class CanonicalActivityTests(unittest.TestCase):
    def test_canonical_passthrough(self) -> None:
        for a in VALID_ACTIVITIES:
            self.assertEqual(canonical_activity(a), a)

    def test_open_vocab_buckets(self) -> None:
        self.assertEqual(canonical_activity("repotting_the_basil"), "tinkering")
        self.assertEqual(canonical_activity("sketching_the_skyline"), "doodling")
        self.assertEqual(canonical_activity("sipping_chamomile"), "snacking")
        self.assertEqual(canonical_activity("stargazing"), "looking_outside")

    def test_unknown_defaults_idle(self) -> None:
        self.assertEqual(canonical_activity("xyzzy_nonsense"), "idle")
        self.assertEqual(canonical_activity(""), "idle")


class ActivityTagTests(unittest.TestCase):
    def test_extract_last_tag(self) -> None:
        text = "mm [[activity:reading]] then [[activity:making_tea]] now"
        self.assertEqual(extract_activity_tag(text), "making_tea")

    def test_no_tag_returns_none(self) -> None:
        self.assertIsNone(extract_activity_tag("just a normal reply"))

    def test_tag_stripped_from_display(self) -> None:
        cleaned = strip_all_meta_tags("here you go [[activity:reorganising_shelf]]")
        self.assertNotIn("activity:", cleaned)
        self.assertNotIn("[[", cleaned)


if __name__ == "__main__":
    unittest.main()
