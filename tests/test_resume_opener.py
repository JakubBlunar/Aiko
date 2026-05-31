"""Tests for the Phase 2a session-resume opener.

Two slices to lock in:

  1. ``NarrativeWeaver.prepare_resume_opener`` actually persists a
     ``source_kind="resume"`` row in :class:`PreparedNudgeStore` when
     given a rolling summary, ignores the per-turn throttle, and
     gracefully degrades when the LLM is unavailable.
  2. The fallback path (no LLM, no candidate) builds a short opener
     out of just the rolling summary.
"""
from __future__ import annotations

import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.goals.agenda import AgendaStore
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import Memory, MemoryStore
from app.core.proactive.prepared_nudge import (
    NarrativeWeaver,
    PreparedNudgeStore,
    _resume_fallback_from_summary,
)


class _FakeOllama:
    def __init__(self, response: str = "Hey — been thinking about that book.") -> None:
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.store = PreparedNudgeStore(self.db)
        self.memory = MemoryStore(self.path, max_memories=50, dedupe_threshold=0.999)
        self.agenda = AgendaStore(self.db)

    def close(self) -> None:
        try:
            self.memory.close()
        except Exception:
            pass
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


def _emb(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(size=8).astype(np.float32)


def _seed_callback_memory(memory: MemoryStore) -> None:
    memory.add(
        content="Jacob mentioned wanting to read more sci-fi.",
        kind="callback",
        embedding=_emb(7),
        salience=0.8,
    )


def _make_weaver(
    *, ollama: _FakeOllama | None, with_callback: bool = True,
) -> tuple[_Fixture, NarrativeWeaver]:
    f = _Fixture()
    if with_callback:
        _seed_callback_memory(f.memory)
    weaver = NarrativeWeaver(
        ollama=ollama,
        store=f.store,
        memory_store=f.memory,
        agenda_store=f.agenda,
        model="m",
        every_n_turns=2,
        rng=random.Random(0),
    )
    return f, weaver


class PrepareResumeOpenerTests(unittest.TestCase):
    def test_persists_resume_kind_when_llm_available(self) -> None:
        ollama = _FakeOllama("Hey — still curious about that sci-fi recommendation.")
        f, weaver = _make_weaver(ollama=ollama)
        try:
            nudge = weaver.prepare_resume_opener(
                "u1",
                rolling_summary="Yesterday we talked about books and sci-fi.",
                hours_since_last=6.0,
                ttl_seconds=1800.0,
            )
            self.assertIsNotNone(nudge)
            assert nudge is not None
            self.assertEqual(nudge.source_kind, "resume")
            self.assertGreater(len(nudge.text), 0)
            stored = f.store.get("u1")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.source_kind, "resume")
            self.assertEqual(len(ollama.calls), 1)
        finally:
            f.close()

    def test_bypasses_throttle(self) -> None:
        """The per-turn throttle on ``maybe_run`` must NOT apply to the
        one-shot resume opener — it's a bootstrap event."""
        ollama = _FakeOllama()
        f, weaver = _make_weaver(ollama=ollama)
        try:
            # No notify_user_turn calls → maybe_run would skip, but
            # prepare_resume_opener must run anyway.
            nudge = weaver.prepare_resume_opener(
                "u1", rolling_summary="some context",
                hours_since_last=8.0,
            )
            self.assertIsNotNone(nudge)
            self.assertEqual(len(ollama.calls), 1)
        finally:
            f.close()

    def test_falls_back_to_summary_when_no_llm_no_candidate(self) -> None:
        f, weaver = _make_weaver(ollama=None, with_callback=False)
        try:
            nudge = weaver.prepare_resume_opener(
                "u1",
                rolling_summary=(
                    "Last conversation: Jacob debugged a tricky issue with the "
                    "database connection pool."
                ),
                hours_since_last=10.0,
            )
            self.assertIsNotNone(nudge)
            assert nudge is not None
            self.assertEqual(nudge.source_kind, "resume")
            # Fallback prefix is deterministic.
            self.assertIn("sitting with", nudge.text)
        finally:
            f.close()

    def test_returns_none_when_no_llm_no_candidate_no_summary(self) -> None:
        f, weaver = _make_weaver(ollama=None, with_callback=False)
        try:
            nudge = weaver.prepare_resume_opener(
                "u1", rolling_summary="", hours_since_last=10.0,
            )
            self.assertIsNone(nudge)
            self.assertIsNone(f.store.get("u1"))
        finally:
            f.close()

    def test_llm_failure_falls_back_to_candidate(self) -> None:
        ollama = _FakeOllama()
        ollama.fail = True
        f, weaver = _make_weaver(ollama=ollama)
        try:
            nudge = weaver.prepare_resume_opener(
                "u1", rolling_summary="something brief",
                hours_since_last=5.0,
            )
            # The candidate-based fallback should still produce a line.
            self.assertIsNotNone(nudge)
            assert nudge is not None
            self.assertEqual(nudge.source_kind, "resume")
        finally:
            f.close()

    def test_resume_fallback_from_summary_truncates(self) -> None:
        long = "a " * 200
        out = _resume_fallback_from_summary(long)
        self.assertIsNotNone(out)
        assert out is not None
        # Truncation cuts at 80 chars + the ellipsis-friendly suffix
        # combine with "Hey — I've been sitting with what we were saying about "
        # → keep length under ~250.
        self.assertLess(len(out), 250)


if __name__ == "__main__":
    unittest.main()
