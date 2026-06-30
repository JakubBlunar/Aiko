"""H4 RAG retriever: document-recall recency boost.

A document chunk uploaded within the last ``_DOCUMENT_RECENCY_DAYS`` days
gets a flat ``+_DOCUMENT_RECENCY_BONUS`` so freshly-added notes/PDFs
surface preferentially; older chunks and unparseable timestamps get no
bonus (and never a penalty).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from app.core.rag.rag_retriever import (
    RagRetriever,
    _DOCUMENT_RECENCY_BONUS,
    _DOCUMENT_RECENCY_DAYS,
    _document_recency_bonus,
)
from app.core.rag.rag_store import DocumentChunk, RagHit


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _doc_hit(*, chunk_id: str, content: str, base: float, created_at: str) -> RagHit:
    return RagHit(
        source="document",
        score=float(base),
        record=DocumentChunk(
            id=chunk_id,
            document_id="doc-" + chunk_id,
            title="notes",
            chunk_index=0,
            content=content,
            created_at=created_at,
        ),
    )


class _StubStore:
    def __init__(self, doc_hits: list[RagHit]) -> None:
        self._doc_hits = doc_hits

    def search_memories(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_messages(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._doc_hits
        ]


class _StubEmbedder:
    def embed(self, _text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


def _build(doc_hits: list[RagHit]) -> RagRetriever:
    return RagRetriever(
        _StubStore(doc_hits),  # type: ignore[arg-type]
        _StubEmbedder(),  # type: ignore[arg-type]
        top_k=10,
        score_threshold=0.0,
        include_messages=False,
        include_documents=True,
    )


class DocumentRecencyHelperTests(unittest.TestCase):
    def test_recent_within_window_gets_bonus(self) -> None:
        self.assertAlmostEqual(
            _document_recency_bonus(_iso_days_ago(1)), _DOCUMENT_RECENCY_BONUS,
        )

    def test_edge_of_window_gets_bonus(self) -> None:
        self.assertAlmostEqual(
            _document_recency_bonus(_iso_days_ago(_DOCUMENT_RECENCY_DAYS - 0.1)),
            _DOCUMENT_RECENCY_BONUS,
        )

    def test_old_document_no_bonus(self) -> None:
        self.assertEqual(_document_recency_bonus(_iso_days_ago(30)), 0.0)

    def test_missing_or_garbage_timestamp_no_bonus(self) -> None:
        self.assertEqual(_document_recency_bonus(""), 0.0)
        self.assertEqual(_document_recency_bonus("not-a-date"), 0.0)

    def test_future_timestamp_clamped_counts_as_recent(self) -> None:
        # clock skew → _hours_since clamps to 0.0, still in-window
        self.assertAlmostEqual(
            _document_recency_bonus(_iso_days_ago(-2)), _DOCUMENT_RECENCY_BONUS,
        )


class DocumentRecencyRetrieveTests(unittest.TestCase):
    def test_recent_outranks_old_at_equal_cosine(self) -> None:
        hits = _build(
            [
                _doc_hit(
                    chunk_id="old", content="old note about tea", base=0.6,
                    created_at=_iso_days_ago(30),
                ),
                _doc_hit(
                    chunk_id="new", content="fresh note about tea", base=0.6,
                    created_at=_iso_days_ago(1),
                ),
            ]
        ).retrieve("tea")
        by_text = {h.text.strip(): h for h in hits}
        self.assertIn("fresh note about tea", by_text)
        self.assertIn("old note about tea", by_text)
        self.assertAlmostEqual(
            by_text["fresh note about tea"].score
            - by_text["old note about tea"].score,
            _DOCUMENT_RECENCY_BONUS,
            places=4,
        )

    def test_old_document_unchanged(self) -> None:
        hits = _build(
            [
                _doc_hit(
                    chunk_id="old", content="ancient note", base=0.6,
                    created_at=_iso_days_ago(45),
                ),
            ]
        ).retrieve("note")
        self.assertAlmostEqual(hits[0].score, 0.6, places=4)


if __name__ == "__main__":
    unittest.main()
