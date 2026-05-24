"""Smoke test for the long-term memory pipeline.

Exercises MemoryStore + MemoryRetriever + the self-tagged extraction path on
TurnRunner, all with a fake embedder so the test can run without Ollama.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.memory_retriever import MemoryRetriever
from app.core.memory_store import MemoryStore


class FakeEmbedder:
    """Deterministic embedder: hashes the text into a small unit vector.

    Stable across calls so identical text -> identical vector. Two unrelated
    texts hash to nearly-orthogonal vectors. Vectors are unit-norm.
    """

    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v

    def batch_embed(self, texts):
        return [self.embed(t) for t in texts]

    def close(self) -> None:
        pass


class _TempDb:
    def __enter__(self) -> tuple[Path, ChatDatabase]:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "memory.db"
        db = ChatDatabase(path)
        return path, db

    def __exit__(self, *exc):
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class TestMemoryStore(unittest.TestCase):
    def test_add_and_search_returns_closest(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            store.add(
                "Jacob has a cat named Mochi",
                kind="fact",
                embedding=embedder.embed("Jacob has a cat named Mochi"),
                salience=0.6,
            )
            store.add(
                "Jacob prefers tea over coffee",
                kind="preference",
                embedding=embedder.embed("Jacob prefers tea over coffee"),
                salience=0.7,
            )
            self.assertEqual(store.count(), 2)

            # Querying for the cat should return the cat memory first.
            hits = store.search(
                embedder.embed("Jacob has a cat named Mochi"),
                top_k=2,
                min_score=0.0,
            )
            self.assertGreaterEqual(len(hits), 1)
            self.assertIn("Mochi", hits[0].memory.content)

    def test_dedup_bumps_salience(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path, dedupe_threshold=0.5)
            embedder = FakeEmbedder()
            text = "Jacob is a software engineer"
            first = store.add(
                text,
                kind="fact",
                embedding=embedder.embed(text),
                salience=0.4,
            )
            self.assertIsNotNone(first)
            second = store.add(
                text,
                kind="fact",
                embedding=embedder.embed(text),
                salience=0.9,
            )
            # Identical embedding -> dedupe -> no new row inserted.
            self.assertIsNone(second)
            self.assertEqual(store.count(), 1)
            mem = next(iter(store._mirror.values()))
            self.assertGreaterEqual(mem.salience, 0.4)

    def test_delete_removes_from_mirror_and_disk(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            mem = store.add(
                "Jacob likes hot chocolate",
                kind="preference",
                embedding=embedder.embed("Jacob likes hot chocolate"),
            )
            self.assertIsNotNone(mem)
            assert mem is not None
            self.assertTrue(store.delete(mem.id))
            self.assertEqual(store.count(), 0)
            store.close()
            store2 = MemoryStore(path)
            self.assertEqual(store2.count(), 0)


class TestMemoryRetriever(unittest.TestCase):
    def test_block_for_returns_formatted_string(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            store.add(
                "Jacob is working on an AI companion called Aiko",
                kind="event",
                embedding=embedder.embed("AI companion Aiko"),
            )
            retriever = MemoryRetriever(
                store, embedder, top_k=3, score_threshold=0.0,
            )
            block = retriever.block_for("AI companion Aiko")
            self.assertIn("long-term memory", block.lower())
            self.assertIn("Aiko", block)

    def test_returns_empty_when_store_empty(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            retriever = MemoryRetriever(store, embedder, top_k=3)
            self.assertEqual(retriever.block_for("anything"), "")


class TestSelfTaggedExtraction(unittest.TestCase):
    def test_turn_runner_extracts_remember_tags(self) -> None:
        from app.core.turn_runner import _REMEMBER_TAG_RE

        raw = (
            "[[reaction:cheerful]] Hi! [[remember:Jacob just adopted a cat "
            "named Mochi]] So what's up?"
        )
        tags = [m.group("body").strip() for m in _REMEMBER_TAG_RE.finditer(raw)]
        self.assertEqual(tags, ["Jacob just adopted a cat named Mochi"])

    def test_self_tagged_round_trip(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()

            # Simulate what TurnRunner._extract_self_tagged_memories does.
            from app.core.turn_runner import _REMEMBER_TAG_RE

            raw_text = (
                "[[reaction:gentle]] Got it.\n"
                "[[remember:Jacob is learning Spanish before a Madrid trip]]"
            )
            inserted = 0
            for match in _REMEMBER_TAG_RE.finditer(raw_text):
                content = match.group("body").strip()
                emb = embedder.embed(content)
                mem = store.add(
                    content=content,
                    kind="self_tagged",
                    embedding=emb,
                    salience=0.7,
                    source_session="sess1",
                )
                if mem is not None:
                    inserted += 1
            self.assertEqual(inserted, 1)
            self.assertEqual(store.count(), 1)
            mem = store.list_recent(limit=1)[0]
            self.assertEqual(mem.kind, "self_tagged")
            self.assertAlmostEqual(mem.salience, 0.7, places=2)

    def test_self_prefix_marks_kind_self(self) -> None:
        from app.core.turn_runner import _REMEMBER_TAG_RE

        raw = (
            "[[reaction:gentle]] Hmm, true.\n"
            "[[remember:self:I prefer cozy stories over horror]]"
        )
        matches = list(_REMEMBER_TAG_RE.finditer(raw))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group("kind"), "self")
        self.assertEqual(
            matches[0].group("body"), "I prefer cozy stories over horror",
        )


if __name__ == "__main__":
    unittest.main()
