"""Tests for cadence / prosody dispatcher (Phase 5b)."""
from __future__ import annotations

import random
import unittest

from app.core.cadence import (
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


if __name__ == "__main__":
    unittest.main()
