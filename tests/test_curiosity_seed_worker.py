"""Tests for :mod:`app.core.proactive.curiosity_seed_worker` (K9 personality backlog).

The worker glues together a deterministic LLM mock, a stub topic
graph, a stub embedder, and a real :class:`MemoryStore` only insofar
as the public surface (``add`` / ``iter_by_kind`` / ``update``) is
mocked so the tests don't need SQLite.
"""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np

from app.core.proactive.curiosity_seed_worker import CuriositySeedWorker


# ── stubs ────────────────────────────────────────────────────────────


@dataclass
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    kind: str = "curiosity_seed"
    salience: float = 0.45
    use_count: int = 0
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "scratchpad"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "content": self.content, "kind": self.kind}


class _StubMemoryStore:
    def __init__(self) -> None:
        self._mirror: dict[int, _StubMemory] = {}
        self._next_id = 1
        self.added: list[dict[str, Any]] = []

    def add(
        self,
        *,
        content: str,
        kind: str,
        embedding: np.ndarray,
        salience: float = 0.5,
        confidence: float = 0.7,
        tier: str = "long_term",
        metadata: dict[str, Any] | None = None,
    ) -> _StubMemory:
        mem_id = self._next_id
        self._next_id += 1
        mem = _StubMemory(
            id=mem_id,
            content=content,
            embedding=embedding,
            kind=kind,
            salience=salience,
            metadata=dict(metadata or {}),
            tier=tier,
        )
        self._mirror[mem_id] = mem
        self.added.append({
            "id": mem_id,
            "content": content,
            "kind": kind,
            "metadata": dict(metadata or {}),
            "tier": tier,
        })
        return mem

    def iter_by_kind(self, kind: str) -> list[_StubMemory]:
        return [m for m in self._mirror.values() if m.kind == kind]


class _StubEmbedder:
    """Deterministic embedder: hash text into a 4-D unit vector."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> np.ndarray:
        self.calls.append(text)
        # Stable per-text vector: 4 dims keyed on len + first/last char.
        h = abs(hash(text)) % 1000
        rng = np.random.default_rng(h)
        v = rng.standard_normal(4).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v


class _StubTopicGraph:
    """Always returns the configured (sim, id) tuple. Tests vary it."""

    def __init__(self, *, best_sim: float = 0.0, best_id: int | None = 1) -> None:
        self.best_sim = best_sim
        self.best_id = best_id
        self.calls = 0

    def best_match(self, vec: np.ndarray) -> tuple[float, int | None]:
        self.calls += 1
        return self.best_sim, self.best_id

    def topic_clusters(self) -> list[Any]:
        return []

    def is_close_to_any_cluster(self, vec, threshold=None) -> bool:  # noqa: D401
        thr = threshold if threshold is not None else 0.65
        return self.best_sim >= thr


class _StubOllama:
    """Yields the configured chunks for ``chat_stream``."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls = 0

    def chat_stream(self, *args: Any, **kwargs: Any):
        self.calls += 1
        yield self._payload


# ── helpers ──────────────────────────────────────────────────────────


def _agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        curiosity_seed_enabled=True,
        curiosity_seed_max_active=6,
        curiosity_seed_max_per_run=2,
        curiosity_seed_min_novelty=0.85,
        topic_graph_filter_threshold=0.65,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory_settings() -> SimpleNamespace:
    return SimpleNamespace(curiosity_seed_interval_seconds=3600)


def _build_worker(
    *,
    payload: str,
    best_sim: float = 0.0,
    settings_overrides: dict[str, Any] | None = None,
) -> tuple[CuriositySeedWorker, _StubMemoryStore, _StubOllama, _StubTopicGraph]:
    store = _StubMemoryStore()
    graph = _StubTopicGraph(best_sim=best_sim)
    ollama = _StubOllama(payload)
    embedder = _StubEmbedder()
    worker = CuriositySeedWorker(
        memory_store=store,
        topic_graph=graph,
        embedder=embedder,
        ollama=ollama,
        chat_model="test-model",
        cancel_event=threading.Event(),
        agent_settings=_agent_settings(**(settings_overrides or {})),
        memory_settings=_memory_settings(),
        persona_provider=lambda: "Curiosity:\n- loves rituals",
        rolling_summary_provider=lambda: "Recent chat: about coffee",
        user_display_name_provider=lambda: "Jacob",
        assistant_display_name_provider=lambda: "Aiko",
    )
    return worker, store, ollama, graph


