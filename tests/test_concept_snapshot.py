"""Tests for :func:`app.core.concepts.concept_snapshot.build_concepts_snapshot`."""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts.concept_snapshot import (
    build_concept_quality,
    build_concepts_snapshot,
    resolve_evidence_facts,
    resolve_evidence_labels,
)
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


class ResolveEvidenceLabelsTests(unittest.TestCase):
    """The L5 grounding clause restricts evidence to thematic node types so
    aiko self-concepts (memory-typed evidence) don't render truncated
    memory sentences."""

    def _store_with_mixed_evidence(self):
        store = _store()
        cid = store.add(_concept("I deflect with teasing"))
        # Memory evidence: a full first-person sentence (would truncate ugly).
        store.add_edge(
            ConceptEdge("memory", "5", "concept", str(cid), "evidence")
        )
        # Cluster evidence: a clean thematic summary.
        store.add_edge(
            ConceptEdge("cluster", "100", "concept", str(cid), "evidence")
        )
        mem = MemStub({
            5: types.SimpleNamespace(
                content="He called me cute while teasing me into a pout and "
                        "I got flustered about it",
            )
        })
        graph = GraphStub([
            types.SimpleNamespace(representative_id=100, summary="teasing")
        ])
        return store, mem, graph, cid

    def test_default_includes_memory(self) -> None:
        store, mem, graph, cid = self._store_with_mixed_evidence()
        labels = resolve_evidence_labels(store, mem, graph, cid)
        self.assertIn("teasing", labels)
        self.assertTrue(any("teasing me into a pout" in l for l in labels))

    def test_src_types_filter_drops_memory(self) -> None:
        store, mem, graph, cid = self._store_with_mixed_evidence()
        labels = resolve_evidence_labels(
            store, mem, graph, cid, src_types=("cluster", "concept"),
        )
        self.assertEqual(labels, ["teasing"])

    def test_memory_only_concept_yields_no_thematic_grounding(self) -> None:
        store = _store()
        cid = store.add(_concept("I over-explain when nervous"))
        store.add_edge(
            ConceptEdge("memory", "9", "concept", str(cid), "evidence")
        )
        mem = MemStub({9: types.SimpleNamespace(content="a long memory " * 20)})
        labels = resolve_evidence_labels(
            store, mem, GraphStub([]), cid, src_types=("cluster", "concept"),
        )
        self.assertEqual(labels, [])


