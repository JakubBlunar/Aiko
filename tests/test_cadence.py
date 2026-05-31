"""Tests for cadence / prosody dispatcher (Phase 5b)."""
from __future__ import annotations

import random
import unittest

from app.core.voice.cadence import (
    CadenceContext,
    ProsodyDispatcher,
    ProsodyParams,
    _apply_text_pauses,
    analyze_sentence,
    derive_sentence_reaction,
)


class DeriveSentenceReactionTests(unittest.TestCase):
    def test_surprise(self):
        self.assertEqual(derive_sentence_reaction("Oh! that's wild", "calm"), "surprised")

    def test_exclamation_alone_surprises(self):
        self.assertEqual(derive_sentence_reaction("That's huge!", "neutral"), "surprised")

    def test_laugh(self):
        self.assertEqual(derive_sentence_reaction("haha that's good", "neutral"), "amused")

    def test_sad(self):
        self.assertEqual(derive_sentence_reaction("I'm sorry to hear that", "calm"), "concerned")

    def test_ellipsis(self):
        self.assertEqual(derive_sentence_reaction("yeah, well…", "calm"), "wistful")

    def test_thoughtful(self):
        self.assertEqual(derive_sentence_reaction("hmm, interesting", "calm"), "thoughtful")

    def test_question_with_neutral_carrier_curious(self):
        self.assertEqual(derive_sentence_reaction("what do you think?", "neutral"), "curious")

    def test_question_keeps_strong_carrier(self):
        self.assertEqual(derive_sentence_reaction("oh really?", "amused"), "surprised")

    def test_falls_back_to_carrier(self):
        self.assertEqual(derive_sentence_reaction("a normal sentence", "warm"), "warm")

    def test_empty_returns_neutral(self):
        self.assertEqual(derive_sentence_reaction("", ""), "neutral")


class AnalyzeSentenceTests(unittest.TestCase):
    def test_long_sentence_pauses_more(self):
        ctx = CadenceContext(base_reaction="neutral", rng=random.Random(0))
        long = "This is a fairly meandering sentence " * 5
        params = analyze_sentence(long, ctx)
        self.assertGreaterEqual(params.pause_after_ms, 220)

    def test_short_sentence_pauses_less(self):
        ctx = CadenceContext(base_reaction="neutral", rng=random.Random(0))
        params = analyze_sentence("Yes.", ctx)
        self.assertLessEqual(params.pause_after_ms, 100)

    def test_ellipsis_extends_pause(self):
        ctx = CadenceContext(base_reaction="neutral", rng=random.Random(0))
        params = analyze_sentence("Yeah…", ctx)
        self.assertGreaterEqual(params.pause_after_ms, 380)

    def test_question_extends_pause(self):
        ctx = CadenceContext(base_reaction="neutral", rng=random.Random(0))
        params = analyze_sentence("Are you alright?", ctx)
        self.assertGreaterEqual(params.pause_after_ms, 260)

    def test_drowsy_stretches_pause(self):
        rng = random.Random(0)
        ctx_normal = CadenceContext(base_reaction="neutral", rng=rng)
        ctx_drowsy = CadenceContext(
            base_reaction="neutral",
            rng=random.Random(0),
            circadian_drowsy=True,
        )
        text = "This is a sentence about a moderately interesting thing happening overall today."
        normal = analyze_sentence(text, ctx_normal)
        drowsy = analyze_sentence(text, ctx_drowsy)
        self.assertGreater(drowsy.pause_after_ms, normal.pause_after_ms)

    def test_restless_shortens_pause(self):
        ctx_neutral = CadenceContext(base_reaction="neutral", rng=random.Random(0))
        ctx_restless = CadenceContext(
            base_reaction="neutral",
            mood_label="restless",
            mood_arousal=0.9,
            rng=random.Random(0),
        )
        text = "Here is a fairly long-ish sentence that should normally breathe a tiny bit."
        normal = analyze_sentence(text, ctx_neutral)
        restless = analyze_sentence(text, ctx_restless)
        self.assertLess(restless.pause_after_ms, normal.pause_after_ms)

    def test_speed_hint_thoughtful_slower(self):
        ctx = CadenceContext(base_reaction="thoughtful", rng=random.Random(0))
        params = analyze_sentence("hmm, let me think", ctx)
        self.assertLess(params.speed_hint, 1.0)

    def test_empty_returns_passthrough(self):
        ctx = CadenceContext(base_reaction="neutral", rng=random.Random(0))
        params = analyze_sentence("", ctx)
        self.assertEqual(params.reaction, "neutral")

    def test_prefix_for_drowsy(self):
        # Force the RNG below the 0.10 threshold so we deterministically
        # see the "Mm." prefix.
        rng = random.Random()
        rng.random = lambda: 0.01  # type: ignore[assignment]
        ctx = CadenceContext(
            base_reaction="calm",
            mood_label="tired",
            circadian_drowsy=True,
            rng=rng,
        )
        params = analyze_sentence("A normal sentence", ctx)
        self.assertEqual(params.prefix_text, "Mm.")


