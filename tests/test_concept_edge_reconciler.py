"""Tests for :mod:`app.core.concepts.concept_edge_reconciler` (L25).

Verifies the three reconciliation paths against a real :class:`ConceptStore`
on a temp DB: delete cascade + recount, the orphan sweep (simulating a
listener-bypassing ``prune()`` via a raw row delete), and repoint-on-merge.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts.concept_edge_reconciler import ConceptEdgeReconciler
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase


def _build() -> tuple[ChatDatabase, ConceptStore, Path]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, ConceptStore(db), path


def _c(label: str, **kw) -> Concept:
    return Concept(label=label, embedding=np.zeros(0, dtype=np.float32), **kw)


def _insert_memory(db: ChatDatabase, mem_id: int) -> None:
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO memories (id, content, kind, embedding, created_at) "
        "VALUES (?, 'm', 'fact', X'00', '2020-01-01')",
        (int(mem_id),),
    )
    conn.commit()


def _evidence(store: ConceptStore, mem_id: int, concept_id: int) -> None:
    store.add_edge(
        ConceptEdge("memory", str(mem_id), "concept", str(concept_id),
                    "evidence")
    )


class DeleteCascadeTests(unittest.TestCase):
    def test_on_memory_deleted_drops_edges_and_recounts(self) -> None:
        _, store, _ = _build()
        cid = store.add(
            _c("c", evidence_count=2, distinct_source_count=2)
        )
        _evidence(store, 10, cid)
        _evidence(store, 11, cid)
        rec = ConceptEdgeReconciler(store)

        rec.on_memory_deleted(10)

        # edge for memory 10 gone; the other remains
        self.assertEqual([e.src_id for e in store.evidence_of(cid)], ["11"])
        got = store.get(cid)
        assert got is not None
        self.assertEqual(got.evidence_count, 1)
        self.assertEqual(got.distinct_source_count, 1)

    def test_on_memory_deleted_also_clears_contradicts(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c", evidence_count=1, distinct_source_count=1))
        _evidence(store, 10, cid)
        store.add_edge(
            ConceptEdge("concept", str(cid), "memory", "10", "contradicts",
                        polarity=-1)
        )
        rec = ConceptEdgeReconciler(store)

        rec.on_memory_deleted(10)

        self.assertEqual(store.edges_into("memory", 10), [])
        self.assertEqual(store.edges_from("memory", 10), [])
        got = store.get(cid)
        assert got is not None
        self.assertEqual(got.evidence_count, 0)
        self.assertEqual(got.distinct_source_count, 0)

    def test_on_memory_deleted_no_edges_is_noop(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c", evidence_count=5, distinct_source_count=5))
        rec = ConceptEdgeReconciler(store)
        rec.on_memory_deleted(999)  # nothing points at it
        got = store.get(cid)
        assert got is not None
        # untouched (no affected concept -> no recount)
        self.assertEqual(got.evidence_count, 5)

    def test_bad_id_tolerated(self) -> None:
        _, store, _ = _build()
        rec = ConceptEdgeReconciler(store)
        rec.on_memory_deleted(None)  # type: ignore[arg-type]
        rec.on_memory_deleted("not-an-int")  # type: ignore[arg-type]


class SweepTests(unittest.TestCase):
    def test_sweep_gcs_orphans_left_by_prune(self) -> None:
        db, store, _ = _build()
        _insert_memory(db, 100)
        cid = store.add(_c("c", evidence_count=2, distinct_source_count=2))
        _evidence(store, 100, cid)  # alive
        _evidence(store, 200, cid)  # orphan (no memory row)
        rec = ConceptEdgeReconciler(store)

        # Simulate prune() hard-deleting rows without firing listeners: the
        # memory 200 never existed, standing in for a row deleted silently.
        stats = rec.sweep(50)

        self.assertEqual(stats["orphans_dropped"], 1)
        self.assertEqual(stats["concepts_reconciled"], 1)
        self.assertEqual([e.src_id for e in store.evidence_of(cid)], ["100"])
        got = store.get(cid)
        assert got is not None
        self.assertEqual(got.evidence_count, 1)
        self.assertEqual(got.distinct_source_count, 1)

    def test_sweep_clean_graph_is_noop(self) -> None:
        db, store, _ = _build()
        _insert_memory(db, 100)
        cid = store.add(_c("c", evidence_count=1, distinct_source_count=1))
        _evidence(store, 100, cid)
        rec = ConceptEdgeReconciler(store)
        stats = rec.sweep(50)
        self.assertEqual(stats["orphans_dropped"], 0)
        self.assertEqual(stats["concepts_reconciled"], 0)


class RepointTests(unittest.TestCase):
    def test_repoint_preserves_evidence_on_survivor(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c", evidence_count=1, distinct_source_count=1))
        _evidence(store, 10, cid)  # victim
        rec = ConceptEdgeReconciler(store)

        moved = rec.repoint(10, 20)  # merge victim 10 into survivor 20

        self.assertEqual(moved, 1)
        self.assertEqual([e.src_id for e in store.evidence_of(cid)], ["20"])
        got = store.get(cid)
        assert got is not None
        # count unchanged (still one distinct source, now the survivor)
        self.assertEqual(got.evidence_count, 1)
        self.assertEqual(got.distinct_source_count, 1)

    def test_repoint_merges_duplicate_source(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c", evidence_count=2, distinct_source_count=2))
        _evidence(store, 10, cid)  # victim
        _evidence(store, 20, cid)  # survivor already supports cid
        rec = ConceptEdgeReconciler(store)

        rec.repoint(10, 20)

        # victim + survivor dedupe onto one edge -> counts drop to 1
        self.assertEqual([e.src_id for e in store.evidence_of(cid)], ["20"])
        got = store.get(cid)
        assert got is not None
        self.assertEqual(got.evidence_count, 1)
        self.assertEqual(got.distinct_source_count, 1)


class DeleteListenerIntegrationTests(unittest.TestCase):
    """End-to-end: reconciler registered as a MemoryStore delete listener."""

    def test_memory_delete_cascades_to_concept_edges(self) -> None:
        db, store, path = _build()
        from app.core.memory.memory_store import MemoryStore

        mem = MemoryStore(path)
        rec = ConceptEdgeReconciler(store)
        mem.add_delete_listener(rec.on_memory_deleted)

        _insert_memory(db, 500)
        cid = store.add(_c("c", evidence_count=1, distinct_source_count=1))
        _evidence(store, 500, cid)

        self.assertTrue(mem.delete(500))

        self.assertEqual(store.evidence_of(cid), [])
        got = store.get(cid)
        assert got is not None
        self.assertEqual(got.evidence_count, 0)
        self.assertEqual(got.distinct_source_count, 0)


if __name__ == "__main__":
    unittest.main()
