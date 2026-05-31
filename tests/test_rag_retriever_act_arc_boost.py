"""H1 + K4 RAG retriever boost: arc + dialogue_act alignment.

Asserts that ``RagRetriever`` adds the configured boosts when a hit's
source ``messages`` row matches the live arc and / or the live user
dialogue_act, and that the combined boost is capped at +0.05 (so a hit
matching on both never gets the full additive +0.06).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.rag.rag_retriever import (
    RagRetriever,
    _MEMORY_PRIOR,
    _RAG_ALIGNMENT_BOOST_CAP,
    _RAG_ARC_BOOST,
    _RAG_DIALOGUE_ACT_BOOST,
)
from app.core.rag.rag_store import (
    MemoryRecord,
    MessageRecord,
    RagHit,
)


def _iso_hours_ago(hours: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


def _memory_hit(
    *,
    record_id: str,
    content: str,
    base_score: float,
    source_message_id: int | None,
) -> RagHit:
    return RagHit(
        source="memory",
        score=float(base_score),
        record=MemoryRecord(
            id=record_id,
            content=content,
            kind="fact",
            salience=0.5,
            source_session=None,
            source_message_id=source_message_id,
            created_at=_iso_hours_ago(48),
            last_used_at=None,
            use_count=0,
        ),
    )


class _StubStore:
    def __init__(self, *, memories: list[RagHit]) -> None:
        self._memories = list(memories)

    def search_memories(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return [
            RagHit(source=h.source, score=h.score, record=h.record)
            for h in self._memories
        ]

    def search_messages(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_args: Any, **_kwargs: Any) -> list[RagHit]:
        return []


class _StubEmbedder:
    def embed(self, _text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


class _StubChatDb:
    def __init__(self, signals: dict[int, tuple[str | None, str | None]]) -> None:
        self._signals = signals

    def get_message_signals(self, ids):
        unique = list(dict.fromkeys(int(i) for i in ids))
        return {i: self._signals[i] for i in unique if i in self._signals}


def _build(
    *,
    memories: list[RagHit],
    signals: dict[int, tuple[str | None, str | None]],
    arc: str | None,
    dialogue_act: str | None,
) -> RagRetriever:
    chat_db = _StubChatDb(signals)
    arc_state = (
        SimpleNamespace(arc=arc, since_turn=0, confidence=0.85, user_id="u1", updated_at="")
        if arc is not None
        else None
    )
    return RagRetriever(
        _StubStore(memories=memories),  # type: ignore[arg-type]
        _StubEmbedder(),  # type: ignore[arg-type]
        top_k=5,
        score_threshold=0.0,
        include_messages=False,
        include_documents=False,
        chat_db=chat_db,  # type: ignore[arg-type]
        arc_state_provider=lambda: arc_state,
        dialogue_act_provider=(lambda _t: dialogue_act),
    )


class AlignmentBoostTests(unittest.TestCase):
    def test_arc_match_alone_adds_three_hundredths(self) -> None:
        hit = _memory_hit(
            record_id="1",
            content="aligned memory",
            base_score=0.5,
            source_message_id=42,
        )
        retriever = _build(
            memories=[hit],
            signals={42: ("support", "story")},
            arc="support",
            dialogue_act="banter",  # mismatch on act
        )
        results = retriever.retrieve("anything")
        self.assertAlmostEqual(
            results[0].score,
            0.5 + _MEMORY_PRIOR + _RAG_ARC_BOOST,
            places=4,
        )

    def test_dialogue_act_match_alone_adds_three_hundredths(self) -> None:
        hit = _memory_hit(
            record_id="2",
            content="dialogue-act-aligned",
            base_score=0.5,
            source_message_id=43,
        )
        retriever = _build(
            memories=[hit],
            signals={43: ("playful", "vent")},
            arc="support",  # mismatch on arc
            dialogue_act="vent",
        )
        results = retriever.retrieve("anything")
        self.assertAlmostEqual(
            results[0].score,
            0.5 + _MEMORY_PRIOR + _RAG_DIALOGUE_ACT_BOOST,
            places=4,
        )

    def test_combined_boost_is_capped_at_five_hundredths(self) -> None:
        hit = _memory_hit(
            record_id="3",
            content="aligned on both",
            base_score=0.5,
            source_message_id=44,
        )
        retriever = _build(
            memories=[hit],
            signals={44: ("support", "vent")},
            arc="support",
            dialogue_act="vent",
        )
        results = retriever.retrieve("anything")
        # Both signals match, but the combined boost must not exceed
        # the +0.05 cap (vs. the additive +0.06 the raw constants
        # would produce).
        self.assertAlmostEqual(
            results[0].score,
            0.5 + _MEMORY_PRIOR + _RAG_ALIGNMENT_BOOST_CAP,
            places=4,
        )
        self.assertLess(
            results[0].score,
            0.5 + _MEMORY_PRIOR + _RAG_ARC_BOOST + _RAG_DIALOGUE_ACT_BOOST,
        )

    def test_no_match_leaves_score_unchanged(self) -> None:
        hit = _memory_hit(
            record_id="4",
            content="nothing matches",
            base_score=0.5,
            source_message_id=45,
        )
        retriever = _build(
            memories=[hit],
            signals={45: ("playful", "story")},
            arc="support",
            dialogue_act="vent",
        )
        results = retriever.retrieve("anything")
        self.assertAlmostEqual(
            results[0].score, 0.5 + _MEMORY_PRIOR, places=4,
        )

    def test_missing_source_message_id_is_skipped(self) -> None:
        # Hits without a source row can't be looked up; their score
        # should remain at the legacy ``base + prior`` value.
        hit = _memory_hit(
            record_id="5",
            content="orphan memory",
            base_score=0.5,
            source_message_id=None,
        )
        retriever = _build(
            memories=[hit],
            signals={},  # nothing to look up anyway
            arc="support",
            dialogue_act="vent",
        )
        results = retriever.retrieve("anything")
        self.assertAlmostEqual(
            results[0].score, 0.5 + _MEMORY_PRIOR, places=4,
        )

    def test_no_providers_disables_boost(self) -> None:
        hit = _memory_hit(
            record_id="6",
            content="legacy retriever",
            base_score=0.5,
            source_message_id=46,
        )
        retriever = RagRetriever(
            _StubStore(memories=[hit]),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            top_k=5,
            score_threshold=0.0,
            include_messages=False,
            include_documents=False,
        )
        results = retriever.retrieve("anything")
        self.assertAlmostEqual(
            results[0].score, 0.5 + _MEMORY_PRIOR, places=4,
        )


if __name__ == "__main__":
    unittest.main()