_GOOD_PAYLOAD = (
    '{"seeds": ['
    '{"topic": "your favourite tea ritual", '
    ' "prompt_text": "I have been wondering what your perfect tea moment looks like.", '
    ' "why": "small, sensory, easy to share."}, '
    '{"topic": "morning lighting habits", '
    ' "prompt_text": "Off-topic, but do you ever notice how morning light hits the room?", '
    ' "why": "ambient curiosity"}'
    ']}'
)


# ── tests ────────────────────────────────────────────────────────────


class WriteShapeTests(unittest.TestCase):
    def test_writes_seeds_with_expected_metadata(self) -> None:
        worker, store, _ollama, _graph = _build_worker(payload=_GOOD_PAYLOAD)
        result = worker.run()
        self.assertGreaterEqual(result.get("wrote", 0), 1)
        self.assertEqual(len(store.added), result["wrote"])
        for entry in store.added:
            self.assertEqual(entry["kind"], "curiosity_seed")
            self.assertEqual(entry["tier"], "scratchpad")
            metadata = entry["metadata"]
            self.assertIn("topic", metadata)
            self.assertIn("prompt_text", metadata)
            self.assertEqual(metadata.get("source"), "llm")
            self.assertIsNone(metadata.get("consumed_at"))
            self.assertIn("generated_at", metadata)
            self.assertIn("candidate_score", metadata)


class GraphFilterTests(unittest.TestCase):
    def test_high_graph_sim_rejects_all_candidates(self) -> None:
        # Every candidate's best_match returns 0.99 -> above the
        # 0.65 default filter threshold -> all rejected.
        worker, store, _ollama, _graph = _build_worker(
            payload=_GOOD_PAYLOAD, best_sim=0.99,
        )
        result = worker.run()
        self.assertEqual(result.get("wrote", 0), 0)
        self.assertGreaterEqual(result.get("rejected_graph", 0), 1)
        self.assertEqual(store.added, [])


class NoveltyFilterTests(unittest.TestCase):
    def test_existing_seed_blocks_duplicate(self) -> None:
        worker, store, _ollama, _graph = _build_worker(payload=_GOOD_PAYLOAD)

        # Pre-seed an "active" seed with the same embedding the
        # embedder will produce for the first candidate. Easiest way
        # to force the novelty filter to fire is to monkeypatch the
        # embedder so every call returns one fixed vector.
        fixed = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        worker._embedder.embed = lambda text: fixed  # type: ignore[assignment]
        existing = _StubMemory(
            id=42,
            content="placeholder",
            embedding=fixed,
            metadata={"topic": "placeholder", "prompt_text": "x"},
        )
        store._mirror[42] = existing

        result = worker.run()
        # All candidates collapse to the same vector as the existing
        # seed -> all rejected by the novelty filter.
        self.assertEqual(result.get("wrote", 0), 0)
        self.assertGreaterEqual(result.get("rejected_novelty", 0), 1)


class IsReadyTests(unittest.TestCase):
    def test_disabled_returns_false(self) -> None:
        worker, _store, _ollama, _graph = _build_worker(
            payload=_GOOD_PAYLOAD,
            settings_overrides={"curiosity_seed_enabled": False},
        )
        self.assertFalse(
            worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None),
        )

    def test_max_active_blocks_until_some_consumed(self) -> None:
        worker, store, _ollama, _graph = _build_worker(
            payload=_GOOD_PAYLOAD,
            settings_overrides={"curiosity_seed_max_active": 1},
        )
        # Insert one active seed -> at the cap.
        store._mirror[1] = _StubMemory(
            id=1,
            content="existing",
            embedding=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            metadata={"topic": "existing", "prompt_text": "..."},
        )
        self.assertFalse(
            worker.is_ready(
                now=datetime.now(timezone.utc),
                last_run_at=None,
            )
        )
        # Mark consumed -> no longer counted -> ready.
        store._mirror[1].metadata["consumed_at"] = "2026-01-01T00:00:00+00:00"
        self.assertTrue(
            worker.is_ready(
                now=datetime.now(timezone.utc),
                last_run_at=None,
            )
        )

    def test_recent_run_blocks(self) -> None:
        worker, _store, _ollama, _graph = _build_worker(payload=_GOOD_PAYLOAD)
        # Last run 60s ago, interval is 3600 -> not ready yet.
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        self.assertFalse(
            worker.is_ready(
                now=datetime.now(timezone.utc),
                last_run_at=recent,
            )
        )


class ParseTests(unittest.TestCase):
    def test_returns_empty_on_invalid_json(self) -> None:
        worker, _store, _ollama, _graph = _build_worker(payload="not json at all")
        result = worker.run()
        self.assertEqual(result.get("wrote", 0), 0)
        self.assertEqual(result.get("checked", 0), 0)


if __name__ == "__main__":
    unittest.main()
