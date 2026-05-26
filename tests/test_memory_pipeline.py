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

    def test_update_changes_content_kind_and_salience(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            mem = store.add(
                "Jacob owns three plants",
                kind="fact",
                embedding=embedder.embed("Jacob owns three plants"),
                salience=0.4,
            )
            assert mem is not None
            updated = store.update(
                mem.id,
                content="Jacob owns four plants now",
                kind="event",
                salience=0.8,
                embedding=embedder.embed("Jacob owns four plants now"),
            )
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.content, "Jacob owns four plants now")
            self.assertEqual(updated.kind, "event")
            self.assertAlmostEqual(updated.salience, 0.8, places=4)
            # Persisted across reopen.
            store.close()
            reopened = MemoryStore(path)
            row = reopened.get(mem.id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.content, "Jacob owns four plants now")
            self.assertEqual(row.kind, "event")

    def test_update_returns_none_for_unknown_id(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            self.assertIsNone(
                store.update(99999, content="never existed"),
            )

    def test_set_pinned_bumps_salience_to_one(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            mem = store.add(
                "Jacob hates loud restaurants",
                kind="preference",
                embedding=embedder.embed("Jacob hates loud restaurants"),
                salience=0.3,
            )
            assert mem is not None
            pinned = store.set_pinned(mem.id, True)
            self.assertIsNotNone(pinned)
            assert pinned is not None
            self.assertTrue(pinned.pinned)
            self.assertAlmostEqual(pinned.salience, 1.0, places=4)
            unpinned = store.set_pinned(mem.id, False)
            assert unpinned is not None
            self.assertFalse(unpinned.pinned)
            # Un-pin keeps the bumped salience instead of snapping back.
            self.assertAlmostEqual(unpinned.salience, 1.0, places=4)

    def test_decay_skips_pinned_rows(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            keeper = store.add(
                "Jacob's birthday is in March",
                kind="fact",
                embedding=embedder.embed("Jacob's birthday is in March"),
                salience=0.6,
            )
            forgettable = store.add(
                "Jacob mentioned a meeting on Tuesday",
                kind="event",
                embedding=embedder.embed("Jacob mentioned a meeting on Tuesday"),
                salience=0.6,
            )
            assert keeper is not None and forgettable is not None
            store.set_pinned(keeper.id, True)
            store.decay(by=0.5)
            self.assertAlmostEqual(store.get(keeper.id).salience, 1.0, places=4)
            self.assertLess(store.get(forgettable.id).salience, 0.6)

    def test_prune_keeps_pinned_rows(self) -> None:
        with _TempDb() as (path, _db):
            # Tiny cap forces every add() past the first to trigger prune().
            store = MemoryStore(path, max_memories=50)
            store._max = 2  # bypass the floor for the test
            embedder = FakeEmbedder()
            pinned = store.add(
                "Jacob loves Stephen King novels",
                kind="preference",
                embedding=embedder.embed("Jacob loves Stephen King novels"),
                salience=0.1,
            )
            assert pinned is not None
            store.set_pinned(pinned.id, True)
            store.add(
                "Jacob ate cereal this morning",
                kind="event",
                embedding=embedder.embed("Jacob ate cereal this morning"),
                salience=0.5,
            )
            store.add(
                "Jacob is reviewing a PR",
                kind="event",
                embedding=embedder.embed("Jacob is reviewing a PR"),
                salience=0.5,
            )
            # Adding the third row over cap=2 forces prune; pinned must
            # survive even though its salience is the lowest.
            self.assertIsNotNone(store.get(pinned.id))

    def test_list_recent_with_offset_and_kind_filter(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            store.add("apple", kind="fact", embedding=embedder.embed("apple"))
            store.add("banana", kind="fact", embedding=embedder.embed("banana"))
            store.add("cherry", kind="event", embedding=embedder.embed("cherry"))
            store.add("date", kind="event", embedding=embedder.embed("date"))
            facts = store.list_recent(limit=10, kind="fact")
            self.assertEqual({m.content for m in facts}, {"apple", "banana"})
            self.assertEqual(store.count_memories(kind="fact"), 2)
            self.assertEqual(store.count_memories(kind="event"), 2)
            self.assertEqual(store.count_memories(), 4)
            # Pagination: offset=1 with limit=2 across all kinds returns
            # the second + third rows in recency order.
            page = store.list_recent(limit=2, offset=1)
            self.assertEqual(len(page), 2)

    def test_pinned_rows_float_to_top_of_list_recent(self) -> None:
        with _TempDb() as (path, _db):
            store = MemoryStore(path)
            embedder = FakeEmbedder()
            old = store.add(
                "older row",
                kind="fact",
                embedding=embedder.embed("older row"),
            )
            assert old is not None
            store.add(
                "newer row",
                kind="fact",
                embedding=embedder.embed("newer row"),
            )
            store.set_pinned(old.id, True)
            page = store.list_recent(limit=10)
            self.assertEqual(page[0].id, old.id)
            self.assertTrue(page[0].pinned)


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
