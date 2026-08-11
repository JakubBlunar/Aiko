"""Unified context-budget: selector allocation, budget sizing, and the
``RagRetriever.candidates`` / ``mark_surfaced`` pool contract."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from app.core.session.context_budget_selector import (
    ContextBudgetSelector,
    ContextCandidate,
    RelevantContext,
    SourceBudget,
)
from app.core.session.prompt_assembler_helpers_mixin import (
    PromptAssemblerHelpersMixin,
)
from app.core.infra import timephrase
from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import MemoryRecord, RagHit


def _cand(source: str, relevance: float, tokens: int, order: int) -> ContextCandidate:
    return ContextCandidate(
        source=source, relevance=relevance, tokens=tokens, order=order,
        payload=(source, order), key=f"{source}{order}",
    )


class SelectorTests(unittest.TestCase):
    def _selector(self, **overrides: object) -> ContextBudgetSelector:
        base = {
            "memory": SourceBudget(floor=1, cap=8, weight=1.0, min_relevance=0.0),
            "cluster": SourceBudget(floor=0, cap=3, weight=1.0, min_relevance=0.3),
            "concept": SourceBudget(floor=0, cap=3, weight=1.0, min_relevance=0.3),
        }
        base.update(overrides)  # type: ignore[arg-type]
        return ContextBudgetSelector(base)

    def test_never_exceeds_budget(self) -> None:
        sel = self._selector()
        cands = {
            "memory": [_cand("memory", 0.9 - i * 0.01, 30, i) for i in range(20)],
            "cluster": [],
            "concept": [],
        }
        out = sel.select(cands, budget_tokens=100)
        self.assertLessEqual(out.used_tokens, 100)
        # 100 / 30 = 3 items fit.
        self.assertEqual(out.source("memory").count, 3)

    def test_floor_reserved_even_against_higher_relevance(self) -> None:
        # A single concept floor is guaranteed a toehold even though every
        # memory outscores it.
        sel = self._selector(
            concept=SourceBudget(floor=1, cap=3, weight=1.0, min_relevance=0.0),
        )
        cands = {
            "memory": [_cand("memory", 0.95, 40, i) for i in range(5)],
            "cluster": [],
            "concept": [_cand("concept", 0.4, 40, 0)],
        }
        out = sel.select(cands, budget_tokens=120)  # 3 items
        self.assertEqual(out.source("concept").count, 1)
        self.assertGreaterEqual(out.source("memory").count, 1)

    def test_min_relevance_drops_noise(self) -> None:
        sel = self._selector()
        cands = {
            "memory": [],
            "cluster": [_cand("cluster", 0.1, 10, 0)],  # below 0.3
            "concept": [_cand("concept", 0.5, 10, 0)],
        }
        out = sel.select(cands, budget_tokens=1000)
        self.assertEqual(out.source("cluster").count, 0)
        self.assertEqual(out.source("cluster").dropped_for_relevance, 1)
        self.assertEqual(out.source("concept").count, 1)

    def test_weight_biases_ranking(self) -> None:
        # Equal raw relevance, but concept weight 2x -> concept wins the one
        # remaining slot after the memory floor.
        sel = self._selector(
            memory=SourceBudget(floor=1, cap=8, weight=1.0, min_relevance=0.0),
            concept=SourceBudget(floor=0, cap=3, weight=2.0, min_relevance=0.0),
        )
        cands = {
            "memory": [_cand("memory", 0.5, 40, 0), _cand("memory", 0.5, 40, 1)],
            "cluster": [],
            "concept": [_cand("concept", 0.5, 40, 0)],
        }
        out = sel.select(cands, budget_tokens=80)  # 2 slots
        self.assertEqual(out.source("memory").count, 1)  # floor only
        self.assertEqual(out.source("concept").count, 1)  # won the fill

    def test_cap_limits_source(self) -> None:
        sel = self._selector(
            memory=SourceBudget(floor=0, cap=2, weight=1.0, min_relevance=0.0),
        )
        cands = {
            "memory": [_cand("memory", 0.9 - i * 0.01, 10, i) for i in range(6)],
            "cluster": [],
            "concept": [],
        }
        out = sel.select(cands, budget_tokens=1000)
        self.assertEqual(out.source("memory").count, 2)
        self.assertGreaterEqual(out.source("memory").dropped_for_cap, 1)

    def test_degrade_2_floors_only(self) -> None:
        sel = self._selector(
            memory=SourceBudget(floor=1, cap=8, weight=1.0, min_relevance=0.0),
        )
        cands = {
            "memory": [_cand("memory", 0.9 - i * 0.01, 10, i) for i in range(6)],
            "cluster": [],
            "concept": [],
        }
        out = sel.select(cands, budget_tokens=1000, degrade_level=2)
        self.assertEqual(out.source("memory").count, 1)  # floor only
        self.assertEqual(out.degrade_level, 2)

    def test_pinned_bypasses_min_relevance_and_cap(self) -> None:
        # A low-relevance pinned concept survives the min_relevance gate AND
        # sits on top of a full cap of relevance picks.
        sel = self._selector(
            concept=SourceBudget(floor=0, cap=2, weight=1.0, min_relevance=0.3),
        )
        pinned = ContextCandidate(
            source="concept", relevance=0.05, tokens=10, order=0,
            payload=("id", 0), key="k1", pinned=True,
        )
        cands = {
            "memory": [],
            "cluster": [],
            "concept": [pinned]
            + [_cand("concept", 0.9 - i * 0.01, 10, 100 + i) for i in range(4)],
        }
        out = sel.select(cands, budget_tokens=1000)
        # 1 pinned + 2 relevance picks (cap only counts the non-pinned).
        self.assertEqual(out.source("concept").count, 3)
        self.assertEqual(out.source("concept").pinned, 1)

    def test_pinned_survives_degrade_2(self) -> None:
        sel = self._selector(
            concept=SourceBudget(floor=0, cap=3, weight=1.0, min_relevance=0.3),
        )
        pinned = ContextCandidate(
            source="concept", relevance=0.0, tokens=10, order=0,
            payload=("id", 0), key="k1", pinned=True,
        )
        cands = {
            "memory": [],
            "cluster": [],
            "concept": [pinned, _cand("concept", 0.9, 10, 100)],
        }
        out = sel.select(cands, budget_tokens=1000, degrade_level=2)
        # Floors-only degrade keeps the pinned item; the relevance pick drops.
        self.assertEqual(out.source("concept").count, 1)
        self.assertEqual(out.source("concept").pinned, 1)

    def test_pinned_still_clipped_to_budget(self) -> None:
        sel = self._selector(
            concept=SourceBudget(floor=0, cap=3, weight=1.0, min_relevance=0.0),
        )
        cands = {
            "memory": [],
            "cluster": [],
            "concept": [
                ContextCandidate(
                    source="concept", relevance=0.0, tokens=60, order=i,
                    payload=("id", i), key=f"k{i}", pinned=True,
                )
                for i in range(3)
            ],
        }
        out = sel.select(cands, budget_tokens=100)  # only ~1 fits
        self.assertLessEqual(out.used_tokens, 100)
        self.assertEqual(out.source("concept").pinned, 1)


class _Sizer:
    """Bare attribute holder for the pure sizing helper."""

    def __init__(self, **kw: object) -> None:
        self._context_budget_enabled = kw.get("enabled", True)
        self._context_budget_fraction = kw.get("fraction", 0.15)
        self._context_budget_max_tokens = kw.get("max_tokens", 4096)
        self._context_budget_min_tokens = kw.get("min_tokens", 256)
        self._context_budget_history_floor_tokens = kw.get("history_floor", 1024)

    size = PromptAssemblerHelpersMixin._size_context_budget


class SizingTests(unittest.TestCase):
    def test_fraction_of_window_capped(self) -> None:
        s = _Sizer(fraction=0.15, max_tokens=4096)
        # 64k * 0.15 = 9830, capped at 4096. Plenty of room.
        budget, degrade = s.size(
            context_window=64000, budget_tokens=60000,
            system_base_tokens=5000, user_tokens=100, aggressive=False,
        )
        self.assertEqual(budget, 4096)
        self.assertEqual(degrade, 0)

    def test_small_window_floored_to_min(self) -> None:
        s = _Sizer(fraction=0.15, max_tokens=4096, min_tokens=256)
        # 1000 * 0.15 = 150 < min_tokens 256; room exists so bump to 256.
        budget, _ = s.size(
            context_window=8000, budget_tokens=4000,
            system_base_tokens=500, user_tokens=100, aggressive=False,
        )
        # target = min(0.15*8000=1200, 4096) = 1200; avail large -> 1200.
        self.assertEqual(budget, 1200)

    def test_clamped_by_history_floor(self) -> None:
        s = _Sizer(fraction=0.5, max_tokens=100000, history_floor=1024)
        # avail = 4000 - 2000 - 100 = 1900; hi = 1900 - 1024 = 876.
        budget, degrade = s.size(
            context_window=8000, budget_tokens=4000,
            system_base_tokens=2000, user_tokens=100, aggressive=False,
        )
        self.assertEqual(budget, 876)
        self.assertEqual(degrade, 1)  # forced below target -> degrade 1

    def test_disabled_returns_zero(self) -> None:
        s = _Sizer(enabled=False)
        budget, _ = s.size(
            context_window=64000, budget_tokens=60000,
            system_base_tokens=5000, user_tokens=100, aggressive=False,
        )
        self.assertEqual(budget, 0)

    def test_aggressive_returns_degrade_2(self) -> None:
        s = _Sizer()
        _, degrade = s.size(
            context_window=64000, budget_tokens=60000,
            system_base_tokens=5000, user_tokens=100, aggressive=True,
        )
        self.assertEqual(degrade, 2)


class _StubStore:
    def __init__(self, memories: list[RagHit]) -> None:
        self._memories = memories

    def search_memories(self, *_a: object, **_k: object) -> list[RagHit]:
        return [RagHit(source=h.source, score=h.score, record=h.record)
                for h in self._memories]

    def search_messages(self, *_a: object, **_k: object) -> list[RagHit]:
        return []

    def search_documents(self, *_a: object, **_k: object) -> list[RagHit]:
        return []


class _StubEmbedder:
    def embed(self, _text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


class _RecordingMemoryStore:
    def __init__(self) -> None:
        self.mark_used_calls: list[list[int]] = []

    def mark_used(self, ids: object) -> None:
        self.mark_used_calls.append([int(i) for i in ids])  # type: ignore[union-attr]


def _mem_hit(mid: int, score: float) -> RagHit:
    return RagHit(
        source="memory", score=score,
        record=MemoryRecord(
            id=str(mid), content=f"memory {mid}", kind="fact", salience=0.5,
            source_session=None, source_message_id=None,
            created_at=None, last_used_at=None, use_count=0,
        ),
    )


class CandidatesPoolTests(unittest.TestCase):
    def _retriever(self, mem_store: object | None = None) -> RagRetriever:
        hits = [_mem_hit(i, 0.9 - i * 0.02) for i in range(15)]
        return RagRetriever(
            _StubStore(hits),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=6,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
            memory_store=mem_store,  # type: ignore[arg-type]
        )

    def test_candidates_returns_larger_pool_than_top_k(self) -> None:
        rag = self._retriever()
        pool = rag.candidates("q", pool_k=12)
        self.assertEqual(len(pool), 12)
        # retrieve() still honours the fixed top_k.
        self.assertEqual(len(rag.retrieve("q")), 6)

    def test_candidates_does_not_mark_used(self) -> None:
        store = _RecordingMemoryStore()
        rag = self._retriever(store)
        rag.candidates("q", pool_k=12)
        self.assertEqual(store.mark_used_calls, [])
        # ...but the time-window snapshot is still stamped for the guard.
        self.assertIsNone(rag.last_time_window)

    def test_mark_surfaced_stamps_only_subset(self) -> None:
        store = _RecordingMemoryStore()
        rag = self._retriever(store)
        pool = rag.candidates("q", pool_k=12)
        chosen = pool[:3]
        rag.mark_surfaced(chosen)
        self.assertEqual(len(store.mark_used_calls), 1)
        self.assertEqual(len(store.mark_used_calls[0]), 3)
        self.assertEqual(len(rag.last_surfaced_memory_ids), 3)

    def test_shared_embedding_is_reused(self) -> None:
        # When an embedding is passed, the embedder is not called again.
        rag = self._retriever()
        calls = {"n": 0}
        orig = rag._embedder.embed

        def _counting(text: str) -> np.ndarray:
            calls["n"] += 1
            return orig(text)

        rag._embedder.embed = _counting  # type: ignore[assignment]
        emb = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        rag.candidates("q", pool_k=8, embedding=emb)
        self.assertEqual(calls["n"], 0)


class _CEdge:
    def __init__(self, dst_type, dst_id, *, src_type=None, src_id=None,
                 relation="evidence"):
        self.dst_type = dst_type
        self.dst_id = str(dst_id)
        self.src_type = src_type
        self.src_id = str(src_id) if src_id is not None else None
        self.relation = relation


class _FakeConceptStore:
    def __init__(self, concepts: list[object], *, near_score: float = 0.6,
                 edges: dict | None = None) -> None:
        self._concepts = concepts
        self._near_score = near_score
        self._edges = edges or {}

    def nearest(self, _vec: object, *, status: str = "active", k: int = 8,
                subject: object = None, kind: object = None):
        # Status is honoured so the L30a hypothesis lane (the one reader
        # that asks for candidates) can be exercised against the same
        # fixture as the confident lanes.
        rows = [
            c for c in self._concepts
            if (status is None or getattr(c, "status", "active") == status)
            and (subject is None or getattr(c, "subject", None) == subject)
            and (kind is None or getattr(c, "kind", None) == kind)
        ]
        return [(c, self._near_score) for c in rows[:k]]

    def list_by(self, *, status: str | None = None, kind: str | None = None,
                subject: object = None, user_id: object = None):
        return [
            c for c in self._concepts
            if (status is None or getattr(c, "status", "active") == status)
            and (kind is None or getattr(c, "kind", None) == kind)
        ]

    def get(self, cid):
        return next(
            (c for c in self._concepts if int(getattr(c, "concept_id", 0)) == int(cid)),
            None,
        )

    def edges_from(self, node_type, node_id):
        return list(self._edges.get((node_type, str(node_id)), []))

    def evidence_of(self, _cid):
        return []

    def cluster_evidence_for(self, concept_ids):
        # L32: {concept_id: (representative memory id, ...)}. Read off the
        # same ``edges`` fixture the activation tests use -- it is keyed by
        # ``edges_from``, and a cluster evidence edge runs cluster ->
        # concept, so grounding a concept on a cluster there gets it an
        # affect lift here for free.
        wanted = {int(c) for c in concept_ids}
        out: dict[int, list[int]] = {}
        for (node_type, node_id), edges in self._edges.items():
            if node_type != "cluster":
                continue
            for e in edges:
                if e.dst_type != "concept" or e.relation != "evidence":
                    continue
                cid = int(e.dst_id)
                if cid in wanted:
                    out.setdefault(cid, []).append(int(node_id))
        return {cid: tuple(v) for cid, v in out.items()}

    def dependents_of(self, _cid):
        return []


class _FakeTopicGraph:
    def __init__(self, rows: list[tuple[int, str, float]],
                 clusters: list | None = None) -> None:
        self._rows = rows
        self._clusters = clusters or []

    def mature(self, *, min_clusters: int = 6) -> bool:
        return True

    def best_clusters_for(self, _vec: object, *, top_n: int = 1, min_sim: float = 0.0):
        return list(self._rows[:top_n])

    def cluster_id_for(self, _mid: int):
        return None

    def topic_clusters(self):
        return list(self._clusters)


def _ms() -> SimpleNamespace:
    return SimpleNamespace(
        context_budget_enabled=True,
        context_budget_memory_pool_k=18,
        context_budget_memory_floor=1,
        context_budget_memory_cap=8,
        context_budget_memory_weight=1.0,
        context_budget_memory_min_relevance=0.0,
        context_budget_cluster_floor=0,
        context_budget_cluster_cap=3,
        context_budget_cluster_weight=0.9,
        context_budget_cluster_min_relevance=0.3,
        context_budget_concept_floor=1,
        context_budget_concept_cap=3,
        context_budget_concept_weight=1.1,
        context_budget_concept_min_relevance=0.3,
        context_budget_core_cap=2,
        context_budget_core_min_confidence=0.75,
        # The two openness mechanisms are off here for the same reason
        # standing is: they deliberately override the ranking these
        # fixtures measure, so a legacy ranking test would start asserting
        # about the override instead of about the rank. Both ship on by
        # default and have their own tests (``OpennessWiringTests`` below,
        # plus ``tests/test_concept_diets.py`` for the selection itself).
        concept_core_openness_slots=0,
        concept_core_openness_min_confidence=0.5,
        concept_flex_generative_floor=0,
        concept_min_clusters=6,
        # Feature-focused tests opt in explicitly; keep legacy ranking fixtures
        # stable when no standing cache was part of their setup.
        concept_surfacing_standing_enabled=False,
        # L32: on, as it is in production. Every concept therefore carries
        # its kind's stake even with no affect data, which is the point --
        # these fixtures should rank the way the live path ranks.
        concept_importance_enabled=True,
        concept_importance_strength=0.4,
        concept_importance_affect_lift=0.5,
        concept_importance_affect_min_samples=3,
        concept_surfacing_overfetch=5,
        # L30a: on, as in production. Fixtures whose concepts are all
        # ``active`` are unaffected -- the lane reads candidates only.
        hypothesis_surfacing_enabled=True,
        context_budget_hypothesis_floor=0,
        context_budget_hypothesis_cap=1,
        context_budget_hypothesis_weight=0.7,
        context_budget_hypothesis_min_relevance=0.35,
        hypothesis_min_unsettled=0.22,
        hypothesis_min_sources=1,
    )


class RegionBuilderTests(unittest.TestCase):
    def _host(self, *, near_score: float = 0.6):
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        hits = [_mem_hit(i, 0.9 - i * 0.02) for i in range(15)]
        store = _RecordingMemoryStore()
        rag = RagRetriever(
            _StubStore(hits),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=6, score_threshold=0.0,
            include_messages=False, include_documents=False,
            memory_store=store,  # type: ignore[arg-type]
        )
        concept = SimpleNamespace(
            concept_id=7, label="enjoys systems thinking", confidence=0.82,
            plasticity=0.5, kind="identity", subject="user", status="active",
            last_reinforced_at=None,
        )

        class _Host(InnerLifePart1Mixin):
            def __init__(self) -> None:
                self._rag_retriever = rag
                self._embedder = _StubEmbedder()
                self._concept_store = _FakeConceptStore(
                    [concept], near_score=near_score,
                )
                self._topic_graph = _FakeTopicGraph([(1, "weekend hiking", 0.6)])
                self._memory_settings = _ms()
                self._memory_store = None

            @property
            def user_display_name(self) -> str:
                return "Jacob"

        return _Host(), store

    def test_region_composes_all_three_sources(self) -> None:
        host, store = self._host()
        region = host.build_relevant_context(
            user_text="tell me about hiking",
            recent_turns=[],
            session_key="s1",
            budget_tokens=2000,
            degrade_level=0,
        )
        self.assertIn("memory 0", region.text)
        self.assertIn("weekend hiking", region.text)
        self.assertIn("enjoys systems thinking", region.text)
        # Concept trace captured.
        self.assertEqual(region.concept_trace["reason"], "surfaced")
        self.assertEqual(region.concept_trace["surfaced"][0]["concept_id"], 7)
        # Only the budgeted subset was marked used (not the whole pool).
        self.assertEqual(len(store.mark_used_calls), 1)

    def test_region_hard_clipped_to_budget(self) -> None:
        host, _ = self._host()
        region = host.build_relevant_context(
            user_text="hi", recent_turns=[], session_key="s1",
            budget_tokens=40, degrade_level=0,
        )
        from app.llm.token_utils import estimate_tokens
        self.assertLessEqual(estimate_tokens(region.text), 40)

    def test_disabled_returns_empty(self) -> None:
        host, _ = self._host()
        host._memory_settings.context_budget_enabled = False
        region = host.build_relevant_context(
            user_text="hi", recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )
        self.assertEqual(region.text, "")
        self.assertEqual(region.reason, "disabled")

    def test_core_concept_pinned_below_relevance_floor(self) -> None:
        # The identity concept is barely relevant to the turn (cosine 0.05,
        # under the 0.3 concept min_relevance) so the relevance path drops it,
        # but the L27 always-on core lane still surfaces it -- and the trace
        # records it as pinned.
        host, _ = self._host(near_score=0.05)
        region = host.build_relevant_context(
            user_text="what's the weather", recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )
        self.assertIn("enjoys systems thinking", region.text)
        surfaced = region.concept_trace["surfaced"][0]
        self.assertEqual(surfaced["concept_id"], 7)
        self.assertTrue(surfaced["pinned"])

    def test_core_lane_disabled_by_cap_zero(self) -> None:
        host, _ = self._host(near_score=0.05)
        host._memory_settings.context_budget_core_cap = 0
        region = host.build_relevant_context(
            user_text="what's the weather", recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )
        # With the lane off and the concept below min_relevance, it vanishes.
        self.assertNotIn("enjoys systems thinking", region.text)


class SurfacingStashTests(unittest.TestCase):
    """L37: ``build_relevant_context`` leaves behind what it surfaced.

    The rows themselves are written in post-turn (they are keyed by the
    assistant message id, which doesn't exist yet here), so all this side
    has to get right is *what* it hands over and *when* it clears.
    """

    _host = RegionBuilderTests._host

    def test_stash_carries_every_surfaced_source(self) -> None:
        host, _ = self._host()
        host.build_relevant_context(
            user_text="tell me about hiking", recent_turns=[],
            session_key="s1", budget_tokens=2000, degrade_level=0,
        )
        items = host._last_surfaced_items
        by_kind: dict[str, list] = {}
        for item in items:
            by_kind.setdefault(item.item_kind, []).append(item)
        self.assertIn("memory", by_kind)
        self.assertEqual([c.item_id for c in by_kind["concept"]], [7])
        self.assertEqual([c.item_id for c in by_kind["cluster"]], [1])
        # Provenance rides along: the lane is what makes the ledger
        # diagnostic rather than just a per-item counter.
        self.assertTrue(by_kind["concept"][0].lane)
        self.assertGreater(by_kind["memory"][0].score, 0.0)

    def test_stash_matches_what_was_rendered(self) -> None:
        """A stash wider than the prompt would credit items Aiko never
        actually saw, which is the one way this measurement can lie.
        """
        host, _ = self._host()
        region = host.build_relevant_context(
            user_text="tell me about hiking", recent_turns=[],
            session_key="s1", budget_tokens=2000, degrade_level=0,
        )
        chosen = {
            int(c.payload.record.id)
            for c in region.selection.source("memory").chosen
        }
        stashed = {
            i.item_id for i in host._last_surfaced_items
            if i.item_kind == "memory"
        }
        self.assertEqual(stashed, {m for m in chosen if m > 0})

    def test_an_early_return_clears_a_previous_stash(self) -> None:
        """Otherwise the disabled turn's reply would inherit the last
        turn's surfaced set and be credited with items it never carried.
        """
        host, _ = self._host()
        host.build_relevant_context(
            user_text="tell me about hiking", recent_turns=[],
            session_key="s1", budget_tokens=2000, degrade_level=0,
        )
        self.assertTrue(host._last_surfaced_items)
        host._memory_settings.context_budget_enabled = False
        host.build_relevant_context(
            user_text="hi", recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )
        self.assertEqual(host._last_surfaced_items, [])

    def test_no_budget_clears_the_stash_too(self) -> None:
        host, _ = self._host()
        host._last_surfaced_items = ["stale"]
        host.build_relevant_context(
            user_text="hi", recent_turns=[], session_key="s1",
            budget_tokens=0, degrade_level=0,
        )
        self.assertEqual(host._last_surfaced_items, [])


class ConceptRenderSubjectTests(unittest.TestCase):
    """Subject-aware framing: aiko self-concepts read in the first person,
    not as things learned about the user."""

    def _host(self):
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        class _Host(InnerLifePart1Mixin):
            def __init__(self) -> None:
                self._concept_store = None  # -> supporting labels resolve to []

            @property
            def user_display_name(self) -> str:
                return "Jacob"

        return _Host()

    def test_groups_by_subject_with_distinct_headers(self) -> None:
        host = self._host()
        user_c = SimpleNamespace(
            concept_id=1, label="Jacob values owning his data",
            confidence=0.9, plasticity=0.3, kind="identity", subject="user",
            last_reinforced_at=None,
        )
        aiko_c = SimpleNamespace(
            concept_id=2, label="I deflect with teasing when I feel exposed",
            confidence=0.9, plasticity=0.3, kind="identity", subject="aiko",
            last_reinforced_at=None,
        )
        text, trace = host._render_relevant_concepts([user_c, aiko_c])
        self.assertEqual(trace["reason"], "surfaced")
        self.assertIn("understand about Jacob", text)
        self.assertIn("understand about yourself", text)
        # The self-concept sits under the "yourself" header, not Jacob's.
        self_idx = text.index("understand about yourself")
        jacob_idx = text.index("understand about Jacob")
        self.assertGreater(text.index("I deflect with teasing"), self_idx)
        self.assertGreater(self_idx, jacob_idx)

    def test_aiko_only_uses_self_header(self) -> None:
        host = self._host()
        aiko_c = SimpleNamespace(
            concept_id=2, label="I tend to over-explain",
            confidence=0.8, plasticity=0.5, kind="identity", subject="aiko",
            last_reinforced_at=None,
        )
        text, _ = host._render_relevant_concepts([aiko_c])
        self.assertIn("understand about yourself", text)
        self.assertNotIn("understand about Jacob", text)

    def test_value_uses_value_voice_not_identity_header(self) -> None:
        # L10: a subject=user/kind=value concept renders under the value
        # header (principles), not the identity "things you've come to
        # understand" header.
        host = self._host()
        user_val = SimpleNamespace(
            concept_id=3, label="Jacob values self-reliance over convenience",
            confidence=0.9, plasticity=0.2, kind="value", subject="user",
            last_reinforced_at=None,
        )
        text, _ = host._render_relevant_concepts([user_val])
        self.assertIn("values", text)
        self.assertIn("principles", text)
        self.assertNotIn("understand about Jacob", text)

    def test_value_and_identity_render_under_separate_headers(self) -> None:
        host = self._host()
        user_id = SimpleNamespace(
            concept_id=1, label="Jacob is a systems thinker",
            confidence=0.9, plasticity=0.3, kind="identity", subject="user",
            last_reinforced_at=None,
        )
        user_val = SimpleNamespace(
            concept_id=2, label="Jacob values owning his data",
            confidence=0.9, plasticity=0.2, kind="value", subject="user",
            last_reinforced_at=None,
        )
        aiko_val = SimpleNamespace(
            concept_id=3, label="I value honesty over agreeableness",
            confidence=0.9, plasticity=0.2, kind="value", subject="aiko",
            last_reinforced_at=None,
        )
        text, _ = host._render_relevant_concepts([user_id, user_val, aiko_val])
        # Identity group still present.
        self.assertIn("understand about Jacob", text)
        # Both value groups render in the value voice.
        self.assertIn("Jacob values", text)
        self.assertIn("come to value", text)  # aiko value header
        # Identity trait renders before the user value block.
        self.assertLess(
            text.index("systems thinker"), text.index("owning his data"),
        )

    def test_the_stance_is_stated_once_for_the_whole_block(self) -> None:
        # Three groups, three headers, and exactly one statement of how to
        # hold any of it. The per-header hedge used to repeat this once per
        # (subject, family) pair -- a dozen times in a full turn, which
        # reads as distrust-what-you-know rather than as a posture.
        host = self._host()
        rows = [
            SimpleNamespace(
                concept_id=i, label=f"label {i}", confidence=0.9,
                plasticity=0.3, kind=kind, subject=subject,
                last_reinforced_at=None,
            )
            for i, (kind, subject) in enumerate(
                (
                    ("identity", "user"),
                    ("value", "user"),
                    ("boundary", "aiko"),
                ),
                start=1,
            )
        ]
        text, _ = host._render_relevant_concepts(rows)
        preamble, *groups = text.split("\n\n")
        self.assertEqual(len(groups), 3)
        self.assertTrue(preamble.startswith("How to hold everything below"))
        for group in groups:
            self.assertNotIn("wrong about", group)
            self.assertNotIn("How to hold", group)

    def test_the_stance_invites_revision_and_wondering(self) -> None:
        # The positive half: hedging alone never says she may change her
        # mind out loud, or that none of this bounds what she can wonder
        # about. That absence was the L28m framing finding.
        preamble = self._host()._concept_stance_preamble("Jacob")
        self.assertIn("change your mind out loud", preamble)
        self.assertIn("no limit on what you may wonder about", preamble)
        self.assertIn("Jacob", preamble)

    def test_no_group_header_repeats_the_stance(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        families = (
            "trait", "value", "affective", "taste", "conduct", "ritual",
            "narrative", "aspiration", "boundary", "communication_style",
            "generalization",
        )
        for subject in ("user", "relationship", "aiko"):
            for family in families:
                header = InnerLifePart1Mixin._concept_group_header(
                    subject, family, "Jacob",
                )
                with self.subTest(subject=subject, family=family):
                    for hedge in (
                        "hold them lightly", "hold these lightly",
                        "hold it lightly", "hold lightly",
                        "stay open to being wrong",
                        "wrong about yourself too",
                        "not facts", "not as facts",
                    ):
                        self.assertNotIn(hedge, header)


class RelevantContextResultTests(unittest.TestCase):
    def test_default_reason(self) -> None:
        rc = RelevantContext()
        self.assertEqual(rc.text, "")
        self.assertEqual(rc.reason, "ok")


class _FakeKV:
    """Minimal ``kv_get`` / ``kv_set`` backed by a dict (the L23 habituation
    store seam)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str):
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeTracker:
    """A relationship tracker whose ``get`` exposes a mutable ``total_turns``."""

    def __init__(self, total_turns: int = 0) -> None:
        self.total_turns = total_turns

    def get(self, _user_id: str):
        return SimpleNamespace(total_turns=self.total_turns)


