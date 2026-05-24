"""Tests for RelationshipPulseWorker (Phase 4b)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.memory_store import MemoryStore
from app.core.relationship import RelationshipStore, RelationshipTracker
from app.core.relationship_pulse import (
    RelationshipPulseWorker,
    _clean_pulse_output,
)


class _FakeOllama:
    def __init__(self, response: str = "We're settling into something steady."):
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _FakeEmbedder:
    def __init__(self, dim: int = 4):
        self.dim = dim
        self._counter = 0

    def embed(self, text: str) -> np.ndarray:
        self._counter += 1
        # Stable but distinct vectors per call so MemoryStore.add doesn't dedupe.
        v = np.zeros(self.dim, dtype=np.float32)
        for i, ch in enumerate((text or "")[: self.dim]):
            v[i] = float(ord(ch) % 17)
        v[(self._counter % self.dim)] += 0.5
        return v


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.store = MemoryStore(self.path, max_memories=50, dedupe_threshold=0.999)
        self.rel_store = RelationshipStore(self.db)
        self.tracker = RelationshipTracker(self.rel_store)
        self.embedder = _FakeEmbedder()

    def close(self):
        try:
            self.store.close()
        except Exception:
            pass
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


def _seed_memories(store: MemoryStore, embedder: _FakeEmbedder, n: int = 4):
    kinds = ["reflection", "promise", "event", "callback"]
    for k in range(n):
        text = f"Memory entry number {k} with some texture"
        store.add(text, kinds[k % len(kinds)], embedder.embed(text), salience=0.6)


class CleanPulseOutputTests(unittest.TestCase):
    def test_strips_quotes(self):
        self.assertEqual(_clean_pulse_output('"steady"'), "steady")

    def test_strips_code_fence_with_lang(self):
        out = _clean_pulse_output("```text\nsentence here\n```")
        self.assertEqual(out, "sentence here")

    def test_truncates(self):
        out = _clean_pulse_output("x " * 500)
        self.assertTrue(out.endswith("…"))

    def test_empty(self):
        self.assertEqual(_clean_pulse_output(""), "")


class ShouldRunTests(unittest.TestCase):
    def test_blocks_when_too_few_turns(self):
        f = _Fixture()
        try:
            f.tracker.register_session_start("u1")
            for _ in range(5):
                f.tracker.record_turn("u1")
            worker = RelationshipPulseWorker(
                ollama=_FakeOllama(),
                memory_store=f.store,
                relationship_store=f.rel_store,
                chat_db=f.db,
                embedder=f.embedder,
                model="m",
                min_hours=0.0,
                min_turns=10,
            )
            self.assertFalse(worker.should_run("u1"))
        finally:
            f.close()

    def test_runs_when_thresholds_met(self):
        f = _Fixture()
        try:
            f.tracker.register_session_start("u1")
            for _ in range(10):
                f.tracker.record_turn("u1")
            worker = RelationshipPulseWorker(
                ollama=_FakeOllama(),
                memory_store=f.store,
                relationship_store=f.rel_store,
                chat_db=f.db,
                embedder=f.embedder,
                model="m",
                min_hours=0.0,
                min_turns=5,
            )
            self.assertTrue(worker.should_run("u1"))
        finally:
            f.close()

    def test_blocks_when_too_recent(self):
        f = _Fixture()
        try:
            f.tracker.register_session_start("u1")
            for _ in range(20):
                f.tracker.record_turn("u1")
            _seed_memories(f.store, f.embedder)
            worker = RelationshipPulseWorker(
                ollama=_FakeOllama(),
                memory_store=f.store,
                relationship_store=f.rel_store,
                chat_db=f.db,
                embedder=f.embedder,
                model="m",
                min_hours=24.0,
                min_turns=5,
            )
            self.assertIsNotNone(worker.maybe_run("u1"))
            self.assertFalse(worker.should_run("u1"))
        finally:
            f.close()


class MaybeRunTests(unittest.TestCase):
    def _make(self, **overrides):
        f = _Fixture()
        f.tracker.register_session_start("u1")
        for _ in range(20):
            f.tracker.record_turn("u1")
        _seed_memories(f.store, f.embedder)
        ollama = _FakeOllama()
        kwargs = {
            "ollama": ollama,
            "memory_store": f.store,
            "relationship_store": f.rel_store,
            "chat_db": f.db,
            "embedder": f.embedder,
            "model": "m",
            "min_hours": 0.0,
            "min_turns": 5,
        }
        kwargs.update(overrides)
        worker = RelationshipPulseWorker(**kwargs)
        return f, ollama, worker

    def test_runs_and_persists_memory(self):
        f, ollama, worker = self._make()
        try:
            calls: list = []
            result = worker.maybe_run("u1", on_pulse=calls.append)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(len(ollama.calls), 1)
            self.assertEqual(len(calls), 1)
            self.assertIsNotNone(result.memory_id)
            mids = {m.id for m in f.store.list_recent(limit=20) if m.kind == "self_tagged"}
            self.assertIn(result.memory_id, mids)
        finally:
            f.close()

    def test_skips_when_no_input(self):
        f = _Fixture()
        try:
            f.tracker.register_session_start("u1")
            for _ in range(10):
                f.tracker.record_turn("u1")
            ollama = _FakeOllama()
            worker = RelationshipPulseWorker(
                ollama=ollama,
                memory_store=f.store,
                relationship_store=f.rel_store,
                chat_db=f.db,
                embedder=f.embedder,
                model="m",
                min_hours=0.0,
                min_turns=5,
            )
            result = worker.maybe_run("u1")
            self.assertIsNone(result)
            self.assertEqual(worker.stats()["skipped_no_input"], 1)
            self.assertEqual(ollama.calls, [])
        finally:
            f.close()

    def test_llm_failure_does_not_crash(self):
        f, ollama, worker = self._make()
        try:
            ollama.fail = True
            result = worker.maybe_run("u1")
            self.assertIsNone(result)
            self.assertEqual(worker.stats()["failed"], 1)
        finally:
            f.close()

    def test_state_persists_across_instances(self):
        f, _ollama, worker = self._make()
        try:
            self.assertIsNotNone(worker.maybe_run("u1"))
            # Build a second worker on the same db; should detect prior run.
            second = RelationshipPulseWorker(
                ollama=_FakeOllama(),
                memory_store=f.store,
                relationship_store=f.rel_store,
                chat_db=f.db,
                embedder=f.embedder,
                model="m",
                min_hours=24.0,
                min_turns=5,
            )
            self.assertFalse(second.should_run("u1"))
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
