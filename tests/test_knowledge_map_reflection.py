"""Tests for K64d — the knowledge-map self-reflection worker.

Covers the worker
(:class:`~app.core.proactive.knowledge_map_reflection_worker.KnowledgeMapReflectionWorker`):
its graph-shape read, the worker-LLM meta-thought pass, the ``[mindmap]``
reflection write, dedupe handling, the wall-clock cooldown, ``force_next``,
and the ``clean_reflection_output`` helper. K64d has no surfacing provider —
its output is a ``kind="reflection"`` memory that flows through the existing
RAG / K28 turning-over path — so the provider side is covered there.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from app.core.concepts.concept_diets import diet_for
from app.core.proactive.knowledge_map_reflection_worker import (
    MINDMAP_PREFIX,
    KnowledgeMapReflectionWorker,
    clean_reflection_output,
    recency_phrase,
)


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    label: str
    size: int


@dataclass
class _Activity:
    label: str
    size: int
    last_active: str
    days_since: float | None
    representative_id: int = 0


@dataclass
class _Concept:
    label: str
    confidence: float = 0.5
    kind: str = "identity"


class _FakeView:
    """Stands in for ConceptView: cluster rep id -> concepts."""

    def __init__(
        self,
        by_rep: dict[int, list[_Concept]] | None = None,
        *,
        enabled: bool = True,
        raises: bool = False,
    ) -> None:
        self._by_rep = by_rep or {}
        self.enabled = enabled
        self._raises = raises
        self.calls: list[int] = []
        self.kinds_asked: list[tuple[str, ...] | None] = []

    def for_cluster(self, rep_id, *, kinds=None) -> list[_Concept]:
        self.calls.append(int(rep_id))
        self.kinds_asked.append(tuple(kinds) if kinds is not None else None)
        if self._raises:
            raise RuntimeError("boom")
        rows = list(self._by_rep.get(int(rep_id), []))
        if kinds is None:
            return rows
        wanted = {str(k) for k in kinds}
        return [r for r in rows if r.kind in wanted]


class _FakeGraphWithActivity:
    """Graph exposing ``cluster_activity`` (the K-time9 recency path)."""

    def __init__(self, rich: list[_Activity], gaps: list[_Entry] | None = None) -> None:
        self._rich = rich
        self._gaps = gaps or []

    def cluster_activity(self, *, top_n: int = 5, min_size=None) -> list[_Activity]:
        return list(self._rich)[:top_n]

    def knowledge_gap_clusters(self, *, top_n: int = 3, **_kw) -> list[_Entry]:
        return list(self._gaps)[:top_n]


class _CapturingLLM:
    def __init__(self, reply: str = "noticing things") -> None:
        self.reply = reply
        self.last_user = ""

    def chat(self, messages, **kw) -> str:
        for m in messages:
            if m.get("role") == "user":
                self.last_user = m.get("content", "")
        return self.reply


class _FakeGraph:
    def __init__(
        self,
        rich: list[_Entry] | None = None,
        gaps: list[_Entry] | None = None,
    ) -> None:
        self._rich = rich or []
        self._gaps = gaps or []
        self.interest_calls = 0
        self.gap_calls = 0

    def interest_map(self, *, top_n: int = 5, min_size=None) -> list[_Entry]:
        self.interest_calls += 1
        return list(self._rich)[:top_n]

    def knowledge_gap_clusters(self, *, top_n: int = 3, **_kw) -> list[_Entry]:
        self.gap_calls += 1
        return list(self._gaps)[:top_n]


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


class _FakeEmbedder:
    def embed(self, text: str) -> np.ndarray:
        return np.ones(4, dtype=np.float32)


@dataclass
class _Mem:
    id: int
    content: str
    kind: str


class _FakeStore:
    def __init__(self, *, dedupe: bool = False) -> None:
        self._next_id = 100
        self.added: list[_Mem] = []
        self._dedupe = dedupe

    def add(self, *, content, kind, embedding, **kw) -> _Mem | None:
        if self._dedupe:
            return None
        mem = _Mem(id=self._next_id, content=content, kind=kind)
        self._next_id += 1
        self.added.append(mem)
        return mem


class _FakeLLM:
    def __init__(self, reply: str = "I realise my head's all about work lately.") -> None:
        self.reply = reply
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        return self.reply


def _rich(n: int) -> list[_Entry]:
    return [_Entry(f"topic {i}", 20 - i) for i in range(n)]


def _make_worker(
    *,
    graph=None,
    store=None,
    embedder=None,
    llm=None,
    kv=None,
    notify=None,
    **kw,
) -> tuple[KnowledgeMapReflectionWorker, _KV]:
    kv = kv or _KV()
    params: dict = {
        "interval_seconds": 86400.0,
        "cooldown_hours": 20.0,
        "min_clusters": 4,
        "rich_top_n": 5,
        "gap_top_n": 3,
        "max_tokens": 120,
        "salience": 0.5,
    }
    params.update(kw)
    worker = KnowledgeMapReflectionWorker(
        topic_graph_provider=lambda: graph,
        memory_store=store if store is not None else _FakeStore(),
        embedder=embedder if embedder is not None else _FakeEmbedder(),
        kv_get=kv.kv_get,
        kv_set=kv.kv_set,
        ollama=llm if llm is not None else _FakeLLM(),
        model="worker-model",
        notify_memory_added=notify,
        **params,
    )
    return worker, kv


# ── clean helper ──────────────────────────────────────────────────────────


class CleanOutputTests(unittest.TestCase):
    def test_strips_quotes(self) -> None:
        self.assertEqual(clean_reflection_output('"hello there"'), "hello there")

    def test_strips_fence(self) -> None:
        self.assertEqual(clean_reflection_output("```\nhello\n```"), "hello")

    def test_empty(self) -> None:
        self.assertEqual(clean_reflection_output(""), "")
        self.assertEqual(clean_reflection_output("   "), "")

    def test_truncates_long(self) -> None:
        out = clean_reflection_output("word " * 100)
        self.assertLessEqual(len(out), 322)
        self.assertTrue(out.endswith("\u2026"))


# ── worker ──────────────────────────────────────────────────────────────


class WorkerTests(unittest.TestCase):
    def test_writes_mindmap_reflection(self) -> None:
        graph = _FakeGraph(rich=_rich(5), gaps=[_Entry("cooking", 6)])
        store = _FakeStore()
        worker, _kv = _make_worker(graph=graph, store=store)
        result = worker.run()
        self.assertEqual(result["wrote"], 1)
        self.assertEqual(len(store.added), 1)
        mem = store.added[0]
        self.assertEqual(mem.kind, "reflection")
        self.assertTrue(mem.content.startswith(MINDMAP_PREFIX))
        self.assertGreater(result["rich"], 0)

    def test_notify_called_with_memory(self) -> None:
        seen: list = []
        worker, _kv = _make_worker(
            graph=_FakeGraph(rich=_rich(5)), notify=seen.append
        )
        worker.run()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].content.startswith(MINDMAP_PREFIX))

    def test_disabled(self) -> None:
        worker, _kv = _make_worker(
            graph=_FakeGraph(rich=_rich(5)), enabled_provider=lambda: False
        )
        self.assertTrue(worker.run().get("disabled"))

    def test_no_llm(self) -> None:
        kv = _KV()
        worker = KnowledgeMapReflectionWorker(
            topic_graph_provider=lambda: _FakeGraph(rich=_rich(5)),
            memory_store=_FakeStore(),
            embedder=_FakeEmbedder(),
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
            ollama=None,
            model=None,
        )
        self.assertTrue(worker.run().get("no_llm"))

    def test_no_embedder(self) -> None:
        # Build explicitly (the helper substitutes a real embedder when
        # embedder=None) to exercise the no_embedder branch.
        kv = _KV()
        worker = KnowledgeMapReflectionWorker(
            topic_graph_provider=lambda: _FakeGraph(rich=_rich(5)),
            memory_store=_FakeStore(),
            embedder=None,
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
            ollama=_FakeLLM(),
            model="m",
        )
        self.assertTrue(worker.run().get("no_embedder"))

    def test_no_graph(self) -> None:
        worker, _kv = _make_worker(graph=None)
        self.assertTrue(worker.run().get("no_graph"))

    def test_no_context_when_too_few_clusters(self) -> None:
        worker, _kv = _make_worker(graph=_FakeGraph(rich=_rich(3)), min_clusters=4)
        self.assertTrue(worker.run().get("no_context"))

    def test_empty_llm_reply_no_reflection(self) -> None:
        worker, _kv = _make_worker(
            graph=_FakeGraph(rich=_rich(5)), llm=_FakeLLM(reply="   ")
        )
        self.assertTrue(worker.run().get("no_reflection"))

    def test_cooldown_blocks_second_run(self) -> None:
        graph = _FakeGraph(rich=_rich(5))
        worker, _kv = _make_worker(graph=graph)
        self.assertEqual(worker.run()["wrote"], 1)
        self.assertTrue(worker.run().get("skipped_cooldown"))

    def test_force_next_bypasses_cooldown(self) -> None:
        graph = _FakeGraph(rich=_rich(5))
        store = _FakeStore()
        worker, _kv = _make_worker(graph=graph, store=store)
        worker.run()
        worker.force_next()
        self.assertEqual(worker.run()["wrote"], 1)
        self.assertEqual(len(store.added), 2)

    def test_dedupe_still_stamps_cooldown(self) -> None:
        graph = _FakeGraph(rich=_rich(5))
        worker, kv = _make_worker(graph=graph, store=_FakeStore(dedupe=True))
        result = worker.run()
        self.assertTrue(result.get("deduped"))
        self.assertEqual(result["wrote"], 0)
        # cooldown stamped so it won't re-attempt every tick
        self.assertIn("knowledge_map_reflection.last_fired_at", kv.d)
        self.assertTrue(worker.run().get("skipped_cooldown"))

    def test_gap_top_n_zero_skips_gap_read(self) -> None:
        graph = _FakeGraph(rich=_rich(5), gaps=[_Entry("x", 6)])
        worker, _kv = _make_worker(graph=graph, gap_top_n=0)
        worker.run()
        self.assertEqual(graph.gap_calls, 0)


class WorkerDemandTests(unittest.TestCase):
    """The P44 probe: cheap gates only, and never the graph read."""

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def test_reflection_due_reports_pressure(self) -> None:
        worker, _kv = _make_worker(graph=_FakeGraph(rich=_rich(5)))
        now = self._now()
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertGreater(signal.pressure, 0.0)
        self.assertTrue(signal.needs_llm)

    def test_cooldown_vetoes(self) -> None:
        worker, _kv = _make_worker(graph=_FakeGraph(rich=_rich(5)))
        self.assertEqual(worker.run()["wrote"], 1)
        now = self._now()
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "skipped_cooldown")

    def test_probe_never_reads_the_graph(self) -> None:
        """``_read_shape`` walks cluster activity — run-shaped, not probe.

        A thin graph is discovered by ``run``; the worker's 2.4-hour
        anti-thrash floor is what stops it being rediscovered often.
        """
        graph = _FakeGraph(rich=_rich(5), gaps=[_Entry("x", 6)])
        worker, _kv = _make_worker(graph=graph)
        now = self._now()
        worker.demand(now=now, last_run_at=None)
        worker.is_ready(now=now, last_run_at=None)
        self.assertEqual(graph.gap_calls, 0)

    def test_probe_does_not_consume_force_next(self) -> None:
        worker, _kv = _make_worker(graph=_FakeGraph(rich=_rich(5)))
        worker.run()
        worker.force_next()
        now = self._now()

        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.reason, "forced")
        self.assertTrue(worker._force_next)

        self.assertEqual(worker.run()["wrote"], 1)
        self.assertFalse(worker._force_next)

    def test_probe_writes_nothing(self) -> None:
        store = _FakeStore()
        worker, kv = _make_worker(graph=_FakeGraph(rich=_rich(5)), store=store)
        now = self._now()
        worker.demand(now=now, last_run_at=None)
        worker.is_ready(now=now, last_run_at=None)
        self.assertEqual(kv.d, {})
        self.assertEqual(len(store.added), 0)


class RecencyPhraseTests(unittest.TestCase):
    def test_none_is_blank(self) -> None:
        self.assertEqual(recency_phrase(None), "")

    def test_buckets(self) -> None:
        self.assertEqual(recency_phrase(1.0), "hot this week")
        self.assertEqual(recency_phrase(7.0), "hot this week")
        self.assertEqual(recency_phrase(20.0), "active recently")
        self.assertEqual(recency_phrase(60.0), "cooled off, weeks since")
        self.assertEqual(recency_phrase(150.0), "quiet for a couple months")
        self.assertEqual(recency_phrase(400.0), "gone quiet, months since")


class ClusterActivityShapeTests(unittest.TestCase):
    """The worker reads recency via ``cluster_activity`` when available and
    threads the recency phrase into the LLM seed payload."""

    def test_recency_phrases_reach_the_llm_payload(self) -> None:
        rich = [
            _Activity("work stuff", 20, "x", 2.0),     # hot this week
            _Activity("old hobby", 12, "y", 300.0),    # gone quiet, months since
            _Activity("cooking", 8, "z", 60.0),        # cooled off, weeks since
            _Activity("travel", 6, "w", 20.0),         # active recently
        ]
        llm = _CapturingLLM()
        worker, _kv = _make_worker(
            graph=_FakeGraphWithActivity(rich), llm=llm, min_clusters=4,
        )
        result = worker.run()
        self.assertEqual(result["wrote"], 1)
        self.assertIn("hot this week", llm.last_user)
        self.assertIn("gone quiet, months since", llm.last_user)
        self.assertIn("how recently active", llm.last_user)

    def test_falls_back_to_interest_map_without_cluster_activity(self) -> None:
        # The legacy _FakeGraph has no cluster_activity -> interest_map path,
        # recency-free, still writes a reflection (back-compat).
        llm = _CapturingLLM()
        worker, _kv = _make_worker(graph=_FakeGraph(rich=_rich(5)), llm=llm)
        self.assertEqual(worker.run()["wrote"], 1)
        self.assertIn("topic 0", llm.last_user)


class ConceptAnnotationTests(unittest.TestCase):
    """L28: territories carry what Aiko believes about them, read through
    ``ConceptView.for_cluster`` off the cluster's representative id."""

    @staticmethod
    def _graph() -> _FakeGraphWithActivity:
        return _FakeGraphWithActivity([
            _Activity("work stuff", 20, "x", 2.0, representative_id=11),
            _Activity("old hobby", 12, "y", 300.0, representative_id=22),
            _Activity("cooking", 8, "z", 60.0, representative_id=0),
            _Activity("travel", 6, "w", 20.0, representative_id=44),
        ])

    def test_concepts_reach_the_llm_payload(self) -> None:
        view = _FakeView({
            11: [_Concept("he burns out when deadlines stack up")],
            22: [_Concept("he misses playing guitar")],
        })
        llm = _CapturingLLM()
        worker, _kv = _make_worker(
            graph=self._graph(), llm=llm, min_clusters=4,
            view_provider=lambda: view,
        )
        self.assertEqual(worker.run()["wrote"], 1)
        self.assertIn("you believe: he burns out when deadlines stack up", llm.last_user)
        self.assertIn("he misses playing guitar", llm.last_user)
        # Still says how much / how recently.
        self.assertIn("20 memories, hot this week", llm.last_user)

    def test_most_confident_concepts_win_the_cap(self) -> None:
        # Same kind throughout, so the kind-spreading draw collapses to a
        # plain confidence order and a one-kind territory still fills up.
        view = _FakeView({
            11: [
                _Concept("weak hunch", 0.2),
                _Concept("firm belief", 0.9),
                _Concept("middling", 0.5),
            ],
        })
        llm = _CapturingLLM()
        worker, _kv = _make_worker(
            graph=self._graph(), llm=llm, min_clusters=4,
            view_provider=lambda: view, concepts_per_cluster=2,
        )
        worker.run()
        self.assertIn("firm belief; middling", llm.last_user)
        self.assertNotIn("weak hunch", llm.last_user)

    def test_the_read_is_scoped_to_the_declared_diet(self) -> None:
        # Unscoped, the annotation took whatever was edged to the cluster.
        view = _FakeView({11: [_Concept("something")]})
        worker, _kv = _make_worker(
            graph=self._graph(), min_clusters=4, view_provider=lambda: view,
        )
        worker.run()
        asked = view.kinds_asked[0]
        self.assertIsNotNone(asked)
        self.assertEqual(set(asked), set(diet_for("knowledge_map_reflection").kinds))

    def test_a_kind_outside_the_diet_never_annotates_a_territory(self) -> None:
        llm = _CapturingLLM()
        view = _FakeView({
            11: [
                _Concept("she will not discuss his ex", 0.95, kind="boundary"),
                _Concept("he burns out on stacked deadlines", 0.6),
            ],
        })
        worker, _kv = _make_worker(
            graph=self._graph(), llm=llm, min_clusters=4,
            view_provider=lambda: view, concepts_per_cluster=1,
        )
        worker.run()
        self.assertIn("he burns out on stacked deadlines", llm.last_user)
        self.assertNotIn("his ex", llm.last_user)

    def test_two_slots_go_to_two_different_kinds(self) -> None:
        # The reason the draw is not a straight confidence sort: with two
        # slots per territory, the firmest two things she holds are
        # routinely the same kind, and a reflection on the whole map wants
        # a taste beside a conviction rather than two of either.
        llm = _CapturingLLM()
        view = _FakeView({
            11: [
                _Concept("he is deadline-driven", 0.9),
                _Concept("he is happiest shipping", 0.85),
                _Concept("she loves the debugging part", 0.4, kind="taste"),
            ],
        })
        worker, _kv = _make_worker(
            graph=self._graph(), llm=llm, min_clusters=4,
            view_provider=lambda: view, concepts_per_cluster=2,
        )
        worker.run()
        self.assertIn("he is deadline-driven", llm.last_user)
        self.assertIn("she loves the debugging part", llm.last_user)
        self.assertNotIn("happiest shipping", llm.last_user)

    def test_unresolved_representative_is_not_queried(self) -> None:
        view = _FakeView({11: [_Concept("something")]})
        worker, _kv = _make_worker(
            graph=self._graph(), min_clusters=4, view_provider=lambda: view,
        )
        worker.run()
        self.assertNotIn(0, view.calls)

    def test_cold_view_leaves_payload_untouched(self) -> None:
        llm = _CapturingLLM()
        worker, _kv = _make_worker(
            graph=self._graph(), llm=llm, min_clusters=4,
            view_provider=lambda: _FakeView(enabled=False),
        )
        self.assertEqual(worker.run()["wrote"], 1)
        self.assertNotIn("you believe", llm.last_user)

    def test_zero_per_cluster_skips_the_view_entirely(self) -> None:
        view = _FakeView({11: [_Concept("something")]})
        worker, _kv = _make_worker(
            graph=self._graph(), min_clusters=4, view_provider=lambda: view,
            concepts_per_cluster=0,
        )
        worker.run()
        self.assertEqual(view.calls, [])

    def test_view_failure_still_writes_the_reflection(self) -> None:
        llm = _CapturingLLM()
        worker, _kv = _make_worker(
            graph=self._graph(), llm=llm, min_clusters=4,
            view_provider=lambda: _FakeView({11: []}, raises=True),
        )
        self.assertEqual(worker.run()["wrote"], 1)
        self.assertNotIn("you believe", llm.last_user)

    def test_provider_raising_is_swallowed(self) -> None:
        def boom():
            raise RuntimeError("no view")

        worker, _kv = _make_worker(
            graph=self._graph(), min_clusters=4, view_provider=boom,
        )
        self.assertEqual(worker.run()["wrote"], 1)


if __name__ == "__main__":
    unittest.main()
