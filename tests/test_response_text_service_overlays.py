"""Tests for the Alexia ``[[overlay:NAME]]`` grammar in
``app.core.services.response_text_service``.

The grammar is a side-channel: the LLM emits ``[[overlay:sweat]]``
inline; ``TurnRunner`` extracts the name and fires it on the
``avatar_overlay`` WS event; the text is stripped from the chat
transcript and TTS so neither the user nor the model hears the
keyword spoken aloud.
"""
from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    _MOTION_TAG_PATTERN,
    _OUTFIT_TAG_PATTERN,
    _OVERLAY_TAG_PATTERN,
    _looks_like_partial_opener,
    extract_motion_commands,
    extract_outfit_commands,
    extract_overlays,
    strip_all_meta_tags,
)


class ExtractOverlaysTests(unittest.TestCase):
    def test_returns_empty_list_for_plain_text(self) -> None:
        self.assertEqual(extract_overlays("hello there"), [])

    def test_returns_empty_list_for_empty_input(self) -> None:
        self.assertEqual(extract_overlays(""), [])
        self.assertEqual(extract_overlays(None), [])  # type: ignore[arg-type]

    def test_extracts_single_overlay(self) -> None:
        result = extract_overlays("oh no [[overlay:sweat]] really")
        self.assertEqual(len(result), 1)
        name, offset = result[0]
        self.assertEqual(name, "sweat")
        # Offset is the start of the ``[[`` marker in the original.
        self.assertEqual(offset, 6)

    def test_extracts_multiple_overlays_in_order(self) -> None:
        result = extract_overlays(
            "[[overlay:blush]] hi [[overlay:stars]]!",
        )
        self.assertEqual([r[0] for r in result], ["blush", "stars"])
        self.assertLess(result[0][1], result[1][1])

    def test_overlay_names_are_lowercased(self) -> None:
        self.assertEqual(
            extract_overlays("yo [[overlay:DIZZY]]")[0][0], "dizzy",
        )
        self.assertEqual(
            extract_overlays("yo [[overlay:Question]]")[0][0], "question",
        )

    def test_underscore_names_supported(self) -> None:
        self.assertEqual(
            extract_overlays("[[overlay:angry_marks]]")[0][0],
            "angry_marks",
        )

    def test_partial_opener_is_not_extracted(self) -> None:
        # Streaming case: tag is incomplete, must not fire.
        self.assertEqual(extract_overlays("hi [[overlay:swe"), [])

    def test_stacked_overlay_returns_full_stack_expression(self) -> None:
        # Phase 3: ``[[overlay:A+B]]`` captures ``a+b`` as the name so
        # downstream dispatch can split on ``+`` and fire each
        # component as its own pulse via ``_emit_avatar_overlay``.
        result = extract_overlays("oh [[overlay:sweat+question]] dear")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "sweat+question")

    def test_three_way_stacked_overlay(self) -> None:
        # The regex allows arbitrarily long stacks; the persona
        # discourages going beyond two but the parser must accept
        # whatever the LLM emits without crashing.
        result = extract_overlays("[[overlay:stars+blush+grin]]")
        self.assertEqual(result[0][0], "stars+blush+grin")


class StripOverlaysTests(unittest.TestCase):
    def test_strip_removes_completed_overlay_tags(self) -> None:
        cleaned = strip_all_meta_tags(
            "I might [[overlay:sweat]]be in trouble.",
        )
        self.assertNotIn("overlay:", cleaned)
        self.assertNotIn("[[", cleaned)
        self.assertEqual(cleaned, "I might be in trouble.")

    def test_strip_removes_unclosed_overlay_at_eos(self) -> None:
        cleaned = strip_all_meta_tags("oh dear [[overlay:swea")
        self.assertNotIn("[[overlay", cleaned)
        self.assertEqual(cleaned, "oh dear ")

    def test_strip_keeps_other_text(self) -> None:
        cleaned = strip_all_meta_tags(
            "[[overlay:blush]]you're sweet[[overlay:stars]]!",
        )
        self.assertEqual(cleaned, "you're sweet!")


class StreamingHoldbackTests(unittest.TestCase):
    def test_overlay_opener_triggers_holdback(self) -> None:
        self.assertTrue(_looks_like_partial_opener("[[overlay:"))

    def test_partial_overlay_name_triggers_holdback(self) -> None:
        self.assertTrue(_looks_like_partial_opener("[[overlay:swe"))

    def test_text_after_overlay_close_is_safe_to_emit(self) -> None:
        # The streaming layer hands ``_looks_like_partial_opener`` the
        # *suffix* after the last definitive boundary. Once the
        # closing ``]]`` has streamed in, the trailing text on its own
        # ("done") is safe to emit immediately.
        self.assertFalse(_looks_like_partial_opener("done"))
        self.assertFalse(_looks_like_partial_opener(" right"))

    def test_short_bracket_prefix_still_holds(self) -> None:
        self.assertTrue(_looks_like_partial_opener("["))
        self.assertTrue(_looks_like_partial_opener("[["))


