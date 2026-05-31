"""Tests for the AffectState updater + ambient block formatter."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.affect.affect_state import (
    AffectState,
    AffectStore,
    AffectUpdater,
    render_ambient_block,
)
from app.core.infra.chat_database import ChatDatabase


class _TempDb:
    def __enter__(self) -> ChatDatabase:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = ChatDatabase(Path(self._tmp.name) / "test.db")
        return self.db

    def __exit__(self, *exc) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self._tmp.cleanup()
        except Exception:
            pass


class AffectStoreTests(unittest.TestCase):
    def test_get_returns_default_when_missing(self) -> None:
        with _TempDb() as db:
            store = AffectStore(db)
            state = store.get("u1")
            self.assertEqual(state.user_id, "u1")
            self.assertEqual(state.mood_label, "content")
            self.assertAlmostEqual(state.valence, 0.0)
            self.assertAlmostEqual(state.arousal, 0.4)

    def test_save_then_get_roundtrips(self) -> None:
        with _TempDb() as db:
            store = AffectStore(db)
            state = AffectState(
                user_id="u1",
                valence=0.4,
                arousal=0.7,
                mood_label="warm",
                mood_intensity=0.8,
                valence_trend_24h=0.2,
                arousal_trend_24h=-0.1,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            store.save(state)
            roundtripped = store.get("u1")
            self.assertAlmostEqual(roundtripped.valence, 0.4)
            self.assertAlmostEqual(roundtripped.arousal, 0.7)
            self.assertEqual(roundtripped.mood_label, "warm")


class AffectUpdaterTests(unittest.TestCase):
    def test_excited_reaction_pushes_valence_up(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            state = updater.apply_turn(
                "u1", reaction="excited", user_text="that was awesome",
            )
            self.assertGreater(state.valence, 0.0)
            self.assertGreater(state.arousal, 0.4)

    def test_sad_reaction_pushes_valence_down(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            state = updater.apply_turn(
                "u1", reaction="sad", user_text="i'm tired",
            )
            self.assertLess(state.valence, 0.0)

    def test_user_hint_negative_compounds_with_reaction(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            base_state = updater.apply_turn(
                "u1", reaction="neutral", user_text="hi",
            )
            db2_state = updater.apply_turn(
                "u1", reaction="neutral", user_text="i'm so tired and stressed",
            )
            self.assertLess(db2_state.valence, base_state.valence)

    def test_mood_label_updates_with_state(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            for _ in range(6):
                state = updater.apply_turn(
                    "u1", reaction="excited", user_text="amazing",
                )
            # Sustained positive arousal/valence should land us in 'playful'
            # or 'curious' or 'warm' — the exact label depends on the
            # arousal magnitude reached after smoothing.
            self.assertIn(
                state.mood_label,
                {"playful", "curious", "warm", "tender"},
            )

    def test_decay_pulls_toward_baseline(self) -> None:
        with _TempDb() as db:
            store = AffectStore(db)
            # Pre-stash a high-valence state with a stale timestamp so the
            # decay path actually has time to act.
            stale_ts = (
                datetime.now(timezone.utc) - timedelta(minutes=120)
            ).isoformat()
            state = AffectState(
                user_id="u1",
                valence=0.8,
                arousal=0.8,
                baseline_valence=0.0,
                baseline_arousal=0.4,
                mood_label="playful",
                mood_intensity=0.8,
                updated_at=stale_ts,
            )
            store.save(state)
            updater = AffectUpdater(store)
            # neutral turn -> decay dominates the impulse
            new_state = updater.apply_turn(
                "u1", reaction="neutral", user_text="",
            )
            self.assertLess(new_state.valence, 0.6)
            self.assertLess(new_state.arousal, 0.7)


class AmbientBlockTests(unittest.TestCase):
    def test_block_includes_label_and_numbers(self) -> None:
        state = AffectState(
            user_id="u1",
            valence=0.4,
            arousal=0.3,
            mood_label="tender",
            valence_trend_24h=0.0,
        )
        text = render_ambient_block(state)
        self.assertIn("tender", text)
        self.assertIn("+0.40", text)
        self.assertIn("0.30", text)

    def test_trend_line_omitted_below_threshold(self) -> None:
        state = AffectState(
            user_id="u1",
            valence=0.3,
            arousal=0.3,
            mood_label="warm",
            valence_trend_24h=0.05,  # below default threshold 0.15
        )
        text = render_ambient_block(state)
        self.assertNotIn("Lately", text)

    def test_trend_line_present_above_threshold(self) -> None:
        state = AffectState(
            user_id="u1",
            valence=0.3,
            arousal=0.3,
            mood_label="warm",
            valence_trend_24h=0.25,
        )
        text = render_ambient_block(state)
        self.assertIn("Lately", text)


if __name__ == "__main__":
    unittest.main()
