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
    STATUS_CONFIRMED,
    STATUS_CONTRADICTED,
    STATUS_STALE,
)
from app.core.infra.chat_database import ChatDatabase


def _build() -> tuple[BeliefStore, BeliefGapDetector]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    store = BeliefStore(db)
    detector = BeliefGapDetector(belief_store=store)
    return store, detector


class MoodGapTests(unittest.TestCase):
    def test_large_valence_drift_flags_gap(self) -> None:
        store, detector = _build()
        b = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.85,
            valence=0.5, arousal=0.7,
        )
        assert b is not None
        affect = AffectState(
            user_id="u1", valence=-0.4, arousal=0.3, mood_label="melancholy",
        )
        gaps = detector.detect(user_id="u1", affect=affect)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].kind, KIND_MOOD)
        self.assertEqual(gaps[0].topic, "tokyo trip")
        self.assertEqual(gaps[0].observed, "melancholy")
        self.assertEqual(store.get(b.id).status, STATUS_CONTRADICTED)

    def test_small_drift_does_not_flag(self) -> None:
        store, detector = _build()
        b = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.85,
            valence=0.5, arousal=0.7,
        )
        assert b is not None
        affect = AffectState(
            user_id="u1", valence=0.45, arousal=0.65,
            mood_label="playful",
        )
        gaps = detector.detect(user_id="u1", affect=affect)
        self.assertEqual(gaps, [])
        # The row was stamp_checked but still active.
        row = store.get(b.id)
        self.assertEqual(row.status, STATUS_ACTIVE)
        self.assertIsNotNone(row.last_checked_at)

    def test_valence_band_flip_flags_gap(self) -> None:
        store, detector = _build()
        # Predicted: positive valence belief. Observed: negative band.
        # Even though val_diff = 0.31 (just over default threshold),
        # band flip alone should trigger.
        b = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="rust language",
            predicted_state="excited", confidence=0.8,
            valence=0.15, arousal=0.5,
        )
        assert b is not None
        affect = AffectState(
            user_id="u1", valence=-0.20, arousal=0.5,
            mood_label="melancholy",
        )
        gaps = detector.detect(user_id="u1", affect=affect)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(store.get(b.id).status, STATUS_CONTRADICTED)


class OpinionGapTests(unittest.TestCase):
    def test_opinion_contradicted_by_user_message(self) -> None:
        store, detector = _build()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="spicy food",
            predicted_state="loves spicy food",
        )
        assert b is not None
        gaps = detector.detect(
            user_id="u1", affect=None,
            recent_user_message="honestly i hate spicy food deeply",
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].kind, KIND_OPINION)
        self.assertEqual(store.get(b.id).status, STATUS_CONTRADICTED)

    def test_opinion_confirmed_by_strong_overlap(self) -> None:
        store, detector = _build()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust language",
            predicted_state="rust language is overhyped",
        )
        assert b is not None
        # Same content words with no contradiction signal -> confirmed.
        gaps = detector.detect(
            user_id="u1", affect=None,
            recent_user_message="rust language overhyped",
        )
        self.assertEqual(gaps, [])
        self.assertEqual(store.get(b.id).status, STATUS_CONFIRMED)

    def test_no_signal_keeps_active(self) -> None:
        store, detector = _build()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust language",
            predicted_state="overhyped",
        )
        assert b is not None
        gaps = detector.detect(
            user_id="u1", affect=None,
            recent_user_message="i think i will try a new restaurant tonight",
        )
        self.assertEqual(gaps, [])
        # Stays active because there was no contradiction or overlap.
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)


class StaleSweepTests(unittest.TestCase):
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
