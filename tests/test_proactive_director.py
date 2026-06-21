"""Tests for ProactiveDirector prepared-nudge fast path (Phase 4c)."""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.prepared_nudge import PreparedNudgeStore
from app.core.proactive.proactive_director import (
    ProactiveDirector,
    _PROACTIVE_HINT_TYPED,
)


class _FakePromptAssembler:
    def build(self, session_key, hint, *, context_window, response_budget):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": hint}]


class _FakeOllama:
    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, *, model, timeout_seconds, options, format_json, **kwargs):
        self.calls += 1

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 4

        return "Sure thing!", _Usage()


class _Fixture:
    def __init__(self, *, typed_eligible: bool = True, typed_tts: bool = False):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.prepared = PreparedNudgeStore(self.db)
        self.spoken: list[tuple[str, str]] = []
        self.notified: list[tuple[str, str]] = []
        self.live = True
        self.busy = False
        self.typed_eligible = typed_eligible
        self.typed_tts = typed_tts
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
            typed_tts_enabled=lambda: self.typed_tts,
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

    def _wait_for_speak(self, fixture: _Fixture, timeout: float = 2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if fixture.spoken:
                return
            time.sleep(0.02)
        raise AssertionError("director never spoke")

    def test_typed_prepared_speaks_when_tts_enabled(self):
        f = _Fixture(typed_tts=True)
        try:
            f.seed_history()
            f.prepared.upsert(
                "u1",
                text="Hey, picked up the migration thread?",
                source_kind="agenda",
                ttl_seconds=120.0,
            )
            f.director.notify_typed_silence("s1")
            self._wait_for_speak(f)
            # Opt-in: the prepared typed nudge is both shown AND spoken.
            self.assertEqual(len(f.notified), 1)
            self.assertEqual(len(f.spoken), 1)
            self.assertIn("migration", f.spoken[0][0])
            stats = f.director.stats()
            self.assertEqual(stats["typed_prepared_consumed"], 1)
            self.assertEqual(stats["typed_llm_path_used"], 0)
        finally:
            f.close()

    def test_typed_llm_speaks_when_tts_enabled(self):
        f = _Fixture(typed_tts=True)
        try:
            f.seed_history()
            f.director.notify_typed_silence("s1")
            self._wait_for_speak(f)
            self.assertEqual(f.ollama.calls, 1)
            self.assertEqual(len(f.spoken), 1)
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


class PreparedLineGuardBackstopTests(unittest.TestCase):
    """Speak-time guard: a leaked third-person prepared line is rejected
    and the director degrades to its own safe LLM turn."""

    _LEAK = "Notices that he warms up after coffee"

    def _wait(self, predicate, *, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_voice_rejects_leaky_prepared_and_uses_llm(self):
        f = _Fixture()
        try:
            f.seed_history()
            f.prepared.upsert(
                "u1", text=self._LEAK, source_kind="reflection",
                ttl_seconds=120.0,
            )
            f.director.notify_silence("s1")
            self.assertTrue(self._wait(lambda: bool(f.spoken)))
            # The leak was NOT spoken; the safe LLM turn was used instead.
            self.assertEqual(len(f.spoken), 1)
            self.assertNotIn("Notices", f.spoken[0][0])
            self.assertEqual(f.spoken[0][0], "Sure thing!")
            self.assertEqual(f.ollama.calls, 1)
            stats = f.director.stats()
            self.assertEqual(stats["prepared_consumed"], 0)
            self.assertEqual(stats["llm_path_used"], 1)
        finally:
            f.close()

    def test_typed_rejects_leaky_prepared_and_uses_llm(self):
        f = _Fixture()
        try:
            f.seed_history()
            f.prepared.upsert(
                "u1", text=self._LEAK, source_kind="reflection",
                ttl_seconds=120.0,
            )
            f.director.notify_typed_silence("s1")
            self.assertTrue(self._wait(lambda: bool(f.notified)))
            self.assertEqual(len(f.notified), 1)
            self.assertNotIn("Notices", f.notified[0][1])
            self.assertEqual(f.notified[0][1], "Sure thing!")
            self.assertEqual(f.ollama.calls, 1)
            stats = f.director.stats()
            self.assertEqual(stats["typed_prepared_consumed"], 0)
            self.assertEqual(stats["typed_llm_path_used"], 1)
        finally:
            f.close()


class TaskEscalationTests(unittest.TestCase):
    """Brain-orchestration chunk 6: notify_task_escalation.

    Pins the contract for the task-driven proactive entry point:

    * Bypasses both cooldown clocks — task results / questions are
      event-driven (the escalation manager already ran a per-cue
      silence window), not time-driven; a recent voice/typed
      proactive must not silence them.
    * Bypasses the vent-dialogue-act skip — the user asked for the
      task earlier, surfacing the result is the follow-up they
      requested.
    * Still honours the busy gate (chat in progress) and the
      inflight gate (one proactive at a time).
    * Picks voice vs typed by ``is_live_mode``: voice routes
      through ``_run_safe``, typed through ``_run_typed_safe``.
    """

    def _wait_for(self, predicate, *, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_voice_mode_speaks(self) -> None:
        f = _Fixture()
        try:
            f.seed_history()
            f.live = True
            f.director.notify_task_escalation("s1")
            self.assertTrue(self._wait_for(lambda: bool(f.spoken)))
            self.assertEqual(len(f.spoken), 1)
            # Voice mode notifies as well (it always does for TTS-
            # backed proactive nudges).
            self.assertEqual(f.ollama.calls, 1)
        finally:
            f.close()

    def test_typed_mode_text_only(self) -> None:
        f = _Fixture()
        try:
            f.seed_history()
            f.live = False
            f.director.notify_task_escalation("s1")
            self.assertTrue(
                self._wait_for(lambda: bool(f.notified))
            )
            # Typed mode never speaks aloud — same as
            # notify_typed_silence.
            self.assertEqual(f.spoken, [])
            self.assertEqual(len(f.notified), 1)
            self.assertEqual(f.ollama.calls, 1)
        finally:
            f.close()

    def test_bypasses_voice_cooldown(self) -> None:
        # Run a voice proactive first to set the cooldown clock,
        # then immediately fire a task_escalation — the second
        # call must NOT be silenced by the cooldown gate.
        f = _Fixture()
        try:
            f.seed_history()
            # Make the cooldown big so the regular notify_silence
            # path would refuse.
            f.director._cooldown = 600.0
            # Force the cooldown clock by setting last-run to "now".
            f.director._last_run_monotonic = time.monotonic()
            f.live = True
            f.director.notify_task_escalation("s1")
            self.assertTrue(self._wait_for(lambda: bool(f.spoken)))
            self.assertEqual(len(f.spoken), 1)
        finally:
            f.close()

    def test_skipped_when_busy(self) -> None:
        f = _Fixture()
        try:
            f.seed_history()
            f.busy = True
            f.director.notify_task_escalation("s1")
            time.sleep(0.2)
            self.assertEqual(f.spoken, [])
            self.assertEqual(f.notified, [])
        finally:
            f.close()

    def test_skipped_when_inflight_voice(self) -> None:
        # If a voice proactive is already in flight, a task
        # escalation must NOT stack on top.
        f = _Fixture()
        try:
            f.seed_history()
            with f.director._lock:
                f.director._inflight = True
            f.live = True
            f.director.notify_task_escalation("s1")
            time.sleep(0.2)
            self.assertEqual(f.spoken, [])
        finally:
            f.close()

    def test_skipped_when_inflight_typed(self) -> None:
        f = _Fixture()
        try:
            f.seed_history()
            with f.director._lock:
                f.director._typed_inflight = True
            f.live = False
            f.director.notify_task_escalation("s1")
            time.sleep(0.2)
            self.assertEqual(f.notified, [])
        finally:
            f.close()

    def test_empty_session_key_is_noop(self) -> None:
        f = _Fixture()
        try:
            f.seed_history()
            f.director.notify_task_escalation("")
            time.sleep(0.1)
            self.assertEqual(f.spoken, [])
            self.assertEqual(f.notified, [])
        finally:
            f.close()

    def test_typed_reports_even_when_not_eligible(self) -> None:
        # The bug fix: a finished task result must report even when the
        # typed-proactive eligibility gate is closed (e.g. the desktop
        # window isn't focused). The boredom-nudge path stays gated; the
        # forced escalation path bypasses eligibility.
        f = _Fixture(typed_eligible=False)
        try:
            f.seed_history()
            f.live = False
            f.director.notify_task_escalation("s1")
            self.assertTrue(self._wait_for(lambda: bool(f.notified)))
            self.assertEqual(len(f.notified), 1)
            self.assertEqual(f.ollama.calls, 1)
            self.assertEqual(f.spoken, [])
        finally:
            f.close()

    def test_voice_reports_even_when_live_flips_off(self) -> None:
        # Voice escalation dispatched while live, then live flips off
        # mid-run: the forced path still delivers (only the busy gate
        # defers it), where the boredom path would discard.
        f = _Fixture()
        try:
            f.seed_history()
            f.live = True
            f.director.notify_task_escalation("s1")
            f.live = False
            self.assertTrue(self._wait_for(lambda: bool(f.spoken)))
            self.assertEqual(len(f.spoken), 1)
        finally:
            f.close()

    def test_force_skips_prepared_nudge(self) -> None:
        # A queued prepared (boredom) nudge must NOT pre-empt a task
        # result: the forced path takes the cue-bearing LLM turn so the
        # result is what surfaces, and the prepared nudge is left intact.
        f = _Fixture()
        try:
            f.seed_history()
            f.live = False
            f.prepared.upsert(
                "u1",
                text="random boredom thought",
                source_kind="agenda",
                ttl_seconds=120.0,
            )
            f.director.notify_task_escalation("s1")
            self.assertTrue(self._wait_for(lambda: bool(f.notified)))
            self.assertEqual(f.ollama.calls, 1)
            self.assertNotIn(
                "random boredom thought",
                f.notified[0][1],
            )
            # The prepared nudge was not consumed by the forced path.
            self.assertIsNotNone(f.prepared.get("u1"))
            stats = f.director.stats()
            self.assertEqual(stats["typed_prepared_consumed"], 0)
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
