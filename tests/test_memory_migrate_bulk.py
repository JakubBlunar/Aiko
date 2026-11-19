"""Startup reconciliation handoff from the authoritative SQLite memory mirror."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=abs(hash(text)) % (2**31))
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


class _TempStore:
    def __enter__(self) -> MemoryStore:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "mem.db"
        ChatDatabase(path)
        self.store = MemoryStore(path)
        return self.store

    def __exit__(self, *exc):
        self.store.close()
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class MigrateToRagBulkTests(unittest.TestCase):
    def test_migrate_passes_one_authoritative_snapshot(self) -> None:
        with _TempStore() as store:
            emb = _FakeEmbedder()
            for content in ("alpha", "beta", "gamma"):
                store.add(content, "fact", emb.embed(content))

            rag = MagicMock()
            rag.reconcile_memories = MagicMock(return_value=3)

            written = store.migrate_to_rag(rag)

            self.assertEqual(written, 3)
            rag.reconcile_memories.assert_called_once()
            # Old per-row path must not be touched.
            rag.add_memory.assert_not_called()

            # The single bulk call should carry exactly one record per
            # seeded memory, with the right shape.
            (records,), _kwargs = rag.reconcile_memories.call_args
            records = list(records)
            self.assertEqual(len(records), 3)
            self.assertEqual(
                {r["content"] for r in records},
                {"alpha", "beta", "gamma"},
            )
            for record in records:
                self.assertIn("record_id", record)
                self.assertIn("embedding", record)
                self.assertEqual(record["kind"], "fact")

    def test_migrate_skips_rows_without_embedding(self) -> None:
        # Memories without an embedding can't be vector-searched, so
        # they shouldn't end up in the bulk batch. The "no embedding"
        # case is realistic when an embedder briefly failed at write
        # time but the SQLite row still landed.
        with _TempStore() as store:
            import dataclasses

            emb = _FakeEmbedder()
            store.add("real", "fact", emb.embed("real"))
            # Sneak a row with embedding=None into the mirror so the
            # migration sees it. ``MemoryStore.add`` always supplies a
            # vector, so we clone an existing row and null the field
            # rather than constructing from scratch.
            with store._lock:
                template = next(iter(store._mirror.values()))
                ghost = dataclasses.replace(
                    template,
                    id=999,
                    content="ghost",
                    embedding=None,
                )
                store._mirror[999] = ghost

            rag = MagicMock()
            rag.reconcile_memories = MagicMock(return_value=1)

            store.migrate_to_rag(rag)
            (records,), _kwargs = rag.reconcile_memories.call_args
            self.assertEqual(len(list(records)), 1)

    def test_migrate_returns_zero_when_rag_store_is_none(self) -> None:
        with _TempStore() as store:
            self.assertEqual(store.migrate_to_rag(None), 0)

    def test_empty_authoritative_store_still_reconciles(self) -> None:
        with _TempStore() as store:
            rag = MagicMock()
            rag.reconcile_memories.return_value = 0
            self.assertEqual(store.migrate_to_rag(rag), 0)
            rag.reconcile_memories.assert_called_once_with([])

    def test_unreadable_mirror_does_not_prune_the_index(self) -> None:
        with _TempStore() as store:
            store._mirror_loaded = False
            rag = MagicMock()
            self.assertEqual(store.migrate_to_rag(rag), 0)
            rag.reconcile_memories.assert_not_called()

    def test_migrate_handles_reconciliation_failure(self) -> None:
        # If the bulk write blows up, ``migrate_to_rag`` should log
        # and return 0 rather than abort startup. Same observable
        # behaviour as the old per-row path that swallowed each
        # individual failure.
        with _TempStore() as store:
            emb = _FakeEmbedder()
            store.add("alpha", "fact", emb.embed("alpha"))

            rag = MagicMock()
            rag.reconcile_memories = MagicMock(side_effect=RuntimeError("boom"))

            written = store.migrate_to_rag(rag)
            self.assertEqual(written, 0)


if __name__ == "__main__":
    unittest.main()
