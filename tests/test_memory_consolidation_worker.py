"""Tests for :class:`app.core.memory.memory_consolidation_worker.MemoryConsolidationWorker`.

All fakes — no real MemoryStore / embedder / LLM. Embeddings are set
directly on the fake rows so cosine is deterministic; ``classify_pair``
is the real pure heuristic, so contradiction-guard tests use genuinely
contradicting text. Covers clustering (threshold boundary, same-kind,
contradiction guard), primary selection, the LLM merge path + fallback,
re-embed only on text change, archive + provenance, caps, and the
pinned / out-of-window / enabled / rate-limit gates.
"""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.memory.memory_consolidation_worker import (
    MemoryConsolidationWorker,
)


def _vec(*values: float) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


def _iso_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@dataclass
class _FakeMemory:
    id: int
    content: str
    kind: str = "fact"
    salience: float = 0.5
    confidence: float = 0.7
    embedding: np.ndarray = field(default_factory=lambda: _vec(1.0, 0.0))
    created_at: str = field(default_factory=lambda: _iso_ago(1))
    pinned: bool = False
    tier: str = "scratchpad"
    metadata: dict[str, Any] = field(default_factory=dict)


class _FakeStore:
    def __init__(self, rows: list[_FakeMemory]) -> None:
        self._rows = {m.id: m for m in rows}
        self.updates: list[dict[str, Any]] = []

    def iter_by_tier(self, tier: str) -> list[_FakeMemory]:
        return [m for m in self._rows.values() if m.tier == tier]

    def update(self, memory_id: int, **kwargs: Any) -> _FakeMemory | None:
        self.updates.append({"id": memory_id, **kwargs})
        mem = self._rows.get(memory_id)
        if mem is None:
            return None
        if kwargs.get("content") is not None:
            mem.content = kwargs["content"]
        if kwargs.get("tier") is not None:
            mem.tier = kwargs["tier"]
        if kwargs.get("salience") is not None:
            mem.salience = kwargs["salience"]
        if kwargs.get("confidence") is not None:
            mem.confidence = kwargs["confidence"]
        md = kwargs.get("metadata")
        if md is not None:
            if kwargs.get("metadata_merge"):
                mem.metadata = {**mem.metadata, **md}
            else:
                mem.metadata = dict(md)
        return mem


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> np.ndarray:
        self.calls.append(text)
        return _vec(1.0, 0.0)


