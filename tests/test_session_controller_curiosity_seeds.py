"""Tests for the SessionController curiosity-seed surfaces (K9).

We don't spin up a full ``SessionController`` -- the integration is
expensive and requires a real DB, embedder, ollama, etc. Instead the
tests bind the unbound methods (``_render_curiosity_seeds_block``
and ``_resolve_curiosity_seeds``) onto a tiny fixture object with
just the attributes those methods read.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np

from app.core.session.session_controller import SessionController


@dataclass
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32),
    )
    kind: str = "curiosity_seed"
    salience: float = 0.5
    use_count: int = 0
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "scratchpad"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "content": self.content}


class _StubMemoryStore:
    def __init__(self) -> None:
        self._items: list[_StubMemory] = []
        self.updated: list[dict[str, Any]] = []

    def iter_by_kind(self, kind: str) -> list[_StubMemory]:
        return [m for m in self._items if m.kind == kind]

    def get(self, mid: int) -> _StubMemory | None:
        for m in self._items:
            if m.id == mid:
                return m
        return None

    def update(
        self,
        mid: int,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_merge: bool = False,
        tier: str | None = None,
        **kwargs: Any,
    ) -> _StubMemory | None:
        target = self.get(mid)
        if target is None:
            return None
        if metadata is not None:
            if metadata_merge:
                target.metadata = {**target.metadata, **dict(metadata)}
            else:
                target.metadata = dict(metadata)
        if tier is not None:
            target.tier = tier
        self.updated.append({
            "id": mid,
            "metadata": dict(target.metadata),
            "tier": target.tier,
        })
        return target


class _StubEmbedder:
    def __init__(self, vec: np.ndarray | None = None) -> None:
        self._vec = vec if vec is not None else np.zeros(4, dtype=np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self._vec


def _make_fixture(
    *,
    enabled: bool = True,
    seeds: list[_StubMemory] | None = None,
    embedder: _StubEmbedder | None = None,
    resolve_threshold: float = 0.50,
) -> SimpleNamespace:
    store = _StubMemoryStore()
    if seeds:
        store._items = list(seeds)
    fixture = SimpleNamespace(
        _settings=SimpleNamespace(
            agent=SimpleNamespace(
                curiosity_seed_enabled=enabled,
                curiosity_seed_resolve_threshold=resolve_threshold,
            ),
        ),
        _memory_store=store,
        _embedder=embedder,
        _notify_memory_updated=MagicMock(),
    )
    return fixture


# ── inner-life block ────────────────────────────────────────────────


class RenderBlockTests(unittest.TestCase):
    def test_empty_when_disabled(self) -> None:
        fixture = _make_fixture(
            enabled=False,
            seeds=[
                _StubMemory(
                    id=1,
                    content="tea ritual",
                    metadata={
                        "topic": "tea ritual",
                        "prompt_text": "want a cup?",
                    },
                ),
            ],
        )
        out = SessionController._render_curiosity_seeds_block(fixture)
        self.assertEqual(out, "")

    def test_empty_when_no_active_seeds(self) -> None:
        fixture = _make_fixture(seeds=[])
        out = SessionController._render_curiosity_seeds_block(fixture)
        self.assertEqual(out, "")

    def test_renders_oldest_two_seeds(self) -> None:
        fixture = _make_fixture(
            seeds=[
                _StubMemory(
                    id=3,
                    content="z",
                    metadata={
                        "topic": "third",
                        "prompt_text": "p3",
                    },
                    created_at="2026-01-03T00:00:00+00:00",
                ),
                _StubMemory(
                    id=1,
                    content="a",
                    metadata={
                        "topic": "first",
                        "prompt_text": "p1",
                    },
                    created_at="2026-01-01T00:00:00+00:00",
                ),
                _StubMemory(
                    id=2,
                    content="b",
                    metadata={
                        "topic": "second",
                        "prompt_text": "p2",
                    },
                    created_at="2026-01-02T00:00:00+00:00",
                ),
            ],
        )
        out = SessionController._render_curiosity_seeds_block(fixture)
        self.assertIn("Quiet curiosity", out)
        self.assertIn("first", out)
        self.assertIn("second", out)
        self.assertNotIn("third", out)  # only two oldest

    def test_consumed_seeds_skipped(self) -> None:
        fixture = _make_fixture(
            seeds=[
                _StubMemory(
                    id=1,
                    content="x",
                    metadata={
                        "topic": "still active",
                        "prompt_text": "p1",
                    },
                ),
                _StubMemory(
                    id=2,
                    content="y",
                    metadata={
                        "topic": "already mentioned",
                        "prompt_text": "p2",
                        "consumed_at": "2026-01-05T00:00:00+00:00",
                    },
                    tier="archive",
                ),
            ],
        )
        out = SessionController._render_curiosity_seeds_block(fixture)
        self.assertIn("still active", out)
        self.assertNotIn("already mentioned", out)


# ── auto-resolve ────────────────────────────────────────────────────


class AutoResolveTests(unittest.TestCase):
    def test_close_match_marks_consumed(self) -> None:
        seed_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        seed = _StubMemory(
            id=1,
            content="topic about tea",
            embedding=seed_vec,
            metadata={
                "topic": "tea",
                "prompt_text": "what's your tea ritual?",
            },
        )
        # Embedder returns the same vec -> cosine 1.0 >= 0.50 -> resolve.
        fixture = _make_fixture(
            seeds=[seed], embedder=_StubEmbedder(vec=seed_vec),
        )
        SessionController._resolve_curiosity_seeds(
            fixture,
            user_text="we drank some matcha today",
            assistant_text="oh cool!",
        )
        self.assertEqual(len(fixture._memory_store.updated), 1)
        update = fixture._memory_store.updated[0]
        self.assertIn("consumed_at", update["metadata"])
        self.assertEqual(update["tier"], "archive")
        fixture._notify_memory_updated.assert_called_once()

    def test_far_match_leaves_seed_alone(self) -> None:
        seed_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        turn_vec = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        seed = _StubMemory(
            id=1,
            content="topic",
            embedding=seed_vec,
            metadata={"topic": "tea"},
        )
        fixture = _make_fixture(
            seeds=[seed], embedder=_StubEmbedder(vec=turn_vec),
        )
        SessionController._resolve_curiosity_seeds(
            fixture, user_text="hi", assistant_text="hello",
        )
        self.assertEqual(fixture._memory_store.updated, [])
        fixture._notify_memory_updated.assert_not_called()

    def test_disabled_short_circuits(self) -> None:
        seed_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        seed = _StubMemory(
            id=1, content="x", embedding=seed_vec, metadata={"topic": "t"},
        )
        fixture = _make_fixture(
            enabled=False,
            seeds=[seed],
            embedder=_StubEmbedder(vec=seed_vec),
        )
        SessionController._resolve_curiosity_seeds(
            fixture, user_text="abc", assistant_text="def",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_no_embedder_short_circuits(self) -> None:
        seed_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        seed = _StubMemory(
            id=1, content="x", embedding=seed_vec, metadata={"topic": "t"},
        )
        fixture = _make_fixture(seeds=[seed], embedder=None)
        # Should not raise even when embedder is None.
        SessionController._resolve_curiosity_seeds(
            fixture, user_text="abc", assistant_text="def",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_short_combined_text_skipped(self) -> None:
        seed_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        seed = _StubMemory(
            id=1, content="x", embedding=seed_vec, metadata={"topic": "t"},
        )
        fixture = _make_fixture(
            seeds=[seed], embedder=_StubEmbedder(vec=seed_vec),
        )
        SessionController._resolve_curiosity_seeds(
            fixture, user_text="", assistant_text="ok",
        )
        # ``ok`` -> 2 chars combined -> short-circuits the embed path.
        self.assertEqual(fixture._memory_store.updated, [])


if __name__ == "__main__":
    unittest.main()
