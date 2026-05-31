"""Tests for the regex backchannel classifier and gate."""
from __future__ import annotations

import unittest

from app.core.conversation.backchannel_classifier import (
    BackchannelGate,
    classify,
)


class ClassifyTests(unittest.TestCase):
    def test_amusement_haha(self) -> None:
        self.assertEqual(classify("haha that's funny"), "amusement")
        self.assertEqual(classify("hahaha"), "amusement")
        self.assertEqual(classify("lol"), "amusement")

    def test_surprise_words(self) -> None:
        self.assertEqual(classify("wow that's amazing"), "surprise")
        self.assertEqual(classify("really? are you sure"), "surprise")
        # "really?" alone (with question mark) is also surprise
        self.assertEqual(classify("really?"), "surprise")

    def test_agreement_words(self) -> None:
        self.assertEqual(classify("yeah I think so"), "agreement")
        self.assertEqual(classify("totally agree"), "agreement")
        # "exactly" is high-confidence agreement
        self.assertEqual(classify("exactly what I mean"), "agreement")

    def test_disagreement(self) -> None:
        self.assertEqual(classify("I don't agree"), "disagreement")
        self.assertEqual(classify("not really"), "disagreement")

    def test_concern(self) -> None:
        self.assertEqual(classify("I'm so tired today"), "concern")
        self.assertEqual(classify("that's terrible"), "concern")

    def test_confused(self) -> None:
        self.assertEqual(classify("wait what"), "confused")
        self.assertEqual(classify("I don't understand"), "confused")
        self.assertEqual(classify("huh?"), "confused")

    def test_thinking(self) -> None:
        self.assertEqual(classify("hmm let me think"), "thinking")
        self.assertEqual(classify("um"), "thinking")
        self.assertEqual(classify("uhhh"), "thinking")

    def test_returns_none_for_neutral(self) -> None:
        self.assertIsNone(classify("the weather today is sunny"))
        self.assertIsNone(classify(""))
        # Check tail-window: a hint at the head should be ignored if the
        # transcription has moved past it.
        long_text = "haha " + ("plain talk " * 30)
        self.assertIsNone(classify(long_text))

    def test_tail_window_picks_recent(self) -> None:
        # The recent tail (~60 chars) wins.
        text = "I was just talking about the project and exactly that"
        self.assertEqual(classify(text), "agreement")


class BackchannelGateTests(unittest.TestCase):
    def test_emits_first_hint(self) -> None:
        gate = BackchannelGate(min_repeat_seconds=1.0)
        result = gate.consider("yeah totally", now=0.0)
        self.assertEqual(result, "agreement")

    def test_rate_limits_repeats(self) -> None:
        gate = BackchannelGate(min_repeat_seconds=2.0)
        gate.consider("yeah", now=0.0)
        # Same hint within rate-limit window: skipped.
        result = gate.consider("yeah totally", now=0.5)
        self.assertIsNone(result)
        # Past the window: re-emitted.
        result = gate.consider("yeah", now=2.5)
        self.assertEqual(result, "agreement")

    def test_different_hint_emits_immediately(self) -> None:
        gate = BackchannelGate(min_repeat_seconds=10.0)
        gate.consider("yeah", now=0.0)
        # Different hint fires even mid-window.
        result = gate.consider("wow that's surprising", now=0.5)
        self.assertEqual(result, "surprise")

    def test_no_hint_returns_none(self) -> None:
        gate = BackchannelGate()
        self.assertIsNone(gate.consider("the weather is mild", now=0.0))


if __name__ == "__main__":
    unittest.main()
