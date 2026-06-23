"""Tests for the F10a cluster-label worker + TopicGraph.set_cluster_label.

The worker names topic-graph clusters with a worker-LLM pass and caches
the result in ``kv_meta`` keyed by the cluster representative so a batch
refit doesn't force a re-label. These tests use a fake LLM (no Ollama)
and a real persistent TopicGraph over a stub memory mirror.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.topic_cluster_store import TopicClusterStore
from app.core.conversation.topic_graph import TopicGraph, _normalise
from app.core.conversation.topic_label_worker import ClusterLabelWorker


@dataclass
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    kind: str = "fact"
    salience: float = 0.5
    use_count: int = 0
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "long_term"


class _StubMemoryStore:
    def __init__(self) -> None:
        self._mirror: dict[int, _StubMemory] = {}
        self._lock = threading.Lock()

    def add(self, mem: _StubMemory) -> None:
        with self._lock:
            self._mirror[mem.id] = mem

    def get(self, memory_id: int) -> _StubMemory | None:
        with self._lock:
            return self._mirror.get(int(memory_id))


def _vec(seed: list[float]) -> np.ndarray:
    return _normalise(np.asarray(seed, dtype=np.float32))


def _two_cluster_store() -> _StubMemoryStore:
    store = _StubMemoryStore()
    for mem in [
        _StubMemory(1, "cat naps in the sun", _vec([0.95, 0.30, 0.0, 0.0])),
        _StubMemory(2, "kittens on the windowsill", _vec([0.92, 0.39, 0.0, 0.0])),
        _StubMemory(3, "warm cats curled up", _vec([0.97, 0.25, 0.0, 0.0])),
        _StubMemory(10, "basil seedlings", _vec([0.0, 0.0, 0.95, 0.30])),
        _StubMemory(11, "watering the rosemary", _vec([0.0, 0.0, 0.92, 0.39])),
        _StubMemory(12, "herbs in clay pots", _vec([0.0, 0.0, 0.97, 0.25])),
    ]:
        store.add(mem)
    return store


class _FakeOllama:
    """Yields a JSON label object; counts calls so we can assert the
    cache-reapply path makes no LLM call."""

    def __init__(self, label: str = "weekend hiking plans") -> None:
        self.label = label
        self.calls = 0

    def chat_stream(
        self,
        messages,
        *,
        options=None,
        model=None,
        stop_event=None,
        format_json=False,
        surface=None,
    ):
        self.calls += 1
        yield json.dumps({"label": self.label})


def _agent_settings(**over: Any) -> SimpleNamespace:
    base = dict(
        topic_label_enabled=True,
        topic_label_interval_seconds=1800.0,
        topic_label_max_per_run=4,
        topic_label_max_tokens=32,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _persistent_graph(mem_store) -> tuple[ChatDatabase, TopicGraph]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    graph = TopicGraph(
        mem_store,
        similarity=0.55,
        min_cluster_size=2,
        filter_threshold=0.65,
        cluster_store=TopicClusterStore(db),
    )
    return db, graph


class SetClusterLabelTests(unittest.TestCase):
    def test_sets_and_persists_label(self) -> None:
        mem = _two_cluster_store()
        db, g = _persistent_graph(mem)
        g.rebuild()
        clusters = g.topic_clusters()
        cid = clusters[0].cluster_id
        self.assertTrue(g.set_cluster_label(cid, "favourite cats"))
        # Live state reflects it immediately.
        summary = next(
            c.summary for c in g.topic_clusters() if c.cluster_id == cid
        )
        self.assertEqual(summary, "favourite cats")
        # Persisted: a fresh graph warm-starts with the label.
        g2 = TopicGraph(
            mem, similarity=0.55, min_cluster_size=2,
            cluster_store=TopicClusterStore(db),
        )
        labels = {c.cluster_id: c.summary for c in g2.topic_clusters()}
        self.assertIn("favourite cats", labels.values())

    def test_unknown_cluster_returns_false(self) -> None:
        mem = _two_cluster_store()
        _, g = _persistent_graph(mem)
        g.rebuild()
        self.assertFalse(g.set_cluster_label(99999, "nope"))

    def test_blank_label_rejected(self) -> None:
        mem = _two_cluster_store()
        _, g = _persistent_graph(mem)
        g.rebuild()
        cid = g.topic_clusters()[0].cluster_id
        self.assertFalse(g.set_cluster_label(cid, "   "))

    def test_non_persistent_is_noop(self) -> None:
        mem = _two_cluster_store()
        g = TopicGraph(mem, similarity=0.55, min_cluster_size=2)
        self.assertFalse(g.set_cluster_label(0, "anything"))


class ClusterLabelWorkerTests(unittest.TestCase):
    def _worker(self, db, g, mem, fake, settings=None) -> ClusterLabelWorker:
        return ClusterLabelWorker(
            topic_graph=g,
            memory_store=mem,
            ollama=fake,
            chat_model="x",
            cancel_event=threading.Event(),
            agent_settings=settings or _agent_settings(),
            kv_get=db.kv_get,
            kv_set=db.kv_set,
        )

    def test_labels_clusters_and_caches(self) -> None:
        mem = _two_cluster_store()
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama(label="cosy cats")
        worker = self._worker(db, g, mem, fake)

        result = worker.run()
        self.assertEqual(result["labeled"], 2)
        self.assertEqual(fake.calls, 2)
        # Both clusters now carry the LLM label.
        for cluster in g.topic_clusters():
            self.assertEqual(cluster.summary, "cosy cats")
        # Cache written keyed by representative id.
        for cluster in g.topic_clusters():
            raw = db.kv_get("aiko.topic_label." + str(cluster.representative_id))
            self.assertIsNotNone(raw)
            self.assertEqual(json.loads(raw)["label"], "cosy cats")

    def test_second_run_no_change_skips_llm(self) -> None:
        mem = _two_cluster_store()
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama(label="cosy cats")
        worker = self._worker(db, g, mem, fake)
        worker.run()
        fake.calls = 0
        # Nothing drifted and labels already applied -> no work.
        result = worker.run()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(result["labeled"], 0)
        self.assertEqual(result["reapplied"], 0)

    def test_rebuild_then_reapply_from_cache_without_llm(self) -> None:
        mem = _two_cluster_store()
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama(label="cosy cats")
        worker = self._worker(db, g, mem, fake)
        worker.run()
        # A batch refit resets live labels back to the heuristic, but the
        # kv cache (keyed by representative) survives.
        g.rebuild()
        heuristic = {c.summary for c in g.topic_clusters()}
        self.assertNotIn("cosy cats", heuristic)
        fake.calls = 0
        result = worker.run()
        self.assertEqual(fake.calls, 0)  # cache reapply, no LLM
        self.assertGreaterEqual(result["reapplied"], 1)
        self.assertIn("cosy cats", {c.summary for c in g.topic_clusters()})

    def test_disabled_skips(self) -> None:
        mem = _two_cluster_store()
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama()
        worker = self._worker(
            db, g, mem, fake, settings=_agent_settings(topic_label_enabled=False)
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(fake.calls, 0)

    def test_max_per_run_bounds_llm_calls(self) -> None:
        mem = _two_cluster_store()
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama()
        worker = self._worker(
            db, g, mem, fake, settings=_agent_settings(topic_label_max_per_run=1)
        )
        result = worker.run()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(result["labeled"], 1)
        self.assertGreaterEqual(result["pending"], 1)


class HelperTests(unittest.TestCase):
    def test_drifted(self) -> None:
        self.assertFalse(ClusterLabelWorker._drifted(10, 10))
        self.assertFalse(ClusterLabelWorker._drifted(12, 10))  # 20% < 50%
        self.assertTrue(ClusterLabelWorker._drifted(20, 10))   # 100% > 50%
        self.assertTrue(ClusterLabelWorker._drifted(10, None))
        self.assertTrue(ClusterLabelWorker._drifted(10, 0))

    def test_parse_label(self) -> None:
        self.assertEqual(
            ClusterLabelWorker._parse_label('{"label": "taste in music"}'),
            "taste in music",
        )
        self.assertEqual(
            ClusterLabelWorker._parse_label('garbage {"label": "x"} trailing'),
            "x",
        )
        self.assertEqual(ClusterLabelWorker._parse_label("not json"), "")
        self.assertEqual(ClusterLabelWorker._parse_label('{"label": ""}'), "")


if __name__ == "__main__":
    unittest.main()
