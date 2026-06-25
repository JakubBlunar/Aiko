"""Schema v7 metadata round-trip on ``MemoryStore`` writes/reads.

These tests focus on the v7 additions:
- the ``Memory.metadata`` dict survives ``add()`` and ``update()``,
- ``metadata_merge=True`` shallow-merges instead of replacing,
- ``iter_by_kind`` returns only matching rows,
- ``pinned=True`` / ``skip_dedupe=True`` bypass the dedupe pass so a
  curated moment never gets silently merged into a fuzzy neighbour.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


class _TempStore:
    def __enter__(self) -> MemoryStore:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "mem.db"
        ChatDatabase(path)  # creates schema v7
        self.store = MemoryStore(path)
        return self.store

    def __exit__(self, *exc):
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class TestMetadataRoundTrip(unittest.TestCase):
    def test_add_persists_metadata(self) -> None:
        with _TempStore() as store:
            emb = _FakeEmbedder().embed("debugged the proactive bug")
            mem = store.add(
                "We debugged the proactive bug together.",
                "shared_moment",
                emb,
                metadata={
                    "vibe": "focused",
                    "when": "2026-01-15T10:00:00+00:00",
                    "what": "debugged the proactive bug",
                    "participants": ["aiko", "jacob"],
                    "source_message_ids": [42, 43],
                },
                pinned=True,
            )
            self.assertIsNotNone(mem)
            self.assertEqual(mem.metadata["vibe"], "focused")
            self.assertEqual(mem.metadata["participants"], ["aiko", "jacob"])
            self.assertTrue(mem.pinned)

            # Reload from SQLite to confirm the JSON column survived a
            # round-trip (don't trust the in-memory mirror alone).
            store2 = MemoryStore(store._db_path)
            reloaded = store2._mirror[mem.id]
            self.assertEqual(reloaded.metadata["vibe"], "focused")
            self.assertEqual(reloaded.metadata["source_message_ids"], [42, 43])
            self.assertTrue(reloaded.pinned)

    def test_update_replaces_metadata_by_default(self) -> None:
        with _TempStore() as store:
            emb = _FakeEmbedder().embed("first moment")
            mem = store.add(
                "first moment",
                "shared_moment",
                emb,
                metadata={"vibe": "warm", "when": "2026-01-01T00:00:00+00:00"},
                pinned=True,
            )
            self.assertIsNotNone(mem)
            updated = store.update(mem.id, metadata={"vibe": "playful"})
            self.assertIsNotNone(updated)
            # Replacement semantics: ``when`` is gone unless we merged.
            self.assertEqual(updated.metadata, {"vibe": "playful"})

    def test_update_with_metadata_merge_shallow_merges(self) -> None:
        with _TempStore() as store:
            emb = _FakeEmbedder().embed("anniversary candidate")
            mem = store.add(
                "anniversary candidate",
                "shared_moment",
                emb,
                metadata={"vibe": "tender", "when": "2025-01-01T00:00:00+00:00"},
                pinned=True,
            )
            self.assertIsNotNone(mem)
            stamped = store.update(
                mem.id,
                metadata={"last_anniversaried_at": "2026-01-01T10:00:00+00:00"},
                metadata_merge=True,
            )
            self.assertIsNotNone(stamped)
            self.assertEqual(stamped.metadata["vibe"], "tender")
            self.assertEqual(stamped.metadata["when"], "2025-01-01T00:00:00+00:00")
            self.assertEqual(
                stamped.metadata["last_anniversaried_at"],
                "2026-01-01T10:00:00+00:00",
            )

    def test_iter_by_kind_filters(self) -> None:
        with _TempStore() as store:
            embedder = _FakeEmbedder()
            store.add("a fact about Jacob", "fact", embedder.embed("a fact"))
            store.add(
                "We laughed about cookies.",
                "shared_moment",
                embedder.embed("we laughed about cookies"),
                metadata={"vibe": "playful"},
                pinned=True,
            )
            store.add(
                "A milestone moment.",
                "shared_moment",
                embedder.embed("milestone moment"),
                metadata={"vibe": "milestone"},
                pinned=True,
            )
            moments = store.iter_by_kind("shared_moment")
            self.assertEqual(len(moments), 2)
            self.assertTrue(all(m.kind == "shared_moment" for m in moments))

    def test_pinned_bypasses_dedupe(self) -> None:
        """Two near-identical pinned moments must both persist (curated)."""
        with _TempStore() as store:
            embedder = _FakeEmbedder()
            v = embedder.embed("we laughed about cookies once again")
            first = store.add(
                "We laughed about cookies.",
                "shared_moment",
                v,
                metadata={"vibe": "playful"},
                pinned=True,
            )
            second = store.add(
                "We laughed about cookies.",
                "shared_moment",
                v,
                metadata={"vibe": "playful"},
                pinned=True,
            )
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first.id, second.id)


class TestGetMany(unittest.TestCase):
    """P4: batch mirror fetch used by the RAG hot loop."""

    def test_returns_dict_keyed_by_id(self) -> None:
        with _TempStore() as store:
            embedder = _FakeEmbedder()
            a = store.add("fact one", "fact", embedder.embed("fact one"))
            b = store.add("fact two", "fact", embedder.embed("fact two"))
            got = store.get_many([a.id, b.id])
            self.assertEqual(set(got), {a.id, b.id})
            self.assertEqual(got[a.id].content, "fact one")
            self.assertEqual(got[b.id].content, "fact two")

    def test_missing_and_bad_ids_are_skipped(self) -> None:
        with _TempStore() as store:
            a = store.add("only fact", "fact", _FakeEmbedder().embed("only fact"))
            got = store.get_many([a.id, 999_999, None, "x"])  # type: ignore[list-item]
            self.assertEqual(set(got), {a.id})

    def test_matches_per_id_get(self) -> None:
        with _TempStore() as store:
            embedder = _FakeEmbedder()
            ids = [
                store.add(f"row {i}", "fact", embedder.embed(f"row {i}")).id
                for i in range(5)
            ]
            batch = store.get_many(ids)
            for mid in ids:
                self.assertEqual(batch[mid].content, store.get(mid).content)

    def test_empty_input_returns_empty_dict(self) -> None:
        with _TempStore() as store:
            self.assertEqual(store.get_many([]), {})


if __name__ == "__main__":
    unittest.main()
