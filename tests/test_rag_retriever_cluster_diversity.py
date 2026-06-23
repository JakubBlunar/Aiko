"""F10b RAG retriever: cluster-aware diversity re-rank.

Verifies that ``RagRetriever`` caps how many of the final top-k hits may
come from a single topic cluster (via :meth:`TopicGraph.cluster_id_for`),
backfills from the deferred overflow so the top-k is never shrunk, and is
byte-identical to the plain score-sorted cut when diversity is disabled or
no topic graph is wired.
"""
from __future__ import annotations

import unittest
from typing import Any

import numpy as np

from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import MemoryRecord, RagHit


class _Embedder:
    DIM = 8

    def embed(self, text: str) -> np.ndarray:
        vec = np.ones(self.DIM, dtype=np.float32)
        return vec / float(np.linalg.norm(vec))


class _StubStore:
    """Returns a fixed set of memory hits; no message / document hits."""

    def __init__(self, hits: list[RagHit]) -> None:
        self._hits = hits

    def search_memories(self, *_a: Any, **_k: Any) -> list[RagHit]:
        # Fresh copies so per-call score mutation doesn't leak between tests.
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._hits
        ]

    def search_messages(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []


class _StubTopicGraph:
    """Maps memory id -> cluster id; ``None`` means unclustered."""

    persistent = True

    def __init__(self, mapping: dict[int, int | None]) -> None:
        self._mapping = mapping

    def cluster_id_for(self, memory_id: int) -> int | None:
        return self._mapping.get(int(memory_id))


def _mem_hit(mem_id: int, score: float) -> RagHit:
    return RagHit(
        source="memory",
        score=score,
        record=MemoryRecord(
            id=str(mem_id),
            content=f"memory {mem_id}",
            kind="fact",
            salience=0.5,
            source_session=None,
            source_message_id=None,
            created_at=None,
            last_used_at=None,
            use_count=0,
        ),
    )


def _build(
    *,
    hits: list[RagHit],
    mapping: dict[int, int | None] | None,
    top_k: int,
    max_per_cluster: int,
    diversity: bool = True,
) -> RagRetriever:
    retriever = RagRetriever(
        _StubStore(hits),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        top_k=top_k,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        memory_store=None,
        cluster_diversity_enabled=diversity,
        max_per_cluster=max_per_cluster,
    )
    if mapping is not None:
        retriever.set_topic_graph(_StubTopicGraph(mapping))  # type: ignore[arg-type]
    return retriever


def _ids(hits: list[RagHit]) -> list[int]:
    return [int(h.record.id) for h in hits]


class ClusterDiversityTests(unittest.TestCase):
    def test_cap_spreads_across_clusters(self) -> None:
        # ids 1-4 in cluster A (highest scores), ids 5-6 in cluster B.
        hits = [_mem_hit(i, 1.0 - 0.01 * i) for i in range(1, 7)]
        mapping = {1: 10, 2: 10, 3: 10, 4: 10, 5: 20, 6: 20}
        retriever = _build(
            hits=hits, mapping=mapping, top_k=4, max_per_cluster=2,
        )
        results = retriever.retrieve("anything")
        # Cluster A capped at 2 -> top two of A, then cluster B fills.
        self.assertEqual(_ids(results), [1, 2, 5, 6])

    def test_disabled_is_plain_score_cut(self) -> None:
        hits = [_mem_hit(i, 1.0 - 0.01 * i) for i in range(1, 7)]
        mapping = {1: 10, 2: 10, 3: 10, 4: 10, 5: 20, 6: 20}
        retriever = _build(
            hits=hits,
            mapping=mapping,
            top_k=4,
            max_per_cluster=2,
            diversity=False,
        )
        results = retriever.retrieve("anything")
        self.assertEqual(_ids(results), [1, 2, 3, 4])

    def test_no_topic_graph_is_plain_score_cut(self) -> None:
        hits = [_mem_hit(i, 1.0 - 0.01 * i) for i in range(1, 7)]
        retriever = _build(
            hits=hits, mapping=None, top_k=4, max_per_cluster=2,
        )
        results = retriever.retrieve("anything")
        self.assertEqual(_ids(results), [1, 2, 3, 4])

    def test_backfill_never_shrinks_topk(self) -> None:
        # Every hit in one cluster: cap defers the rest, backfill restores
        # the full top-k in score order.
        hits = [_mem_hit(i, 1.0 - 0.01 * i) for i in range(1, 7)]
        mapping = {i: 10 for i in range(1, 7)}
        retriever = _build(
            hits=hits, mapping=mapping, top_k=4, max_per_cluster=2,
        )
        results = retriever.retrieve("anything")
        self.assertEqual(_ids(results), [1, 2, 3, 4])

    def test_unclustered_hits_are_uncapped(self) -> None:
        # ids 1-3 in cluster A, ids 4-5 unclustered (None).
        hits = [_mem_hit(i, 1.0 - 0.01 * i) for i in range(1, 6)]
        mapping = {1: 10, 2: 10, 3: 10, 4: None, 5: None}
        retriever = _build(
            hits=hits, mapping=mapping, top_k=4, max_per_cluster=2,
        )
        results = retriever.retrieve("anything")
        # A capped at 2 (ids 1,2); id 3 deferred; unclustered 4,5 admitted.
        self.assertEqual(_ids(results), [1, 2, 4, 5])


class ClusterIdForTests(unittest.TestCase):
    def test_non_persistent_returns_none(self) -> None:
        from app.core.conversation.topic_graph import TopicGraph

        class _MS:
            def snapshot(self) -> list[Any]:
                return []

        graph = TopicGraph(_MS())  # type: ignore[arg-type]
        self.assertFalse(graph.persistent)
        self.assertIsNone(graph.cluster_id_for(1))

    def test_persistent_reads_assignment(self) -> None:
        from app.core.conversation.topic_graph import TopicGraph

        class _MS:
            def snapshot(self) -> list[Any]:
                return []

        class _Store:
            pass

        graph = TopicGraph(_MS(), cluster_store=_Store())  # type: ignore[arg-type]
        self.assertTrue(graph.persistent)
        graph._assignment = {7: 42}  # type: ignore[attr-defined]
        self.assertEqual(graph.cluster_id_for(7), 42)
        self.assertIsNone(graph.cluster_id_for(999))
        self.assertIsNone(graph.cluster_id_for("bad"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
