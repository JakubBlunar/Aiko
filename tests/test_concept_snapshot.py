"""Tests for :func:`app.core.concepts.concept_snapshot.build_concepts_snapshot`."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

from app.core.infra import timephrase

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
        self.gets = 0

    def get(self, mid):
        self.gets += 1
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


class PagingTests(unittest.TestCase):
    """The snapshot never truncates, so it has to page instead. A mature
    graph resolves thousands of evidence edges into megabytes of JSON,
    which is enough to lock up a phone in transit and again in render."""

    def _graph_of(self, n: int) -> tuple[ConceptStore, MemStub]:
        """``n`` concepts, one memory evidence edge each, descending
        confidence so the sort order is known up front."""
        store = _store()
        for i in range(n):
            c = _concept(f"belief {i}")
            c.confidence = 1.0 - i / 100.0
            cid = store.add(c)
            store.add_edge(
                ConceptEdge("memory", str(i), "concept", str(cid), "evidence")
            )
        rows = {
            i: types.SimpleNamespace(content=f"memory {i}") for i in range(n)
        }
        return store, MemStub(rows)

    def test_a_page_is_bounded_but_still_reports_the_whole(self) -> None:
        store, mem = self._graph_of(10)
        snap = build_concepts_snapshot(store, mem, None, limit=3)
        self.assertEqual(len(snap["concepts"]), 3)
        self.assertEqual(snap["total"], 10)
        self.assertEqual(snap["matched"], 10)
        self.assertEqual(snap["counts"]["by_status"], {"candidate": 10})

    def test_evidence_is_resolved_only_for_the_page(self) -> None:
        # The point of the whole exercise: cost tracks the page, not the
        # graph. Resolving all 40 edges to serve 3 rows is the freeze.
        store, mem = self._graph_of(40)
        build_concepts_snapshot(store, mem, None, limit=3)
        self.assertEqual(mem.gets, 3)

    def test_paging_walks_the_graph_without_gaps_or_repeats(self) -> None:
        store, mem = self._graph_of(10)
        seen: list[int] = []
        for offset in range(0, 10, 4):
            snap = build_concepts_snapshot(
                store, mem, None, limit=4, offset=offset
            )
            seen.extend(c["id"] for c in snap["concepts"])
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 10)

    def test_tied_confidences_still_page_deterministically(self) -> None:
        # Without a stable tie-break, equal-confidence rows can reorder
        # between two requests and a row lands on both pages or neither.
        store = _store()
        for i in range(6):
            c = _concept(f"tied {i}")
            c.confidence = 0.5
            store.add(c)
        first = build_concepts_snapshot(store, None, None, limit=3)
        second = build_concepts_snapshot(store, None, None, limit=3, offset=3)
        ids = [c["id"] for c in first["concepts"] + second["concepts"]]
        self.assertEqual(len(set(ids)), 6)
        # And the same request twice gives the same page.
        again = build_concepts_snapshot(store, None, None, limit=3)
        self.assertEqual(
            [c["id"] for c in first["concepts"]],
            [c["id"] for c in again["concepts"]],
        )

    def test_a_filter_narrows_the_page_but_not_the_counts(self) -> None:
        store = _store()
        for status in ("active", "active", "dormant"):
            c = _concept(f"a {status}")
            c.status = status
            store.add(c)
        snap = build_concepts_snapshot(store, None, None, status="active")
        self.assertEqual(len(snap["concepts"]), 2)
        self.assertEqual(snap["matched"], 2)
        # ``total`` and the pills still describe the whole store, so the
        # filter UI can show what it would select before you select it.
        self.assertEqual(snap["total"], 3)
        self.assertEqual(
            snap["counts"]["by_status"], {"active": 2, "dormant": 1}
        )

    def test_subject_and_status_narrow_together(self) -> None:
        store = _store()
        for subject, status in (
            ("user", "active"),
            ("user", "dormant"),
            ("aiko", "active"),
        ):
            c = _concept(f"{subject}/{status}")
            c.subject = subject
            c.status = status
            store.add(c)
        snap = build_concepts_snapshot(
            store, None, None, status="active", subject="user"
        )
        self.assertEqual(snap["matched"], 1)
        self.assertEqual(snap["concepts"][0]["subject"], "user")

    def test_paging_past_the_end_is_empty_not_an_error(self) -> None:
        store, mem = self._graph_of(3)
        snap = build_concepts_snapshot(store, mem, None, limit=5, offset=99)
        self.assertEqual(snap["concepts"], [])
        self.assertEqual(snap["matched"], 3)

    def test_no_limit_still_means_the_whole_graph(self) -> None:
        # Unparameterised callers (the MCP dump's widest page, tests)
        # must keep seeing everything.
        store, mem = self._graph_of(7)
        snap = build_concepts_snapshot(store, mem, None)
        self.assertEqual(len(snap["concepts"]), 7)


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
        self.assertTrue(any("teasing me into a pout" in label for label in labels))

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


class ImportanceFieldTests(unittest.TestCase):
    """L32: the axis rides along on the reads that already happened.

    The failure mode worth guarding is silence -- importance is optional
    everywhere, so a broken join produces a snapshot that looks fine and
    is quietly missing an axis.
    """

    def _graph(self):
        return GraphStub([
            types.SimpleNamespace(
                cluster_id=7,
                representative_id=100,
                summary="work stress",
                member_ids=(100, 101),
            )
        ])

    def _affect(self, key):
        # A charged cluster, so the lift is visible rather than inferred.
        if key.endswith(".user"):
            return json.dumps({
                "7": {
                    "valence": -0.9, "arousal": 0.8, "samples": 12,
                    "valence_samples": 12,
                    "updated_at": timephrase.utcnow().isoformat(),
                }
            })
        return None

    def _grounded_store(self, kind: str = "boundary"):
        store = _store()
        c = _concept("go gentler about work")
        c.kind = kind
        c.subject = "user"
        c.status = "active"
        cid = store.add(c)
        store.add_edge(
            ConceptEdge("cluster", "100", "concept", str(cid), "evidence")
        )
        return store

    def test_the_axis_is_absent_without_a_kv_reader(self) -> None:
        # No affect source means no importance, and dropping the fields is
        # the honest answer -- a bare kind prior dressed up as the full
        # number would read as "no affect on its topics".
        snap = build_concepts_snapshot(
            self._grounded_store(), MemStub({}), self._graph()
        )
        self.assertNotIn("importance", snap["concepts"][0])

    def test_a_grounded_concept_is_lifted_by_its_topic(self) -> None:
        snap = build_concepts_snapshot(
            self._grounded_store(), MemStub({}), self._graph(),
            kv_get=self._affect,
        )
        row = snap["concepts"][0]
        self.assertGreater(row["importance_charge"], 0.0)
        self.assertGreater(row["importance"], row["importance_prior"])
        self.assertLessEqual(row["importance"], 1.0)

    def test_the_prior_tracks_the_kind(self) -> None:
        def prior(kind: str) -> float:
            snap = build_concepts_snapshot(
                self._grounded_store(kind), MemStub({}), self._graph(),
                kv_get=self._affect,
            )
            return snap["concepts"][0]["importance_prior"]

        self.assertGreater(prior("boundary"), prior("taste"))

    def test_a_broken_kv_read_drops_the_axis_not_the_snapshot(self) -> None:
        def explode(_key):
            raise RuntimeError("kv is down")

        snap = build_concepts_snapshot(
            self._grounded_store(), MemStub({}), self._graph(),
            kv_get=explode,
        )
        # load_map swallows the read, so the context still builds -- what
        # matters is that the page came back whole either way.
        self.assertTrue(snap["enabled"])
        self.assertEqual(len(snap["concepts"]), 1)

    def test_the_quality_report_measures_the_attention_gap(self) -> None:
        # The section's reason for existing: an active belief that matters
        # more than it is established.
        store = _store()
        c = _concept("be gentle about his work stress")
        c.kind = "boundary"
        c.subject = "user"
        c.status = "active"
        c.confidence = 0.4
        cid = store.add(c)
        store.add_edge(
            ConceptEdge("cluster", "100", "concept", str(cid), "evidence")
        )
        report = build_concept_quality(
            store, MemStub({}), self._graph(), None, kv_get=self._affect,
        )
        section = report["importance"]
        self.assertEqual(section["active"], 1)
        self.assertEqual(section["affect_lifted"], 1)
        self.assertEqual(section["attention_gap"], 1)
        self.assertEqual(
            section["attention_gap_sample"][0]["id"], cid
        )

    def test_the_importance_section_is_empty_without_a_kv_reader(self) -> None:
        store = _store()
        store.add(_concept("no affect source"))
        report = build_concept_quality(store, None, None, None)
        self.assertEqual(report["importance"], {})


def _concept(label: str) -> Concept:
    return Concept(label=label, embedding=np.zeros(0, dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