class GrammarPatternTests(unittest.TestCase):
    """Direct guards on the regex itself, so changes are intentional."""

    def test_pattern_matches_simple_name(self) -> None:
        self.assertIsNotNone(_OVERLAY_TAG_PATTERN.search("[[overlay:cry]]"))

    def test_pattern_rejects_dash_in_name(self) -> None:
        # Names are ``[A-Za-z_][A-Za-z0-9_]*`` — dashes would conflict
        # with future namespacing and shouldn't slip through.
        self.assertIsNone(
            _OVERLAY_TAG_PATTERN.search("[[overlay:angry-marks]]"),
        )

    def test_pattern_rejects_leading_digit(self) -> None:
        self.assertIsNone(_OVERLAY_TAG_PATTERN.search("[[overlay:1cool]]"))


class ExtractOutfitCommandsTests(unittest.TestCase):
    def test_returns_empty_for_plain_text(self) -> None:
        self.assertEqual(extract_outfit_commands("hello there"), [])

    def test_extracts_outfit_directive(self) -> None:
        result = extract_outfit_commands(
            "Time for bed. [[outfit:pajamas]] Sweet dreams."
        )
        self.assertEqual([r[0] for r in result], ["pajamas"])

    def test_extracts_multiple_outfit_directives_in_order(self) -> None:
        result = extract_outfit_commands(
            "[[outfit:day]] morning! later [[outfit:pajamas]]"
        )
        self.assertEqual([r[0] for r in result], ["day", "pajamas"])

    def test_outfit_names_are_lowercased(self) -> None:
        self.assertEqual(
            extract_outfit_commands("[[outfit:PAJAMAS]]")[0][0], "pajamas",
        )

    def test_partial_outfit_opener_does_not_match(self) -> None:
        self.assertEqual(extract_outfit_commands("[[outfit:pjam"), [])


class ExtractMotionCommandsTests(unittest.TestCase):
    def test_returns_empty_for_plain_text(self) -> None:
        self.assertEqual(extract_motion_commands("hello there"), [])

    def test_extracts_motion_directive(self) -> None:
        self.assertEqual(
            [r[0] for r in extract_motion_commands("hi [[motion:wave]]")],
            ["wave"],
        )

    def test_motion_names_are_lowercased_and_underscored(self) -> None:
        self.assertEqual(
            extract_motion_commands("[[motion:HEAD_NOD]]")[0][0],
            "head_nod",
        )

    def test_partial_motion_opener_does_not_match(self) -> None:
        self.assertEqual(extract_motion_commands("[[motion:wav"), [])


class StripOutfitAndMotionTagsTests(unittest.TestCase):
    def test_strip_removes_outfit_tags(self) -> None:
        cleaned = strip_all_meta_tags(
            "Bedtime. [[outfit:pajamas]] Goodnight."
        )
        self.assertNotIn("outfit:", cleaned)
        self.assertEqual(cleaned, "Bedtime.  Goodnight.")

    def test_strip_removes_motion_tags(self) -> None:
        cleaned = strip_all_meta_tags("[[motion:wave]]hey there")
        self.assertEqual(cleaned, "hey there")

    def test_strip_removes_unclosed_outfit_at_eos(self) -> None:
        cleaned = strip_all_meta_tags("settling in [[outfit:paj")
        self.assertNotIn("[[outfit", cleaned)
        self.assertEqual(cleaned, "settling in ")

    def test_strip_removes_unclosed_motion_at_eos(self) -> None:
        cleaned = strip_all_meta_tags("hi [[motion:wav")
        self.assertNotIn("[[motion", cleaned)
        self.assertEqual(cleaned, "hi ")

    def test_strip_handles_mixed_grammars(self) -> None:
        cleaned = strip_all_meta_tags(
            "[[overlay:blush]]hi[[motion:wave]]there[[outfit:day]]!"
        )
        self.assertEqual(cleaned, "hithere!")


class HoldbackForOutfitAndMotionTests(unittest.TestCase):
    def test_outfit_opener_triggers_holdback(self) -> None:
        self.assertTrue(_looks_like_partial_opener("[[outfit:"))
        self.assertTrue(_looks_like_partial_opener("[[outfit:pa"))

    def test_motion_opener_triggers_holdback(self) -> None:
        self.assertTrue(_looks_like_partial_opener("[[motion:"))
        self.assertTrue(_looks_like_partial_opener("[[motion:wav"))


class OutfitMotionPatternTests(unittest.TestCase):
    def test_outfit_pattern_matches_simple_name(self) -> None:
        self.assertIsNotNone(_OUTFIT_TAG_PATTERN.search("[[outfit:day]]"))

    def test_motion_pattern_matches_simple_name(self) -> None:
        self.assertIsNotNone(_MOTION_TAG_PATTERN.search("[[motion:bow]]"))

    def test_outfit_pattern_rejects_dash(self) -> None:
        self.assertIsNone(
            _OUTFIT_TAG_PATTERN.search("[[outfit:day-clothes]]")
        )


if __name__ == "__main__":
    unittest.main()
