"""Tests for ProactiveDirector prepared-nudge fast path (Phase 4c)."""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from app.core.chat_database import ChatDatabase
from app.core.prepared_nudge import PreparedNudgeStore
from app.core.proactive_director import (
    ProactiveDirector,
    _PROACTIVE_HINT_TYPED,
)


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
    def __init__(self, *, typed_eligible: bool = True):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.prepared = PreparedNudgeStore(self.db)
        self.spoken: list[tuple[str, str]] = []
        self.notified: list[tuple[str, str]] = []
        self.live = True
        self.busy = False
        self.typed_eligible = typed_eligible
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
            cooldown_seconds_typed=0.01,
            is_typed_eligible=lambda: self.typed_eligible,
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


class TypedProactivePathTests(unittest.TestCase):
    """Phase C1 typed-mode coverage: prepared / LLM / no-TTS / cooldown."""

    def _wait_for_notify(self, fixture: _Fixture, timeout: float = 2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if fixture.notified:
                return
            time.sleep(0.02)
        raise AssertionError("director never produced a typed nudge")

    def test_typed_prepared_fast_path_skips_tts(self):
        f = _Fixture()
        try:
            f.seed_history()
            f.prepared.upsert(
                "u1",
                text="Hey, picked up the migration thread?",
                source_kind="agenda",
                ttl_seconds=120.0,
            )
            f.director.notify_typed_silence("s1")
            self._wait_for_notify(f)
            # Notify happened (UI bubble) but TTS never did — the whole
            # point of typed mode is text-only.
            self.assertEqual(len(f.notified), 1)
            self.assertEqual(f.notified[0][0], "Assistant (proactive)")
            self.assertEqual(f.spoken, [])
            self.assertEqual(f.ollama.calls, 0)
            stats = f.director.stats()
            self.assertEqual(stats["typed_prepared_consumed"], 1)
            self.assertEqual(stats["typed_llm_path_used"], 0)
        finally:
            f.close()

    def test_typed_falls_back_to_llm_with_typed_hint(self):
        f = _Fixture()
        try:
            f.seed_history()

            captured_hint: list[str] = []

            class _CapturingPrompt:
                def build(
                    self, session_key, hint, *, context_window, response_budget,
                ):
                    captured_hint.append(hint)
                    return [{"role": "system", "content": "sys"}]

            f.director._prompt = _CapturingPrompt()
            f.director.notify_typed_silence("s1")
            self._wait_for_notify(f)
            self.assertEqual(f.ollama.calls, 1)
            self.assertEqual(captured_hint, [_PROACTIVE_HINT_TYPED])
            # Confirm we did NOT call the voice-mode hint by accident.
            self.assertNotIn(
                "quiet for a moment", _PROACTIVE_HINT_TYPED,
            )
            # Typed mode never speaks aloud.
            self.assertEqual(f.spoken, [])
            stats = f.director.stats()
            self.assertEqual(stats["typed_llm_path_used"], 1)
        finally:
            f.close()

    def test_typed_skipped_when_not_eligible(self):
        f = _Fixture(typed_eligible=False)
        try:
            f.seed_history()
            f.director.notify_typed_silence("s1")
            time.sleep(0.2)
            self.assertEqual(f.notified, [])
            self.assertEqual(f.ollama.calls, 0)
        finally:
            f.close()

    def test_typed_cooldown_independent_from_voice(self):
        # After a voice-mode run consumes its cooldown clock, the typed
        # cooldown should still be untouched. The director runs both
        # paths sequentially and we confirm the typed counter still
        # increments cleanly.
        f = _Fixture()
        try:
            f.seed_history()
            f.director.notify_silence("s1")
            # Wait for voice path to land.
            end = time.monotonic() + 2.0
            while time.monotonic() < end and not f.spoken:
                time.sleep(0.02)
            self.assertEqual(len(f.spoken), 1)
            voice_stats = f.director.stats()
            # Now fire a typed nudge — independent cooldown lets it
            # through despite the voice cooldown clock having moved.
            f.director.notify_typed_silence("s1")
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                stats = f.director.stats()
                if (
                    stats["typed_llm_path_used"]
                    + stats["typed_prepared_consumed"]
                    > 0
                ):
                    break
                time.sleep(0.02)
            stats = f.director.stats()
            self.assertGreaterEqual(
                stats["typed_prepared_consumed"]
                + stats["typed_llm_path_used"],
                1,
            )
            # Voice path counters didn't move backwards either.
            self.assertEqual(
                stats["llm_path_used"], voice_stats["llm_path_used"],
            )
            # And typed path didn't auto-speak via TTS.
            self.assertEqual(len(f.spoken), 1)
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
