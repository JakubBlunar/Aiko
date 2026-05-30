"""K22 RAG retriever boost: callback bonus.

Verifies that ``RagRetriever`` applies the callback bonus
(``_RAG_CALLBACK_BONUS``) to memory hits whose stored
``metadata.callback_count >= 1`` and skips the bonus on rows whose
metadata is missing / count is zero.

Pattern mirrors :mod:`tests.test_rag_retriever_goal_alignment` -- a
``MemoryStore`` is populated with a real row + metadata, a stub
``RagStore`` returns a hand-built ``RagHit``, and the retriever's
join path picks up the metadata + applies the bonus.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.memory_store import MemoryStore
from app.core.rag_retriever import (
    RagRetriever,
    _MEMORY_PINNED_BONUS,
    _MEMORY_PRIOR,
    _MEMORY_TIER_OFFSET,
    _RAG_CALLBACK_BONUS,
)
from app.core.rag_store import MemoryRecord, RagHit


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
    memory_content: str,
    memory_kind: str = "fact",
    metadata: dict[str, Any] | None = None,
    pinned: bool = False,
) -> RagRetriever:
    """Build a retriever whose only memory hit is the one configured.

    ``metadata`` is written through ``MemoryStore.update`` so the
    JSON column carries it (an extra write after ``add`` because
    ``add`` doesn't take a metadata kwarg today -- not relevant to
    K22 semantics).
    """
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
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
    if metadata is not None:
        memory_store.update(
            candidate.id,
            metadata=dict(metadata),
            metadata_merge=True,
        )
    if pinned:
        memory_store.set_pinned(candidate.id, True)
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
    return RagRetriever(
        _StubStore(hits=[hit]),  # type: ignore[arg-type]
        embedder,  # type: ignore[arg-type]
        top_k=5,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        memory_store=memory_store,
    )


def _base_score() -> float:
    """Score floor with prior + long_term tier offset but no bonuses."""
    return 0.5 + _MEMORY_PRIOR + _MEMORY_TIER_OFFSET.get("long_term", 0.0)


class CallbackBonusTests(unittest.TestCase):

    def test_callback_count_one_gets_bonus(self) -> None:
        retriever = _make_environment(
            memory_content="rubber duck debugging story",
            metadata={
                "callback_count": 1,
                "last_callback_at": "2026-05-29T12:00:00+00:00",
            },
        )
        results = retriever.retrieve("debugging notes")
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(
            results[0].score,
            _base_score() + _RAG_CALLBACK_BONUS,
            places=4,
        )

    def test_callback_count_zero_no_bonus(self) -> None:
        retriever = _make_environment(
            memory_content="never-cited story",
            metadata={"callback_count": 0},
        )
        results = retriever.retrieve("never-cited")
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(
            results[0].score,
            _base_score(),
            places=4,
        )

    def test_missing_metadata_no_bonus(self) -> None:
        # Defensive: legacy rows (or fresh rows pre-K22) have no
        # ``metadata`` JSON. The retriever must not raise and must
        # not award a bonus.
        retriever = _make_environment(
            memory_content="legacy row no metadata",
            metadata=None,
        )
        results = retriever.retrieve("legacy row")
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(
            results[0].score,
            _base_score(),
            places=4,
        )

    def test_callback_bonus_compounds_with_pinned(self) -> None:
        # A pinned row that's also been called back should earn
        # both bumps independently -- the two reinforcement signals
        # measure different things (user-curated vs Aiko-cited).
        retriever = _make_environment(
            memory_content="pinned and called-back beat",
            metadata={"callback_count": 3},
            pinned=True,
        )
        results = retriever.retrieve("pinned beat")
        self.assertEqual(len(results), 1)
        # Pinning also clamps confidence to >= 0.9, which is above
        # the 0.5 floor that triggers the confidence penalty; the
        # penalty is 0.0 here regardless. So the only adjustments
        # beyond the base are pinned + callback.
        expected = (
            _base_score()
            + _MEMORY_PINNED_BONUS
            + _RAG_CALLBACK_BONUS
        )
        self.assertAlmostEqual(
            results[0].score,
            expected,
            places=4,
        )

    def test_high_callback_count_does_not_compound_in_bonus(self) -> None:
        # The K22 bonus is single-step regardless of how many times
        # the row has been called back -- the compounding lives on
        # the salience bump applied at record-time, not on the
        # retriever bonus.
        retriever = _make_environment(
            memory_content="frequently-cited beat",
            metadata={"callback_count": 50},
        )
        results = retriever.retrieve("frequently cited")
        self.assertAlmostEqual(
            results[0].score,
            _base_score() + _RAG_CALLBACK_BONUS,
            places=4,
        )

    def test_malformed_callback_count_treated_as_zero(self) -> None:
        # Defensive: a row whose metadata got corrupted to a string
        # should silently land at zero, not raise.
        retriever = _make_environment(
            memory_content="corrupt count row",
            metadata={"callback_count": "oops"},
        )
        results = retriever.retrieve("corrupt")
        self.assertAlmostEqual(
            results[0].score,
            _base_score(),
            places=4,
        )


if __name__ == "__main__":
    unittest.main()