class _FakeEventStore:
    """A concept-event timeline whose ``list`` returns newest-first events."""

    def __init__(self, events: list) -> None:
        self._events = events

    def list(self, *, limit: int = 200):
        return self._events[: max(1, int(limit))]


class HabituationWiringTests(unittest.TestCase):
    """L23 habituation wired through ``build_relevant_context``: the chosen
    concepts are stamped into ``kv_meta`` and damp themselves on later turns
    (flex), while the always-on core lane *rotates* rather than suppresses."""

    def _host(self, concepts: list, *, near_score: float, tracker: _FakeTracker,
              kv: _FakeKV, events: list | None = None,
              edges: dict | None = None, cluster_rows: list | None = None,
              clusters: list | None = None):
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        hits = [_mem_hit(i, 0.9 - i * 0.02) for i in range(15)]
        rag = RagRetriever(
            _StubStore(hits),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=6, score_threshold=0.0,
            include_messages=False, include_documents=False,
            memory_store=_RecordingMemoryStore(),  # type: ignore[arg-type]
        )
        rows = (
            cluster_rows if cluster_rows is not None
            else [(1, "weekend hiking", 0.6)]
        )

        class _Host(InnerLifePart1Mixin):
            def __init__(self) -> None:
                self._rag_retriever = rag
                self._embedder = _StubEmbedder()
                self._concept_store = _FakeConceptStore(
                    concepts, near_score=near_score, edges=edges,
                )
                self._topic_graph = _FakeTopicGraph(rows, clusters=clusters)
                self._memory_settings = _ms()
                self._memory_store = None
                self._chat_db = kv
                self._relationship_tracker = tracker
                self._user_id = "u1"
                self._concept_event_store = (
                    _FakeEventStore(events) if events is not None else None
                )
                # K47 gate. Stubbed rather than built from real settings:
                # the real reader needs _settings + _debug_overrides, and
                # the L30a lane only consumes the boolean.
                self.k47_armed = False

            @property
            def user_display_name(self) -> str:
                return "Jacob"

            def _question_balance_suppressed(self) -> bool:
                return self.k47_armed

        return _Host()

    def _run(self, host, text="tell me about hiking"):
        return host.build_relevant_context(
            user_text=text, recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )

    def _trace_by_id(self, region):
        return {
            int(e["concept_id"]): e
            for e in region.concept_trace.get("surfaced", [])
        }

    def test_chosen_ids_written_to_kv(self) -> None:
        from app.core.concepts.concept_surfacing import (
            HABITUATION_KV_KEY,
            load_habituation,
        )
        concept = SimpleNamespace(
            concept_id=7, label="enjoys systems thinking", confidence=0.82,
            plasticity=0.5, kind="identity", subject="user", status="active",
            last_reinforced_at=None,
        )
        kv = _FakeKV()
        host = self._host([concept], near_score=0.6,
                          tracker=_FakeTracker(3), kv=kv)
        region = self._run(host)
        self.assertIn("enjoys systems thinking", region.text)
        state = load_habituation(kv.kv_get)
        # Stamped at total_turns + 1 (the in-flight turn index).
        self.assertEqual(state.get(7), 4)
        self.assertIn(HABITUATION_KV_KEY, kv.store)

    def test_flex_concept_habituated_on_resurface(self) -> None:
        # A non-core (affective) concept that surfaces on the turn-relevant lane
        # is damped when it comes back a turn later.
        concept = SimpleNamespace(
            concept_id=11, label="loves talking about music", confidence=0.6,
            plasticity=0.5, kind="affective", subject="user", status="active",
            last_reinforced_at=None,
        )
        kv = _FakeKV()
        tracker = _FakeTracker(3)
        host = self._host([concept], near_score=0.7, tracker=tracker, kv=kv)

        first = self._run(host)
        self.assertIn(11, self._trace_by_id(first))
        comp1 = self._trace_by_id(first)[11]["score"]
        self.assertAlmostEqual(comp1["habituation"], 1.0)

        # Next user-turn: the post-turn counter advanced by one. Surfaced last
        # turn, habituation pushes it under the relevance floor -> it steps aside
        # entirely this turn (repetition suppression / anti-nag).
        tracker.total_turns = 4
        second = self._run(host)
        self.assertNotIn(11, self._trace_by_id(second))

    def test_core_lane_soft_rotation(self) -> None:
        # Three core-qualifying identity concepts, a core cap of two. Pre-seed
        # the habituation clock so #1 and #2 were surfaced last turn while #3 is
        # rested: the core lane rotates the rested #3 in and drops the *weaker*
        # just-shown one (#2), never emptying the pinned set.
        from app.core.concepts.concept_surfacing import save_habituation

        def _c(cid: int, conf: float):
            return SimpleNamespace(
                concept_id=cid, label=f"trait {cid}", confidence=conf,
                plasticity=0.3, kind="identity", subject="user",
                status="active", last_reinforced_at=None,
            )

        concepts = [_c(1, 0.9), _c(2, 0.85), _c(3, 0.8)]
        kv = _FakeKV()
        tracker = _FakeTracker(10)  # current turn == 11
        save_habituation(kv.kv_set, {1: 10, 2: 10})  # both surfaced last turn
        host = self._host(concepts, near_score=0.0, tracker=tracker, kv=kv)

        region = self._run(host, text="what's the weather")
        pinned = {
            cid for cid, e in self._trace_by_id(region).items()
            if e.get("pinned")
        }
        # #3 (rested) rotates into the core lane; the cap is still honoured and
        # at least one previously-shown concept is retained.
        self.assertIn(3, pinned)
        self.assertEqual(len(pinned), 2)
        self.assertTrue(pinned & {1, 2})

    def test_salience_lifts_freshly_changed_concept(self) -> None:
        # Two equally-relevant affective concepts; only #21 was just
        # contradicted. Salience should lift #21's flex score above its stale
        # sibling #22.
        def _c(cid: int):
            return SimpleNamespace(
                concept_id=cid, label=f"feels strongly about topic {cid}",
                confidence=0.6, plasticity=0.5, kind="affective",
                subject="user", status="active", last_reinforced_at=None,
            )

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        events = [
            SimpleNamespace(
                concept_id=21, event_type="contradicted", created_at=now_iso,
            )
        ]
        kv = _FakeKV()
        host = self._host(
            [_c(21), _c(22)], near_score=0.7, tracker=_FakeTracker(3),
            kv=kv, events=events,
        )
        region = self._run(host)
        comps = self._trace_by_id(region)
        self.assertGreater(comps[21]["score"]["salience"], 0.0)
        self.assertEqual(comps[22]["score"]["salience"], 0.0)
        self.assertGreater(
            comps[21]["score"]["score"], comps[22]["score"]["score"]
        )

    def test_earned_standing_is_loaded_once_and_recorded_in_flex_trace(self) -> None:
        from app.core.concepts.concept_surfacing import save_standing

        def _c(cid: int):
            return SimpleNamespace(
                concept_id=cid, label=f"likes topic {cid}", confidence=0.6,
                plasticity=0.5, kind="affective", subject="user",
                status="active", last_reinforced_at=None,
            )

        kv = _FakeKV()
        save_standing(kv.kv_set, {31: 1.0, 32: 0.35})
        host = self._host(
            [_c(31), _c(32)], near_score=0.7,
            tracker=_FakeTracker(3), kv=kv,
        )
        host._memory_settings.concept_surfacing_standing_enabled = True
        comps = self._trace_by_id(self._run(host))
        self.assertEqual(comps[31]["score"]["standing"], 1.0)
        self.assertEqual(comps[32]["score"]["standing"], 0.35)
        self.assertGreater(
            comps[31]["score"]["score"], comps[32]["score"]["score"]
        )

    def test_activation_lifts_hot_cluster_sibling(self) -> None:
        # A low-cosine identity concept that spans the turn's hot topic cluster
        # gets a spreading-activation boost that carries it over the relevance
        # floor -- surfacing on association, not direct similarity.
        concept = SimpleNamespace(
            concept_id=2, label="prefers minimalist tools", confidence=0.6,
            plasticity=0.3, kind="identity", subject="user", status="active",
            last_reinforced_at=None,
        )
        # best_clusters_for -> cluster_id 5; the bridge maps it to rep id 100;
        # concept #2 spans cluster 100.
        cluster_rows = [(5, "developer tooling", 0.6)]
        clusters = [SimpleNamespace(cluster_id=5, representative_id=100)]
        edges = {("cluster", "100"): [_CEdge("concept", 2)]}

        def _mk():
            return self._host(
                [concept], near_score=0.1, tracker=_FakeTracker(3),
                kv=_FakeKV(), cluster_rows=cluster_rows, clusters=clusters,
                edges=edges,
            )

        # With activation on, the sibling surfaces and its trace records a boost.
        host = _mk()
        region = self._run(host, text="what tools should I use")
        comps = self._trace_by_id(region)
        self.assertIn(2, comps)
        self.assertGreater(comps[2]["score"].get("activation", 0.0), 0.0)

        # With activation off, the same low-cosine concept stays below the floor.
        host_off = _mk()
        host_off._memory_settings.concept_surfacing_activation_enabled = False
        region_off = self._run(host_off, text="what tools should I use")
        self.assertNotIn(2, self._trace_by_id(region_off))

    # ── L35 surface reasons ──────────────────────────────────────────

    def test_core_concept_is_traced_as_a_core_belief(self) -> None:
        concept = SimpleNamespace(
            concept_id=1, label="values understanding systems", confidence=0.9,
            plasticity=0.3, kind="identity", subject="user", status="active",
            last_reinforced_at=None,
        )
        host = self._host([concept], near_score=0.0, tracker=_FakeTracker(3),
                          kv=_FakeKV())
        entry = self._trace_by_id(
            self._run(host, text="what's the weather")
        )[1]
        self.assertTrue(entry["pinned"])
        self.assertEqual(entry["surface_reason"], "core_belief")
        self.assertEqual(entry["surface_reason_label"], "always-on core belief")

    def test_contradicted_concept_is_traced_as_the_contradiction(self) -> None:
        """The salience machinery already lifts a just-contradicted concept;
        L35 is what makes the trace *say* that's why it's here."""
        from datetime import datetime, timezone

        concept = SimpleNamespace(
            concept_id=21, label="feels strongly about deadlines",
            confidence=0.6, plasticity=0.5, kind="affective", subject="user",
            status="active", last_reinforced_at=None,
        )
        events = [
            SimpleNamespace(
                concept_id=21, event_type="contradicted",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ]
        # Modest cosine, so the fresh contradiction is the biggest term of
        # the affective blend (context 0.5, recency 0.3, salience 0.2).
        host = self._host([concept], near_score=0.3, tracker=_FakeTracker(3),
                          kv=_FakeKV(), events=events)
        entry = self._trace_by_id(self._run(host))[21]
        self.assertEqual(entry["surface_reason"], "unresolved_contradiction")
        self.assertEqual(
            entry["surface_reason_label"], "unresolved contradiction"
        )
        self.assertEqual(entry["score"]["reason"], entry["surface_reason"])

    def test_core_lane_ranks_by_rest_not_confidence(self) -> None:
        # L40: habituation used to be read only as a threshold, so the stale
        # group kept core_lane's confidence order and a belief shown last turn
        # preceded one rested for three. Order is the only thing that governs
        # the pinned lane, so the graded factor has to sort it.
        from app.core.concepts.concept_surfacing import save_habituation

        def _c(cid: int, conf: float):
            return SimpleNamespace(
                concept_id=cid, label=f"trait {cid}", confidence=conf,
                plasticity=0.3, kind="identity", subject="user",
                status="active", last_reinforced_at=None,
            )

        concepts = [_c(1, 0.9), _c(2, 0.85), _c(3, 0.8)]
        kv = _FakeKV()
        # Current turn 11: #1 and #2 were shown last turn, #3 three turns ago.
        # All three are inside the habituation window, so all are "stale" and
        # the binary split alone can't tell them apart.
        save_habituation(kv.kv_set, {1: 10, 2: 10, 3: 8})
        host = self._host(concepts, near_score=0.0, tracker=_FakeTracker(10),
                          kv=kv)

        trace = self._trace_by_id(self._run(host, text="what's the weather"))
        pinned = {cid for cid, e in trace.items() if e.get("pinned")}
        # The cap is two. The most-rested #3 takes a slot despite having the
        # *lowest* confidence, displacing the just-shown #2.
        self.assertEqual(pinned, {1, 3})
        self.assertGreater(
            trace[3]["score"]["habituation"], trace[1]["score"]["habituation"],
        )

    def test_never_reinforced_concept_is_not_traced_as_recent(self) -> None:
        """``recency_boost`` neutral-defaults to 1.0 for a concept with no
        ``last_reinforced_at``. That must not read as "reinforced recently"
        -- the affective blend weights recency above salience, so this is
        exactly where a missing signal would otherwise win."""
        concept = SimpleNamespace(
            concept_id=31, label="lights up about synths", confidence=0.6,
            plasticity=0.5, kind="affective", subject="user", status="active",
            last_reinforced_at=None,
        )
        host = self._host([concept], near_score=0.5, tracker=_FakeTracker(3),
                          kv=_FakeKV())
        entry = self._trace_by_id(self._run(host))[31]
        self.assertEqual(entry["score"]["recency"], 1.0)
        self.assertEqual(entry["surface_reason"], "topic_match")


class ImportanceWiringTests(unittest.TestCase):
    """L32 through ``build_relevant_context``: the second strength axis
    reaches the score and the trace, and the affect join actually joins.

    The pure scorer is covered in ``test_concept_importance``; what these
    pin is the wiring, which is where the axis is easiest to lose --
    every failure path in the join returns ``None`` on purpose, so a
    broken bridge produces a working prompt with the feature silently off.
    """

    _host = HabituationWiringTests._host
    _run = HabituationWiringTests._run
    _trace_by_id = HabituationWiringTests._trace_by_id

    @staticmethod
    def _concept(cid: int, kind: str, *, conf: float = 0.6, subject="user"):
        return SimpleNamespace(
            concept_id=cid, label=f"{kind} belief {cid}", confidence=conf,
            plasticity=0.4, kind=kind, subject=subject, status="active",
            last_reinforced_at=None,
        )

    @staticmethod
    def _charged_kv(cluster_id: int = 5) -> _FakeKV:
        from app.core.concepts.cluster_affect import (
            KV_CLUSTER_AFFECT_USER,
            ClusterAffectState,
            save_map,
        )

        kv = _FakeKV()
        save_map(kv.kv_set, KV_CLUSTER_AFFECT_USER, {
            str(cluster_id): ClusterAffectState(
                valence=-0.9, arousal=0.85, samples=12,
                updated_at=timephrase.utcnow().isoformat(),
                valence_samples=12,
            )
        })
        return kv

    def _grounded(self, concepts, *, kv, near_score=0.5):
        """A host where concept #2 is grounded on cluster 5 (rep memory 100)."""
        return self._host(
            concepts, near_score=near_score, tracker=_FakeTracker(3), kv=kv,
            clusters=[
                SimpleNamespace(
                    cluster_id=5, representative_id=100, member_ids=(100, 101),
                )
            ],
            edges={("cluster", "100"): [_CEdge("concept", 2)]},
        )

    def test_the_trace_carries_the_axis_and_its_inputs(self) -> None:
        host = self._grounded(
            [self._concept(2, "affective")], kv=self._charged_kv(),
        )
        score = self._trace_by_id(self._run(host))[2]["score"]
        self.assertIn("importance", score)
        self.assertIn("importance_prior", score)
        self.assertIn("importance_charge", score)

    def test_a_charged_topic_lifts_the_concept_above_its_kind(self) -> None:
        # The join end to end: cluster evidence names memory 100, the graph
        # puts 100 in cluster 5, cluster 5 carries affect. Break any link
        # and the charge silently reads zero.
        host = self._grounded(
            [self._concept(2, "affective")], kv=self._charged_kv(),
        )
        score = self._trace_by_id(self._run(host))[2]["score"]
        self.assertGreater(score["importance_charge"], 0.0)
        self.assertGreater(score["importance"], score["importance_prior"])

    def test_an_ungrounded_concept_rests_on_its_kind_prior(self) -> None:
        from app.core.concepts.concept_importance import kind_importance

        host = self._host(
            [self._concept(9, "affective")], near_score=0.5,
            tracker=_FakeTracker(3), kv=self._charged_kv(),
        )
        score = self._trace_by_id(self._run(host))[9]["score"]
        self.assertEqual(score["importance_charge"], 0.0)
        self.assertAlmostEqual(
            score["importance"], kind_importance("affective"), places=3
        )

    def test_affect_on_an_unrelated_cluster_does_not_leak(self) -> None:
        # Charge belongs to the topics a concept actually stands on.
        host = self._grounded(
            [self._concept(2, "affective")], kv=self._charged_kv(999),
        )
        score = self._trace_by_id(self._run(host))[2]["score"]
        self.assertEqual(score["importance_charge"], 0.0)

    def test_disabling_the_axis_removes_it_from_the_trace(self) -> None:
        host = self._grounded(
            [self._concept(2, "affective")], kv=self._charged_kv(),
        )
        host._memory_settings.concept_importance_enabled = False
        score = self._trace_by_id(self._run(host))[2]["score"]
        self.assertNotIn("importance", score)

    def test_zero_strength_leaves_the_ranking_untouched(self) -> None:
        # The escape hatch: one knob to 0 has to reproduce the pre-L32
        # score exactly, not approximately.
        def score_of(strength: float) -> float:
            host = self._host(
                [self._concept(4, "boundary")], near_score=0.5,
                tracker=_FakeTracker(3), kv=_FakeKV(),
            )
            host._memory_settings.concept_importance_strength = strength
            return self._trace_by_id(self._run(host))[4]["score"]["score"]

        host_off = self._host(
            [self._concept(4, "boundary")], near_score=0.5,
            tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        host_off._memory_settings.concept_importance_enabled = False
        baseline = self._trace_by_id(
            self._run(host_off)
        )[4]["score"]["score"]
        self.assertEqual(score_of(0.0), baseline)
        self.assertGreater(score_of(0.4), baseline)

    def test_stakes_break_a_tie_between_equally_relevant_beliefs(self) -> None:
        # The headline: same cosine, same confidence, different stakes.
        host = self._host(
            [self._concept(1, "boundary"), self._concept(2, "taste",
                                                         subject="aiko")],
            near_score=0.6, tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        comps = self._trace_by_id(self._run(host))
        self.assertGreater(
            comps[1]["score"]["score"], comps[2]["score"]["score"]
        )

    def test_a_missing_store_method_does_not_sink_the_turn(self) -> None:
        # Every join failure is meant to degrade to neutral importance
        # rather than an empty prompt region.
        host = self._grounded(
            [self._concept(2, "affective")], kv=self._charged_kv(),
        )
        del type(host._concept_store).cluster_evidence_for
        try:
            region = self._run(host)
            self.assertIn(2, self._trace_by_id(region))
            self.assertNotIn(
                "importance", self._trace_by_id(region)[2]["score"]
            )
        finally:
            type(host._concept_store).cluster_evidence_for = (
                _cluster_evidence_for
            )

    def test_the_flex_lane_over_fetches_wider_than_it_renders(self) -> None:
        # Importance can only re-rank what cosine brought back, so the
        # over-fetch has to exceed the render cap or the axis can reorder
        # the winners but never promote a newcomer.
        seen: list[int] = []
        host = self._host(
            [self._concept(i, "identity") for i in range(1, 30)],
            near_score=0.6, tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        original = host._concept_store.nearest

        def spy(vec, *, status="active", k=8):
            seen.append(int(k))
            return original(vec, status=status, k=k)

        host._concept_store.nearest = spy
        self._run(host)
        cap = host._memory_settings.context_budget_concept_cap
        self.assertTrue(seen)
        self.assertGreater(max(seen), cap * 2)


#: Captured before any test deletes it off the class, so the teardown in
#: ``test_a_missing_store_method_does_not_sink_the_turn`` can put it back.
_cluster_evidence_for = _FakeConceptStore.cluster_evidence_for


class OpennessWiringTests(unittest.TestCase):
    """The two brain-line openness mechanisms, through the real builder.

    Both exist because a concept selection ranked on strength converges on
    the kinds that constrain Aiko: ``boundary`` and ``value`` carry the
    two highest importance priors in the registry, and the pinned lane is
    not even *eligible* to carry a generative kind. The selection logic is
    unit-tested in ``tests/test_concept_diets.py``; what these pin is the
    wiring, which is where a feature like this is easiest to lose --
    every path is a silent no-op when it fails.
    """

    _host = HabituationWiringTests._host
    _run = HabituationWiringTests._run
    _trace_by_id = HabituationWiringTests._trace_by_id

    @staticmethod
    def _concept(cid: int, kind: str, *, conf: float = 0.9, subject="user"):
        return SimpleNamespace(
            concept_id=cid, label=f"{kind} belief {cid}", confidence=conf,
            plasticity=0.4, kind=kind, subject=subject, status="active",
            last_reinforced_at=None,
        )

    def _stocked(self, **settings):
        host = self._host(
            [
                self._concept(1, "identity", conf=0.95),
                self._concept(2, "value", conf=0.95),
                self._concept(3, "boundary", conf=0.92),
                self._concept(4, "aspiration", conf=0.80),
            ],
            near_score=0.6, tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        for key, value in settings.items():
            setattr(host._memory_settings, key, value)
        return host

    def _pinned(self, region) -> set[int]:
        return {
            cid for cid, entry in self._trace_by_id(region).items()
            if entry.get("pinned")
        }

    # ── the openness reserve (pinned lane) ────────────────────────────

    def test_the_pinned_lane_carries_no_generative_kind_by_default(
        self,
    ) -> None:
        # The state the reserve exists to fix, asserted here rather than
        # only described in a comment: ``core_lane_kinds()`` returns two
        # anchors and two guides, so no amount of tuning the cap reaches
        # an aspiration.
        pinned = self._pinned(
            self._run(self._stocked(concept_core_openness_slots=0))
        )
        self.assertTrue(pinned)
        self.assertNotIn(4, pinned)

    def test_the_reserve_pins_a_generative_concept(self) -> None:
        region = self._run(self._stocked(
            concept_core_openness_slots=1,
            concept_core_openness_min_confidence=0.5,
            context_budget_core_cap=2,
        ))
        self.assertIn(4, self._pinned(region))

    def test_the_reserve_does_not_take_over_a_small_lane(self) -> None:
        # ``core_cap`` defaults to 2 and the reserve to 2 slots, so a
        # literal reading would pin nothing but generative concepts and
        # drop the identity that says who she is talking to.
        pinned = self._pinned(self._run(self._stocked(
            concept_core_openness_slots=2, context_budget_core_cap=2,
        )))
        self.assertIn(4, pinned)
        self.assertTrue(pinned - {4})

    def test_a_shaky_generative_concept_is_not_pinned(self) -> None:
        host = self._stocked(
            concept_core_openness_slots=1,
            concept_core_openness_min_confidence=0.85,
        )
        self.assertNotIn(4, self._pinned(self._run(host)))

    # ── the generative floor (flex lane) ──────────────────────────────

    def _tilted(self, **settings):
        """A turn whose flex pick would otherwise be guides only."""
        host = self._host(
            [
                self._concept(11, "boundary", conf=0.9),
                self._concept(12, "value", conf=0.9),
                self._concept(13, "taste", conf=0.9, subject="aiko"),
            ],
            near_score=0.6, tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        host._memory_settings.context_budget_core_cap = 0
        host._memory_settings.context_budget_concept_cap = 2
        for key, value in settings.items():
            setattr(host._memory_settings, key, value)
        return host

    def test_without_the_floor_the_tilt_wins_outright(self) -> None:
        region = self._run(self._tilted(concept_flex_generative_floor=0))
        surfaced = {
            int(row["concept_id"]) for row in region.concept_trace["surfaced"]
        }
        self.assertEqual(surfaced, {11, 12})
        self.assertFalse(region.concept_trace["roles"]["floor_fired"])

    def test_the_floor_makes_room_for_the_generative_concept(self) -> None:
        region = self._run(self._tilted(concept_flex_generative_floor=1))
        surfaced = {
            int(row["concept_id"]) for row in region.concept_trace["surfaced"]
        }
        self.assertIn(13, surfaced)
        self.assertTrue(region.concept_trace["roles"]["floor_fired"])

    def test_the_floor_displaces_a_guide_and_not_an_anchor(self) -> None:
        # Losing a boundary from one turn's pick is recoverable; losing the
        # identity concept that says who she is talking to is not.
        host = self._host(
            [
                self._concept(11, "identity", conf=0.95),
                self._concept(12, "boundary", conf=0.9),
                self._concept(13, "taste", conf=0.9, subject="aiko"),
            ],
            near_score=0.6, tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        host._memory_settings.context_budget_core_cap = 0
        host._memory_settings.context_budget_concept_cap = 2
        host._memory_settings.concept_flex_generative_floor = 1
        region = self._run(host)
        surfaced = {
            int(row["concept_id"]) for row in region.concept_trace["surfaced"]
        }
        self.assertIn(11, surfaced)
        self.assertIn(13, surfaced)
        self.assertNotIn(12, surfaced)

    def test_the_floor_stays_quiet_when_the_pick_is_already_open(
        self,
    ) -> None:
        host = self._tilted(concept_flex_generative_floor=1)
        host._memory_settings.context_budget_concept_cap = 3
        region = self._run(host)
        self.assertFalse(region.concept_trace["roles"]["floor_fired"])

    def test_the_floor_never_grows_the_region(self) -> None:
        region = self._run(self._tilted(concept_flex_generative_floor=1))
        self.assertLessEqual(
            region.selection.used_tokens, region.selection.budget_tokens,
        )

    # ── the role mix ──────────────────────────────────────────────────

    def test_the_trace_reports_the_role_mix(self) -> None:
        roles = self._run(
            self._tilted(concept_flex_generative_floor=1)
        ).concept_trace["roles"]
        self.assertEqual(roles["generative"], 1)
        self.assertEqual(roles["guide"], 1)
        self.assertEqual(roles["constraint_ratio"], 0.5)

    def test_an_all_anchor_turn_reports_no_constraint_ratio(self) -> None:
        # 0.0 would read as the openest possible turn; the honest answer is
        # that the question does not apply.
        host = self._host(
            [self._concept(1, "identity")],
            near_score=0.6, tracker=_FakeTracker(3), kv=_FakeKV(),
        )
        roles = self._run(host).concept_trace["roles"]
        self.assertIsNone(roles["constraint_ratio"])
        self.assertEqual(roles["anchor"], 1)


class ProfileClaimDedupeTests(unittest.TestCase):
    """L39: a ``subject=user`` identity / value concept reaches the prompt via
    two independent paths in one assembly -- the T0 profile block and the T3
    relevant_context lanes. T0 wins by architecture (built first, slice-cached),
    so T3 skips whatever ``_profile_concept_lines`` already claimed."""

    _host = HabituationWiringTests._host
    _run = HabituationWiringTests._run
    _trace_by_id = HabituationWiringTests._trace_by_id

    @staticmethod
    def _identity(cid: int, conf: float = 0.82, label: str | None = None):
        return SimpleNamespace(
            concept_id=cid, label=label or f"trait {cid}", confidence=conf,
            plasticity=0.5, kind="identity", subject="user", status="active",
            last_reinforced_at=None,
        )

    def _fresh_host(self, concepts: list, *, near_score: float, claimed=None):
        host = self._host(concepts, near_score=near_score,
                          tracker=_FakeTracker(3), kv=_FakeKV())
        if claimed is not None:
            host._last_profile_concept_ids = frozenset(claimed)
        return host

    def test_claimed_concept_is_dropped_from_the_core_lane(self) -> None:
        concept = self._identity(7, label="enjoys systems thinking")
        # Baseline: with nothing claimed it surfaces, so the assertion below
        # is about the claim rather than an inert lane.
        baseline = self._run(self._fresh_host([concept], near_score=0.6))
        self.assertIn("enjoys systems thinking", baseline.text)

        host = self._fresh_host([concept], near_score=0.6, claimed={7})
        region = self._run(host)
        self.assertNotIn("enjoys systems thinking", region.text)
        self.assertNotIn(7, self._trace_by_id(region))
        # The drop is recorded, so an empty concept lane stays distinguishable
        # from a cold layer.
        self.assertEqual(region.concept_trace.get("claimed_by_profile"), [7])

    def test_claimed_concept_is_dropped_from_the_flex_lane(self) -> None:
        # A value at 0.6 is under the kind's 0.85 core bar, so it can only
        # arrive on the turn-relevant lane -- the corner a core-lane-only skip
        # would miss, which would just relocate the duplicate.
        concept = SimpleNamespace(
            concept_id=9, label="cares about honesty", confidence=0.6,
            plasticity=0.2, kind="value", subject="user", status="active",
            last_reinforced_at=None,
        )
        baseline = self._run(self._fresh_host([concept], near_score=0.7))
        entry = self._trace_by_id(baseline).get(9)
        self.assertIsNotNone(entry)
        self.assertFalse(entry.get("pinned"))

        host = self._fresh_host([concept], near_score=0.7, claimed={9})
        region = self._run(host)
        self.assertNotIn(9, self._trace_by_id(region))

    def test_claimed_concept_does_not_consume_a_core_slot(self) -> None:
        # Cap of two over three core-qualifying concepts. Claiming the
        # strongest must promote the third, not leave a hole -- the skip
        # happens before the cap slice.
        concepts = [
            self._identity(1, 0.9), self._identity(2, 0.85),
            self._identity(3, 0.8),
        ]
        unclaimed = self._run(
            self._fresh_host(concepts, near_score=0.0),
            text="what's the weather",
        )
        self.assertEqual(
            {cid for cid, e in self._trace_by_id(unclaimed).items()
             if e.get("pinned")},
            {1, 2},
        )

        host = self._fresh_host(concepts, near_score=0.0, claimed={1})
        region = self._run(host, text="what's the weather")
        self.assertEqual(
            {cid for cid, e in self._trace_by_id(region).items()
             if e.get("pinned")},
            {2, 3},
        )

    def test_unclaimed_when_the_stash_is_absent(self) -> None:
        # The T3 lanes read the stash defensively: a host that never rendered a
        # profile block (or a cold concept layer, which clears it) must behave
        # exactly as before.
        concept = self._identity(7, label="enjoys systems thinking")
        host = self._fresh_host([concept], near_score=0.6)
        self.assertFalse(hasattr(host, "_last_profile_concept_ids"))
        region = self._run(host)
        self.assertIn("enjoys systems thinking", region.text)
        self.assertNotIn("claimed_by_profile", region.concept_trace)


class HypothesisLaneTests(unittest.TestCase):
    """L30a end-to-end: candidates reach the prompt as open questions, in
    their own budget lane, without contaminating the confident register."""

    _host = HabituationWiringTests._host
    _run = HabituationWiringTests._run

    @staticmethod
    def _candidate(cid: int, label: str, *, conf: float = 0.85,
                   sources: int = 1, kind: str = "identity",
                   subject: str = "user"):
        return SimpleNamespace(
            concept_id=cid, label=label, confidence=conf, plasticity=0.5,
            kind=kind, subject=subject, status="candidate",
            distinct_source_count=sources, last_reinforced_at=None,
        )

    @staticmethod
    def _active(cid: int, label: str, *, conf: float = 0.85):
        return SimpleNamespace(
            concept_id=cid, label=label, confidence=conf, plasticity=0.5,
            kind="identity", subject="user", status="active",
            distinct_source_count=4, last_reinforced_at=None,
        )

    def _build(self, concepts: list, *, near_score: float = 0.6):
        return self._host(
            concepts, near_score=near_score,
            tracker=_FakeTracker(3), kv=_FakeKV(),
        )

    def test_a_thin_candidate_surfaces_as_an_open_question(self) -> None:
        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        region = self._run(host)
        self.assertIn("Jacob is quietly burnt out", region.text)
        self.assertIn("Open questions", region.text)

    def test_it_is_never_phrased_as_a_belief(self) -> None:
        # The failure this lane exists to avoid. Candidate confidence is
        # high (0.9 here, and a median of 0.82 on the measured graph), so
        # reusing the confident lane's hedge would render an unproven
        # hunch as "You're fairly sure".
        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out", conf=0.9),
        ])
        text = self._run(host).text
        self.assertNotIn("You're fairly sure Jacob is quietly burnt out", text)
        self.assertNotIn(
            "You have a sense that Jacob is quietly burnt out", text
        )
        line = next(
            ln for ln in text.splitlines()
            if "Jacob is quietly burnt out" in ln and ln.startswith("-")
        )
        self.assertTrue(
            any(
                lead in line
                for lead in host._HYPOTHESIS_LEADS  # noqa: SLF001
            ),
            line,
        )

    def test_a_settled_candidate_stays_out(self) -> None:
        # Two sources at full conviction: a candidate only because the
        # promotion age floor has not elapsed, not because Aiko is unsure.
        host = self._build([
            self._candidate(
                11, "Jacob prefers dark mode", conf=0.85, sources=2,
            ),
        ])
        region = self._run(host)
        self.assertNotIn("Jacob prefers dark mode", region.text)

    def test_an_ungrounded_proposal_stays_out(self) -> None:
        host = self._build([
            self._candidate(12, "Jacob secretly hates cats", sources=0),
        ])
        self.assertNotIn("Jacob secretly hates cats", self._run(host).text)

    def test_the_two_registers_are_rendered_apart(self) -> None:
        host = self._build([
            self._active(1, "Jacob values owning his data"),
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        text = self._run(host).text
        belief_at = text.index("Jacob values owning his data")
        question_at = text.index("Jacob is quietly burnt out")
        header_at = text.index("Open questions")
        # The confident belief leads; the open question trails its own
        # header, so the two never read as one list.
        self.assertLess(belief_at, header_at)
        self.assertLess(header_at, question_at)

    def test_a_concept_is_never_both_a_belief_and_a_question(self) -> None:
        host = self._build([
            self._active(1, "Jacob values owning his data"),
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        trace = self._run(host).concept_trace
        confident = {int(e["concept_id"]) for e in trace["surfaced"]}
        tentative = {
            int(e["concept_id"])
            for e in trace.get("hypotheses", {}).get("surfaced", [])
        }
        self.assertEqual(confident & tentative, set())
        self.assertIn(11, tentative)

    def test_a_demoted_belief_still_in_the_profile_block_is_skipped(
        self,
    ) -> None:
        # The narrow case that makes the cross-lane dedup more than
        # decorative. Status alone keeps the two registers apart -- a row
        # cannot be active and candidate at once -- but
        # ``_last_profile_concept_ids`` is a *stash from a previous
        # render* and survives a slice-cache hit. If L3 demotes a concept
        # to candidate in between, its id is in the T0 block on screen
        # while the lane now sees it as an open question, and Aiko would
        # assert it and wonder about it in the same assembly.
        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        baseline = self._run(host)
        self.assertIn("Jacob is quietly burnt out", baseline.text)

        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        host._last_profile_concept_ids = frozenset({11})
        region = self._run(host)
        self.assertNotIn("Jacob is quietly burnt out", region.text)

    def test_the_trace_explains_the_pick(self) -> None:
        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        trace = self._run(host).concept_trace["hypotheses"]
        entry = trace["surfaced"][0]
        self.assertEqual(entry["concept_id"], 11)
        self.assertEqual(entry["distinct_source_count"], 1)
        score = entry["score"]
        self.assertEqual(score["lane"], "hypothesis")
        for field in ("cosine", "unsettled", "importance", "habituation"):
            self.assertIn(field, score)
        self.assertGreater(score["unsettled"], 0.22)

    def test_stakes_decide_between_two_equally_open_questions(self) -> None:
        # A boundary and a taste, identically unsettled and identically
        # on-topic. L32 importance is the only thing separating them, and
        # the cap of one means only the weightier may speak.
        host = self._build([
            self._candidate(11, "Jacob needs space when overloaded",
                            kind="boundary"),
            self._candidate(12, "Jacob likes tabs over spaces", kind="taste"),
        ])
        text = self._run(host).text
        self.assertIn("Jacob needs space when overloaded", text)
        self.assertNotIn("Jacob likes tabs over spaces", text)

    def test_the_cap_holds_at_one(self) -> None:
        host = self._build([
            self._candidate(11, "first open question"),
            self._candidate(12, "second open question"),
            self._candidate(13, "third open question"),
        ])
        region = self._run(host)
        self.assertEqual(region.selection.source("hypothesis").count, 1)

    def test_disabling_the_lane_removes_it_entirely(self) -> None:
        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        host._memory_settings.hypothesis_surfacing_enabled = False
        region = self._run(host)
        self.assertNotIn("Jacob is quietly burnt out", region.text)
        self.assertNotIn("hypotheses", region.concept_trace)

    def test_an_immature_graph_stays_quiet(self) -> None:
        # L21: a hypothesis block on a cold graph is the exact "blurt a
        # half-formed model" anti-pattern. The lane shares the confident
        # lanes' maturity gate rather than relaxing it.
        host = self._build([
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        host._topic_graph.mature = lambda *, min_clusters=6: False
        self.assertNotIn(
            "Jacob is quietly burnt out", self._run(host).text
        )

    def test_a_view_without_the_lane_does_not_sink_the_turn(self) -> None:
        # Mirrors the L32 guard: an older or leaner view double simply
        # produces no hypotheses rather than an exception.
        host = self._build([
            self._active(1, "Jacob values owning his data"),
            self._candidate(11, "Jacob is quietly burnt out"),
        ])
        from app.core.concepts.concept_view import ConceptView

        original = ConceptView.hypotheses
        try:
            del ConceptView.hypotheses
            region = self._run(host)
        finally:
            ConceptView.hypotheses = original
        self.assertIn("Jacob values owning his data", region.text)
        self.assertNotIn("Jacob is quietly burnt out", region.text)

    def test_a_surfaced_question_is_habituated(self) -> None:
        # The eligible pool is small, so without this the same question
        # would lead every turn and read as a fixation.
        from app.core.concepts.concept_surfacing import load_habituation

        kv = _FakeKV()
        host = self._host(
            [self._candidate(11, "Jacob is quietly burnt out")],
            near_score=0.6, tracker=_FakeTracker(3), kv=kv,
        )
        self._run(host)
        self.assertIn(11, load_habituation(kv.kv_get))


class HypothesisRenderTests(unittest.TestCase):
    def _host(self):
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        class _Host(InnerLifePart1Mixin):
            def __init__(self) -> None:
                self._concept_store = None
                self.k47_armed = False

            @property
            def user_display_name(self) -> str:
                return "Jacob"

            def _question_balance_suppressed(self) -> bool:
                return self.k47_armed

        return _Host()

    @staticmethod
    def _c(cid: int, label: str, subject: str = "user"):
        return SimpleNamespace(
            concept_id=cid, label=label, subject=subject, kind="identity",
            confidence=0.8, distinct_source_count=1,
        )

    def test_empty_renders_nothing(self) -> None:
        text, trace = self._host()._render_hypothesis_concepts([])
        self.assertEqual(text, "")
        self.assertEqual(trace["reason"], "no_eligible")

    def test_subjects_get_their_own_voice(self) -> None:
        host = self._host()
        text, _ = host._render_hypothesis_concepts([
            self._c(1, "Jacob is quietly burnt out"),
            self._c(2, "I get clingy when he goes quiet", subject="aiko"),
        ])
        self.assertIn("about Jacob", text)
        self.assertIn("about yourself", text)
        self.assertGreater(
            text.index("I get clingy"), text.index("about yourself")
        )

    def test_every_header_forbids_asserting_the_question(self) -> None:
        host = self._host()
        for subject in ("user", "aiko", "relationship"):
            header = host._hypothesis_header(subject, "Jacob", may_ask=True)
            self.assertIn("questions, not conclusions", header)

    def test_the_lead_is_stable_for_a_given_concept(self) -> None:
        host = self._host()
        first, _ = host._render_hypothesis_concepts(
            [self._c(7, "Jacob is quietly burnt out")]
        )
        second, _ = host._render_hypothesis_concepts(
            [self._c(7, "Jacob is quietly burnt out")]
        )
        self.assertEqual(first, second)

    def test_every_lead_reads_as_a_question_not_a_belief(self) -> None:
        # Guards the register against a future edit importing the
        # confident lane's vocabulary.
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        for lead in InnerLifePart1Mixin._HYPOTHESIS_LEADS:
            self.assertNotIn("sure", lead.lower())
            self.assertNotIn("impression", lead.lower())

    def test_a_blank_label_is_skipped(self) -> None:
        host = self._host()
        text, trace = host._render_hypothesis_concepts([self._c(1, "   ")])
        self.assertEqual(text, "")
        self.assertEqual(trace["reason"], "no_eligible")


class InventedRenderTests(unittest.TestCase):
    """L30 Phase B in the L30a lane.

    The failure this guards is precise: an invention that renders in the
    same register as a grounded candidate is indistinguishable in the
    prompt from something Aiko noticed, and the model will assert either
    one. So the two never share a header, an invented row is always last,
    and its disclaimer says outright that nothing is behind it.
    """

    def _host(self):
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        class _Host(InnerLifePart1Mixin):
            def __init__(self) -> None:
                self._concept_store = None
                self.k47_armed = False

            @property
            def user_display_name(self) -> str:
                return "Jacob"

            def _question_balance_suppressed(self) -> bool:
                return self.k47_armed

        return _Host()

    @staticmethod
    def _grounded(cid: int, label: str):
        return SimpleNamespace(
            concept_id=cid, label=label, subject="user", kind="identity",
            confidence=0.8, distinct_source_count=1,
        )

    @staticmethod
    def _invented(hid: int, statement: str, subject: str = "user"):
        from app.core.concepts.hypothesis_lane import adapt
        from app.core.concepts.hypothesis_store import Hypothesis

        row = Hypothesis(statement=statement, subject=subject, credence=0.5)
        row.hypothesis_id = hid
        return adapt(row)

    def test_an_invention_says_it_rests_on_nothing(self) -> None:
        text, _ = self._host()._render_hypothesis_concepts(
            [self._invented(3, "Jacob would love sailing")]
        )

        self.assertIn("invented rather than noticed", text)
        self.assertIn("no evidence behind them", text)

    def test_it_never_borrows_the_grounded_header(self) -> None:
        text, _ = self._host()._render_hypothesis_concepts(
            [self._invented(3, "Jacob would love sailing")]
        )

        self.assertNotIn("half-noticed", text)
        self.assertNotIn("about Jacob", text)

    def test_the_two_origins_get_separate_sections(self) -> None:
        text, _ = self._host()._render_hypothesis_concepts([
            self._grounded(1, "Jacob is quietly burnt out"),
            self._invented(3, "Jacob would love sailing"),
        ])

        self.assertIn("about Jacob", text)
        self.assertIn("Idle speculation", text)

    def test_the_weakest_thing_is_read_last(self) -> None:
        text, _ = self._host()._render_hypothesis_concepts([
            self._invented(3, "Jacob would love sailing"),
            self._grounded(1, "Jacob is quietly burnt out"),
        ])

        self.assertLess(
            text.index("Jacob is quietly burnt out"),
            text.index("Jacob would love sailing"),
        )

    def test_the_invented_leads_never_claim_an_observation(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        for lead in InnerLifePart1Mixin._INVENTED_LEADS:
            for claim in ("noticed", "sense", "impression", "sure"):
                self.assertNotIn(claim, lead.lower())

    def test_the_k47_gate_drops_the_invitation_not_the_thought(self) -> None:
        host = self._host()
        host.k47_armed = True

        text, _ = host._render_hypothesis_concepts(
            [self._invented(3, "Jacob would love sailing")]
        )

        self.assertIn("Jacob would love sailing", text)
        self.assertIn("keep them to yourself", text)

    def test_the_ids_go_to_their_own_dedupe_set(self) -> None:
        """The ask cue for an invention carries a hypothesis id."""
        host = self._host()

        host._render_hypothesis_concepts([
            self._grounded(1, "Jacob is quietly burnt out"),
            self._invented(3, "Jacob would love sailing"),
        ])

        self.assertEqual(host._last_hypothesis_lane_concept_ids, {1})
        self.assertEqual(host._last_hypothesis_lane_hypothesis_ids, {3})

    def test_a_negative_id_never_leaks_into_the_concept_set(self) -> None:
        host = self._host()

        host._render_hypothesis_concepts(
            [self._invented(3, "Jacob would love sailing")]
        )

        self.assertEqual(host._last_hypothesis_lane_concept_ids, frozenset())

    def test_the_trace_marks_which_pool_a_row_came_from(self) -> None:
        _text, trace = self._host()._render_hypothesis_concepts([
            self._grounded(1, "Jacob is quietly burnt out"),
            self._invented(3, "Jacob would love sailing"),
        ])

        origins = [e.get("origin") for e in trace["surfaced"]]
        self.assertEqual(origins, [None, "invented"])

    def test_two_inventions_get_their_own_habituation_slots(self) -> None:
        """Negative ids must not collide, or the pair rotates as one."""
        a = self._invented(3, "one")
        b = self._invented(4, "two")

        self.assertNotEqual(a.concept_id, b.concept_id)
        self.assertLess(a.concept_id, 0)

    def test_an_invention_reports_no_evidence_of_its_own(self) -> None:
        """Answering with credence would let it rank as though grounded."""
        row = self._invented(3, "Jacob would love sailing")

        self.assertEqual(row.confidence, 0.0)
        self.assertEqual(row.distinct_source_count, 0)


class OnePerOriginTests(unittest.TestCase):
    """The lane offers at most one candidate per origin.

    Scoring alone would bury the inventions: L32 importance blends a kind
    prior with the emotional charge of grounded topic clusters, and an
    invention has no grounded memories, so it falls back to the bare
    prior and loses nearly every time.
    """

    @staticmethod
    def _cand(key: str, origin: str | None):
        payload = SimpleNamespace(origin=origin) if origin else SimpleNamespace()
        return SimpleNamespace(key=key, payload=payload)

    def _keys(self, cands) -> list[str]:
        from app.core.concepts.hypothesis_lane import one_per_origin

        return [c.key for c in one_per_origin(cands)]

    def test_a_second_grounded_row_is_dropped(self) -> None:
        cands = [self._cand("a", None), self._cand("b", None)]

        self.assertEqual(self._keys(cands), ["a"])

    def test_one_of_each_origin_survives(self) -> None:
        cands = [self._cand("a", None), self._cand("b", "invented")]

        self.assertEqual(self._keys(cands), ["a", "b"])

    def test_it_keeps_the_better_ranked_row_of_each_origin(self) -> None:
        cands = [
            self._cand("a", None),
            self._cand("b", "invented"),
            self._cand("c", "invented"),
        ]

        self.assertEqual(self._keys(cands), ["a", "b"])

    def test_an_invention_is_not_crowded_out_by_grounded_rows(self) -> None:
        cands = [
            self._cand("g1", None),
            self._cand("g2", None),
            self._cand("g3", None),
            self._cand("inv", "invented"),
        ]

        self.assertEqual(self._keys(cands), ["g1", "inv"])

    def test_an_empty_list_stays_empty(self) -> None:
        self.assertEqual(self._keys([]), [])


if __name__ == "__main__":
    unittest.main()