class ResolveEvidenceFactsTests(unittest.TestCase):
    """The L22 A/B graph joins. These are the half the pure scorer cannot
    do, so the mapping from edges to cluster span / memory confidence is
    where signal A and B actually live or die."""

    def _graph(self):
        # Two clusters; memories 1-3 belong to cluster 100, memory 9 to 200.
        return GraphStub([
            types.SimpleNamespace(
                representative_id=100, summary="baths", member_ids=(1, 2, 3)
            ),
            types.SimpleNamespace(
                representative_id=200, summary="music", member_ids=(9,)
            ),
        ])

    def _memories(self):
        return MemStub({
            1: types.SimpleNamespace(content="a", confidence=0.9),
            2: types.SimpleNamespace(content="b", confidence=0.8),
            3: types.SimpleNamespace(content="c", confidence=0.7),
            9: types.SimpleNamespace(content="d", confidence=0.2),
        })

    def test_memory_edges_resolve_through_to_their_cluster(self) -> None:
        # Three memories, one cluster: distinct_source_count would say 3,
        # cluster span correctly says 1. This is the whole point of A.
        store = _store()
        cid = store.add(_concept("all from one topic"))
        for mid in (1, 2, 3):
            store.add_edge(
                ConceptEdge("memory", str(mid), "concept", str(cid), "evidence")
            )
        facts = resolve_evidence_facts(store, self._memories(), self._graph())
        self.assertEqual(facts[cid].cluster_span, 1)

    def test_span_counts_distinct_clusters_across_edge_types(self) -> None:
        store = _store()
        cid = store.add(_concept("spans two topics"))
        store.add_edge(
            ConceptEdge("memory", "1", "concept", str(cid), "evidence")
        )
        store.add_edge(
            ConceptEdge("cluster", "200", "concept", str(cid), "evidence")
        )
        facts = resolve_evidence_facts(store, self._memories(), self._graph())
        self.assertEqual(facts[cid].cluster_span, 2)

    def test_memory_and_its_own_cluster_are_not_double_counted(self) -> None:
        store = _store()
        cid = store.add(_concept("same topic twice"))
        store.add_edge(
            ConceptEdge("memory", "1", "concept", str(cid), "evidence")
        )
        store.add_edge(
            ConceptEdge("cluster", "100", "concept", str(cid), "evidence")
        )
        facts = resolve_evidence_facts(store, self._memories(), self._graph())
        self.assertEqual(facts[cid].cluster_span, 1)

    def test_concept_edges_do_not_contribute_span(self) -> None:
        # Meta concepts are grounded on other concepts, not topics.
        store = _store()
        cid = store.add(_concept("meta"))
        store.add_edge(
            ConceptEdge("concept", "77", "concept", str(cid), "evidence")
        )
        facts = resolve_evidence_facts(store, self._memories(), self._graph())
        self.assertEqual(facts[cid].cluster_span, 0)

    def test_unclustered_memories_contribute_no_span(self) -> None:
        store = _store()
        cid = store.add(_concept("orphan evidence"))
        store.add_edge(
            ConceptEdge("memory", "404", "concept", str(cid), "evidence")
        )
        facts = resolve_evidence_facts(store, self._memories(), self._graph())
        self.assertEqual(facts[cid].cluster_span, 0)

    def test_memory_confidences_are_collected(self) -> None:
        store = _store()
        cid = store.add(_concept("weakly supported"))
        store.add_edge(
            ConceptEdge("memory", "9", "concept", str(cid), "evidence")
        )
        facts = resolve_evidence_facts(store, self._memories(), self._graph())
        self.assertEqual(facts[cid].memory_confidences, (0.2,))
        self.assertAlmostEqual(facts[cid].memory_confidence_mean, 0.2)

    def test_missing_memory_store_yields_no_confidences(self) -> None:
        store = _store()
        cid = store.add(_concept("no mirror"))
        store.add_edge(
            ConceptEdge("memory", "1", "concept", str(cid), "evidence")
        )
        facts = resolve_evidence_facts(store, None, self._graph())
        self.assertEqual(facts[cid].memory_confidences, ())
        # Span still resolves -- it only needs the topic graph.
        self.assertEqual(facts[cid].cluster_span, 1)


class BuildConceptQualityTests(unittest.TestCase):
    def test_disabled_when_store_none(self) -> None:
        report = build_concept_quality(None, None, None, None)
        self.assertFalse(report["enabled"])

    def test_report_carries_the_resolved_signals(self) -> None:
        store = _store()
        c = _concept("single-topic belief")
        c.status = "active"
        c.distinct_source_count = 3
        c.evidence_count = 3
        cid = store.add(c)
        for mid in (1, 2, 3):
            store.add_edge(
                ConceptEdge("memory", str(mid), "concept", str(cid), "evidence")
            )
        graph = GraphStub([
            types.SimpleNamespace(
                representative_id=100, summary="baths", member_ids=(1, 2, 3)
            )
        ])
        memories = MemStub({
            mid: types.SimpleNamespace(content="x", confidence=0.3)
            for mid in (1, 2, 3)
        })

        report = build_concept_quality(store, memories, graph, None)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["totals"]["total"], 1)
        # Both A and B fire on this one concept.
        self.assertEqual(report["evidence"]["single_cluster_active"], 1)
        self.assertEqual(report["evidence"]["weak_memory_active"], 1)

    def test_event_store_failure_does_not_sink_the_report(self) -> None:
        class BrokenEvents:
            def counts_by_type(self):
                raise RuntimeError("boom")

        store = _store()
        store.add(_concept("still fine"))
        report = build_concept_quality(store, None, None, BrokenEvents())
        self.assertTrue(report["enabled"])
        self.assertEqual(report["flow"]["promotion_rate_pct"], 0.0)


def _concept(label: str) -> Concept:
    return Concept(label=label, embedding=np.zeros(0, dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
