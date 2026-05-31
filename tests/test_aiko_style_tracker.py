"""Tests for :mod:`app.core.persona.aiko_style_tracker` (anti-rut layer).

The tracker is a pure rolling-window detector -- no embedder, no LLM
-- so the tests just feed scripted assistant-text streams and assert
the band classification, per-band cooldown, warmup, priority order,
and short-text gating behaviour.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.persona.aiko_style_tracker import (
    BAND_LENGTH_SPRAWL,
    BAND_OPENER_RUT,
    BAND_QUESTION_SATURATION,
    AikoStylePatternTracker,
    StyleRutResult,
    _extract_features,
    render_inner_life_block,
)


# ── stub helpers ────────────────────────────────────────────────────


def _settings(**overrides: object) -> SimpleNamespace:
    """Compact ``AgentSettings`` stub via ``SimpleNamespace`` getattr."""
    base: dict[str, object] = dict(
        style_tracker_enabled=True,
        style_tracker_window=10,
        style_tracker_warmup=6,
        style_tracker_opener_count_threshold=4,
        style_tracker_opener_topk_share=0.60,
        style_tracker_question_rate_threshold=0.75,
        style_tracker_avg_questions_threshold=1.5,
        style_tracker_length_avg_threshold=50.0,
        style_tracker_cue_cooldown_turns=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(**overrides: object) -> AikoStylePatternTracker:
    return AikoStylePatternTracker(agent_settings=_settings(**overrides))


def _short_reply(opener: str = "yeah", *, words: int = 8) -> str:
    """Filler reply that ends on a period (not a question) and has the
    requested opener. Reused across opener-rut cases."""
    body = " ".join(["really"] * max(0, words - 1))
    return f"{opener.capitalize()} {body}.".strip()


def _long_reply(words: int = 60) -> str:
    """Verbose statement-only reply, no question, varied opener."""
    body = " ".join(["circuits"] * max(0, words - 1))
    return f"Honestly {body}.".strip()


def _question_reply(opener: str = "wait") -> str:
    return f"{opener.capitalize()} did that go through okay?"


# ── feature extraction ──────────────────────────────────────────────


class ExtractFeaturesTests(unittest.TestCase):
    def test_opener_lowercased_and_stripped(self) -> None:
        features = _extract_features("Yeah, that makes sense.")
        self.assertEqual(features.opener, "yeah")

    def test_quoted_opener_strips_punctuation(self) -> None:
        features = _extract_features('"Oh," she said honestly.')
        self.assertEqual(features.opener, "oh")

    def test_word_count_and_sentence_count(self) -> None:
        features = _extract_features("First. Second sentence here.")
        self.assertEqual(features.word_count, 4)
        self.assertEqual(features.sentence_count, 2)

    def test_question_count_includes_stacked(self) -> None:
        features = _extract_features("Did you? Or did you really?")
        self.assertEqual(features.question_count, 2)
        self.assertTrue(features.ends_with_question)

    def test_ends_with_question_handles_trailing_quote(self) -> None:
        features = _extract_features('She asked "Why?"')
        self.assertTrue(features.ends_with_question)

    def test_statement_does_not_end_with_question(self) -> None:
        features = _extract_features("Just a statement, that's all.")
        self.assertFalse(features.ends_with_question)
        self.assertEqual(features.question_count, 0)


# ── warmup / short-text gating ──────────────────────────────────────


class WarmupTests(unittest.TestCase):
    def test_silent_below_warmup_threshold(self) -> None:
        # Warmup=6 with all 5 turns ruting on the same opener should
        # still emit nothing because the deque hasn't filled the
        # warmup quota yet.
        tracker = _build(style_tracker_warmup=6)
        for _ in range(5):
            tracker.record_turn(_short_reply("yeah", words=8))
        self.assertIsNone(tracker.detect())
        self.assertEqual(tracker.window_size(), 5)

    def test_short_replies_skip_window(self) -> None:
        # A one-word "yeah." reply should not push the window forward
        # -- min word count is 2, so single-word reactions slip past.
        tracker = _build()
        tracker.record_turn("Yeah.")
        tracker.record_turn("Ok.")
        tracker.record_turn("")
        tracker.record_turn("   ")
        self.assertEqual(tracker.window_size(), 0)


# ── opener-rut band ─────────────────────────────────────────────────


class OpenerRutTests(unittest.TestCase):
    def test_count_threshold_triggers(self) -> None:
        # 4 of 6 turns open with "yeah" -> opener_count_threshold=4
        # trips on turn 6 (warmup just satisfied).
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_opener_count_threshold=4,
            # Disable the share trigger so we isolate count-only.
            style_tracker_opener_topk_share=2.0,
        )
        for _ in range(4):
            tracker.record_turn(_short_reply("yeah", words=8))
        # Two unique-opener turns to push window=6 without raising
        # the share trigger.
        tracker.record_turn(_short_reply("ok", words=8))
        tracker.record_turn(_short_reply("hmm", words=8))
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_OPENER_RUT)

    def test_share_threshold_triggers(self) -> None:
        # No single opener hits the 4-count floor (we use 5), but
        # top-2 share covers >= 60% -> the share trigger fires.
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_opener_count_threshold=5,
            style_tracker_opener_topk_share=0.60,
        )
        # Top-2 are "yeah" x3 + "oh" x2 = 5/8 = 62.5% share.
        for opener in ["yeah", "yeah", "yeah", "oh", "oh", "wait", "ok", "hmm"]:
            tracker.record_turn(_short_reply(opener, words=8))
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_OPENER_RUT)

    def test_varied_openers_do_not_trip(self) -> None:
        tracker = _build(style_tracker_warmup=6)
        for opener in [
            "yeah", "oh", "wait", "hmm", "ok", "honestly", "well", "right",
        ]:
            tracker.record_turn(_short_reply(opener, words=8))
        self.assertIsNone(tracker.detect())

    def test_cooldown_silences_repeat(self) -> None:
        # Fire, then with the same rut still active the next several
        # detect calls must stay silent until the cooldown ticks down.
        # cooldown_turns=4 means: fire on T1, silent on T2/T3/T4,
        # eligible again on T5.
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_opener_count_threshold=4,
            style_tracker_cue_cooldown_turns=4,
            style_tracker_opener_topk_share=2.0,
        )
        for _ in range(6):
            tracker.record_turn(_short_reply("yeah", words=8))
        # Turn 1 -- fires.
        first = tracker.detect()
        self.assertIsNotNone(first)
        # Turns 2/3/4 -- still in cooldown even though the rut persists.
        for _ in range(3):
            tracker.record_turn(_short_reply("yeah", words=8))
            self.assertIsNone(tracker.detect())
        # Turn 5 -- cooldown has fully ticked down so the rut fires
        # again on the next eligible detect.
        tracker.record_turn(_short_reply("yeah", words=8))
        second = tracker.detect()
        self.assertIsNotNone(second)


# ── question-saturation band ────────────────────────────────────────


class QuestionSaturationTests(unittest.TestCase):
    def test_question_end_rate_triggers(self) -> None:
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_question_rate_threshold=0.75,
            # Push opener share AND count out of range so the higher
            # priority band can't steal the win.
            style_tracker_opener_count_threshold=99,
            style_tracker_opener_topk_share=2.0,
            style_tracker_avg_questions_threshold=99.0,
        )
        # 7 of 8 replies end on a question (87.5%).
        for opener in ["wait", "hmm", "really", "huh", "ok", "well", "honestly"]:
            tracker.record_turn(_question_reply(opener))
        # One non-question reply.
        tracker.record_turn(_short_reply("right", words=10))
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_QUESTION_SATURATION)

    def test_no_questions_does_not_trip(self) -> None:
        tracker = _build(style_tracker_warmup=6)
        for opener in [
            "yeah", "oh", "wait", "hmm", "ok", "honestly", "well", "right",
        ]:
            tracker.record_turn(_short_reply(opener, words=8))
        self.assertIsNone(tracker.detect())


# ── length-sprawl band ──────────────────────────────────────────────


class LengthSprawlTests(unittest.TestCase):
    def test_avg_words_above_threshold_triggers(self) -> None:
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_length_avg_threshold=50.0,
            # Disable opener and question bands so length wins.
            style_tracker_opener_count_threshold=99,
            style_tracker_opener_topk_share=2.0,
            style_tracker_question_rate_threshold=2.0,
            style_tracker_avg_questions_threshold=99.0,
        )
        # 6 long, varied-opener, statement-only replies (~60 words).
        for _ in range(6):
            tracker.record_turn(_long_reply(words=60))
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_LENGTH_SPRAWL)

    def test_short_replies_do_not_trip(self) -> None:
        tracker = _build(style_tracker_warmup=6)
        for opener in [
            "yeah", "oh", "wait", "hmm", "ok", "honestly",
        ]:
            tracker.record_turn(_short_reply(opener, words=10))
        self.assertIsNone(tracker.detect())


# ── priority + multi-band interaction ───────────────────────────────


class PriorityTests(unittest.TestCase):
    def test_opener_wins_over_question_and_length(self) -> None:
        # Turns satisfy ALL three conditions: same opener, end on
        # questions, runaway length. Opener has highest priority.
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_opener_count_threshold=4,
            style_tracker_question_rate_threshold=0.75,
            style_tracker_length_avg_threshold=30.0,
            style_tracker_avg_questions_threshold=99.0,
            style_tracker_opener_topk_share=2.0,
        )
        for _ in range(6):
            # 30+ words, opens with "yeah", ends with question.
            body = " ".join(["really"] * 32)
            tracker.record_turn(f"Yeah {body} right?")
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_OPENER_RUT)

    def test_question_wins_when_opener_varies(self) -> None:
        # Mixed openers (no rut), but every reply ends on a question
        # AND replies are long. Question saturation wins over length.
        tracker = _build(
            style_tracker_warmup=6,
            style_tracker_opener_count_threshold=99,
            style_tracker_opener_topk_share=2.0,
            style_tracker_question_rate_threshold=0.75,
            style_tracker_avg_questions_threshold=99.0,
            style_tracker_length_avg_threshold=30.0,
        )
        for opener in ["wait", "hmm", "really", "huh", "ok", "well"]:
            body = " ".join(["really"] * 32)
            tracker.record_turn(f"{opener.capitalize()} {body} right?")
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_QUESTION_SATURATION)


# ── settings / disabled-path ────────────────────────────────────────


class SettingsDisabledPathTests(unittest.TestCase):
    def test_no_settings_uses_module_defaults(self) -> None:
        # Construct with no agent_settings stub at all -- module-level
        # defaults must keep the tracker healthy (no AttributeError).
        tracker = AikoStylePatternTracker()
        for _ in range(8):
            tracker.record_turn(_short_reply("yeah", words=10))
        # Default opener_count_threshold=4 -> rut should fire.
        result = tracker.detect()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, BAND_OPENER_RUT)


# ── render_inner_life_block ─────────────────────────────────────────


class RenderTests(unittest.TestCase):
    def test_none_returns_empty_string(self) -> None:
        self.assertEqual(render_inner_life_block(None), "")

    def test_each_band_renders_distinct_copy(self) -> None:
        opener = render_inner_life_block(
            StyleRutResult(
                band=BAND_OPENER_RUT, detail="x", window_size=10,
            )
        )
        question = render_inner_life_block(
            StyleRutResult(
                band=BAND_QUESTION_SATURATION, detail="x", window_size=10,
            )
        )
        length = render_inner_life_block(
            StyleRutResult(
                band=BAND_LENGTH_SPRAWL, detail="x", window_size=10,
            )
        )
        self.assertIn("opened with", opener.lower())
        self.assertIn("question", question.lower())
        self.assertIn("running long", length.lower())
        # Sanity: distinct copies.
        self.assertNotEqual(opener, question)
        self.assertNotEqual(opener, length)
        self.assertNotEqual(question, length)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
