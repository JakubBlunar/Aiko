"""Tests for the F10g per-cluster rolling digest worker.

The worker writes one high-salience ``kind="topic_digest"`` memory per
dense cluster, cached by representative id, and rebuilds a
``{cluster_id: memory_id}`` map the RAG retriever reads. It uses a fake
LLM + embedder and a real persistent :class:`TopicGraph` over a stub
memory mirror that supports the worker's ``add``/``update`` API as well
as the graph's mirror snapshot.
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
from app.core.conversation.topic_digest_worker import TopicDigestWorker


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
    source_session: str | None = None
    source_message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "content": self.content, "kind": self.kind}


class _StubMemoryStore:
    """Supports the graph mirror snapshot AND the worker add/update API."""

    def __init__(self) -> None:
        self._mirror: dict[int, _StubMemory] = {}
        self._lock = threading.Lock()
        self._next_id = 1000

    # seed helper (not the worker API)
    def seed(self, mem: _StubMemory) -> None:
        with self._lock:
            self._mirror[mem.id] = mem

    def get(self, memory_id: int) -> _StubMemory | None:
        with self._lock:
            return self._mirror.get(int(memory_id))

    def add(
        self,
        content: str,
        kind: str,
        embedding: np.ndarray,
        *,
        salience: float = 0.5,
        tier: str | None = None,
        skip_dedupe: bool = False,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> _StubMemory:
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            mem = _StubMemory(
                id=mid,
                content=content,
                embedding=np.asarray(embedding, dtype=np.float32),
                kind=kind,
                salience=salience,
                tier=tier or "long_term",
                metadata=dict(metadata or {}),
            )
            self._mirror[mid] = mem
            return mem

    def update(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        embedding: np.ndarray | None = None,
        salience: float | None = None,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> _StubMemory | None:
        with self._lock:
            mem = self._mirror.get(int(memory_id))
            if mem is None:
                return None
            if content is not None:
                mem.content = content
            if embedding is not None:
                mem.embedding = np.asarray(embedding, dtype=np.float32)
            if salience is not None:
                mem.salience = salience
            if metadata is not None:
                mem.metadata = dict(metadata)
            return mem


def _vec(seed: list[float]) -> np.ndarray:
    return _normalise(np.asarray(seed, dtype=np.float32))


def _dense_store(n: int = 5) -> _StubMemoryStore:
    store = _StubMemoryStore()
    for i in range(n):
        jitter = 0.30 + i * 0.01
        store.seed(
            _StubMemory(i + 1, f"cats and naps {i}", _vec([0.95, jitter, 0.0, 0.0]))
        )
    return store


class _FakeEmbedder:
    def embed(self, text: str) -> np.ndarray:
        return _vec([0.95, 0.30, 0.0, 0.0])


class _FakeOllama:
    def __init__(self, digest: str = "Cats and naps come up a lot.") -> None:
        self.digest = digest
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
        yield json.dumps({"digest": self.digest})


def _agent_settings(**over: Any) -> SimpleNamespace:
    base = dict(
        topic_digest_enabled=True,
        topic_digest_interval_seconds=3600.0,
        topic_digest_max_per_run=3,
        topic_digest_max_tokens=256,
        topic_digest_min_cluster_size=3,
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


def _worker(db, g, mem, fake, settings=None) -> TopicDigestWorker:
    return TopicDigestWorker(
        topic_graph=g,
        memory_store=mem,
        embedder=_FakeEmbedder(),
        ollama=fake,
        chat_model="x",
        cancel_event=threading.Event(),
        agent_settings=settings or _agent_settings(),
        kv_get=db.kv_get,
        kv_set=db.kv_set,
    )


class TopicDigestWorkerTests(unittest.TestCase):
    def test_writes_digest_and_builds_map(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama("Cats and naps.")
        worker = _worker(db, g, mem, fake)

        result = worker.run()
        self.assertEqual(result["written"], 1)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(worker.cluster_digest_map), 1)

        cid, mem_id = next(iter(worker.cluster_digest_map.items()))
        digest = mem.get(mem_id)
        self.assertIsNotNone(digest)
        self.assertEqual(digest.kind, "topic_digest")
        self.assertEqual(worker.digest_for_cluster(cid), mem_id)
        # Cache keyed by representative id.
        cluster = g.topic_clusters()[0]
        raw = db.kv_get("aiko.topic_digest." + str(cluster.representative_id))
        self.assertIsNotNone(raw)
        self.assertEqual(json.loads(raw)["memory_id"], mem_id)

    def test_digest_excluded_from_clustering(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        worker = _worker(db, g, mem, _FakeOllama())
        worker.run()
        digest_id = next(iter(worker.cluster_digest_map.values()))
        # A fresh rebuild must never pull the digest into a cluster.
        g.rebuild()
        for cluster in g.topic_clusters():
            self.assertNotIn(digest_id, set(cluster.member_ids))

    def test_second_run_reuses_without_llm(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama()
        worker = _worker(db, g, mem, fake)
        worker.run()
        first_id = next(iter(worker.cluster_digest_map.values()))
        fake.calls = 0

        result = worker.run()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(result["reused"], 1)
        self.assertEqual(result["written"], 0)
        self.assertEqual(
            next(iter(worker.cluster_digest_map.values())), first_id
        )

    def test_drift_refreshes_in_place(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama()
        worker = _worker(db, g, mem, fake)
        worker.run()
        cluster = g.topic_clusters()[0]
        rep = cluster.representative_id
        first_id = worker.cluster_digest_map[cluster.cluster_id]
        # Force a cache "size" that looks badly drifted vs the live size.
        db.kv_set(
            "aiko.topic_digest." + str(rep),
            json.dumps({"memory_id": first_id, "size": 1}),
        )
        fake.calls = 0
        worker.run()
        self.assertEqual(fake.calls, 1)  # regenerated
        # Updated in place: same memory id, content refreshed.
        self.assertEqual(worker.cluster_digest_map[cluster.cluster_id], first_id)

    def test_min_cluster_size_skips_small(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama()
        worker = _worker(
            db, g, mem, fake,
            settings=_agent_settings(topic_digest_min_cluster_size=99),
        )
        result = worker.run()
        self.assertEqual(result.get("written", 0), 0)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(len(worker.cluster_digest_map), 0)

    def test_disabled_skips(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        fake = _FakeOllama()
        worker = _worker(
            db, g, mem, fake,
            settings=_agent_settings(topic_digest_enabled=False),
        )
        self.assertTrue(worker.run().get("skipped"))
        self.assertEqual(fake.calls, 0)

    def test_not_persistent_skips(self) -> None:
        mem = _dense_store(5)
        g = TopicGraph(mem, similarity=0.55, min_cluster_size=2)
        tmp = tempfile.mkdtemp()
        db = ChatDatabase(Path(tmp) / "t.db")
        worker = _worker(db, g, mem, _FakeOllama())
        self.assertTrue(worker.run().get("skipped"))

    def test_map_persisted_and_loaded(self) -> None:
        mem = _dense_store(5)
        db, g = _persistent_graph(mem)
        g.rebuild()
        worker = _worker(db, g, mem, _FakeOllama())
        worker.run()
        self.assertIsNotNone(db.kv_get("aiko.topic_digest_map"))
        # A fresh worker warm-loads the map from kv.
        worker2 = _worker(db, g, mem, _FakeOllama())
        self.assertEqual(worker2.cluster_digest_map, worker.cluster_digest_map)


class HelperTests(unittest.TestCase):
    def test_drifted(self) -> None:
        self.assertFalse(TopicDigestWorker._drifted(10, 10))
        self.assertFalse(TopicDigestWorker._drifted(12, 10))  # 20% < 50%
        self.assertTrue(TopicDigestWorker._drifted(20, 10))   # 100% > 50%
        self.assertTrue(TopicDigestWorker._drifted(10, None))
        self.assertTrue(TopicDigestWorker._drifted(10, 0))

    def test_parse_digest(self) -> None:
        self.assertEqual(
            TopicDigestWorker._parse_digest('{"digest": "He loves cats a lot."}'),
            "He loves cats a lot.",
        )
        self.assertEqual(
            TopicDigestWorker._parse_digest('junk {"digest": "ok then it is"} x'),
            "ok then it is",
        )
        self.assertEqual(TopicDigestWorker._parse_digest("not json"), "")
        self.assertEqual(TopicDigestWorker._parse_digest('{"digest": "short"}'), "")

    def test_cached_memory_id(self) -> None:
        self.assertEqual(TopicDigestWorker._cached_memory_id({"memory_id": 7}), 7)
        self.assertIsNone(TopicDigestWorker._cached_memory_id({}))
        self.assertIsNone(TopicDigestWorker._cached_memory_id({"memory_id": "x"}))
        self.assertIsNone(TopicDigestWorker._cached_memory_id({"memory_id": 0}))


if __name__ == "__main__":
    unittest.main()
