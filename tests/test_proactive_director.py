"""Tests for ProactiveDirector prepared-nudge fast path (Phase 4c)."""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from app.core.chat_database import ChatDatabase
from app.core.prepared_nudge import PreparedNudgeStore
from app.core.proactive_director import ProactiveDirector


class _FakePromptAssembler:
    def build(self, session_key, hint, *, context_window, response_budget):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": hint}]


class _FakeOllama:
    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, *, model, timeout_seconds, options, format_json):
        self.calls += 1

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 4

        return "Sure thing!", _Usage()


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.prepared = PreparedNudgeStore(self.db)
        self.spoken: list[tuple[str, str]] = []
        self.notified: list[tuple[str, str]] = []
        self.live = True
        self.busy = False
        self.ollama = _FakeOllama()
        self.director = ProactiveDirector(
            ollama=self.ollama,
            db=self.db,
            prompt_assembler=_FakePromptAssembler(),
            model="m",
            speak=lambda text, mood: self.spoken.append((text, mood)),
            is_busy=lambda: self.busy,
            is_live_mode=lambda: self.live,
            cooldown_seconds=0.01,
            max_tokens=20,
            timeout_seconds=2.0,
            context_window=2048,
            notify_message=lambda speaker, text: self.notified.append((speaker, text)),
            prepared_nudge_store=self.prepared,
            user_id="u1",
        )

    def close(self):
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

    def seed_history(self):
        self.db.add_message(session_id="s1", role="user", content="hi", token_count=1)
        self.db.add_message(session_id="s1", role="assistant", content="hello", token_count=1)


class PreparedFastPathTests(unittest.TestCase):
    def _wait_for_speak(self, fixture: _Fixture, timeout: float = 2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if fixture.spoken:
                return
            time.sleep(0.02)
        raise AssertionError("director never spoke")

    def test_consumes_prepared_when_fresh(self):
        f = _Fixture()
        try:
            f.seed_history()
            f.prepared.upsert(
                "u1",
                text="Hey, picked up the migration thread?",
                source_kind="agenda",
                ttl_seconds=120.0,
            )
            f.director.notify_silence("s1")
            self._wait_for_speak(f)
            self.assertEqual(len(f.spoken), 1)
            self.assertIn("migration", f.spoken[0][0])
            self.assertEqual(f.ollama.calls, 0)
            self.assertIsNone(f.prepared.get("u1"))
            stats = f.director.stats()
            self.assertEqual(stats["prepared_consumed"], 1)
            self.assertEqual(stats["llm_path_used"], 0)
        finally:
            f.close()

    def test_falls_back_to_llm_without_prepared(self):
        f = _Fixture()
        try:
            f.seed_history()
            f.director.notify_silence("s1")
            self._wait_for_speak(f)
            self.assertEqual(f.ollama.calls, 1)
            stats = f.director.stats()
            self.assertEqual(stats["prepared_consumed"], 0)
            self.assertEqual(stats["llm_path_used"], 1)
        finally:
            f.close()

    def test_skipped_when_no_history(self):
        f = _Fixture()
        try:
            f.prepared.upsert("u1", text="anything", ttl_seconds=120.0)
            f.director.notify_silence("s1")
            time.sleep(0.2)
            self.assertEqual(f.spoken, [])
            self.assertIsNotNone(f.prepared.get("u1"))
        finally:
            f.close()

    def test_skipped_when_state_changes_mid_run(self):
        f = _Fixture()
        try:
            f.seed_history()
            # Mark busy AFTER the silence call kicks off — we approximate by
            # toggling the predicate before the worker thread runs.
            f.busy = True
            f.prepared.upsert("u1", text="prepped line", ttl_seconds=120.0)
            f.director.notify_silence("s1")
            time.sleep(0.3)
            # We never speak the prepared line because the busy guard rejects.
            self.assertEqual(f.spoken, [])
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
