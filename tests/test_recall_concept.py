"""L5 concept recall: ``RagRetriever.recall_concept`` + ``RecallConceptTool``.

Covers the self-contained bundle shape (concept + capped evidence
memories + supporting cluster labels), the ``all_evidence`` cap lift,
empty-when-sparse behaviour, the tool JSON contract, and the
tool-pass-gate family membership.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.rag.rag_retriever import RagRetriever
from app.llm.tools.base import ToolError
from app.llm.tools.builtins import RecallConceptTool


def _e0(dim: int = 4) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[0] = 1.0
    return v


class _Embedder:
    def embed(self, text: str) -> np.ndarray:
        return _e0()


class _Edge:
    def __init__(
        self, src_type: str, src_id: Any, relation: str = "evidence",
        *, dst_type: str = "concept", dst_id: Any = 0,
    ) -> None:
        self.src_type = src_type
        self.src_id = str(src_id)
        self.dst_type = dst_type
        self.dst_id = str(dst_id)
        self.relation = relation
        self.ordinal = None


class _ConceptStore:
    def __init__(
        self, matches, edges, *, concepts=None, dependents=None,
        edges_from=None, edges_into=None,
    ) -> None:
        self._matches = matches
        self._edges = edges
        self._concepts = {int(k): v for k, v in (concepts or {}).items()}
        self._dependents = dependents or {}
        self._edges_from = edges_from or {}
        self._edges_into = edges_into or {}
        self.nearest_calls: list[dict] = []

    def nearest(self, q, *, subject=None, status=None, k=8, **_kw):
        self.nearest_calls.append(
            {"subject": subject, "status": status, "k": k}
        )
        return list(self._matches)

    def evidence_of(self, concept_id):
        return list(self._edges)

    def get(self, concept_id):
        return self._concepts.get(int(concept_id))

    def dependents_of(self, concept_id):
        return list(self._dependents.get(int(concept_id), []))

    def edges_from(self, node_type, node_id):
        return list(self._edges_from.get(str(node_id), []))

    def edges_into(self, node_type, node_id):
        return list(self._edges_into.get(str(node_id), []))


class _Cluster:
    def __init__(self, rep: int, summary: str, member_ids) -> None:
        self.representative_id = rep
        self.summary = summary
        self.member_ids = tuple(member_ids)

    @property
    def size(self) -> int:
        return len(self.member_ids)


class _Graph:
    persistent = True

    def __init__(self, clusters) -> None:
        self._clusters = clusters

    def topic_clusters(self):
        return list(self._clusters)


class _Mem:
    def __init__(self, mid: int, content: str) -> None:
        self.id = mid
        self.content = content


class _MemoryStore:
    def __init__(self, mems: dict[int, _Mem]) -> None:
        self._mems = mems

    def get(self, mid: int):
        return self._mems.get(int(mid))


class _StubStore:
    def search_memories(self, *_a: Any, **_k: Any):
        return []

    def search_messages(self, *_a: Any, **_k: Any):
        return []

    def search_documents(self, *_a: Any, **_k: Any):
        return []


def _concept(cid=1, label="Jacob enjoys understanding systems", confidence=0.82):
    return SimpleNamespace(
        concept_id=cid,
        label=label,
        kind="identity",
        subject="user",
        status="active",
        confidence=confidence,
        rationale="links several technical clusters",
    )


def _meta(cid, label, *, kind="tension", subject="user", status="active"):
    return SimpleNamespace(
        concept_id=cid, label=label, kind=kind, subject=subject,
        status=status, confidence=0.7, rationale="",
    )


def _build(*, matches, edges, clusters, mems, store_kwargs=None):
    retriever = RagRetriever(
        _StubStore(),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        top_k=4,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        memory_store=_MemoryStore(mems),  # type: ignore[arg-type]
        cluster_diversity_enabled=False,
        topic_expansion_enabled=False,
    )
    retriever.set_topic_graph(_Graph(clusters))  # type: ignore[arg-type]
    retriever.set_concept_store(
        _ConceptStore(matches, edges, **(store_kwargs or {}))  # type: ignore[arg-type]
    )
    return retriever


class RecallConceptRetrieverTests(unittest.TestCase):
    def test_bundle_shape(self) -> None:
        c = _concept()
        edges = [_Edge("cluster", 100)]
        clusters = [_Cluster(100, "distributed systems", [1, 2, 3])]
        mems = {
            1: _Mem(1, "debugging the CPU scheduler"),
            2: _Mem(2, "self-hosting the whole stack"),
            3: _Mem(3, "reverse-engineering a protocol"),
        }
        r = _build(matches=[(c, 0.7)], edges=edges, clusters=clusters, mems=mems)
        out = r.recall_concept("how I approach problems")
        self.assertIsNotNone(out)
        self.assertEqual(out["concept"]["label"], c.label)
        self.assertEqual(out["concept"]["subject"], "user")
        self.assertEqual(out["clusters"], ["distributed systems"])
        self.assertEqual(len(out["evidence"]), 3)
        self.assertIn("text", out["evidence"][0])

    def test_queries_active_concepts_across_subjects(self) -> None:
        # Broadened from user-only so self-directed questions can reach
        # subject=aiko self-concepts; subject is left unfiltered (None).
        c = _concept()
        r = _build(matches=[(c, 0.7)], edges=[], clusters=[], mems={})
        r.recall_concept("x")
        call = r._concept_store.nearest_calls[0]  # type: ignore[attr-defined]
        self.assertIsNone(call["subject"])
        self.assertEqual(call["status"], "active")

    def test_surfaces_aiko_self_concept(self) -> None:
        # A first-person self-concept (subject=aiko, memory-typed evidence)
        # is recalled and reported as hers.
        c = _concept(
            label="I deflect with teasing when I feel exposed",
        )
        c.subject = "aiko"
        edges = [_Edge("memory", 5)]
        mems = {5: _Mem(5, "kept up the act of indifference")}
        r = _build(matches=[(c, 0.7)], edges=edges, clusters=[], mems=mems)
        out = r.recall_concept("what are you like")
        self.assertIsNotNone(out)
        self.assertEqual(out["concept"]["subject"], "aiko")
        self.assertEqual(out["evidence"][0]["text"], "kept up the act of indifference")

    def test_cap_and_all_evidence(self) -> None:
        c = _concept()
        edges = [_Edge("cluster", 100)]
        member_ids = list(range(1, 11))  # 10 members
        clusters = [_Cluster(100, "systems", member_ids)]
        mems = {i: _Mem(i, f"memory {i}") for i in member_ids}
        r = _build(matches=[(c, 0.7)], edges=edges, clusters=clusters, mems=mems)
        capped = r.recall_concept("x", limit=3)
        self.assertEqual(len(capped["evidence"]), 3)
        full = r.recall_concept("x", limit=3, all_evidence=True)
        self.assertEqual(len(full["evidence"]), 10)

    def test_direct_memory_edges(self) -> None:
        c = _concept()
        edges = [_Edge("memory", 5), _Edge("memory", 6)]
        mems = {5: _Mem(5, "note five"), 6: _Mem(6, "note six")}
        r = _build(matches=[(c, 0.7)], edges=edges, clusters=[], mems=mems)
        out = r.recall_concept("x")
        texts = {e["text"] for e in out["evidence"]}
        self.assertEqual(texts, {"note five", "note six"})
        self.assertEqual(out["clusters"], [])

    def test_none_when_no_match(self) -> None:
        r = _build(matches=[], edges=[], clusters=[], mems={})
        self.assertIsNone(r.recall_concept("x"))

    def test_none_when_below_similarity_floor(self) -> None:
        c = _concept()
        r = _build(matches=[(c, 0.05)], edges=[], clusters=[], mems={})
        self.assertIsNone(r.recall_concept("x", min_concept_sim=0.20))

    def test_related_includes_dependent_meta(self) -> None:
        # A base concept whose lookup surfaces the tension meta that
        # references it, tagged with the meta's kind as the relation.
        base = _concept(cid=1, label="values honesty")
        meta = _meta(10, "honesty vs kindness pull", kind="tension")
        r = _build(
            matches=[(base, 0.7)], edges=[], clusters=[], mems={},
            store_kwargs={"concepts": {10: meta}, "dependents": {1: [10]}},
        )
        out = r.recall_concept("why do you think I value honesty")
        self.assertIn("related", out)
        rels = out["related"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["label"], "honesty vs kindness pull")
        self.assertEqual(rels[0]["relation"], "tension")

    def test_related_includes_base_concepts_of_meta(self) -> None:
        # A tension meta surfaces its concept-typed evidence (its two bases).
        meta = _meta(10, "honesty vs kindness pull", kind="tension")
        base_a = _concept(cid=1, label="values honesty")
        base_b = _concept(cid=2, label="wants to spare feelings")
        edges = [_Edge("concept", 1), _Edge("concept", 2)]
        r = _build(
            matches=[(meta, 0.7)], edges=edges, clusters=[], mems={},
            store_kwargs={"concepts": {1: base_a, 2: base_b}},
        )
        out = r.recall_concept("why the pull")
        labels = {rel["label"] for rel in out["related"]}
        self.assertEqual(labels, {"values honesty", "wants to spare feelings"})
        self.assertTrue(
            all(rel["relation"] == "references" for rel in out["related"])
        )

    def test_related_skips_inactive_neighbour(self) -> None:
        base = _concept(cid=1, label="values honesty")
        meta = _meta(10, "retired tension", status="retired")
        r = _build(
            matches=[(base, 0.7)], edges=[], clusters=[], mems={},
            store_kwargs={"concepts": {10: meta}, "dependents": {1: [10]}},
        )
        out = r.recall_concept("x")
        self.assertEqual(out["related"], [])

    def test_related_empty_without_concept_edges(self) -> None:
        c = _concept()
        edges = [_Edge("cluster", 100)]
        clusters = [_Cluster(100, "systems", [1])]
        mems = {1: _Mem(1, "a note")}
        r = _build(matches=[(c, 0.7)], edges=edges, clusters=clusters, mems=mems)
        out = r.recall_concept("x")
        self.assertEqual(out["related"], [])

    def test_rationale_survives_past_280(self) -> None:
        # The bundle cap was raised 280 -> 500: a 350-char rationale is kept
        # whole, and a very long one is trimmed to 500.
        long = "r" * 350
        c = _concept()
        c.rationale = long
        r = _build(matches=[(c, 0.7)], edges=[], clusters=[], mems={})
        out = r.recall_concept("x")
        self.assertEqual(len(out["concept"]["rationale"]), 350)

        c.rationale = "r" * 600
        out2 = r.recall_concept("x")
        self.assertEqual(len(out2["concept"]["rationale"]), 500)

    def test_none_when_store_not_wired(self) -> None:
        retriever = RagRetriever(
            _StubStore(),  # type: ignore[arg-type]
            _Embedder(),  # type: ignore[arg-type]
            top_k=4,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
        )
        self.assertIsNone(retriever.recall_concept("x"))


class _FakeRag:
    def __init__(self, bundle) -> None:
        self._bundle = bundle
        self.calls: list[dict] = []

    def recall_concept(self, query, *, limit=8, all_evidence=False):
        self.calls.append(
            {"query": query, "limit": limit, "all_evidence": all_evidence}
        )
        return self._bundle


class RecallConceptToolTests(unittest.TestCase):
    def test_returns_bundle_json(self) -> None:
        bundle = {
            "concept": {"label": "systems thinker", "confidence": 0.8},
            "evidence": [{"text": "a"}],
            "clusters": ["systems"],
        }
        tool = RecallConceptTool(_FakeRag(bundle))
        out = json.loads(tool.run({"query": "what do you think of me"}))
        self.assertEqual(out["concept"]["label"], "systems thinker")

    def test_passes_all_evidence_and_limit(self) -> None:
        rag = _FakeRag({"concept": {}, "evidence": [], "clusters": []})
        tool = RecallConceptTool(rag)
        tool.run({"query": "q", "limit": 20, "all_evidence": True})
        # limit clamped to 15; all_evidence forwarded.
        self.assertEqual(rag.calls[0]["limit"], 15)
        self.assertTrue(rag.calls[0]["all_evidence"])

    def test_empty_when_no_concept(self) -> None:
        tool = RecallConceptTool(_FakeRag(None))
        out = json.loads(tool.run({"query": "q"}))
        self.assertIsNone(out["concept"])
        self.assertIn("note", out)

    def test_requires_query(self) -> None:
        tool = RecallConceptTool(_FakeRag(None))
        with self.assertRaises(ToolError):
            tool.run({"query": "   "})

    def test_schema_advertises_params(self) -> None:
        schema = RecallConceptTool(_FakeRag(None)).schema()
        self.assertEqual(schema.name, "recall_concept")
        props = schema.parameters["properties"]
        self.assertIn("query", props)
        self.assertIn("all_evidence", props)
        self.assertEqual(schema.parameters["required"], ["query"])


class ToolFamilyTests(unittest.TestCase):
    def test_recall_concept_in_recall_family(self) -> None:
        from app.core.session.tool_pass_gate import _TOOL_FAMILY

        self.assertEqual(_TOOL_FAMILY.get("recall_concept"), "recall")


if __name__ == "__main__":
    unittest.main()
