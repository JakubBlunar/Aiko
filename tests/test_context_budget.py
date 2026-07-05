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


class _FakeConceptStore:
    def __init__(self, concepts: list[object], *, near_score: float = 0.6) -> None:
        self._concepts = concepts
        self._near_score = near_score

    def nearest(self, _vec: object, *, status: str = "active", k: int = 8):
        return [(c, self._near_score) for c in self._concepts[:k]]

    def list_by(self, *, status: str | None = None, kind: str | None = None,
                subject: object = None, user_id: object = None):
        return [
            c for c in self._concepts
            if (status is None or getattr(c, "status", "active") == status)
            and (kind is None or getattr(c, "kind", None) == kind)
        ]


class _FakeTopicGraph:
    def __init__(self, rows: list[tuple[int, str, float]]) -> None:
        self._rows = rows

    def mature(self, *, min_clusters: int = 6) -> bool:
        return True

    def best_clusters_for(self, _vec: object, *, top_n: int = 1, min_sim: float = 0.0):
        return list(self._rows[:top_n])

    def cluster_id_for(self, _mid: int):
        return None


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
        context_budget_identity_cap=2,
        context_budget_identity_min_confidence=0.75,
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

    def test_identity_concept_pinned_below_relevance_floor(self) -> None:
        # The identity concept is barely relevant to the turn (cosine 0.05,
        # under the 0.3 concept min_relevance) so the relevance path drops it,
        # but the always-on identity lane still surfaces it.
        host, _ = self._host(near_score=0.05)
        region = host.build_relevant_context(
            user_text="what's the weather", recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )
        self.assertIn("enjoys systems thinking", region.text)
        self.assertEqual(region.concept_trace["surfaced"][0]["concept_id"], 7)

    def test_identity_lane_disabled_by_cap_zero(self) -> None:
        host, _ = self._host(near_score=0.05)
        host._memory_settings.context_budget_identity_cap = 0
        region = host.build_relevant_context(
            user_text="what's the weather", recent_turns=[], session_key="s1",
            budget_tokens=2000, degrade_level=0,
        )
        # With the lane off and the concept below min_relevance, it vanishes.
        self.assertNotIn("enjoys systems thinking", region.text)


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


class RelevantContextResultTests(unittest.TestCase):
    def test_default_reason(self) -> None:
        rc = RelevantContext()
        self.assertEqual(rc.text, "")
        self.assertEqual(rc.reason, "ok")


if __name__ == "__main__":
    unittest.main()
