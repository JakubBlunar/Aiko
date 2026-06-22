"""F8 RAG retriever: informational-gated ``knowledge`` boost + tag.

Two surfaces:

* ``retrieve`` adds ``_RAG_KNOWLEDGE_BONUS`` to a ``knowledge``-kind hit
  only when the live dialogue act is informational (``question``); a
  non-question turn leaves the score untouched, and non-``knowledge``
  kinds never get the bonus regardless of act.
* ``format_block`` appends the invisible ``(learned)`` suffix to
  ``knowledge`` rows so the persona rule can surface a distilled fact
  naturally.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.rag.rag_retriever import RagRetriever, _RAG_KNOWLEDGE_BONUS
from app.core.rag.rag_store import MemoryRecord, RagHit


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _memory_hit(*, record_id: str, content: str, kind: str, base: float) -> RagHit:
    return RagHit(
        source="memory",
        score=float(base),
        record=MemoryRecord(
            id=record_id,
            content=content,
            kind=kind,
            salience=0.6,
            source_session=None,
            source_message_id=None,
            created_at=_iso_hours_ago(48),
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
    """Minimal ``.get`` / ``.mark_used`` surface for the join path."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def get(self, _id: int) -> SimpleNamespace:
        return SimpleNamespace(
            kind=self._kind,
            pinned=False,
            tier="long_term",
            confidence=0.7,
            salience=0.6,
            embedding=None,
            metadata=None,
            temporal_type="durable",
            event_time=None,
            relevance_until=None,
        )

    def mark_used(self, _ids: Any) -> None:
        pass


def _build(*, kind: str, act: str) -> RagRetriever:
    hit = _memory_hit(
        record_id="100", content="Italian roast is very dark.", kind=kind, base=0.5,
    )
    return RagRetriever(
        _StubStore([hit]),  # type: ignore[arg-type]
        _StubEmbedder(),  # type: ignore[arg-type]
        top_k=5,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        memory_store=_StubMemoryStore(kind),  # type: ignore[arg-type]
        dialogue_act_provider=(lambda _t: act),
    )


class KnowledgeBoostTests(unittest.TestCase):
    def test_knowledge_boosted_on_question(self) -> None:
        q = _build(kind="knowledge", act="question").retrieve("what is X?")
        b = _build(kind="knowledge", act="banter").retrieve("lol")
        self.assertAlmostEqual(
            q[0].score - b[0].score, _RAG_KNOWLEDGE_BONUS, places=4,
        )

    def test_non_knowledge_kind_never_boosted(self) -> None:
        q = _build(kind="fact", act="question").retrieve("what is X?")
        b = _build(kind="fact", act="banter").retrieve("lol")
        self.assertAlmostEqual(q[0].score - b[0].score, 0.0, places=4)

    def test_no_dialogue_act_provider_no_boost(self) -> None:
        hit = _memory_hit(
            record_id="100", content="Italian roast is very dark.",
            kind="knowledge", base=0.5,
        )
        with_provider = _build(kind="knowledge", act="question").retrieve("what is X?")
        without = RagRetriever(
            _StubStore([hit]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
            memory_store=_StubMemoryStore("knowledge"),  # type: ignore[arg-type]
        ).retrieve("what is X?")
        self.assertAlmostEqual(
            with_provider[0].score - without[0].score, _RAG_KNOWLEDGE_BONUS,
            places=4,
        )


class LearnedTagTests(unittest.TestCase):
    def test_knowledge_row_gets_learned_suffix(self) -> None:
        hit = _memory_hit(
            record_id="1", content="Slowdive is a shoegaze band.",
            kind="knowledge", base=0.5,
        )
        block = RagRetriever.format_block([hit])
        self.assertIn("Slowdive is a shoegaze band. (learned)", block)

    def test_non_knowledge_row_has_no_learned_suffix(self) -> None:
        hit = _memory_hit(
            record_id="2", content="Jacob likes tea.", kind="preference", base=0.5,
        )
        block = RagRetriever.format_block([hit])
        self.assertIn("Jacob likes tea.", block)
        self.assertNotIn("(learned)", block)


if __name__ == "__main__":
    unittest.main()
