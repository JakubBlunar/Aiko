"""Tests for :mod:`app.core.relationship.belief_gap_detector`."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.affect.affect_state import AffectState
from app.core.relationship.belief_gap_detector import (
    BeliefGap,
    BeliefGapDetector,
    render_inner_life_block,
)
from app.core.relationship.belief_store import (
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
    STATUS_ACTIVE,
    STATUS_STALE,
)
from app.core.infra.chat_database import ChatDatabase


def _build() -> tuple[BeliefStore, BeliefGapDetector]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    store = BeliefStore(db)
    detector = BeliefGapDetector(belief_store=store)
    return store, detector


class EvidenceSafetyTests(unittest.TestCase):
    def test_aikos_emotion_cannot_contradict_user_readings(self):
        store, detector = _build()
        b = store.upsert(user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
                         predicted_state="excited", valence=0.9, arousal=0.8)
        affect = AffectState(user_id="u1", valence=-0.8, arousal=0.1)
        self.assertEqual(detector.detect(user_id="u1", affect=affect,
                                        recent_user_message="Tokyo trip"), [])
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)
        self.assertEqual(store.list_outcomes(user_id="u1"), [])

    def test_related_words_and_antonyms_are_not_semantic_verification(self):
        store, detector = _build()
        b = store.upsert(user_id="u1", kind=KIND_OPINION, topic="rust",
                         predicted_state="overhyped")
        for text in ("Rust is overhyped", "Rust is not overhyped", "They said Rust is overhyped"):
            self.assertEqual(detector.detect(user_id="u1", recent_user_message=text), [])
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)
        self.assertEqual(store.list_outcomes(user_id="u1"), [])

    def test_unrelated_checks_do_not_refresh_support(self):
        store, detector = _build()
        b = store.upsert(user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
                         predicted_state="excited", valence=0.8)
        detector.detect(user_id="u1", recent_user_message="The commute was slow")
        row = store.get(b.id)
        self.assertEqual(row.observed_at, b.observed_at)
        self.assertIsNone(row.last_checked_at)

    def test_stale_sweep_runs(self) -> None:
        store, detector = _build()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="ancient belief",
            predicted_state="x",
            observed_at="1990-01-01T00:00:00+00:00",
        )
        assert b is not None
        # No affect, no user message -> only the stale sweep runs.
        detector.detect(user_id="u1")
        self.assertEqual(store.get(b.id).status, STATUS_STALE)


class RenderTests(unittest.TestCase):
    def test_render_inner_life_block_caps_at_two(self) -> None:
        gaps = [
            BeliefGap(
                belief_id=1, kind=KIND_MOOD, topic="tokyo trip",
                predicted_state="excited", confidence=0.8,
                reason="r", observed="melancholy",
            ),
            BeliefGap(
                belief_id=2, kind=KIND_OPINION, topic="rust",
                predicted_state="overhyped", confidence=0.6,
                reason="r",
            ),
            BeliefGap(
                belief_id=3, kind=KIND_OPINION, topic="python",
                predicted_state="elegant", confidence=0.6,
                reason="r",
            ),
        ]
        block = render_inner_life_block(gaps)
        self.assertEqual(block.count("\n"), 1)  # exactly two lines
        self.assertIn("tokyo trip", block)
        self.assertIn("melancholy", block)
        self.assertIn("rust", block)

    def test_render_inner_life_block_empty(self) -> None:
        self.assertEqual(render_inner_life_block([]), "")


if __name__ == "__main__":
    unittest.main()
