"""Unit tests for the K38 self-correction detector.

Exercises
:func:`app.core.conversation.self_correction_detector.detect_self_correction`
-- the pure, embedding-free function that catches when Aiko's reply
contradicts one of her own high-confidence ``fact`` / ``preference``
memories via a content-word overlap shortlist + the shared F5
contradiction heuristic (:func:`conflict_heuristics.classify_pair`).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.core.conversation.self_correction_detector import (
    SelfCorrectionHit,
    detect_self_correction,
)


@dataclass(frozen=True)
class _Mem:
    id: int
    content: str
    kind: str = "fact"
    confidence: float = 0.8


class ContradictionFoundTests(unittest.TestCase):
    def test_antonym_definite_hit(self) -> None:
        mem = _Mem(id=7, content="I really love hiking in the mountains.",
                   kind="preference", confidence=0.85)
        reply = "Honestly, these days I actually hate hiking in the mountains."
        hit = detect_self_correction(reply, [mem])
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.memory_id, 7)
        self.assertEqual(hit.label, "definite")

    def test_no_contradiction_returns_none(self) -> None:
        mem = _Mem(id=1, content="I really love hiking in the mountains.",
                   kind="preference")
        reply = "I had a great day at the park with friends today."
        self.assertIsNone(detect_self_correction(reply, [mem]))

    def test_empty_reply_returns_none(self) -> None:
        mem = _Mem(id=1, content="I love coffee a lot.")
        self.assertIsNone(detect_self_correction("", [mem]))

    def test_no_memories_returns_none(self) -> None:
        self.assertIsNone(
            detect_self_correction("I hate coffee and tea both.", [])
        )


class ConfidenceGateTests(unittest.TestCase):
    def test_low_confidence_memory_excluded(self) -> None:
        mem = _Mem(id=3, content="I really love hiking in the mountains.",
                   kind="preference", confidence=0.4)
        reply = "These days I actually hate hiking in the mountains."
        # Below the default 0.6 floor -> not a candidate.
        self.assertIsNone(detect_self_correction(reply, [mem]))

    def test_custom_min_confidence(self) -> None:
        mem = _Mem(id=3, content="I really love hiking in the mountains.",
                   kind="preference", confidence=0.4)
        reply = "These days I actually hate hiking in the mountains."
        hit = detect_self_correction(reply, [mem], min_confidence=0.3)
        self.assertIsNotNone(hit)


class KindAllowListTests(unittest.TestCase):
    def test_reflection_kind_ignored(self) -> None:
        mem = _Mem(id=9, content="I really love hiking in the mountains.",
                   kind="reflection", confidence=0.9)
        reply = "These days I actually hate hiking in the mountains."
        self.assertIsNone(detect_self_correction(reply, [mem]))

    def test_goal_kind_ignored(self) -> None:
        mem = _Mem(id=10, content="I really love hiking in the mountains.",
                   kind="goal", confidence=0.9)
        reply = "These days I actually hate hiking in the mountains."
        self.assertIsNone(detect_self_correction(reply, [mem]))


class OverlapShortlistTests(unittest.TestCase):
    def test_insufficient_overlap_skips(self) -> None:
        # "I love coffee" vs "I hate coffee" share only {coffee} == 1.
        mem = _Mem(id=2, content="I love coffee.", kind="preference")
        reply = "Actually I hate coffee."
        self.assertIsNone(
            detect_self_correction(reply, [mem], min_overlap=2)
        )

    def test_overlap_one_allows_hit(self) -> None:
        mem = _Mem(id=2, content="I love coffee.", kind="preference")
        reply = "Actually I hate coffee."
        hit = detect_self_correction(reply, [mem], min_overlap=1)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.label, "definite")


class StrongestHitTests(unittest.TestCase):
    def test_definite_outranks_borderline(self) -> None:
        definite_mem = _Mem(
            id=100,
            content="I really love hiking in the mountains.",
            kind="preference",
        )
        borderline_mem = _Mem(
            id=200,
            content="I own 2 dogs at my home.",
            kind="fact",
        )
        reply = (
            "These days I actually hate hiking in the mountains. "
            "I own 5 dogs at my home now."
        )
        hit = detect_self_correction(reply, [borderline_mem, definite_mem])
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.memory_id, 100)
        self.assertEqual(hit.label, "definite")

    def test_borderline_when_only_number_mismatch(self) -> None:
        mem = _Mem(id=200, content="I own 2 dogs at my home.", kind="fact")
        reply = "I own 5 dogs at my home now."
        hit = detect_self_correction(reply, [mem])
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.label, "borderline")


class HitShapeTests(unittest.TestCase):
    def test_hit_fields_populated(self) -> None:
        mem = _Mem(id=42, content="I love spicy food a lot.",
                   kind="preference")
        reply = "I really hate spicy food, to be honest."
        hit = detect_self_correction(reply, [mem])
        self.assertIsInstance(hit, SelfCorrectionHit)
        assert hit is not None
        self.assertEqual(hit.memory_id, 42)
        self.assertEqual(hit.memory_content, "I love spicy food a lot.")
        self.assertTrue(hit.reply_snippet)
        self.assertGreaterEqual(hit.overlap, 2)


if __name__ == "__main__":
    unittest.main()
