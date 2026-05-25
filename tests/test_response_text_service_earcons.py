"""Tests for the Phase 1c stage-direction grammar in
``response_text_service``.

Two contracts to lock in:

  - ``strip_all_meta_tags`` must remove ``[[laugh]]`` etc. from chat
    text just like it does for reaction tags.
  - ``split_text_with_stage_directions`` must yield earcons in stream
    order with the surrounding text intact, so the TTS pipeline can
    splice audio cues between spoken chunks.

Streaming holdback (``safe_visible_prefix``) is also covered: a partial
``[[la`` opener at the tail of a streaming buffer must be held back
until enough characters have arrived to either complete or rule out
the tag.
"""
from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    STAGE_DIRECTION_KINDS,
    extract_stage_directions,
    safe_visible_prefix,
    split_text_with_stage_directions,
    strip_all_meta_tags,
)


class StripStageDirectionsTests(unittest.TestCase):
    def test_strip_removes_laugh(self) -> None:
        out = strip_all_meta_tags("That's wild [[laugh]] honestly.")
        self.assertNotIn("[[laugh]]", out)
        self.assertIn("That's wild", out)
        self.assertIn("honestly", out)

    def test_strip_removes_all_kinds(self) -> None:
        text = " ".join(f"[[{k}]]" for k in STAGE_DIRECTION_KINDS)
        out = strip_all_meta_tags(f"prefix {text} suffix")
        for kind in STAGE_DIRECTION_KINDS:
            self.assertNotIn(f"[[{kind}]]", out)
        self.assertIn("prefix", out)
        self.assertIn("suffix", out)

    def test_strip_is_case_insensitive(self) -> None:
        out = strip_all_meta_tags("Wow [[Laugh]] cool [[GASP]] yes")
        self.assertNotIn("[[", out)
        self.assertIn("Wow", out)
        self.assertIn("cool", out)
        self.assertIn("yes", out)

    def test_strip_leaves_unknown_double_bracket_alone(self) -> None:
        out = strip_all_meta_tags("Hi [[meow]] friend.")
        # Unknown tag is not in the stage-direction set; it remains.
        self.assertIn("[[meow]]", out)


class SplitWithStageDirectionsTests(unittest.TestCase):
    def test_splits_in_order(self) -> None:
        pieces = split_text_with_stage_directions(
            "Yeah [[laugh]] right [[sigh]] anyway.",
        )
        kinds = [p[0] for p in pieces]
        self.assertEqual(kinds, ["text", "earcon", "text", "earcon", "text"])
        self.assertEqual(pieces[1], ("earcon", "laugh"))
        self.assertEqual(pieces[3], ("earcon", "sigh"))

    def test_splits_with_no_directions_returns_single_text(self) -> None:
        pieces = split_text_with_stage_directions("Just a sentence.")
        self.assertEqual(pieces, [("text", "Just a sentence.")])

    def test_splits_with_only_directions(self) -> None:
        pieces = split_text_with_stage_directions("[[laugh]][[gasp]]")
        self.assertEqual(pieces, [("earcon", "laugh"), ("earcon", "gasp")])

    def test_splits_handles_empty_string(self) -> None:
        self.assertEqual(split_text_with_stage_directions(""), [])
        self.assertEqual(split_text_with_stage_directions(None), [])  # type: ignore[arg-type]

    def test_extract_stage_directions_positions(self) -> None:
        markers = extract_stage_directions("Hi [[laugh]] there [[sigh]]!")
        self.assertEqual(len(markers), 2)
        self.assertEqual(markers[0][0], "laugh")
        self.assertEqual(markers[1][0], "sigh")
        self.assertLess(markers[0][1], markers[1][1])


class SafeVisiblePrefixHoldbackTests(unittest.TestCase):
    """Streaming UI must hold characters that could still grow into a
    stage-direction opener so we don't flash a partial ``[[la`` before
    the rest of the tag arrives."""

    def test_partial_laugh_opener_held_back(self) -> None:
        # ``[[la`` could grow into ``[[laugh]]`` so it must be held.
        out = safe_visible_prefix("Hello [[la")
        self.assertEqual(out, "Hello ")

    def test_partial_si_opener_held_back(self) -> None:
        out = safe_visible_prefix("Hmm [[si")
        self.assertEqual(out, "Hmm ")

    def test_complete_laugh_tag_dropped_and_text_visible(self) -> None:
        out = safe_visible_prefix("Hello [[laugh]] world")
        self.assertEqual(out, "Hello  world")

    def test_partial_after_complete_tag_still_held(self) -> None:
        out = safe_visible_prefix("Hello [[laugh]] [[ga")
        # Content before the partial opener is visible; the partial
        # opener itself is held back.
        self.assertEqual(out, "Hello  ")


if __name__ == "__main__":
    unittest.main()
