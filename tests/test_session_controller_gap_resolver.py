"""F2.1 personality backlog tests for the post-turn gap resolver.

Mirrors the fixture style from ``test_session_controller_curiosity_seeds.py``:
we don't spin up a full ``SessionController`` -- the integration is
expensive and requires a real DB, embedder, ollama, etc. Instead the
tests bind the unbound ``_resolve_knowledge_gaps`` method onto a tiny
fixture object with just the attributes the method reads.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np

from app.core.session_controller import SessionController


@dataclass
class _StubGap:
    id: int
    content: str
    embedding: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32),
    )
    kind: str = "knowledge_gap"
    salience: float = 0.4
    use_count: int = 0
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "long_term"
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "content": self.content}


class _StubMemoryStore:
    def __init__(self, gaps: list[_StubGap] | None = None) -> None:
        self._gaps: list[_StubGap] = list(gaps or [])
        self.updated: list[dict[str, Any]] = []

    def get(self, mid: int) -> _StubGap | None:
        for m in self._gaps:
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
    ) -> _StubGap | None:
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


class _StubGapStore:
    """Minimal :class:`KnowledgeGapStore` shim for the fixture.

    Real ``mark_resolved`` flows through ``_memory_store.update`` so the
    test fixture's update spy captures it. We replicate just that path.
    """

    def __init__(self, memory_store: _StubMemoryStore) -> None:
        self._memory_store = memory_store

    def list_open(self) -> list[_StubGap]:
        return [
            g for g in self._memory_store._gaps
            if not (g.metadata or {}).get("resolved_at")
        ]

    def mark_resolved(
        self,
        gap_id: int,
        *,
        answer_memory_id: int | None,
        resolved_by: str | None = None,
        similarity: float | None = None,
    ) -> bool:
        target = self._memory_store.get(int(gap_id))
        if target is None or target.kind != "knowledge_gap":
            return False
        meta: dict[str, Any] = {"resolved_at": "now"}
        if answer_memory_id is not None:
            meta["resolved_by_memory_id"] = int(answer_memory_id)
        if resolved_by:
            meta["resolved_by"] = str(resolved_by).strip()
        if similarity is not None:
            meta["resolved_similarity"] = round(float(similarity), 4)
        self._memory_store.update(
            int(gap_id), metadata=meta, metadata_merge=True,
        )
        return True


class _StubEmbedder:
    def __init__(self, vec: np.ndarray | None = None) -> None:
        self._vec = vec if vec is not None else np.zeros(4, dtype=np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self._vec


def _make_fixture(
    *,
    gaps: list[_StubGap] | None = None,
    embedder: _StubEmbedder | None = None,
    threshold: float = 0.50,
) -> SimpleNamespace:
    store = _StubMemoryStore(gaps=gaps)
    gap_store = _StubGapStore(store)
    fixture = SimpleNamespace(
        _settings=SimpleNamespace(
            agent=SimpleNamespace(
                gap_user_answer_resolve_threshold=threshold,
            ),
        ),
        _memory_store=store,
        _knowledge_gap_store=gap_store,
        _embedder=embedder,
        _notify_memory_updated=MagicMock(),
    )
    return fixture


class PostTurnGapResolverTests(unittest.TestCase):
    def test_user_reply_resolves_matching_gap(self) -> None:
        # Gap embedding aligned with the turn embedding -> cosine 1.0
        # (both are the same unit vector).
        gap_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        gap = _StubGap(
            id=89,
            content="music: does Jacob listen to genres while watching anime",
            embedding=gap_vec,
            metadata={"topic": "music", "resolved_at": None},
        )
        fixture = _make_fixture(
            gaps=[gap], embedder=_StubEmbedder(vec=gap_vec),
        )
        SessionController._resolve_knowledge_gaps(
            fixture,
            user_text="i listen to metal and anime soundtracks while watching anime",
            assistant_text="oh nice, metal pairs really well with battle scenes",
        )
        self.assertEqual(len(fixture._memory_store.updated), 1)
        update = fixture._memory_store.updated[0]
        self.assertEqual(update["id"], 89)
        self.assertEqual(update["metadata"].get("resolved_by"), "user_answer")
        self.assertIn("resolved_at", update["metadata"])
        # The resolver should backfill the audit similarity field.
        self.assertIn("resolved_similarity", update["metadata"])
        fixture._notify_memory_updated.assert_called_once()

    def test_unrelated_reply_leaves_gap_open(self) -> None:
        gap_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        # Orthogonal turn vector -> cosine 0.0 -> below threshold.
        turn_vec = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        gap = _StubGap(
            id=89,
            content="music: does Jacob listen to genres while watching anime",
            embedding=gap_vec,
            metadata={"topic": "music", "resolved_at": None},
        )
        fixture = _make_fixture(
            gaps=[gap], embedder=_StubEmbedder(vec=turn_vec),
        )
        SessionController._resolve_knowledge_gaps(
            fixture,
            user_text="totally unrelated topic about weather and rain",
            assistant_text="ah, sounds peaceful",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_only_matching_gaps_resolve(self) -> None:
        match_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        miss_vec = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        gap_match = _StubGap(
            id=1,
            content="music: while watching anime",
            embedding=match_vec,
            metadata={"topic": "music", "resolved_at": None},
        )
        gap_miss = _StubGap(
            id=2,
            content="weather: does Jacob like rain",
            embedding=miss_vec,
            metadata={"topic": "weather", "resolved_at": None},
        )
        fixture = _make_fixture(
            gaps=[gap_match, gap_miss],
            embedder=_StubEmbedder(vec=match_vec),
        )
        SessionController._resolve_knowledge_gaps(
            fixture,
            user_text="i listen to metal while watching anime",
            assistant_text="cool",
        )
        ids = {u["id"] for u in fixture._memory_store.updated}
        self.assertEqual(ids, {1})

    def test_already_resolved_gap_skipped(self) -> None:
        # A gap whose ``resolved_at`` is already set must not be
        # re-resolved (would clobber audit metadata).
        gap_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        gap = _StubGap(
            id=1,
            content="music: while watching anime",
            embedding=gap_vec,
            metadata={
                "topic": "music",
                "resolved_at": "2026-05-30T00:00:00+00:00",
                "resolved_by": "fact_checker",
            },
        )
        fixture = _make_fixture(
            gaps=[gap], embedder=_StubEmbedder(vec=gap_vec),
        )
        SessionController._resolve_knowledge_gaps(
            fixture,
            user_text="i listen to metal while watching anime",
            assistant_text="cool",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_no_open_gaps_returns_silently(self) -> None:
        fixture = _make_fixture(
            gaps=[],
            embedder=_StubEmbedder(),
        )
        SessionController._resolve_knowledge_gaps(
            fixture,
            user_text="i listen to metal while watching anime",
            assistant_text="cool",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_no_embedder_returns_silently(self) -> None:
        gap = _StubGap(
            id=1,
            content="music: while watching anime",
            embedding=np.array([1.0, 0.0], dtype=np.float32),
            metadata={"resolved_at": None},
        )
        fixture = _make_fixture(gaps=[gap], embedder=None)
        SessionController._resolve_knowledge_gaps(
            fixture, user_text="hi", assistant_text="hello there",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_short_combined_text_short_circuits(self) -> None:
        # Combined "hi ok" -> 5 chars; min is 4 so this would actually
        # pass. Use 3 chars total instead.
        gap = _StubGap(
            id=1,
            content="music: while watching anime",
            embedding=np.array([1.0, 0.0], dtype=np.float32),
            metadata={"resolved_at": None},
        )
        fixture = _make_fixture(
            gaps=[gap], embedder=_StubEmbedder(),
        )
        SessionController._resolve_knowledge_gaps(
            fixture, user_text="", assistant_text="hi",
        )
        self.assertEqual(fixture._memory_store.updated, [])

    def test_threshold_respected(self) -> None:
        # Both vectors orthogonal -> cosine 0.0; with threshold 0.0
        # the resolver still requires sim >= threshold so 0.0 passes
        # only if threshold is exactly 0. Use threshold 0.5.
        gap_vec = np.array([1.0, 0.0], dtype=np.float32)
        # Half-aligned: cosine ~ 0.707
        turn_vec = np.array([1.0, 1.0], dtype=np.float32)
        turn_vec /= np.linalg.norm(turn_vec)
        gap_vec_n = gap_vec / np.linalg.norm(gap_vec)
        gap = _StubGap(
            id=1,
            content="music: while watching anime",
            embedding=gap_vec_n,
            metadata={"resolved_at": None},
        )
        # First with high threshold -> no resolution.
        fixture_high = _make_fixture(
            gaps=[gap], embedder=_StubEmbedder(vec=turn_vec),
            threshold=0.95,
        )
        SessionController._resolve_knowledge_gaps(
            fixture_high,
            user_text="some content here that is non-trivial",
            assistant_text="response",
        )
        self.assertEqual(fixture_high._memory_store.updated, [])

        # Same vectors, lower threshold -> resolves.
        gap2 = _StubGap(
            id=2,
            content="music: while watching anime",
            embedding=gap_vec_n,
            metadata={"resolved_at": None},
        )
        fixture_low = _make_fixture(
            gaps=[gap2], embedder=_StubEmbedder(vec=turn_vec),
            threshold=0.50,
        )
        SessionController._resolve_knowledge_gaps(
            fixture_low,
            user_text="some content here that is non-trivial",
            assistant_text="response",
        )
        self.assertEqual(len(fixture_low._memory_store.updated), 1)


if __name__ == "__main__":
    unittest.main()