class ApplyTextPausesTests(unittest.TestCase):
    def test_long_pause_appends_ellipsis(self):
        params = ProsodyParams(reaction="neutral", pause_after_ms=400)
        out = _apply_text_pauses("Yeah", params)
        self.assertTrue(out.endswith("…"))

    def test_medium_pause_appends_period(self):
        params = ProsodyParams(reaction="neutral", pause_after_ms=220)
        out = _apply_text_pauses("Maybe so", params)
        self.assertTrue(out.endswith("."))

    def test_keeps_existing_terminal_punct(self):
        params = ProsodyParams(reaction="neutral", pause_after_ms=400)
        out = _apply_text_pauses("Are you okay?", params)
        self.assertTrue(out.endswith("?"))

    def test_pause_before_inserts_dash(self):
        params = ProsodyParams(reaction="neutral", pause_before_ms=120)
        out = _apply_text_pauses("oh, by the way", params)
        self.assertTrue(out.startswith("— "))


class ProsodyDispatcherTests(unittest.TestCase):
    def _dispatcher(self, **kwargs):
        sent: list[tuple[str, str | None]] = []

        def enqueue(text: str, reaction: str | None = None):
            sent.append((text, reaction))

        d = ProsodyDispatcher(enqueue, rng=random.Random(0), **kwargs)
        return d, sent

    def test_passes_through_when_disabled(self):
        d, sent = self._dispatcher(enabled=False)
        d.dispatch("hello world", "warm")
        self.assertEqual(sent, [("hello world", "warm")])
        self.assertEqual(d.stats()["chunks"], 0)

    def test_dispatches_with_reaction_change(self):
        d, sent = self._dispatcher()
        d.dispatch("Oh! That's wild", "neutral")
        # Carrier reaction was changed to "surprised".
        self.assertTrue(any(r == "surprised" for _, r in sent))
        self.assertEqual(d.stats()["chunks"], 1)
        self.assertEqual(d.stats()["reactions_changed"], 1)

    def test_empty_text_skipped(self):
        d, sent = self._dispatcher()
        d.dispatch("   ", "warm")
        self.assertEqual(sent, [])

    def test_emits_prefix_chunk(self):
        d, sent = self._dispatcher()
        # Stateful RNG that always rolls below the prefix threshold.
        shared_rng = random.Random()
        shared_rng.random = lambda: 0.01  # type: ignore[assignment]

        def context_provider() -> CadenceContext:
            return CadenceContext(
                base_reaction="calm",
                mood_label="tired",
                circadian_drowsy=True,
                rng=shared_rng,
            )

        d.set_context_provider(context_provider)
        d.dispatch("sentence number one talking about stuff", "calm")
        prefixes = [t for t, _ in sent if t in ("Mm.", "Oh,", "Yeah,")]
        self.assertEqual(prefixes, ["Mm."])
        self.assertEqual(d.stats()["prefixes"], 1)

    def test_context_provider_failure_falls_back(self):
        d, sent = self._dispatcher()

        def boom() -> CadenceContext:
            raise RuntimeError("provider broken")

        d.set_context_provider(boom)
        d.dispatch("hello", "warm")
        self.assertEqual(len(sent), 1)
        self.assertEqual(d.stats()["chunks"], 1)

    def test_analyze_does_not_emit(self):
        d, sent = self._dispatcher()
        d.analyze("Are you okay?", "neutral")
        self.assertEqual(sent, [])

    def test_speed_passthrough_when_enqueue_supports_it(self):
        """Phase 1b: when the enqueue callable accepts ``speed=``, the
        per-sentence speed_hint must actually flow through."""
        sent: list[tuple[str, str | None, float]] = []

        def enqueue(text: str, reaction: str | None = None, speed: float = 1.0):
            sent.append((text, reaction, speed))

        d = ProsodyDispatcher(enqueue, rng=random.Random(0))

        def context_provider() -> CadenceContext:
            return CadenceContext(
                base_reaction="thoughtful",
                mood_label="tired",
                circadian_drowsy=True,
                rng=random.Random(0),
            )

        d.set_context_provider(context_provider)
        d.dispatch("Let me think about that for a moment.", "thoughtful")
        # All emitted chunks should carry a speed below 1.0 because the
        # cadence layer slows thoughtful + drowsy speech.
        self.assertTrue(sent)
        for _text, _reaction, speed in sent:
            self.assertLess(speed, 1.0)

    def test_speed_omitted_when_enqueue_is_legacy_two_arg(self):
        """Legacy two-arg enqueue callables (no ``speed`` kwarg) must
        still work — TypeError fallback drops the speed silently."""
        legacy_calls: list[tuple[str, str | None]] = []

        def legacy_enqueue(text: str, reaction: str | None = None):
            legacy_calls.append((text, reaction))

        d = ProsodyDispatcher(legacy_enqueue, rng=random.Random(0))
        d.dispatch("Hello there.", "warm")
        self.assertEqual(len(legacy_calls), 1)


if __name__ == "__main__":
    unittest.main()
