"""Tests for the Phase 2b :class:`DreamWorker`.

Contract surface:

  * Runs at most once per process (guarded by ``_has_run_this_boot``).
  * Honors the ``min_hours_since_last`` gate.
  * Skips when LLM / memory store / embedder are missing.
  * Skips when there is no rolling summary, no callbacks, no self
    memories — there's nothing to dream about.
  * Persists a ``reflection`` memory tagged with the ``[dream]``
    content prefix so the resume opener can detect it later.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.dream_worker import DreamWorker, _DREAM_PREFIX, _clean_dream_output
from app.core.memory.memory_store import MemoryStore


class _FakeOllama:
    def __init__(
        self,
        response: str = (
            "I keep turning over how Jacob lit up about that "
            "weird command-line trick yesterday."
        ),
    ) -> None:
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _FakeEmbedder:
    def embed(self, text: str) -> np.ndarray:
        # Deterministic 8-dim embedding so two identical contents
        # round-trip identically through MemoryStore dedup.
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        return rng.normal(size=8).astype(np.float32)


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.memory = MemoryStore(self.path, max_memories=100, dedupe_threshold=0.999)
        self.embedder = _FakeEmbedder()

    def close(self) -> None:
        try:
            self.memory.close()
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


def _make_worker(
    *,
    fixture: _Fixture,
    ollama: _FakeOllama | None,
    embedder: _FakeEmbedder | None = None,
    memory: MemoryStore | None = None,
    min_hours: float = 6.0,
) -> DreamWorker:
    return DreamWorker(
        ollama=ollama,
        memory_store=memory if memory is not None else fixture.memory,
        embedder=embedder if embedder is not None else fixture.embedder,
        model="dream-model",
        chat_db=fixture.db,
        min_hours_since_last=min_hours,
    )


class DreamWorkerTests(unittest.TestCase):
    def test_persists_reflection_memory_with_dream_prefix(self) -> None:
        f = _Fixture()
        try:
            ollama = _FakeOllama("Quietly thinking about that book Jacob mentioned.")
            worker = _make_worker(fixture=f, ollama=ollama)
            mem = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=8.0,
                rolling_summary="We talked about cozy sci-fi novels yesterday.",
            )
            self.assertIsNotNone(mem)
            assert mem is not None
            self.assertEqual(mem.kind, "reflection")
            self.assertTrue(mem.content.startswith(_DREAM_PREFIX))
            self.assertIn("Quietly thinking", mem.content)
            self.assertEqual(worker.stats()["memories_written"], 1)
            self.assertEqual(len(ollama.calls), 1)
        finally:
            f.close()

    def test_runs_at_most_once_per_boot(self) -> None:
        f = _Fixture()
        try:
            ollama = _FakeOllama()
            worker = _make_worker(fixture=f, ollama=ollama)
            first = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=12.0,
                rolling_summary="anything",
            )
            self.assertIsNotNone(first)
            second = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=12.0,
                rolling_summary="anything",
            )
            self.assertIsNone(second)
            self.assertEqual(len(ollama.calls), 1)
        finally:
            f.close()

    def test_skips_below_threshold(self) -> None:
        f = _Fixture()
        try:
            ollama = _FakeOllama()
            worker = _make_worker(fixture=f, ollama=ollama, min_hours=6.0)
            mem = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=2.0,  # below the 6h gate
                rolling_summary="something",
            )
            self.assertIsNone(mem)
            self.assertEqual(ollama.calls, [])
        finally:
            f.close()

    def test_skips_without_context(self) -> None:
        f = _Fixture()
        try:
            ollama = _FakeOllama()
            worker = _make_worker(fixture=f, ollama=ollama)
            mem = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=12.0,
                rolling_summary="",
                recent_callbacks=None,
                recent_self_memories=None,
            )
            self.assertIsNone(mem)
            self.assertEqual(ollama.calls, [])
            self.assertEqual(worker.stats()["skipped_no_context"], 1)
        finally:
            f.close()

    def test_skips_when_disabled_components(self) -> None:
        f = _Fixture()
        try:
            worker = _make_worker(fixture=f, ollama=None)
            mem = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=12.0,
                rolling_summary="anything",
            )
            self.assertIsNone(mem)
            self.assertEqual(worker.stats()["skipped_disabled"], 1)
        finally:
            f.close()

    def test_llm_failure_increments_failed_and_does_not_persist(self) -> None:
        f = _Fixture()
        try:
            ollama = _FakeOllama()
            ollama.fail = True
            worker = _make_worker(fixture=f, ollama=ollama)
            mem = worker.maybe_run(
                user_id="u1",
                session_key="s1",
                hours_since_last=12.0,
                rolling_summary="anything",
            )
            self.assertIsNone(mem)
            self.assertEqual(worker.stats()["failed"], 1)
            self.assertEqual(worker.stats()["memories_written"], 0)
        finally:
            f.close()

    def test_clean_output_strips_quotes_and_truncates(self) -> None:
        out = _clean_dream_output('  "A quiet thought about Jacob."  ')
        self.assertEqual(out, "A quiet thought about Jacob.")

    def test_clean_output_truncates_long(self) -> None:
        long = "Word " * 200
        out = _clean_dream_output(long)
        self.assertLess(len(out), 260)


if __name__ == "__main__":
    unittest.main()
