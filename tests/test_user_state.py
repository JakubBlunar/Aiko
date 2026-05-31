"""Tests for the heuristic UserStateEstimator + UserStateStore (Phase 3a)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.affect.user_state import (
    UserStateEstimator,
    UserStateNow,
    UserStateStore,
)


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = UserStateStore(self.db)
        self.estimator = UserStateEstimator(self.store)

    def close(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class UserStateStoreTests(unittest.TestCase):
    def test_get_returns_default_for_unknown(self):
        f = _Fixture()
        try:
            state = f.store.get("nobody")
            self.assertEqual(state.perceived_mood, "unknown")
            self.assertEqual(state.last_topic, "")
        finally:
            f.close()

    def test_upsert_round_trip(self):
        f = _Fixture()
        try:
            seed = UserStateNow(
                user_id="u1",
                perceived_mood="high",
                perceived_energy="high",
                perceived_focus="working",
                last_topic="building the prefetcher",
            )
            f.store.upsert(seed)
            got = f.store.get("u1")
            self.assertEqual(got.perceived_mood, "high")
            self.assertEqual(got.last_topic, "building the prefetcher")
            # Upsert again -> merge.
            seed2 = UserStateNow(
                user_id="u1",
                perceived_mood="low",
                perceived_energy="low",
                perceived_focus="asking",
                last_topic="why is everything broken",
            )
            f.store.upsert(seed2)
            got2 = f.store.get("u1")
            self.assertEqual(got2.perceived_mood, "low")
            self.assertEqual(got2.last_topic, "why is everything broken")
        finally:
            f.close()

    def test_render_block_omits_unknown(self):
        f = _Fixture()
        try:
            f.store.upsert(UserStateNow(user_id="u2", perceived_mood="high"))
            block = f.store.render_block("u2")
            self.assertIn("mood reads as high", block)
            self.assertNotIn("energy", block)
            self.assertNotIn("focus", block)
        finally:
            f.close()

    def test_render_block_empty_when_all_unknown(self):
        f = _Fixture()
        try:
            self.assertEqual(f.store.render_block("u3"), "")
        finally:
            f.close()


class UserStateEstimatorTests(unittest.TestCase):
    def test_detects_negative_mood_first(self):
        f = _Fixture()
        try:
            state = f.estimator.estimate("u4", user_text="not great today, kinda tired")
            self.assertEqual(state.perceived_mood, "low")
            self.assertEqual(state.perceived_energy, "low")
        finally:
            f.close()

    def test_detects_positive_mood(self):
        f = _Fixture()
        try:
            state = f.estimator.estimate("u5", user_text="I'm feeling great about this project!")
            self.assertEqual(state.perceived_mood, "high")
            self.assertEqual(state.perceived_energy, "high")
        finally:
            f.close()

    def test_focus_question(self):
        f = _Fixture()
        try:
            state = f.estimator.estimate("u6", user_text="What time is the meeting?")
            self.assertEqual(state.perceived_focus, "asking")
        finally:
            f.close()

    def test_focus_task(self):
        f = _Fixture()
        try:
            state = f.estimator.estimate(
                "u7", user_text="I need to debug this assertion error",
            )
            self.assertEqual(state.perceived_focus, "working")
        finally:
            f.close()

    def test_short_text_low_energy(self):
        f = _Fixture()
        try:
            state = f.estimator.estimate("u8", user_text="ok")
            self.assertEqual(state.perceived_energy, "low")
        finally:
            f.close()

    def test_topic_truncates(self):
        f = _Fixture()
        try:
            text = "We were discussing the new feature for context window squishing."
            state = f.estimator.estimate("u9", user_text=text)
            self.assertTrue(state.last_topic.startswith("We were discussing"))
            self.assertTrue(len(state.last_topic) <= 80)
        finally:
            f.close()

    def test_apply_turn_persists(self):
        f = _Fixture()
        try:
            f.estimator.apply_turn("u10", user_text="I'm super excited about this!")
            stored = f.store.get("u10")
            self.assertEqual(stored.perceived_mood, "high")
        finally:
            f.close()

    def test_blank_text_returns_previous(self):
        f = _Fixture()
        try:
            f.store.upsert(UserStateNow(user_id="u11", perceived_mood="high"))
            state = f.estimator.estimate("u11", user_text="")
            self.assertEqual(state.perceived_mood, "high")
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
