"""Tests for the recency / revival scoring layered onto memory hits in
:class:`app.core.rag_retriever.RagRetriever` (Phase A3 of the personality
depth pass).

These tests stub the ``RagStore`` so we can dictate exactly what hits the
retriever sees and assert how the recency-aware adjustments reorder them.
The full LanceDB integration is covered separately in
``tests/test_rag_store.py`` -- here we focus on the pure-Python scoring
math + the ``mark_used`` side effect.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from app.core.rag_retriever import (
    RagRetriever,
    _MEMORY_PRIOR,
    _MEMORY_RECENCY_PENALTY,
    _MEMORY_RECENCY_PENALTY_HOURS,
    _MEMORY_REVIVAL_BONUS,
    _MEMORY_REVIVAL_DAYS,
    _memory_recency_adjust,
)
from app.core.rag_store import (
    DocumentChunk,
    MemoryRecord,
    MessageRecord,
    RagHit,
)


def _iso_hours_ago(hours: float) -> str:
    """ISO-8601 UTC timestamp ``hours`` ago. Helper for controllable
    ``last_used_at`` / ``created_at`` fixtures.
    """
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


def _memory_record(
    *,
    record_id: str,
    content: str,
    last_used_at: str | None,
    use_count: int = 0,
    kind: str = "fact",
    salience: float = 0.5,
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        content=content,
        kind=kind,
        salience=salience,
        source_session=None,
        source_message_id=None,
        created_at=_iso_hours_ago(48),  # arbitrary; not under test
        last_used_at=last_used_at,
        use_count=use_count,
    )


def _memory_hit(
    *,
    record_id: str,
    content: str,
    base_score: float,
    last_used_at: str | None,
    use_count: int = 0,
    salience: float = 0.5,
) -> RagHit:
    """Build a memory-source ``RagHit`` with the cosine score the
    LanceDB layer would have produced (already including the salience
    boost the store adds internally). The retriever then layers
    ``_MEMORY_PRIOR`` and the recency adjustment on top.
    """
    return RagHit(
        source="memory",
        score=float(base_score),
        record=_memory_record(
            record_id=record_id,
            content=content,
            last_used_at=last_used_at,
            use_count=use_count,
            salience=salience,
        ),
    )


class _StubStore:
    """Stand-in for :class:`RagStore` that returns canned hits.

    Mirrors the three search methods the retriever calls. Each returned
    list is a deep copy so the retriever can mutate ``hit.score`` in
    place without polluting future test assertions.
    """

    def __init__(
        self,
        *,
        memories: list[RagHit] | None = None,
        messages: list[RagHit] | None = None,
        documents: list[RagHit] | None = None,
    ) -> None:
        self._memories = list(memories or [])
        self._messages = list(messages or [])
        self._documents = list(documents or [])

    def search_memories(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._memories
        ]

    def search_messages(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._messages
        ]

    def search_documents(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._documents
        ]


class _StubEmbedder:
    """Minimal embedder that returns a deterministic unit vector."""

    def embed(self, _text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


class _RecordingMemoryStore:
    """Captures ``mark_used`` calls so tests can assert the exact id list."""

    def __init__(self) -> None:
        self.mark_used_calls: list[list[int]] = []

    def mark_used(self, ids):
        self.mark_used_calls.append([int(i) for i in ids])


class _ConfidenceJoinMemoryStore:
    """Memory store stub that returns canned confidence per id.

    Mirrors the duck-typed surface ``RagRetriever`` calls during the
    join: ``get(id)`` returning an object with ``pinned``, ``tier``,
    ``metadata``, ``kind``, and ``confidence`` attributes, plus the
    obligatory ``mark_used``.
    """

    def __init__(self, confidences: dict[int, float]) -> None:
        self._confidences = {int(k): float(v) for k, v in confidences.items()}
        self.mark_used_calls: list[list[int]] = []

    def get(self, memory_id: int):  # type: ignore[no-untyped-def]
        if int(memory_id) not in self._confidences:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            id=int(memory_id),
            pinned=False,
            tier="long_term",
            metadata={},
            kind="fact",
            confidence=self._confidences[int(memory_id)],
        )

    def mark_used(self, ids):  # type: ignore[no-untyped-def]
        self.mark_used_calls.append([int(i) for i in ids])


class MemoryRecencyAdjustTests(unittest.TestCase):
    """Pure-function tests on the helper. No retriever involved."""

    def test_never_used_memory_unchanged(self) -> None:
        self.assertEqual(
            _memory_recency_adjust(last_used_at=None, use_count=0),
            0.0,
        )

    def test_used_within_penalty_window_returns_negative(self) -> None:
        delta = _memory_recency_adjust(
            last_used_at=_iso_hours_ago(1.0),
            use_count=2,
        )
        self.assertEqual(delta, -_MEMORY_RECENCY_PENALTY)

    def test_used_just_over_penalty_window_returns_zero(self) -> None:
        # Past the penalty window but not yet stale enough to revive.
        hours = _MEMORY_RECENCY_PENALTY_HOURS + 1.0
        delta = _memory_recency_adjust(
            last_used_at=_iso_hours_ago(hours),
            use_count=1,
        )
        self.assertEqual(delta, 0.0)

    def test_revival_bonus_for_old_used_memory(self) -> None:
        delta = _memory_recency_adjust(
            last_used_at=_iso_hours_ago(_MEMORY_REVIVAL_DAYS * 24 + 24),
            use_count=2,
        )
        self.assertEqual(delta, _MEMORY_REVIVAL_BONUS)

    def test_revival_bonus_requires_prior_use(self) -> None:
        # Old timestamp but use_count == 0 means it was never *actually*
        # surfaced; treat as fresh discovery, no revival bump.
        delta = _memory_recency_adjust(
            last_used_at=_iso_hours_ago(_MEMORY_REVIVAL_DAYS * 24 + 48),
            use_count=0,
        )
        self.assertEqual(delta, 0.0)

    def test_unparseable_timestamp_treated_as_never_used(self) -> None:
        self.assertEqual(
            _memory_recency_adjust(last_used_at="not a date", use_count=3),
            0.0,
        )

    def test_future_timestamp_clamped(self) -> None:
        # Clock skew shouldn't accidentally trigger a revival.
        future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()
        self.assertEqual(
            _memory_recency_adjust(last_used_at=future, use_count=5),
            -_MEMORY_RECENCY_PENALTY,
        )


class RetrieverScoringTests(unittest.TestCase):
    """End-to-end tests against a stubbed store. Asserts the retriever
    applies the prior + recency math and reorders results accordingly.
    """

    def test_recently_used_memory_is_penalised_below_unused(self) -> None:
        # Two memories with identical cosine scores. The recency
        # penalty must hand the win to the never-used one.
        recent = _memory_hit(
            record_id="42",
            content="recently surfaced thought",
            base_score=0.80,
            last_used_at=_iso_hours_ago(1.0),
            use_count=3,
        )
        unused = _memory_hit(
            record_id="43",
            content="fresh discovery",
            base_score=0.80,
            last_used_at=None,
            use_count=0,
        )
        retriever = RagRetriever(
            _StubStore(memories=[recent, unused]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
        )
        hits = retriever.retrieve("anything")
        self.assertEqual(hits[0].record.content, "fresh discovery")
        self.assertEqual(hits[1].record.content, "recently surfaced thought")
        # Scores: unused = base + prior; recent = base + prior - penalty.
        self.assertAlmostEqual(hits[0].score, 0.80 + _MEMORY_PRIOR, places=4)
        self.assertAlmostEqual(
            hits[1].score,
            0.80 + _MEMORY_PRIOR - _MEMORY_RECENCY_PENALTY,
            places=4,
        )

    def test_old_used_memory_gets_revival_bonus(self) -> None:
        # Two memories: stale-but-used vs identically-similar fresh
        # discovery. The revival bump should tip the scale toward the
        # stale one so dormant threads can re-emerge.
        revived = _memory_hit(
            record_id="44",
            content="that fish-cookie thing from weeks ago",
            base_score=0.70,
            last_used_at=_iso_hours_ago(_MEMORY_REVIVAL_DAYS * 24 + 48),
            use_count=2,
        )
        unused = _memory_hit(
            record_id="45",
            content="fresh-but-equal-cosine memory",
            base_score=0.70,
            last_used_at=None,
            use_count=0,
        )
        retriever = RagRetriever(
            _StubStore(memories=[revived, unused]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
        )
        hits = retriever.retrieve("anything")
        self.assertEqual(
            hits[0].record.content,
            "that fish-cookie thing from weeks ago",
        )
        self.assertAlmostEqual(
            hits[0].score,
            0.70 + _MEMORY_PRIOR + _MEMORY_REVIVAL_BONUS,
            places=4,
        )

    def test_mark_used_called_with_returned_memory_ids_only(self) -> None:
        memory = _memory_hit(
            record_id="100",
            content="memory hit",
            base_score=0.80,
            last_used_at=None,
        )
        # Document hit must NOT be passed to mark_used; only memories.
        doc_hit = RagHit(
            source="document",
            score=0.75,
            record=DocumentChunk(
                id="d1",
                document_id="doc",
                title="notes.md",
                chunk_index=0,
                content="some doc text",
                created_at=_iso_hours_ago(2),
            ),
        )
        # Message hit also must NOT be passed.
        msg_hit = RagHit(
            source="message",
            score=0.78,
            record=MessageRecord(
                id="m1",
                session_id="s1",
                message_id=1,
                role="user",
                content="something jacob said",
                created_at=_iso_hours_ago(2),
            ),
        )
        memstore = _RecordingMemoryStore()
        retriever = RagRetriever(
            _StubStore(  # type: ignore[arg-type]
                memories=[memory],
                messages=[msg_hit],
                documents=[doc_hit],
            ),
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            memory_store=memstore,  # type: ignore[arg-type]
        )
        hits = retriever.retrieve("anything")
        self.assertGreaterEqual(len(hits), 1)
        # mark_used invoked exactly once, with only the memory id.
        self.assertEqual(memstore.mark_used_calls, [[100]])

    def test_mark_used_skipped_when_no_memory_hits(self) -> None:
        # Document-only retrieval shouldn't trigger an empty mark_used
        # call — defensive against future regressions where the helper
        # might do work even with []. Belt-and-braces: the recording
        # store must show a single entry with [] only if the retriever
        # actively decided to call it; we want zero calls instead.
        doc_hit = RagHit(
            source="document",
            score=0.75,
            record=DocumentChunk(
                id="d1",
                document_id="doc",
                title="",
                chunk_index=0,
                content="doc text",
                created_at=_iso_hours_ago(2),
            ),
        )
        memstore = _RecordingMemoryStore()
        retriever = RagRetriever(
            _StubStore(documents=[doc_hit]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            memory_store=memstore,  # type: ignore[arg-type]
        )
        retriever.retrieve("anything")
        self.assertEqual(memstore.mark_used_calls, [])

    def test_mark_used_failure_does_not_break_retrieval(self) -> None:
        """A broken memory store must not abort the prompt build —
        we still want the prompt assembled so the LLM can answer.
        """
        class _ExplodingStore:
            def mark_used(self, _ids):
                raise RuntimeError("disk full")

        memory = _memory_hit(
            record_id="200",
            content="thing",
            base_score=0.8,
            last_used_at=None,
        )
        retriever = RagRetriever(
            _StubStore(memories=[memory]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            memory_store=_ExplodingStore(),  # type: ignore[arg-type]
        )
        hits = retriever.retrieve("anything")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].record.content, "thing")

    def test_no_memory_store_skips_mark_used(self) -> None:
        """When the retriever was instantiated without a memory_store,
        retrieval still works — we just don't bump ``last_used_at``.
        Lean deployments and tests use this path.
        """
        memory = _memory_hit(
            record_id="300",
            content="thing",
            base_score=0.8,
            last_used_at=None,
        )
        retriever = RagRetriever(
            _StubStore(memories=[memory]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            memory_store=None,
        )
        hits = retriever.retrieve("anything")
        self.assertEqual(len(hits), 1)

    def test_messages_and_documents_unaffected_by_recency_adjust(self) -> None:
        """The recency penalty/revival math is memory-only — message
        and document hits go through the existing paths untouched.
        """
        # Use a single memory hit + one message + one document. The
        # message hit has last_used_at-shaped fields absent because
        # MessageRecord doesn't carry them; if the retriever
        # accidentally tried to apply the memory adjustment to it, the
        # test would either crash or score it differently.
        msg_hit = RagHit(
            source="message",
            score=0.90,  # outscore the memory after adjustments
            record=MessageRecord(
                id="m1",
                session_id="s1",
                message_id=1,
                role="user",
                content="message text",
                created_at=_iso_hours_ago(1),
            ),
        )
        memory = _memory_hit(
            record_id="400",
            content="memory",
            base_score=0.50,
            last_used_at=None,
        )
        retriever = RagRetriever(
            _StubStore(memories=[memory], messages=[msg_hit]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_documents=False,
        )
        hits = retriever.retrieve("anything")
        # Both surface; message wins on raw cosine after its own prior.
        self.assertEqual({h.source for h in hits}, {"message", "memory"})


class ConfidencePenaltyTests(unittest.TestCase):
    """Schema v9: low-confidence memories get demoted at merge time.

    Joined from the SQLite mirror inside ``retrieve()`` — the LanceDB
    record alone does not carry confidence. This stub exercises the
    join + penalty arithmetic end-to-end.
    """

    def test_low_confidence_ranks_below_high_confidence_at_same_base(self) -> None:
        high = _memory_hit(
            record_id="700",
            content="high confidence fact",
            base_score=0.70,
            last_used_at=None,
        )
        low = _memory_hit(
            record_id="701",
            content="low confidence fact",
            base_score=0.70,
            last_used_at=None,
        )
        memstore = _ConfidenceJoinMemoryStore({700: 0.95, 701: 0.1})
        retriever = RagRetriever(
            _StubStore(memories=[high, low]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
            memory_store=memstore,  # type: ignore[arg-type]
        )
        hits = retriever.retrieve("anything")
        self.assertEqual(hits[0].record.content, "high confidence fact")
        self.assertEqual(hits[1].record.content, "low confidence fact")
        # Penalty for confidence=0.1: (0.5 - 0.1) / 0.5 * 0.15 = 0.12
        self.assertAlmostEqual(
            hits[1].score, hits[0].score - 0.12, places=4
        )

    def test_confidence_is_stamped_on_hit(self) -> None:
        memory = _memory_hit(
            record_id="800",
            content="something",
            base_score=0.70,
            last_used_at=None,
        )
        memstore = _ConfidenceJoinMemoryStore({800: 0.4})
        retriever = RagRetriever(
            _StubStore(memories=[memory]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
            memory_store=memstore,  # type: ignore[arg-type]
        )
        hits = retriever.retrieve("anything")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].confidence, 0.4)


class FormatBlockUncertaintySuffixTests(unittest.TestCase):
    """``RagRetriever.format_block`` appends "(uncertain)" to lines whose
    hit's ``confidence`` is below 0.5. Pure render-layer test.
    """

    def test_low_confidence_line_gets_suffix(self) -> None:
        hit = RagHit(
            source="memory",
            score=0.6,
            record=_memory_record(
                record_id="9",
                content="something Aiko isn't sure about",
                last_used_at=None,
            ),
            confidence=0.3,
        )
        block = RagRetriever.format_block([hit], user_display_name="Friend")
        self.assertIn("(uncertain)", block)

    def test_high_confidence_line_unchanged(self) -> None:
        hit = RagHit(
            source="memory",
            score=0.6,
            record=_memory_record(
                record_id="10",
                content="solid known fact",
                last_used_at=None,
            ),
            confidence=0.9,
        )
        block = RagRetriever.format_block([hit], user_display_name="Friend")
        self.assertNotIn("(uncertain)", block)

    def test_missing_confidence_treated_as_high(self) -> None:
        # Defensive — non-memory hits or unresolved joins leave the
        # confidence ``None``; format_block must not crash and must not
        # append the suffix.
        hit = RagHit(
            source="memory",
            score=0.6,
            record=_memory_record(
                record_id="11",
                content="unjoined memory",
                last_used_at=None,
            ),
            confidence=None,
        )
        block = RagRetriever.format_block([hit], user_display_name="Friend")
        self.assertNotIn("(uncertain)", block)


if __name__ == "__main__":
    unittest.main()
