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

import numpy as np

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


if __name__ == "__main__":
    unittest.main()