class _FakeOllama:
    def __init__(self, merged: str = "Merged sentence.") -> None:
        self._merged = merged
        self.calls = 0

    def chat_json(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        import json as _json

        return (_json.dumps({"merged": self._merged}), {})


class _FakeRateLimiter:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.calls = 0

    def allow(self, now: datetime | None = None) -> bool:
        self.calls += 1
        return self._allow


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(memory_consolidation_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory(**overrides: Any) -> SimpleNamespace:
    base = dict(
        consolidation_interval_seconds=21600,
        consolidation_lookback_days=30,
        consolidation_similarity_threshold=0.90,
        consolidation_max_corpus=1000,
        consolidation_max_clusters_per_run=20,
        consolidation_min_cluster_size=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


_UNSET = object()


def _make_worker(
    store: _FakeStore,
    *,
    embedder: _FakeEmbedder | None = None,
    ollama: Any = _UNSET,
    rate_limiter: _FakeRateLimiter | None = None,
    agent: SimpleNamespace | None = None,
    memory: SimpleNamespace | None = None,
    notify: Any | None = None,
) -> MemoryConsolidationWorker:
    return MemoryConsolidationWorker(
        memory_store=store,
        embedder=embedder or _FakeEmbedder(),
        ollama=_FakeOllama() if ollama is _UNSET else ollama,
        chat_model="worker-model",
        rate_limiter=rate_limiter or _FakeRateLimiter(),
        cancel_event=threading.Event(),
        agent_settings=agent or _agent(),
        memory_settings=memory or _memory(),
        notify_memory_updated=notify,
    )


# Two near-duplicate vectors (cosine ~0.99) and one orthogonal.
_DUP_A = _vec(1.0, 0.02)
_DUP_B = _vec(1.0, 0.05)
_FAR = _vec(0.0, 1.0)


class ClusteringTests(unittest.TestCase):
    def test_merges_near_duplicate_pair(self) -> None:
        rows = [
            _FakeMemory(1, "I enjoy hiking on weekends", embedding=_DUP_A),
            _FakeMemory(2, "I enjoy hiking during weekends", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        result = worker.run()
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["absorbed"], 1)

    def test_sub_threshold_pair_not_merged(self) -> None:
        rows = [
            _FakeMemory(1, "I enjoy hiking", embedding=_DUP_A),
            _FakeMemory(2, "I dislike crowds", embedding=_FAR),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        result = worker.run()
        self.assertEqual(result.get("merged", 0), 0)

    def test_different_kinds_not_merged(self) -> None:
        rows = [
            _FakeMemory(1, "coffee in the morning", kind="fact", embedding=_DUP_A),
            _FakeMemory(2, "coffee in the morning", kind="preference", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        result = worker.run()
        self.assertEqual(result.get("merged", 0), 0)

    def test_contradiction_guard_excludes_pair(self) -> None:
        # High cosine but genuinely contradicting -> left for F5.
        rows = [
            _FakeMemory(1, "I love coffee", embedding=_DUP_A),
            _FakeMemory(2, "I hate coffee", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        result = worker.run()
        self.assertEqual(result.get("merged", 0), 0)


class PrimaryAndCommitTests(unittest.TestCase):
    def test_primary_is_highest_confidence(self) -> None:
        rows = [
            _FakeMemory(1, "I enjoy hiking trips", confidence=0.7, embedding=_DUP_A),
            _FakeMemory(2, "I enjoy hiking outings", confidence=0.95, embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        worker.run()
        # Row 2 (higher confidence) is the primary -> promoted long_term.
        self.assertEqual(store._rows[2].tier, "long_term")
        self.assertEqual(store._rows[1].tier, "archive")

    def test_provenance_stamped(self) -> None:
        rows = [
            _FakeMemory(1, "I enjoy hiking trips", confidence=0.95, embedding=_DUP_A),
            _FakeMemory(2, "I enjoy hiking outings", confidence=0.7, embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        worker.run()
        primary = store._rows[1]
        self.assertEqual(primary.tier, "long_term")
        self.assertEqual(sorted(primary.metadata["source_ids"]), [1, 2])
        self.assertIn("consolidated_at", primary.metadata)
        absorbed = store._rows[2]
        self.assertEqual(absorbed.tier, "archive")
        self.assertEqual(absorbed.metadata["consolidated_into"], 1)

    def test_salience_and_confidence_lifted_to_max(self) -> None:
        rows = [
            _FakeMemory(1, "trip a", confidence=0.95, salience=0.4, embedding=_DUP_A),
            _FakeMemory(2, "trip b", confidence=0.7, salience=0.9, embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        worker.run()
        primary = store._rows[1]
        self.assertAlmostEqual(primary.salience, 0.9)
        self.assertAlmostEqual(primary.confidence, 0.95)

    def test_notify_fires_for_primary_and_absorbed(self) -> None:
        notified: list[int] = []
        rows = [
            _FakeMemory(1, "trip a", embedding=_DUP_A),
            _FakeMemory(2, "trip b", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(
            store, notify=lambda p: notified.append(p["memory_id"])
        )
        worker.run()
        self.assertIn(1, notified)
        self.assertIn(2, notified)


class MergeTextTests(unittest.TestCase):
    def test_llm_merge_used_and_reembedded(self) -> None:
        embedder = _FakeEmbedder()
        ollama = _FakeOllama(merged="I enjoy hiking on the weekends.")
        rows = [
            _FakeMemory(1, "I enjoy hiking on weekends", embedding=_DUP_A),
            _FakeMemory(2, "I enjoy hiking during weekends", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store, embedder=embedder, ollama=ollama)
        result = worker.run()
        self.assertEqual(result["llm_used"], 1)
        self.assertEqual(ollama.calls, 1)
        # Merged text differs from primary -> re-embedded once.
        self.assertEqual(len(embedder.calls), 1)

    def test_fallback_when_no_ollama(self) -> None:
        embedder = _FakeEmbedder()
        rows = [
            _FakeMemory(1, "I enjoy hiking on weekends", embedding=_DUP_A),
            _FakeMemory(2, "I enjoy hiking during weekends", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store, embedder=embedder, ollama=None)
        result = worker.run()
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["llm_used"], 0)
        # No text change -> no re-embed.
        self.assertEqual(len(embedder.calls), 0)

    def test_rate_limited_falls_back(self) -> None:
        embedder = _FakeEmbedder()
        ollama = _FakeOllama()
        limiter = _FakeRateLimiter(allow=False)
        rows = [
            _FakeMemory(1, "I enjoy hiking on weekends", embedding=_DUP_A),
            _FakeMemory(2, "I enjoy hiking during weekends", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(
            store, embedder=embedder, ollama=ollama, rate_limiter=limiter,
        )
        result = worker.run()
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["llm_used"], 0)
        self.assertEqual(ollama.calls, 0)


class GateTests(unittest.TestCase):
    def test_disabled_skips(self) -> None:
        rows = [
            _FakeMemory(1, "trip a", embedding=_DUP_A),
            _FakeMemory(2, "trip b", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(
            store, agent=_agent(memory_consolidation_enabled=False)
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "disabled")

    def test_pinned_excluded(self) -> None:
        rows = [
            _FakeMemory(1, "trip a", embedding=_DUP_A, pinned=True),
            _FakeMemory(2, "trip b", embedding=_DUP_B),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        result = worker.run()
        # Only one non-pinned candidate -> corpus too small.
        self.assertTrue(result.get("skipped"))

    def test_out_of_window_excluded(self) -> None:
        rows = [
            _FakeMemory(1, "trip a", embedding=_DUP_A, created_at=_iso_ago(1)),
            _FakeMemory(2, "trip b", embedding=_DUP_B, created_at=_iso_ago(90)),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(store)
        result = worker.run()
        self.assertTrue(result.get("skipped"))

    def test_max_clusters_cap(self) -> None:
        # Two independent dup pairs; cap to 1 cluster per run.
        rows = [
            _FakeMemory(1, "hiking weekends a", embedding=_vec(1.0, 0.02)),
            _FakeMemory(2, "hiking weekends b", embedding=_vec(1.0, 0.04)),
            _FakeMemory(3, "reading novels a", embedding=_vec(0.02, 1.0)),
            _FakeMemory(4, "reading novels b", embedding=_vec(0.04, 1.0)),
        ]
        store = _FakeStore(rows)
        worker = _make_worker(
            store, memory=_memory(consolidation_max_clusters_per_run=1),
        )
        result = worker.run()
        self.assertEqual(result["clusters"], 1)
        self.assertEqual(result["merged"], 1)


if __name__ == "__main__":
    unittest.main()
