"""Tests for inline ``[[moment:…]]`` parsing and Track-2 LLM payload parsing."""
from __future__ import annotations

import unittest

from app.core.relationship.shared_moment_extractor import (
    SharedMomentCandidate,
    VIBE_VOCABULARY,
    _parse_llm_payload,
    detect_moment_reaction_tags,
    extract_inline_tags,
    normalise_vibe,
    strip_inline_tags,
)


class TestNormaliseVibe(unittest.TestCase):
    def test_known_vibes_pass_through(self) -> None:
        for v in VIBE_VOCABULARY:
            self.assertEqual(normalise_vibe(v), v)

    def test_unknown_vibe_collapses_to_general(self) -> None:
        self.assertEqual(normalise_vibe("uwu"), "general")
        self.assertEqual(normalise_vibe(""), "general")
        self.assertEqual(normalise_vibe(None), "general")

    def test_synonyms_map_to_canonical(self) -> None:
        self.assertEqual(normalise_vibe("funny"), "playful")
        self.assertEqual(normalise_vibe("loving"), "tender")
        self.assertEqual(normalise_vibe("achievement"), "victory")
        self.assertEqual(normalise_vibe("present"), "gift")


class TestInlineTagExtraction(unittest.TestCase):
    def test_single_tag_extracted(self) -> None:
        text = "ahaha okay [[moment:playful:we laughed about the cookie jar misunderstanding]]"
        candidates = extract_inline_tags(text)
        self.assertEqual(len(candidates), 1)
        self.assertIsInstance(candidates[0], SharedMomentCandidate)
        self.assertEqual(candidates[0].vibe, "playful")
        self.assertIn("cookie jar", candidates[0].summary)
        self.assertEqual(candidates[0].source, "tag")

    def test_multiple_tags_dedup(self) -> None:
        text = (
            "[[moment:playful:we laughed about cookies]]\n"
            "and then again [[moment:playful:we laughed about cookies]]\n"
            "[[moment:tender:you told me about Mochi]]"
        )
        candidates = extract_inline_tags(text)
        self.assertEqual(len(candidates), 2)
        vibes = {c.vibe for c in candidates}
        self.assertEqual(vibes, {"playful", "tender"})

    def test_unknown_vibe_normalises(self) -> None:
        text = "[[moment:funny:we made a goofy little script]]"
        candidates = extract_inline_tags(text)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].vibe, "playful")

    def test_too_short_summary_ignored(self) -> None:
        # Summary must be >=4 chars after stripping noise. ``yes`` is too short.
        text = "[[moment:warm:yes]]"
        candidates = extract_inline_tags(text)
        self.assertEqual(candidates, [])

    def test_strip_inline_tags_leaves_clean_text(self) -> None:
        text = (
            "hey, that was nice. [[moment:warm:we sat quietly for a minute]] "
            "anyway, more code?"
        )
        cleaned = strip_inline_tags(text)
        self.assertNotIn("[[moment:", cleaned)
        self.assertIn("anyway, more code?", cleaned)


class TestLLMPayloadParsing(unittest.TestCase):
    def test_valid_moment_parses(self) -> None:
        raw = (
            '{"moment": {"summary": "Jacob and I debugged the proactive bug",'
            ' "vibe": "focused"}}'
        )
        candidate = _parse_llm_payload(raw)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.source, "llm")
        # "focused" isn't in the vocabulary -> collapses to general.
        self.assertEqual(candidate.vibe, "general")

    def test_valid_moment_known_vibe(self) -> None:
        raw = '{"moment": {"summary": "we laughed at the typo", "vibe": "playful"}}'
        candidate = _parse_llm_payload(raw)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.vibe, "playful")

    def test_null_moment_returns_none(self) -> None:
        self.assertIsNone(_parse_llm_payload('{"moment": null}'))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(_parse_llm_payload(""))
        self.assertIsNone(_parse_llm_payload("nope"))
        self.assertIsNone(_parse_llm_payload("{not json"))

    def test_fenced_json_block_supported(self) -> None:
        raw = '```json\n{"moment": {"summary": "we cooked dinner together", "vibe": "warm"}}\n```'
        candidate = _parse_llm_payload(raw)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.vibe, "warm")

    def test_too_short_summary_rejected(self) -> None:
        raw = '{"moment": {"summary": "yes", "vibe": "warm"}}'
        self.assertIsNone(_parse_llm_payload(raw))


class TestReactionTagDetection(unittest.TestCase):
    def test_high_affect_tag_detected(self) -> None:
        self.assertEqual(
            detect_moment_reaction_tags("[[reaction:laugh]] ahah okay"),
            {"laugh"},
        )

    def test_neutral_tag_ignored(self) -> None:
        self.assertEqual(detect_moment_reaction_tags("[[reaction:neutral]] hi"), set())

    def test_multiple_tags(self) -> None:
        found = detect_moment_reaction_tags(
            "[[reaction:tender]] yeah... [[reaction:tender]] same"
        )
        self.assertEqual(found, {"tender"})


if __name__ == "__main__":
    unittest.main()
