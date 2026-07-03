"""Tests for :func:`app.core.concepts.concept_snapshot.build_concepts_snapshot`."""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts.concept_snapshot import build_concepts_snapshot
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase


def _store() -> ConceptStore:
    tmp = tempfile.mkdtemp()
    return ConceptStore(ChatDatabase(Path(tmp) / "test.db"))


class MemStub:
    def __init__(self, rows):
        self._rows = rows

    def get(self, mid):
        return self._rows.get(int(mid))


class GraphStub:
    def __init__(self, clusters):
        self._clusters = clusters

    def topic_clusters(self):
        return self._clusters


class ConceptSnapshotTests(unittest.TestCase):
    def test_disabled_when_store_none(self) -> None:
        snap = build_concepts_snapshot(None, None, None)
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["concepts"], [])
        self.assertEqual(snap["total"], 0)

    def test_resolves_evidence_labels_untruncated(self) -> None:
        store = _store()
        cid = store.add(
            Concept(
                label="Systems thinker",
                kind="identity",
                subject="user",
                confidence=0.7,
                embedding=np.zeros(0, dtype=np.float32),
            )
        )
        store.add_edge(
            ConceptEdge("memory", "5", "concept", str(cid), "evidence")
        )
        store.add_edge(
            ConceptEdge("cluster", "100", "concept", str(cid), "evidence")
        )

        long_text = "x" * 500  # longer than any topic-graph member clamp
        mem = MemStub({5: types.SimpleNamespace(content=long_text)})
        graph = GraphStub([
            types.SimpleNamespace(representative_id=100, summary="debugging")
        ])

        snap = build_concepts_snapshot(store, mem, graph)
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["total"], 1)
        self.assertEqual(snap["counts"]["by_subject"], {"user": 1})
        self.assertEqual(snap["counts"]["by_status"], {"candidate": 1})

        concept = snap["concepts"][0]
        self.assertEqual(concept["label"], "Systems thinker")
        by_type = {e["src_type"]: e["label"] for e in concept["evidence"]}
        self.assertEqual(by_type["memory"], long_text)  # full text, no trim
        self.assertEqual(by_type["cluster"], "debugging")

    def test_missing_evidence_targets_resolve_empty(self) -> None:
        store = _store()
        cid = store.add(_concept("orphan"))
        store.add_edge(
            ConceptEdge("memory", "404", "concept", str(cid), "evidence")
        )
        snap = build_concepts_snapshot(store, MemStub({}), GraphStub([]))
        self.assertEqual(snap["concepts"][0]["evidence"][0]["label"], "")


def _concept(label: str) -> Concept:
    return Concept(label=label, embedding=np.zeros(0, dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
