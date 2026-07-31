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

    def nearest(self, _vec: object, *, status: str = "active", k: int = 8):
        return [(c, self._near_score) for c in self._concepts[:k]]

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
        concept_min_clusters=6,
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

            @property
            def user_display_name(self) -> str:
                return "Jacob"

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


if __name__ == "__main__":
    unittest.main()
