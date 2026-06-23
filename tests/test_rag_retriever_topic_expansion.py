"""F10c topic multi-hop expansion + F10d cluster-scoped recall.

F10c: when a turn's strongest memory hit belongs to a topic cluster, the
retriever appends a couple of that cluster's sibling members (beyond the
top-k) as ``expansion`` hits, gated by a trigger score and a per-sibling
cosine floor, and ``format_block`` renders them in a separate section.

F10d: ``RagRetriever.recall_topic`` does a coarse centroid match to a
single cluster then drills into its members ranked by cosine; the
``recall_topic`` tool wraps it.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import MemoryRecord, RagHit
from app.llm.tools.builtins import RecallTopicTool


def _e(idx: int, dim: int = 4) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[idx] = 1.0
    return v


def _unit(seed: list[float]) -> np.ndarray:
    v = np.asarray(seed, dtype=np.float32)
    return v / float(np.linalg.norm(v))


class _Embedder:
    """Always embeds to e0 so member cosines are controlled by the stubs."""

    DIM = 4

    def embed(self, text: str) -> np.ndarray:
        return _e(0)


@dataclass
class _Mem:
    id: int
    content: str
    embedding: np.ndarray
    kind: str = "fact"
    salience: float = 0.5
    source_session: str | None = None
    source_message_id: int | None = None
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    use_count: int = 0
    tier: str = "long_term"


class _MemoryStore:
    def __init__(self, mems: dict[int, _Mem]) -> None:
        self._mems = mems
        self.marked: list[int] = []

    def get(self, memory_id: int) -> _Mem | None:
        return self._mems.get(int(memory_id))

    def mark_used(self, ids: list[int]) -> None:
        self.marked.extend(ids)


class _StubStore:
    def __init__(self, hits: list[RagHit]) -> None:
        self._hits = hits

    def search_memories(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return [RagHit(source=h.source, score=h.score, record=h.record) for h in self._hits]

    def search_messages(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []


class _StubGraph:
    persistent = True

    def __init__(
        self,
        *,
        cluster_of: dict[int, int] | None = None,
        members: dict[int, list[int]] | None = None,
        best: list[tuple[int, str, float]] | None = None,
    ) -> None:
        self._cluster_of = cluster_of or {}
        self._members = members or {}
        self._best = best or []

    def cluster_id_for(self, memory_id: int) -> int | None:
        return self._cluster_of.get(int(memory_id))

    def cluster_member_ids(self, cluster_id: int) -> list[int]:
        return list(self._members.get(int(cluster_id), []))

    def best_clusters_for(self, q: Any, *, top_n: int = 1, min_sim: float = 0.0) -> list[tuple[int, str, float]]:
        return list(self._best[:top_n])


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
    store_mems: dict[int, _Mem],
    graph: _StubGraph,
    top_k: int = 4,
    expansion: bool = True,
    expand_max: int = 2,
    trigger: float = 0.55,
    min_sim: float = 0.45,
) -> tuple[RagRetriever, _MemoryStore]:
    ms = _MemoryStore(store_mems)
    retriever = RagRetriever(
        _StubStore(hits),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        top_k=top_k,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        memory_store=ms,  # type: ignore[arg-type]
        cluster_diversity_enabled=False,
        topic_expansion_enabled=expansion,
        expand_max=expand_max,
        expand_trigger_score=trigger,
        expand_min_sim=min_sim,
    )
    retriever.set_topic_graph(graph)  # type: ignore[arg-type]
    return retriever, ms


def _ids(hits: list[RagHit]) -> list[int]:
    return [int(h.record.id) for h in hits]


class TopicExpansionTests(unittest.TestCase):
    def test_appends_close_siblings_beyond_topk(self) -> None:
        hits = [_mem_hit(1, 0.9)]
        store = {
            2: _Mem(2, "sibling two", _e(0)),   # cosine 1.0
            3: _Mem(3, "sibling three", _e(0)),  # cosine 1.0
            4: _Mem(4, "sibling four", _e(1)),   # cosine 0.0 -> excluded
        }
        graph = _StubGraph(cluster_of={1: 10}, members={10: [1, 2, 3, 4]})
        retriever, ms = _build(hits=hits, store_mems=store, graph=graph)
        results = retriever.retrieve("anything")
        self.assertEqual(_ids(results), [1, 2, 3])
        # The two appended hits are flagged as expansion; the anchor is not.
        flags = {int(h.record.id): h.expansion for h in results}
        self.assertFalse(flags[1])
        self.assertTrue(flags[2])
        self.assertTrue(flags[3])
        # Expansion hits flow through mark_used / revival snapshot.
        self.assertEqual(sorted(ms.marked), [1, 2, 3])
        self.assertEqual(sorted(retriever.last_surfaced_memory_ids), [1, 2, 3])

    def test_weak_anchor_does_not_trigger(self) -> None:
        hits = [_mem_hit(1, 0.4)]  # below trigger 0.55
        store = {2: _Mem(2, "sibling two", _e(0))}
        graph = _StubGraph(cluster_of={1: 10}, members={10: [1, 2]})
        retriever, _ = _build(hits=hits, store_mems=store, graph=graph)
        self.assertEqual(_ids(retriever.retrieve("x")), [1])

    def test_disabled_no_expansion(self) -> None:
        hits = [_mem_hit(1, 0.9)]
        store = {2: _Mem(2, "sibling two", _e(0))}
        graph = _StubGraph(cluster_of={1: 10}, members={10: [1, 2]})
        retriever, _ = _build(
            hits=hits, store_mems=store, graph=graph, expansion=False,
        )
        self.assertEqual(_ids(retriever.retrieve("x")), [1])

    def test_far_siblings_filtered_by_min_sim(self) -> None:
        hits = [_mem_hit(1, 0.9)]
        store = {2: _Mem(2, "far", _e(1)), 3: _Mem(3, "far too", _e(2))}
        graph = _StubGraph(cluster_of={1: 10}, members={10: [1, 2, 3]})
        retriever, _ = _build(hits=hits, store_mems=store, graph=graph)
        self.assertEqual(_ids(retriever.retrieve("x")), [1])

    def test_expand_max_caps_count(self) -> None:
        hits = [_mem_hit(1, 0.9)]
        store = {i: _Mem(i, f"s{i}", _e(0)) for i in (2, 3, 4, 5)}
        graph = _StubGraph(cluster_of={1: 10}, members={10: [1, 2, 3, 4, 5]})
        retriever, _ = _build(
            hits=hits, store_mems=store, graph=graph, expand_max=2,
        )
        results = retriever.retrieve("x")
        self.assertEqual(len(results), 3)  # anchor + 2

    def test_unclustered_anchor_no_expansion(self) -> None:
        hits = [_mem_hit(1, 0.9)]
        store = {2: _Mem(2, "s", _e(0))}
        graph = _StubGraph(cluster_of={1: None}, members={10: [1, 2]})
        retriever, _ = _build(hits=hits, store_mems=store, graph=graph)
        self.assertEqual(_ids(retriever.retrieve("x")), [1])


class FormatBlockExpansionTests(unittest.TestCase):
    def test_expansion_hits_render_in_own_section(self) -> None:
        direct = _mem_hit(1, 0.9)
        sib = _mem_hit(2, 0.8)
        sib.expansion = True
        block = RagRetriever.format_block([direct, sib], user_display_name="Jacob")
        self.assertIn("What you know about Jacob", block)
        self.assertIn("Related notes from the same topic", block)
        # The sibling text lives only under the expansion section.
        before, _, after = block.partition("Related notes from the same topic")
        self.assertIn("memory 1", before)
        self.assertIn("memory 2", after)
        self.assertNotIn("memory 2", before)


class RecallTopicTests(unittest.TestCase):
    def _retriever(self, *, store_mems, graph) -> RagRetriever:
        ms = _MemoryStore(store_mems)
        r = RagRetriever(
            _StubStore([]),  # type: ignore[arg-type]
            _Embedder(),  # type: ignore[arg-type]
            top_k=6,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
            memory_store=ms,  # type: ignore[arg-type]
        )
        r.set_topic_graph(graph)  # type: ignore[arg-type]
        return r

    def test_coarse_match_then_ranked_drill_in(self) -> None:
        store = {
            1: _Mem(1, "job at lab", _e(0)),                 # cosine 1.0
            2: _Mem(2, "office", _unit([0.8, 0.6, 0.0, 0.0])),  # cosine 0.8
            3: _Mem(3, "unrelated", _e(1)),                   # cosine 0.0
        }
        graph = _StubGraph(
            members={10: [1, 2, 3]},
            best=[(10, "my job", 0.9)],
        )
        r = self._retriever(store_mems=store, graph=graph)
        label, hits = r.recall_topic("tell me about my job", limit=2)
        self.assertEqual(label, "my job")
        self.assertEqual(_ids(hits), [1, 2])

    def test_no_cluster_match_returns_empty(self) -> None:
        graph = _StubGraph(members={}, best=[])
        r = self._retriever(store_mems={}, graph=graph)
        self.assertEqual(r.recall_topic("anything"), ("", []))

    def test_no_topic_graph_returns_empty(self) -> None:
        ms = _MemoryStore({})
        r = RagRetriever(
            _StubStore([]),  # type: ignore[arg-type]
            _Embedder(),  # type: ignore[arg-type]
            top_k=6,
            memory_store=ms,  # type: ignore[arg-type]
        )
        self.assertEqual(r.recall_topic("x"), ("", []))


class RecallTopicToolTests(unittest.TestCase):
    class _Rag:
        def __init__(self, ret: tuple[str, list[RagHit]]) -> None:
            self._ret = ret

        def recall_topic(self, topic: str, *, limit: int = 8) -> tuple[str, list[RagHit]]:
            return self._ret

    def test_run_returns_label_and_hits(self) -> None:
        hits = [_mem_hit(1, 0.9), _mem_hit(2, 0.7)]
        tool = RecallTopicTool(self._Rag(("my job", hits)))
        out = json.loads(tool.run({"topic": "job"}))
        self.assertEqual(out["topic_label"], "my job")
        self.assertEqual(len(out["hits"]), 2)
        self.assertEqual(out["hits"][0]["text"], "memory 1")

    def test_run_empty_has_note(self) -> None:
        tool = RecallTopicTool(self._Rag(("", [])))
        out = json.loads(tool.run({"topic": "nothing"}))
        self.assertEqual(out["hits"], [])
        self.assertIn("note", out)

    def test_run_requires_topic(self) -> None:
        from app.llm.tools.base import ToolError

        tool = RecallTopicTool(self._Rag(("", [])))
        with self.assertRaises(ToolError):
            tool.run({"topic": "   "})

    def test_schema_name(self) -> None:
        tool = RecallTopicTool(self._Rag(("", [])))
        self.assertEqual(tool.schema().name, "recall_topic")


if __name__ == "__main__":
    unittest.main()
