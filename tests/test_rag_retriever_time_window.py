"""K-time2: date-anchored retrieval boost + empty-window guard.

Two surfaces on :class:`RagRetriever`:

* ``retrieve`` adds ``_RAG_TIME_WINDOW_BONUS`` to a memory/message hit
  whose ``created_at`` / ``event_time`` falls inside the relative-time
  window the query named ("yesterday", "last week", ...). A query with no
  time phrase leaves the score untouched.
* ``block_for`` appends an anti-confabulation guard note when a clearly
  retrospective query surfaced zero in-window hits, and stays silent when
  an in-window hit was found or the phrase is non-guardable ("today").
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.infra import timephrase
from app.core.rag.rag_retriever import RagRetriever, _RAG_TIME_WINDOW_BONUS
from app.core.rag.rag_store import MemoryRecord, RagHit

# Wednesday, 2026-06-17 12:00 UTC.
NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _memory_hit(*, record_id: str, content: str, base: float) -> RagHit:
    return RagHit(
        source="memory",
        score=float(base),
        record=MemoryRecord(
            id=record_id,
            content=content,
            kind="event",
            salience=0.6,
            source_session=None,
            source_message_id=None,
            created_at=_iso(NOW - timedelta(hours=48)),
            last_used_at=None,
            use_count=0,
        ),
    )


class _StubStore:
    def __init__(self, hits: list[RagHit]) -> None:
        self._hits = hits

    def search_memories(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return [RagHit(source=h.source, score=h.score, record=h.record) for h in self._hits]

    def search_messages(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []


class _StubEmbedder:
    def embed(self, _text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


class _StubMemoryStore:
    """Minimal join surface returning a row with the given dates."""

    def __init__(self, *, created_at: str, event_time: str | None = None) -> None:
        self._created_at = created_at
        self._event_time = event_time

    def get(self, _id: int) -> SimpleNamespace:
        return SimpleNamespace(
            kind="event",
            pinned=False,
            tier="long_term",
            confidence=0.7,
            salience=0.6,
            embedding=None,
            metadata=None,
            temporal_type="durable",
            created_at=self._created_at,
            event_time=self._event_time,
            relevance_until=None,
        )

    def mark_used(self, _ids: Any) -> None:
        pass


def _build(*, created_at: str, event_time: str | None = None) -> RagRetriever:
    hit = _memory_hit(record_id="100", content="we shipped the dashboard.", base=0.5)
    return RagRetriever(
        _StubStore([hit]),  # type: ignore[arg-type]
        _StubEmbedder(),  # type: ignore[arg-type]
        top_k=5,
        score_threshold=-5.0,
        include_messages=False,
        include_documents=False,
        memory_store=_StubMemoryStore(created_at=created_at, event_time=event_time),  # type: ignore[arg-type]
    )


class TimeWindowBoostTests(unittest.TestCase):
    def setUp(self) -> None:
        timephrase.set_now_provider(lambda: NOW)

    def tearDown(self) -> None:
        timephrase.set_now_provider(None)

    def test_created_at_in_window_is_boosted(self) -> None:
        created = _iso(NOW - timedelta(days=1, hours=-2))  # yesterday 14:00
        anchored = _build(created_at=created).retrieve("what did I tell you yesterday?")
        plain = _build(created_at=created).retrieve("tell me about the dashboard")
        self.assertAlmostEqual(
            anchored[0].score - plain[0].score, _RAG_TIME_WINDOW_BONUS, places=4,
        )

    def test_created_at_outside_window_not_boosted(self) -> None:
        # Recorded today, but the query asks about yesterday -> no boost.
        created = _iso(NOW - timedelta(hours=2))
        anchored = _build(created_at=created).retrieve("what did I tell you yesterday?")
        plain = _build(created_at=created).retrieve("tell me about the dashboard")
        self.assertAlmostEqual(anchored[0].score - plain[0].score, 0.0, places=4)

    def test_event_time_in_window_is_boosted(self) -> None:
        # created_at today but event_time anchored to yesterday -> boosted.
        anchored = _build(
            created_at=_iso(NOW - timedelta(hours=2)),
            event_time=_iso(NOW - timedelta(days=1)),
        ).retrieve("what happened yesterday?")
        plain = _build(
            created_at=_iso(NOW - timedelta(hours=2)),
            event_time=_iso(NOW - timedelta(days=1)),
        ).retrieve("what happened with the dashboard?")
        self.assertAlmostEqual(
            anchored[0].score - plain[0].score, _RAG_TIME_WINDOW_BONUS, places=4,
        )


class TimeWindowGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        timephrase.set_now_provider(lambda: NOW)

    def tearDown(self) -> None:
        timephrase.set_now_provider(None)

    def test_guard_fires_on_empty_retrospective_window(self) -> None:
        # Query names "yesterday", the only hit was recorded today.
        r = _build(created_at=_iso(NOW - timedelta(hours=2)))
        block = r.block_for("what did I tell you yesterday?")
        self.assertIn("yesterday", block)
        self.assertIn("instead of guessing", block)

    def test_no_guard_when_in_window_hit_found(self) -> None:
        r = _build(created_at=_iso(NOW - timedelta(days=1)))
        block = r.block_for("what did I tell you yesterday?")
        self.assertNotIn("instead of guessing", block)

    def test_no_guard_for_non_guardable_today(self) -> None:
        r = _build(created_at=_iso(NOW - timedelta(days=5)))
        block = r.block_for("how are you today?")
        self.assertNotIn("instead of guessing", block)

    def test_no_guard_without_time_phrase(self) -> None:
        r = _build(created_at=_iso(NOW - timedelta(days=5)))
        block = r.block_for("tell me about the dashboard")
        self.assertIsNone(r.time_window_guard_note())


if __name__ == "__main__":
    unittest.main()
