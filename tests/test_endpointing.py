"""Tests for the tiered voice-endpointing decision logic.

Covers the contract documented in `app/stt/endpointing.py` and the plan:

- Sentence-final partials commit fast (>= ``fast_close_silence_seconds``).
- Hesitation markers extend (reset silence counter).
- Ambiguous partials fall through to the hard turn boundary.
- ``enabled=False`` and ``use_partial_transcript=False`` degrade safely.
"""
from __future__ import annotations

import unittest

from app.core.infra.settings import EndpointingSettings
from app.stt.endpointing import decide, is_hesitation_marker, is_sentence_final


def _settings(**overrides: object) -> EndpointingSettings:
    base = EndpointingSettings()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class HesitationMarkerTests(unittest.TestCase):
    def test_matches_short_fillers(self) -> None:
        for sample in ["um", "uh", "uhh", "hmm", "er", "eh"]:
            self.assertTrue(
                is_hesitation_marker(sample),
                msg=f"expected hesitation match: {sample!r}",
            )

    def test_matches_trailing_conjunctions(self) -> None:
        for sample in [
            "I want to and",
            "It was good but",
            "Maybe so",
            "let me ask, because",
        ]:
            self.assertTrue(
                is_hesitation_marker(sample),
                msg=f"expected hesitation match: {sample!r}",
            )

    def test_matches_thinking_phrases(self) -> None:
        for sample in [
            "you know",
            "i mean",
            "let me think",
            "kind of",
            "sort of",
            "how can i say",
        ]:
            self.assertTrue(
                is_hesitation_marker(sample),
                msg=f"expected hesitation match: {sample!r}",
            )

    def test_does_not_match_clean_declaratives(self) -> None:
        for sample in [
            "Tell me about the weather",
            "I would like a cup of tea",
            "What is your name",
            "Hello there",
        ]:
            self.assertFalse(
                is_hesitation_marker(sample),
                msg=f"unexpected hesitation match: {sample!r}",
            )

    def test_empty_input_is_not_hesitation(self) -> None:
        self.assertFalse(is_hesitation_marker(""))
        self.assertFalse(is_hesitation_marker("   "))

    def test_extra_pattern_via_settings(self) -> None:
        # Regression: user-supplied pattern is consulted before built-ins.
        self.assertTrue(
            is_hesitation_marker(
                "ano sa",
                extra_patterns=[r"\bano\s+sa\s*$"],
            )
        )

    def test_invalid_extra_pattern_is_skipped(self) -> None:
        # Bad regex should not crash.
        self.assertFalse(
            is_hesitation_marker(
                "hello",
                extra_patterns=[r"["],  # invalid
            )
        )


class SentenceFinalTests(unittest.TestCase):
    def test_punctuation(self) -> None:
        for sample in [
            "Hello world.",
            "Are you there?",
            "Stop!",
            "It's done.",
        ]:
            self.assertTrue(
                is_sentence_final(sample),
                msg=f"expected sentence-final: {sample!r}",
            )

    def test_closer_phrases(self) -> None:
        for sample in [
            "thanks",
            "thank you",
            "okay",
            "OK",
            "got it",
            "that's all",
            "that is everything",
        ]:
            self.assertTrue(
                is_sentence_final(sample),
                msg=f"expected sentence-final: {sample!r}",
            )

    def test_mid_clause_is_not_final(self) -> None:
        for sample in [
            "I want to talk about",
            "could you please",
            "let me know if",
        ]:
            self.assertFalse(
                is_sentence_final(sample),
                msg=f"unexpected sentence-final: {sample!r}",
            )


class DecideTests(unittest.TestCase):
    def test_below_phrase_boundary_waits(self) -> None:
        s = _settings()
        self.assertEqual(decide(0.0, "anything", s), "wait")
        self.assertEqual(decide(0.5, "anything", s), "wait")

    def test_fast_close_on_sentence_final(self) -> None:
        s = _settings(
            fast_close_silence_seconds=0.6,
            phrase_silence_seconds=1.0,
            turn_silence_seconds=3.0,
        )
        # >= fast and < phrase: fires only because partial is sentence-final.
        self.assertEqual(decide(0.6, "okay.", s), "commit")
        self.assertEqual(decide(0.7, "thanks", s), "commit")

    def test_phrase_boundary_with_hesitation_extends(self) -> None:
        s = _settings()
        self.assertEqual(decide(1.0, "I want to and", s), "extend")
        self.assertEqual(decide(1.5, "let me think", s), "extend")

    def test_phrase_boundary_with_sentence_final_commits(self) -> None:
        s = _settings()
        self.assertEqual(decide(1.0, "Hello there.", s), "commit")
        self.assertEqual(decide(1.2, "thanks", s), "commit")

    def test_phrase_boundary_with_ambiguous_waits(self) -> None:
        # Ambiguous partial → fall through to the hard cap; loop's own
        # silence_chunks_to_stop fires at turn_silence_seconds.
        s = _settings()
        self.assertEqual(decide(1.0, "hello world", s), "wait")
        self.assertEqual(decide(2.0, "hello world", s), "wait")

    def test_turn_boundary_always_commits(self) -> None:
        s = _settings(turn_silence_seconds=3.0)
        self.assertEqual(decide(3.0, "I want to and", s), "commit")
        self.assertEqual(decide(3.5, "anything", s), "commit")
        self.assertEqual(decide(4.0, "", s), "commit")

    def test_disabled_settings_always_waits(self) -> None:
        s = _settings(enabled=False)
        self.assertEqual(decide(0.0, "x", s), "wait")
        self.assertEqual(decide(5.0, "thanks", s), "wait")
        self.assertEqual(decide(10.0, "and uh", s), "wait")

    def test_disabled_partial_falls_back_to_two_tier(self) -> None:
        # Without the lexical signal, hesitation markers don't extend
        # and sentence-final markers don't fast-close. The phrase boundary
        # acts as a regular silence boundary that hands off to the loop's
        # cap (which the caller wires to turn_silence_seconds).
        s = _settings(use_partial_transcript=False)
        self.assertEqual(decide(0.5, "thanks", s), "wait")
        self.assertEqual(decide(1.0, "thanks", s), "wait")
        self.assertEqual(decide(1.0, "and uh", s), "wait")
        self.assertEqual(decide(3.0, "thanks", s), "commit")

    def test_extend_can_be_disabled(self) -> None:
        s = _settings(hesitation_extend_to_turn=False)
        # Hesitation no longer extends; ambiguous behaviour ("wait") applies.
        self.assertEqual(decide(1.0, "and uh", s), "wait")

    def test_empty_partial_at_phrase_boundary_waits(self) -> None:
        s = _settings()
        # Empty partial = not finished, not hesitation. Wait for hard cap.
        self.assertEqual(decide(1.0, "", s), "wait")
        self.assertEqual(decide(1.5, "", s), "wait")

    def test_extra_hesitation_pattern_extends(self) -> None:
        s = _settings(hesitation_markers=[r"\bano\s+sa\s*$"])
        self.assertEqual(decide(1.0, "ano sa", s), "extend")

    def test_extra_sentence_final_pattern_commits(self) -> None:
        s = _settings(sentence_final_markers=[r"\bover\s+and\s+out\s*$"])
        self.assertEqual(decide(0.6, "over and out", s), "commit")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
