"""K1 RAG retriever boost: goal-alignment.

Verifies that ``RagRetriever`` applies the goal-alignment bonus
(``_RAG_GOAL_ALIGNMENT_BOOST``) to memory hits whose embedding cosine-
aligns with any of Aiko's active long-term goals above
``_RAG_GOAL_ALIGNMENT_THRESHOLD``, and skips the bonus on the goal
rows themselves so the cosine signal doesn't compound.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.goals.goal_store import GoalStore
from app.core.memory.memory_store import MemoryStore
from app.core.rag.rag_retriever import (
    RagRetriever,
    _MEMORY_PRIOR,
    _RAG_GOAL_ALIGNMENT_BOOST,
    _RAG_GOAL_ALIGNMENT_THRESHOLD,
    _MEMORY_TIER_OFFSET,
)
from app.core.rag.rag_store import MemoryRecord, RagHit


class _DeterministicEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            slot = hash(token) % self.DIM
            vec[slot] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


class _StubStore:
    def __init__(self, *, hits: list[RagHit]) -> None:
        self._hits = list(hits)

    def search_memories(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._hits
        ]

    def search_messages(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return []


def _make_environment(
    *,
    goal_summaries: list[str],
    memory_content: str,
    memory_kind: str = "fact",
) -> tuple[RagRetriever, str]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    goal_store = GoalStore(memory_store=memory_store, embedder=embedder)
    for summary in goal_summaries:
        goal_store.add_goal(summary=summary, source="user")
    # Persist a candidate memory row in the SQLite mirror so the
    # retriever's join lookup can pull the embedding.
    candidate = memory_store.add(
        content=memory_content,
        kind=memory_kind,
        embedding=embedder.embed(memory_content),
        salience=0.6,
        tier="long_term",
        confidence=0.8,
        skip_dedupe=True,
    )
    assert candidate is not None
    # Build the LanceDB-side RagHit by hand — the stub store returns it
    # verbatim and the retriever joins against ``memory_store.get``.
    hit = RagHit(
        source="memory",
        score=0.5,
        record=MemoryRecord(
            id=str(candidate.id),
            content=memory_content,
            kind=memory_kind,
            salience=0.6,
            source_session=None,
            source_message_id=None,
            created_at=(
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat(),
            last_used_at=None,
            use_count=0,
        ),
    )
    retriever = RagRetriever(
        _StubStore(hits=[hit]),  # type: ignore[arg-type]
        embedder,  # type: ignore[arg-type]
        top_k=5,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        memory_store=memory_store,
        goal_store=goal_store,
    )
    return retriever, memory_content


def _expected_score(*, base: float) -> float:
    # The join path also applies the ``long_term`` tier offset (=0.0)
    # so we just need the prior + base.
    return base + _MEMORY_PRIOR + _MEMORY_TIER_OFFSET.get("long_term", 0.0)


class GoalAlignmentBoostTests(unittest.TestCase):
    def test_aligned_memory_gets_goal_bonus(self) -> None:
        retriever, _ = _make_environment(
            goal_summaries=[
                "practice jazz piano sevenths and ninths daily",
            ],
            memory_content="practice jazz piano sevenths and ninths daily",
        )
        results = retriever.retrieve("looking for jazz piano practice")
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(
            results[0].score,
            _expected_score(base=0.5) + _RAG_GOAL_ALIGNMENT_BOOST,
            places=4,
        )

    def test_unaligned_memory_gets_no_goal_bonus(self) -> None:
        retriever, _ = _make_environment(
            goal_summaries=[
                "practice jazz piano sevenths and ninths daily",
            ],
            memory_content="kubernetes containers orchestration nginx",
        )
        results = retriever.retrieve("looking for jazz piano practice")
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(
            results[0].score,
            _expected_score(base=0.5),
            places=4,
        )

    def test_goal_rows_themselves_are_excluded_from_bonus(self) -> None:
        # A ``goal`` kind hit would otherwise self-align and double-dip.
        retriever, _ = _make_environment(
            goal_summaries=[
                "practice jazz piano sevenths and ninths daily",
            ],
            memory_content="practice jazz piano sevenths and ninths daily",
            memory_kind="goal",
        )
        results = retriever.retrieve("looking for jazz piano practice")
        self.assertEqual(len(results), 1)
        # No goal-alignment bonus on a ``goal`` hit.
        self.assertAlmostEqual(
            results[0].score,
            _expected_score(base=0.5),
            places=4,
        )

    def test_no_goal_store_disables_bonus(self) -> None:
        # Build a retriever without a goal store and confirm the
        # score stays at the legacy ``base + prior`` value even when
        # the hit's content would have aligned.
        retriever, _ = _make_environment(
            goal_summaries=[
                "practice jazz piano sevenths and ninths daily",
            ],
            memory_content="practice jazz piano sevenths and ninths daily",
        )
        retriever.set_goal_store(None)
        results = retriever.retrieve("looking for jazz piano practice")
        self.assertAlmostEqual(
            results[0].score,
            _expected_score(base=0.5),
            places=4,
        )


if __name__ == "__main__":
    unittest.main()
