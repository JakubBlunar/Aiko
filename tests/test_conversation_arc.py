"""Tests for conversation arc tracker (Phase 4c)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.chat_database import ChatDatabase
from app.core.conversation_arc import (
    ArcEstimator,
    ArcSmootherWorker,
    ArcStore,
    VALID_ARCS,
    _format_smooth_block,
    _parse_smooth_output,
)


class _FakeOllama:
    def __init__(self, response: str = '{"arc":"casual_check_in","confidence":0.6}'):
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = ArcStore(self.db)

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


class ArcStoreTests(unittest.TestCase):
    def test_default_when_missing(self):
        f = _Fixture()
        try:
            self.assertIsNone(f.store.get("u1"))
            default = f.store.get_or_default("u1")
            self.assertEqual(default.arc, "casual_check_in")
            self.assertEqual(default.confidence, 0.5)
        finally:
            f.close()

    def test_upsert_and_read_back(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", arc="support", since_turn=5, confidence=0.85)
            state = f.store.get("u1")
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.arc, "support")
            self.assertEqual(state.since_turn, 5)
            self.assertAlmostEqual(state.confidence, 0.85, places=2)
        finally:
            f.close()

    def test_invalid_arc_falls_back(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", arc="not_valid", since_turn=0, confidence=0.5)
            self.assertEqual(f.store.get("u1").arc, "casual_check_in")
        finally:
            f.close()

    def test_render_block_only_when_interesting(self):
        f = _Fixture()
        try:
            self.assertEqual(f.store.render_block("u1"), "")
            f.store.upsert("u1", arc="casual_check_in", since_turn=0, confidence=0.4)
            self.assertEqual(f.store.render_block("u1"), "")
            f.store.upsert("u1", arc="support", since_turn=3, confidence=0.85)
            block = f.store.render_block("u1", current_turn=10)
            self.assertIn("vent", block.lower())
            self.assertIn("turn", block.lower())
        finally:
            f.close()


class ArcEstimatorTests(unittest.TestCase):
    def _est(self, store: ArcStore) -> ArcEstimator:
        return ArcEstimator(store, sticky_confidence=0.85)

    def test_support_signal(self):
        f = _Fixture()
        try:
            est = self._est(f.store)
            state = est.apply_turn(
                "u1",
                user_text="I feel exhausted today, total burnout",
                current_turn=2,
            )
            self.assertEqual(state.arc, "support")
            self.assertGreaterEqual(state.confidence, 0.5)
            self.assertLess(state.confidence, 0.85)
            self.assertEqual(state.since_turn, 2)
        finally:
            f.close()

    def test_planning_signal(self):
        f = _Fixture()
        try:
            est = self._est(f.store)
            state = est.apply_turn(
                "u1", user_text="Let's plan the launch step by step", current_turn=1,
            )
            self.assertEqual(state.arc, "planning")
        finally:
            f.close()

    def test_silly_signal(self):
        f = _Fixture()
        try:
            est = self._est(f.store)
            state = est.apply_turn(
                "u1",
                user_text="imagine if cats ran the post office, that would be hilarious",
                current_turn=4,
            )
            self.assertEqual(state.arc, "silly")
        finally:
            f.close()

    def test_no_signal_decays_confidence(self):
        f = _Fixture()
        try:
            est = ArcEstimator(f.store, decay_per_turn=0.1)
            f.store.upsert("u1", arc="planning", since_turn=2, confidence=0.7)
            state = est.apply_turn("u1", user_text="okay sounds good", current_turn=5)
            self.assertEqual(state.arc, "planning")  # unchanged
            self.assertLess(state.confidence, 0.7)
        finally:
            f.close()

    def test_sticky_priors_resist_weak_change(self):
        f = _Fixture()
        try:
            est = ArcEstimator(f.store, sticky_confidence=0.8)
            f.store.upsert("u1", arc="support", since_turn=2, confidence=0.95)
            # weak playful hit shouldn't override sticky support
            state = est.apply_turn(
                "u1",
                user_text="lol that's hilarious",
                current_turn=5,
            )
            self.assertEqual(state.arc, "support")
        finally:
            f.close()

    def test_same_arc_bumps_confidence(self):
        f = _Fixture()
        try:
            est = self._est(f.store)
            first = est.apply_turn(
                "u1",
                user_text="i've been thinking about it a lot lately",
                current_turn=1,
            )
            after = est.apply_turn(
                "u1",
                user_text="looking back, i realized something",
                current_turn=2,
            )
            self.assertEqual(first.arc, "reflection")
            self.assertEqual(after.arc, "reflection")
            self.assertGreaterEqual(after.confidence, first.confidence)
        finally:
            f.close()


class ParseSmoothOutputTests(unittest.TestCase):
    def test_plain_json(self):
        out = _parse_smooth_output('{"arc":"silly","confidence":0.7}')
        self.assertEqual(out, ("silly", 0.7))

    def test_fenced_json(self):
        raw = "```json\n{\"arc\":\"support\",\"confidence\":0.9}\n```"
        out = _parse_smooth_output(raw)
        self.assertEqual(out, ("support", 0.9))

    def test_invalid_arc(self):
        self.assertIsNone(_parse_smooth_output('{"arc":"nope","confidence":0.6}'))

    def test_missing_confidence_defaults(self):
        out = _parse_smooth_output('{"arc":"playful"}')
        self.assertEqual(out, ("playful", 0.6))

    def test_garbage(self):
        self.assertIsNone(_parse_smooth_output("garbage"))

    def test_clipped_high_confidence(self):
        out = _parse_smooth_output('{"arc":"casual_check_in","confidence":2.5}')
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out[1], 1.0)


class ArcSmootherWorkerTests(unittest.TestCase):
    def _make(self, response: str = '{"arc":"silly","confidence":0.8}', **overrides):
        f = _Fixture()
        ollama = _FakeOllama(response)
        kwargs = {
            "ollama": ollama,
            "store": f.store,
            "model": "m",
            "every_n_turns": 2,
        }
        kwargs.update(overrides)
        return f, ollama, ArcSmootherWorker(**kwargs)

    def test_throttles_below_min_turns(self):
        f, ollama, worker = self._make()
        try:
            worker.notify_user_turn()
            self.assertIsNone(
                worker.maybe_run(
                    "u1",
                    history_provider=lambda: [("user", "hi")],
                    current_turn=1,
                )
            )
            self.assertEqual(ollama.calls, [])
        finally:
            f.close()

    def test_runs_and_switches_arc(self):
        f, ollama, worker = self._make()
        try:
            for _ in range(2):
                worker.notify_user_turn()
            f.store.upsert("u1", arc="casual_check_in", since_turn=0, confidence=0.5)
            state = worker.maybe_run(
                "u1",
                history_provider=lambda: [
                    ("user", "what if we made everything out of cheese"),
                    ("assistant", "ok!"),
                ],
                current_turn=4,
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.arc, "silly")
            self.assertEqual(worker.stats()["switches"], 1)
        finally:
            f.close()

    def test_runs_and_keeps_arc(self):
        f, ollama, worker = self._make(response='{"arc":"casual_check_in","confidence":0.9}')
        try:
            for _ in range(2):
                worker.notify_user_turn()
            f.store.upsert("u1", arc="casual_check_in", since_turn=0, confidence=0.5)
            state = worker.maybe_run(
                "u1",
                history_provider=lambda: [("user", "hey"), ("assistant", "hi")],
                current_turn=4,
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.arc, "casual_check_in")
            self.assertEqual(worker.stats()["switches"], 0)
            self.assertGreaterEqual(state.confidence, 0.9)
        finally:
            f.close()

    def test_failure_does_not_crash(self):
        f, ollama, worker = self._make()
        try:
            ollama.fail = True
            for _ in range(2):
                worker.notify_user_turn()
            self.assertIsNone(
                worker.maybe_run(
                    "u1",
                    history_provider=lambda: [("user", "hi")],
                    current_turn=1,
                )
            )
            self.assertEqual(worker.stats()["failed"], 1)
        finally:
            f.close()

    def test_no_history_skips(self):
        f, ollama, worker = self._make()
        try:
            for _ in range(2):
                worker.notify_user_turn()
            self.assertIsNone(
                worker.maybe_run(
                    "u1",
                    history_provider=lambda: [],
                    current_turn=1,
                )
            )
            self.assertEqual(worker.stats()["skipped_no_history"], 1)
        finally:
            f.close()


class FormatSmoothBlockTests(unittest.TestCase):
    def test_basic_render(self):
        from app.core.conversation_arc import ArcState

        state = ArcState(
            user_id="u1",
            arc="planning",
            since_turn=4,
            confidence=0.75,
            updated_at="2024-01-01T00:00:00",
        )
        block = _format_smooth_block(
            state,
            [("user", "hey"), ("assistant", "hi"), ("user", "let's plan")],
            max_chars=200,
        )
        self.assertIn("planning", block)
        self.assertIn("Jacob", block)
        self.assertIn("Aiko", block)


class ValidArcsConstantTests(unittest.TestCase):
    def test_count_and_uniqueness(self):
        self.assertEqual(len(set(VALID_ARCS)), len(VALID_ARCS))
        self.assertIn("casual_check_in", VALID_ARCS)
        self.assertIn("silly", VALID_ARCS)
        # Dropped values from the v13 taxonomy refresh.
        self.assertNotIn("deep_dive", VALID_ARCS)
        self.assertNotIn("debug", VALID_ARCS)
        self.assertEqual(len(VALID_ARCS), 6)


if __name__ == "__main__":
    unittest.main()
