"""Tests for prepared nudge store + narrative weaver (Phase 4c)."""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.agenda import AgendaStore
from app.core.chat_database import ChatDatabase
from app.core.memory_store import Memory, MemoryStore
from app.core.prepared_nudge import (
    NarrativeWeaver,
    PreparedNudgeStore,
    _clean_weave_output,
    _fallback_phrasing,
)


import numpy as np


class _FakeOllama:
    def __init__(self, response: str = "Picking that thread back up — how's it sitting?"):
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.store = PreparedNudgeStore(self.db)
        self.memory = MemoryStore(self.path, max_memories=50, dedupe_threshold=0.999)
        self.agenda = AgendaStore(self.db)

    def close(self):
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


class PreparedNudgeStoreTests(unittest.TestCase):
    def test_upsert_and_get(self):
        f = _Fixture()
        try:
            nudge = f.store.upsert(
                "u1",
                text="Hey, still curious about that book.",
                source_kind="callback",
                source_id="42",
            )
            self.assertIsNotNone(nudge)
            assert nudge is not None
            self.assertEqual(nudge.source_id, "42")
            fetched = f.store.get("u1")
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.text, nudge.text)
        finally:
            f.close()

    def test_upsert_replaces(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", text="first", source_kind="callback")
            f.store.upsert("u1", text="second", source_kind="agenda")
            row = f.store.get("u1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.text, "second")
            self.assertEqual(row.source_kind, "agenda")
        finally:
            f.close()

    def test_invalid_kind_falls_back(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", text="x", source_kind="weird")
            row = f.store.get("u1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.source_kind, "mixed")
        finally:
            f.close()

    def test_empty_text_rejected(self):
        f = _Fixture()
        try:
            self.assertIsNone(f.store.upsert("u1", text="   "))
            self.assertIsNone(f.store.get("u1"))
        finally:
            f.close()

    def test_get_fresh_respects_ttl(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", text="hello", ttl_seconds=60.0)
            self.assertIsNotNone(f.store.get_fresh("u1"))
            future = datetime.now(timezone.utc) + timedelta(seconds=120)
            self.assertIsNone(f.store.get_fresh("u1", now_utc=future))
        finally:
            f.close()

    def test_consume_deletes(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", text="hi")
            taken = f.store.consume("u1")
            self.assertIsNotNone(taken)
            self.assertIsNone(f.store.get("u1"))
        finally:
            f.close()

    def test_consume_misses_when_stale(self):
        f = _Fixture()
        try:
            f.store.upsert("u1", text="hi", ttl_seconds=60.0)
            future = datetime.now(timezone.utc) + timedelta(seconds=120)
            self.assertIsNone(f.store.consume("u1", now_utc=future))
        finally:
            f.close()


class CleanWeaveOutputTests(unittest.TestCase):
    def test_strips_quotes(self):
        self.assertEqual(_clean_weave_output('"hello"'), "hello")

    def test_takes_first_line(self):
        self.assertEqual(_clean_weave_output("first\nsecond"), "first")

    def test_strips_fenced(self):
        self.assertEqual(_clean_weave_output("```\nhi\n```"), "hi")

    def test_truncates(self):
        out = _clean_weave_output("x " * 300)
        self.assertTrue(out.endswith("…"))
        self.assertLess(len(out), 260)

    def test_empty(self):
        self.assertEqual(_clean_weave_output(""), "")


class FallbackPhrasingTests(unittest.TestCase):
    def test_callback_template(self):
        from app.core.prepared_nudge import _Candidate

        c = _Candidate(kind="callback", source_id="1", text="that book", salience=0.6)
        out = _fallback_phrasing(c)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("that book", out)

    def test_unknown_kind_returns_text(self):
        from app.core.prepared_nudge import _Candidate

        c = _Candidate(kind="huh", source_id="1", text="some thread", salience=0.6)
        self.assertEqual(_fallback_phrasing(c), "some thread")


class NarrativeWeaverTests(unittest.TestCase):
    def _seed_memories(self, memory: MemoryStore):
        memory.add("Wonders if Jacob picked the python book back up", "callback", _emb(1), salience=0.8)
        memory.add("Why is the deploy so flaky on Friday", "open_question", _emb(2), salience=0.7)
        memory.add("Will reply to that long email tomorrow", "promise", _emb(3), salience=0.6)
        memory.add("Notices that he warms up after coffee", "reflection", _emb(4), salience=0.5)

    def _make(
        self,
        response: str = "Hey, did you ever pick that book back up?",
        rng_seed: int = 0,
        **overrides,
    ):
        f = _Fixture()
        self._seed_memories(f.memory)
        ollama = _FakeOllama(response)
        kwargs = {
            "ollama": ollama,
            "store": f.store,
            "memory_store": f.memory,
            "agenda_store": f.agenda,
            "model": "m",
            "every_n_turns": 2,
            "rng": random.Random(rng_seed),
        }
        kwargs.update(overrides)
        return f, ollama, NarrativeWeaver(**kwargs)

    def test_throttles_below_min_turns(self):
        f, ollama, weaver = self._make()
        try:
            weaver.notify_user_turn()
            self.assertIsNone(weaver.maybe_run("u1"))
            self.assertEqual(ollama.calls, [])
        finally:
            f.close()

    def test_runs_and_persists(self):
        f, ollama, weaver = self._make()
        try:
            for _ in range(2):
                weaver.notify_user_turn()
            result = weaver.maybe_run("u1")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreater(len(result.text), 0)
            stored = f.store.get_fresh("u1")
            self.assertIsNotNone(stored)
        finally:
            f.close()

    def test_skips_when_fresh_nudge_exists(self):
        f, ollama, weaver = self._make()
        try:
            f.store.upsert("u1", text="already prepared", source_kind="callback", ttl_seconds=600)
            for _ in range(2):
                weaver.notify_user_turn()
            self.assertIsNone(weaver.maybe_run("u1"))
        finally:
            f.close()

    def test_falls_back_when_no_llm(self):
        f, _ollama, weaver = self._make()
        try:
            weaver._ollama = None  # type: ignore[attr-defined]
            for _ in range(2):
                weaver.notify_user_turn()
            result = weaver.maybe_run("u1")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreater(len(result.text), 0)
        finally:
            f.close()

    def test_llm_failure_falls_back(self):
        f, ollama, weaver = self._make()
        try:
            ollama.fail = True
            for _ in range(2):
                weaver.notify_user_turn()
            result = weaver.maybe_run("u1")
            # Should still produce a fallback nudge without crashing.
            self.assertIsNotNone(result)
        finally:
            f.close()

    def test_picks_agenda_when_present(self):
        f, ollama, weaver = self._make(rng_seed=99)
        try:
            f.agenda.add("u1", goal="finish the migration", importance=0.9)
            for _ in range(2):
                weaver.notify_user_turn()
            result = weaver.maybe_run("u1")
            self.assertIsNotNone(result)
        finally:
            f.close()

    def test_skipped_no_candidate_when_empty(self):
        f = _Fixture()
        try:
            ollama = _FakeOllama()
            weaver = NarrativeWeaver(
                ollama=ollama,
                store=f.store,
                memory_store=f.memory,
                agenda_store=None,
                model="m",
                every_n_turns=2,
            )
            for _ in range(2):
                weaver.notify_user_turn()
            self.assertIsNone(weaver.maybe_run("u1"))
            self.assertEqual(weaver.stats()["skipped_no_candidate"], 1)
        finally:
            f.close()


class CuriositySeedSourceTests(unittest.TestCase):
    """K9: NarrativeWeaver picks up ``curiosity_seed`` memories.

    Seeds carry a fully-rendered ``metadata.prompt_text``; the weaver
    uses that verbatim and tags the resulting nudge with
    ``source_kind='curiosity_seed'`` so the proactive director can
    label it correctly.
    """

    def test_seed_becomes_prepared_nudge(self):
        f = _Fixture()
        try:
            # Insert a single curiosity_seed; nothing else competes.
            f.memory.add(
                "your favourite tea ritual",
                "curiosity_seed",
                _emb(7),
                salience=0.5,
                tier="scratchpad",
                metadata={
                    "topic": "your favourite tea ritual",
                    "prompt_text": (
                        "Off-topic, but I've been wondering "
                        "what your perfect tea moment looks like."
                    ),
                    "source": "llm",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "consumed_at": None,
                    "candidate_score": 0.42,
                },
            )
            ollama = _FakeOllama()
            # ``every_n_turns=1`` so the weaver runs immediately.
            weaver = NarrativeWeaver(
                ollama=ollama,
                store=f.store,
                memory_store=f.memory,
                agenda_store=None,
                model="m",
                every_n_turns=1,
                rng=random.Random(0),
            )
            weaver.notify_user_turn()
            result = weaver.maybe_run("u1")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.source_kind, "curiosity_seed")
            # Seed bypasses the LLM weave so the prompt_text comes
            # through verbatim (modulo trim).
            self.assertIn("tea moment", result.text)
            # No LLM call should have happened — the curiosity_seed
            # branch short-circuits ``_weave``.
            self.assertEqual(ollama.calls, [])
            self.assertEqual(
                weaver.stats().get("from_curiosity_seed"), 1,
            )
        finally:
            f.close()

    def test_consumed_seed_is_skipped(self):
        f = _Fixture()
        try:
            # Mark the seed already consumed -> shouldn't surface.
            f.memory.add(
                "tea ritual",
                "curiosity_seed",
                _emb(8),
                salience=0.5,
                tier="scratchpad",
                metadata={
                    "topic": "tea ritual",
                    "prompt_text": "p",
                    "consumed_at": "2026-01-05T00:00:00+00:00",
                },
            )
            ollama = _FakeOllama()
            weaver = NarrativeWeaver(
                ollama=ollama,
                store=f.store,
                memory_store=f.memory,
                agenda_store=None,
                model="m",
                every_n_turns=1,
            )
            weaver.notify_user_turn()
            self.assertIsNone(weaver.maybe_run("u1"))
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
