"""Tests for the Phase 3c self-correction grammar.

Verifies the [[correct]]old[[/correct]]new pipeline:
- ``strip_all_meta_tags`` drops the ``old`` text
- ``prepare_tts_text`` drops the ``old`` text
- ``safe_visible_prefix`` holds back partial corrections
- ``extract_corrections`` reports boundaries
- ``strip_correction_for_tts`` is the lighter, single-purpose stripper
"""
from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    extract_corrections,
    safe_visible_prefix,
    strip_all_meta_tags,
    strip_correction_for_tts,
)
from app.core.session_text_utils import prepare_tts_text


class StripAllMetaTagsCorrectionTests(unittest.TestCase):
    def test_drops_old_keeps_new(self) -> None:
        text = "I think it's [[correct]]green[[/correct]] blue, actually."
        out = strip_all_meta_tags(text)
        self.assertNotIn("green", out)
        self.assertIn("blue", out)

    def test_open_only_at_end_is_held(self) -> None:
        text = "I think [[correct]]green and"
        out = strip_all_meta_tags(text)
        self.assertNotIn("green", out)
        self.assertNotIn("[[correct]]", out)

    def test_multiple_corrections(self) -> None:
        text = (
            "We saw [[correct]]Mario[[/correct]]Luigi yesterday at "
            "[[correct]]2pm[[/correct]]3pm."
        )
        out = strip_all_meta_tags(text)
        self.assertNotIn("Mario", out)
        self.assertNotIn("2pm", out)
        self.assertIn("Luigi", out)
        self.assertIn("3pm", out)


class PrepareTtsTextCorrectionTests(unittest.TestCase):
    def test_tts_speaks_only_new_text(self) -> None:
        text = "It's [[correct]]apple[[/correct]] orange juice."
        out = prepare_tts_text(text)
        self.assertNotIn("apple", out)
        self.assertIn("orange juice", out)


class SafeVisiblePrefixCorrectionTests(unittest.TestCase):
    def test_partial_open_holds_back(self) -> None:
        # Open tag in stream but no close yet -> nothing inside leaks.
        text = "Hello [[correct]]old"
        out = safe_visible_prefix(text)
        self.assertNotIn("old", out)
        self.assertIn("Hello", out)

    def test_completed_block_shows_only_new(self) -> None:
        text = "Sure, [[correct]]Tuesday[[/correct]]Wednesday works"
        out = safe_visible_prefix(text)
        self.assertNotIn("Tuesday", out)
        self.assertIn("Wednesday works", out)


class ExtractCorrectionsTests(unittest.TestCase):
    def test_reports_each_block(self) -> None:
        text = (
            "[[correct]]red[[/correct]]blue and "
            "[[correct]]nine[[/correct]]ten."
        )
        out = extract_corrections(text)
        self.assertEqual(len(out), 2)
        olds = [old for old, _ in out]
        self.assertEqual(olds, ["red", "nine"])

    def test_no_blocks_returns_empty(self) -> None:
        self.assertEqual(extract_corrections("no corrections here"), [])


class StripCorrectionForTtsTests(unittest.TestCase):
    def test_focused_stripper(self) -> None:
        out = strip_correction_for_tts(
            "[[correct]]old[[/correct]]new and rest",
        )
        self.assertNotIn("old", out)
        # The lightweight stripper preserves surrounding meta; only
        # corrections are removed. ``new`` is OUTSIDE the block, so
        # it survives.
        self.assertIn("new and rest", out)


if __name__ == "__main__":
    unittest.main()
