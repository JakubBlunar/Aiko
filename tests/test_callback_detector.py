"""Tests for the K22 callback / inside-joke detector.

Covers both halves of the module:

  - ``detect()`` — cosine walk over an in-memory mirror with
    allow-list, age floor, cooldown, threshold, and top-K caps.
  - ``record()`` — the metadata + salience/revival bumps applied
    when hits land, including the ``notify_memory_updated``
    callback contract.

Uses fake ``Memory`` objects + a tiny fake ``MemoryStore`` so the
tests stay pure-Python (no LanceDB, no SQLite). Embeddings are
manually-orthogonal unit vectors so the cosines come out at
known values.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from app.core.conversation import callback_detector
from app.core.conversation.callback_detector import (
    CALLBACK_KINDS,
    CallbackHit,
    detect,
    record,
)


# ── Fake memory + store ─────────────────────────────────────────────


@dataclass
class _FakeMemory:
    id: int
    content: str
    kind: str
    embedding: np.ndarray
    created_at: str
    salience: float = 0.5
    revival_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "salience": self.salience,
            "revival_score": self.revival_score,
            "metadata": dict(self.metadata),
        }


class _FakeStore:
    """Minimal duck-typed ``MemoryStore`` for unit tests."""

    def __init__(self, mems: list[_FakeMemory]) -> None:
        self._mems: dict[int, _FakeMemory] = {m.id: m for m in mems}
        self.update_calls: list[tuple[int, dict[str, Any]]] = []
        self.list_recent_calls = 0
        self.iter_by_kinds_calls = 0

    def list_recent(self, *, limit: int = 10_000) -> list[_FakeMemory]:
        self.list_recent_calls += 1
        return list(self._mems.values())

    def iter_by_kinds(self, kinds: Any) -> list[_FakeMemory]:
        # P17: mirror MemoryStore.iter_by_kinds — single filtered walk.
        self.iter_by_kinds_calls += 1
        kind_set = {
            k.strip().lower() for k in kinds if k and str(k).strip()
        }
        if not kind_set:
            return []
        return [
            m for m in self._mems.values()
            if (m.kind or "").lower() in kind_set
        ]

    def get(self, memory_id: int) -> _FakeMemory | None:
        return self._mems.get(int(memory_id))

    def update(
        self,
        memory_id: int,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_merge: bool = False,
        salience: float | None = None,
        revival_score: float | None = None,
    ) -> _FakeMemory | None:
        # Mirror ``MemoryStore.update``'s clamps + merge semantics so
        # tests assert on the same shape production code does.
        mem = self._mems.get(int(memory_id))
        if mem is None:
            return None
        self.update_calls.append(
            (int(memory_id), {
                "metadata": metadata,
                "metadata_merge": metadata_merge,
                "salience": salience,
                "revival_score": revival_score,
            }),
        )
        if metadata is not None:
            if metadata_merge:
                mem.metadata = {**mem.metadata, **metadata}
            else:
                mem.metadata = dict(metadata)
        if salience is not None:
            mem.salience = max(0.0, min(1.0, float(salience)))
        if revival_score is not None:
            mem.revival_score = max(0.0, min(1.0, float(revival_score)))
        return mem


# ── Helpers ─────────────────────────────────────────────────────────


def _iso_days_ago(days: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


def _iso_hours_ago(hours: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


def _unit(*components: float) -> np.ndarray:
    arr = np.asarray(components, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return arr
    return arr / norm


# Three orthogonal unit basis vectors so the cosines are 1.0 / 0.0
# without floating-point noise. Plus a 0.6-vs-0.8 mix used for the
# similarity-ordering test.
VEC_A = _unit(1.0, 0.0, 0.0)
VEC_B = _unit(0.0, 1.0, 0.0)
VEC_C = _unit(0.0, 0.0, 1.0)
VEC_AB_HIGH = _unit(0.95, 0.20, 0.0)  # ~0.95 cosine with A
VEC_AB_MID = _unit(0.60, 0.80, 0.0)   # ~0.60 cosine with A
VEC_AB_LOW = _unit(0.30, 0.95, 0.05)  # ~0.30 cosine with A


# ── detect() tests ──────────────────────────────────────────────────


class DetectTests(unittest.TestCase):

    def test_returns_hits_above_threshold_sorted_by_similarity(self) -> None:
        # Two eligible long-old fact memories with different cosines
        # vs the assistant vector. Higher should land first.
        mems = [
            _FakeMemory(
                id=1, content="midly similar", kind="fact",
                embedding=VEC_AB_MID,
                created_at=_iso_days_ago(30),
            ),
            _FakeMemory(
                id=2, content="very similar", kind="fact",
                embedding=VEC_AB_HIGH,
                created_at=_iso_days_ago(30),
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual([h.memory_id for h in hits], [2, 1])
        self.assertGreater(hits[0].similarity, hits[1].similarity)
        self.assertGreaterEqual(hits[1].similarity, 0.55)

    def test_skips_memories_younger_than_age_floor(self) -> None:
        # A 1-day-old memory isn't a callback; it's just normal context
        # from the same recent thread. Must be filtered out even with
        # perfect cosine similarity.
        mems = [
            _FakeMemory(
                id=10, content="yesterday's beat", kind="fact",
                embedding=VEC_A,
                created_at=_iso_days_ago(1),
            ),
            _FakeMemory(
                id=11, content="last month's beat", kind="fact",
                embedding=VEC_AB_HIGH,
                created_at=_iso_days_ago(45),
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual([h.memory_id for h in hits], [11])

    def test_skips_disallowed_kinds(self) -> None:
        # curiosity_seed, knowledge_gap, agenda, promise, goal,
        # goal_progress, milestone, open_question — all explicitly
        # excluded even with perfect cosine + old age.
        disallowed = [
            "curiosity_seed", "knowledge_gap", "agenda", "promise",
            "goal", "goal_progress", "milestone", "open_question",
        ]
        mems = [
            _FakeMemory(
                id=100 + idx,
                content=f"old {kind}",
                kind=kind,
                embedding=VEC_A,
                created_at=_iso_days_ago(60),
            )
            for idx, kind in enumerate(disallowed)
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=10,
        )
        self.assertEqual(hits, [])

    def test_allowed_kinds_match_constant(self) -> None:
        # Anchor the public allow-list shape so a future kind addition
        # to MemoryStore doesn't silently change K22 eligibility.
        self.assertEqual(
            CALLBACK_KINDS,
            frozenset({
                "fact", "preference", "event", "relationship",
                "self", "self_tagged",
                "shared_moment", "catchphrase",
            }),
        )

    def test_respects_cooldown(self) -> None:
        # A memory that was already called back 6 hours ago must not
        # re-fire under a 24h cooldown. A memory called back 30h ago
        # is fair game again.
        recent_meta = {"last_callback_at": _iso_hours_ago(6)}
        old_meta = {"last_callback_at": _iso_hours_ago(30)}
        mems = [
            _FakeMemory(
                id=20, content="recently called back", kind="fact",
                embedding=VEC_A,
                created_at=_iso_days_ago(60),
                metadata=recent_meta,
            ),
            _FakeMemory(
                id=21, content="cooled-down callback", kind="fact",
                embedding=VEC_A,
                created_at=_iso_days_ago(60),
                metadata=old_meta,
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual([h.memory_id for h in hits], [21])

    def test_top_k_caps_hits(self) -> None:
        # Five eligible high-similarity memories; top_k=2 should
        # return only the two most similar (here the order between
        # equally-similar siblings is implementation-defined but the
        # *count* is fixed).
        mems = [
            _FakeMemory(
                id=30 + idx, content=f"clone {idx}", kind="fact",
                embedding=VEC_AB_HIGH,
                created_at=_iso_days_ago(30 + idx),
            )
            for idx in range(5)
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=2,
        )
        self.assertEqual(len(hits), 2)

    def test_below_threshold_returns_empty(self) -> None:
        mems = [
            _FakeMemory(
                id=40, content="low sim", kind="fact",
                embedding=VEC_AB_LOW,
                created_at=_iso_days_ago(60),
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual(hits, [])

    def test_missing_embedding_is_skipped(self) -> None:
        # A row with an empty embedding (e.g. legacy / failed
        # embedder) must not raise; just gets dropped.
        mems = [
            _FakeMemory(
                id=50, content="no embedding", kind="fact",
                embedding=np.array([], dtype=np.float32),
                created_at=_iso_days_ago(30),
            ),
            _FakeMemory(
                id=51, content="good embedding", kind="fact",
                embedding=VEC_AB_HIGH,
                created_at=_iso_days_ago(30),
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual([h.memory_id for h in hits], [51])

    def test_prior_count_carried_through(self) -> None:
        # Existing callback_count is surfaced on the hit so record()
        # can compute the increment without re-reading the store.
        mems = [
            _FakeMemory(
                id=60, content="has prior history", kind="shared_moment",
                embedding=VEC_AB_HIGH,
                created_at=_iso_days_ago(60),
                metadata={"callback_count": 4},
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].prior_count, 4)

    def test_p17_prefers_iter_by_kinds_over_full_mirror(self) -> None:
        # P17: when the store exposes iter_by_kinds, detect must use it
        # (single filtered walk) and never fall back to the full-mirror
        # list_recent copy + double sort.
        mems = [
            _FakeMemory(
                id=70, content="old fact", kind="fact",
                embedding=VEC_AB_HIGH,
                created_at=_iso_days_ago(40),
            ),
            _FakeMemory(
                id=71, content="ineligible bulk row", kind="knowledge_gap",
                embedding=VEC_A,
                created_at=_iso_days_ago(40),
            ),
        ]
        store = _FakeStore(mems)
        hits = detect(
            assistant_vec=VEC_A,
            memory_store=store,
            threshold=0.55,
            age_floor_days=3,
            cooldown_hours=24,
            top_k=3,
        )
        self.assertEqual([h.memory_id for h in hits], [70])
        self.assertEqual(store.iter_by_kinds_calls, 1)
        self.assertEqual(store.list_recent_calls, 0)


# ── record() tests ──────────────────────────────────────────────────


class RecordTests(unittest.TestCase):

    def _make_hit(
        self,
        *,
        memory_id: int = 1,
        prior_count: int = 0,
        similarity: float = 0.7,
    ) -> CallbackHit:
        return CallbackHit(
            memory_id=memory_id,
            kind="fact",
            similarity=similarity,
            age_days=42,
            prior_count=prior_count,
        )

    def test_increments_callback_count_and_stamps_last_callback_at(
        self,
    ) -> None:
        mems = [
            _FakeMemory(
                id=1, content="x", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
            ),
        ]
        store = _FakeStore(mems)
        mutated = record(
            memory_store=store,
            hits=[self._make_hit(memory_id=1, prior_count=0)],
            salience_bump=0.05,
            revival_bump=0.10,
            now=datetime(2026, 5, 30, 19, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(mutated, 1)
        self.assertEqual(mems[0].metadata["callback_count"], 1)
        self.assertIn("last_callback_at", mems[0].metadata)
        self.assertIn("last_callback_similarity", mems[0].metadata)

    def test_increment_preserves_prior_count(self) -> None:
        # A row with prior_count=4 lands at 5, not 1. The detector
        # passes the prior count through so record() can do the
        # increment without re-reading.
        mems = [
            _FakeMemory(
                id=2, content="x", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
                metadata={"callback_count": 4, "vibe": "playful"},
            ),
        ]
        store = _FakeStore(mems)
        record(
            memory_store=store,
            hits=[self._make_hit(memory_id=2, prior_count=4)],
            salience_bump=0.05,
            revival_bump=0.10,
        )
        self.assertEqual(mems[0].metadata["callback_count"], 5)
        # Merge semantics preserve the pre-existing metadata fields.
        self.assertEqual(mems[0].metadata["vibe"], "playful")

    def test_bumps_salience_and_revival_score_with_clamps(self) -> None:
        # Row already at salience=0.98 should land at 1.0 (clamped),
        # not 1.03. Revival at 0.95 + 0.10 should clamp at 1.0.
        mems = [
            _FakeMemory(
                id=3, content="x", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
                salience=0.98,
                revival_score=0.95,
            ),
        ]
        store = _FakeStore(mems)
        record(
            memory_store=store,
            hits=[self._make_hit(memory_id=3)],
            salience_bump=0.05,
            revival_bump=0.10,
        )
        self.assertAlmostEqual(mems[0].salience, 1.0, places=4)
        self.assertAlmostEqual(mems[0].revival_score, 1.0, places=4)

    def test_calls_notify_memory_updated_for_each_hit(self) -> None:
        mems = [
            _FakeMemory(
                id=4, content="a", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
            ),
            _FakeMemory(
                id=5, content="b", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
            ),
        ]
        store = _FakeStore(mems)
        notifications: list[dict[str, Any]] = []

        def _notify(snapshot: dict[str, Any]) -> None:
            notifications.append(snapshot)

        record(
            memory_store=store,
            hits=[
                self._make_hit(memory_id=4),
                self._make_hit(memory_id=5),
            ],
            salience_bump=0.05,
            revival_bump=0.10,
            notify_memory_updated=_notify,
        )
        self.assertEqual(len(notifications), 2)
        self.assertEqual({n["id"] for n in notifications}, {4, 5})

    def test_skips_empty_hits_list(self) -> None:
        store = _FakeStore([])
        mutated = record(
            memory_store=store,
            hits=[],
            salience_bump=0.05,
            revival_bump=0.10,
        )
        self.assertEqual(mutated, 0)
        self.assertEqual(store.update_calls, [])

    def test_zero_bumps_still_increments_count(self) -> None:
        # The count is the read-side signal -- it must increment even
        # when salience and revival bumps are off.
        mems = [
            _FakeMemory(
                id=6, content="x", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
                salience=0.5,
                revival_score=0.0,
            ),
        ]
        store = _FakeStore(mems)
        record(
            memory_store=store,
            hits=[self._make_hit(memory_id=6, prior_count=2)],
            salience_bump=0.0,
            revival_bump=0.0,
        )
        self.assertEqual(mems[0].metadata["callback_count"], 3)

    def test_notify_failure_does_not_break_loop(self) -> None:
        # A raising notify callback must not skip subsequent hits.
        mems = [
            _FakeMemory(
                id=7, content="a", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
            ),
            _FakeMemory(
                id=8, content="b", kind="fact",
                embedding=VEC_A, created_at=_iso_days_ago(30),
            ),
        ]
        store = _FakeStore(mems)

        def _broken_notify(snapshot: dict[str, Any]) -> None:
            raise RuntimeError("frontend down")

        mutated = record(
            memory_store=store,
            hits=[
                self._make_hit(memory_id=7),
                self._make_hit(memory_id=8),
            ],
            salience_bump=0.05,
            revival_bump=0.10,
            notify_memory_updated=_broken_notify,
        )
        self.assertEqual(mutated, 2)


if __name__ == "__main__":
    unittest.main()
