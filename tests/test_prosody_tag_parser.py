"""Layer 3a tests: ``[[prosody:LABEL]]`` parser, stripper, streaming guard.

Covers:
  * ``parse_prosody_tag`` accepts the five v1 values, rejects unknown.
  * ``consume_leading_prosody_tag`` removes the tag *only* when leading.
  * ``strip_all_meta_tags`` drops misplaced / partial tags so neither
    chat transcript nor TTS sees them.
  * ``safe_visible_prefix`` (streaming holdback) treats a partial
    ``[[prosody:`` opener as a hold candidate.
"""
from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    PROSODY_TAG_VALUES,
    consume_leading_prosody_tag,
    parse_prosody_tag,
    safe_visible_prefix,
    strip_all_meta_tags,
)


class ParseProsodyTagTests(unittest.TestCase):
    def test_all_five_known_values(self) -> None:
        for label in PROSODY_TAG_VALUES:
            self.assertEqual(
                parse_prosody_tag(f"[[prosody:{label}]] hello"), label,
            )

    def test_uppercase_lowercased(self) -> None:
        self.assertEqual(
            parse_prosody_tag("[[PROSODY:WHISPER]] hi"), "whisper",
        )

    def test_unknown_value_rejected(self) -> None:
        self.assertIsNone(parse_prosody_tag("[[prosody:scream]] hi"))

    def test_non_leading_rejected(self) -> None:
        self.assertIsNone(parse_prosody_tag("hi [[prosody:whisper]] there"))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(parse_prosody_tag(""))
        self.assertIsNone(parse_prosody_tag(None))  # type: ignore[arg-type]

    def test_extra_whitespace_ok(self) -> None:
        self.assertEqual(
            parse_prosody_tag("   [[prosody:slow]]   take it easy"),
            "slow",
        )


class ConsumeLeadingProsodyTagTests(unittest.TestCase):
    def test_strips_leading_tag(self) -> None:
        label, rest = consume_leading_prosody_tag(
            "[[prosody:whisper]] secret line",
        )
        self.assertEqual(label, "whisper")
        self.assertEqual(rest.strip(), "secret line")

    def test_no_tag_passes_through(self) -> None:
        label, rest = consume_leading_prosody_tag("regular sentence")
        self.assertIsNone(label)
        self.assertEqual(rest, "regular sentence")

    def test_unknown_value_passes_through(self) -> None:
        label, rest = consume_leading_prosody_tag(
            "[[prosody:scream]] hi there",
        )
        self.assertIsNone(label)
        # Unknown values are NOT consumed by the leading helper -- the
        # global stripper will catch them later.
        self.assertEqual(rest, "[[prosody:scream]] hi there")


class StripAllMetaTagsProsodyTests(unittest.TestCase):
    def test_leading_tag_stripped(self) -> None:
        cleaned = strip_all_meta_tags("[[prosody:slow]] tell me again")
        self.assertNotIn("[[prosody:", cleaned)
        self.assertIn("tell me again", cleaned)

    def test_mid_sentence_tag_stripped(self) -> None:
        cleaned = strip_all_meta_tags("hi [[prosody:firm]] there friend")
        self.assertNotIn("[[prosody:", cleaned)
        self.assertIn("hi", cleaned)
        self.assertIn("there friend", cleaned)

    def test_mid_sentence_tag_collapses_flanking_whitespace(self) -> None:
        # Regression: stripping a tag with a space on both sides used to
        # leave a double space ("cute.  I") in the streamed text.
        cleaned = strip_all_meta_tags("cute. [[prosody:soft]] I missed you.")
        self.assertEqual(cleaned, "cute. I missed you.")
        self.assertNotIn("  ", cleaned)

    def test_partial_open_at_eol_stripped(self) -> None:
        cleaned = strip_all_meta_tags("hi there [[prosody:whisp")
        self.assertNotIn("[[prosody:", cleaned)
        # Everything from the opener onward goes; the prefix survives.
        self.assertIn("hi there", cleaned)


class SafeVisiblePrefixProsodyTests(unittest.TestCase):
    def test_partial_prosody_is_held(self) -> None:
        # ``[[prosody:`` is the start of a known opener -- the streaming
        # holdback must not flush it yet.
        out = safe_visible_prefix("hi [[prosody:")
        self.assertEqual(out, "hi ")

    def test_completed_prosody_passes_through_after_strip(self) -> None:
        out = safe_visible_prefix("hi [[prosody:slow]] there")
        self.assertNotIn("[[prosody:", out)
        # The catch-all strip removes the tag, leaving the surrounding
        # text intact.
        self.assertIn("there", out)


class ValidValuesConstantTests(unittest.TestCase):
    """Pin the v1 vocabulary so a silent rename gets caught in code review."""

    def test_six_values_locked(self) -> None:
        self.assertEqual(
            tuple(PROSODY_TAG_VALUES),
            ("whisper", "soft", "slow", "fast", "firm"),
        )


if __name__ == "__main__":
    unittest.main()
