"""Tests for the voice utterance merge flow.

When a user pauses mid-thought ("Hey aiko how … are you doing today"),
the endpointer commits phrase A and the LLM begins streaming. If a
partial of phrase B arrives before TTS has begun on phrase A's reply,
``SessionController`` must abort the in-flight turn, fold phrase B's
text into the existing user row, and re-run with the combined text.
Once the first TTS chunk lands the merge window closes and any further
speech falls back to the existing barge-in flow.

Tests here exercise the merge logic end-to-end via a minimal
``SessionController`` harness assembled with ``__new__`` (the real
``__init__`` is too heavyweight — it spins up Ollama clients, RAG
stores, MCP servers, and a half-dozen background workers). The
collaborators we don't care about are stubbed; the real
:class:`ChatDatabase` and the real merge methods do the heavy lifting.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from app.core.infra.chat_database import ChatDatabase
from app.core.session.session_controller import SessionController, _MergeBuffer
from app.core.session.turn_runner import TurnResult


# ── shared helpers ────────────────────────────────────────────────────────


@dataclass
class _FakeTurnRunner:
    """Stand-in for :class:`app.core.session.turn_runner.TurnRunner`.

    Records each ``run`` call (text, kwargs) and lets the test override
    the streaming behaviour: ``streaming_action`` may emit TTS chunks,
    request stop mid-stream, or just return.
    """
    runs: list[dict[str, Any]] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    streaming_action: Any = None  # callable(runner, on_tts_chunk) | None

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(
        self,
        session_key: str,
        user_text: str,
        *,
        on_token=None,
        on_tts_chunk=None,
        on_earcon=None,
        on_overlay=None,
        on_outfit=None,
        on_motion=None,
        stop_requested=None,
        resume_user_message_id=None,
    ) -> TurnResult:
        self.runs.append({
            "session_key": session_key,
            "user_text": user_text,
            "on_token": on_token,
            "on_tts_chunk": on_tts_chunk,
            "on_earcon": on_earcon,
            "on_overlay": on_overlay,
            "on_outfit": on_outfit,
            "on_motion": on_motion,
            "stop_requested": stop_requested,
            "resume_user_message_id": resume_user_message_id,
        })
        self.stop_event.clear()
        if self.streaming_action is not None:
            self.streaming_action(self, on_tts_chunk)
        aborted = self.stop_event.is_set()
        return TurnResult(
            text="" if aborted else "ok",
            reaction="neutral",
            aborted=aborted,
        )


class _FakeRealtimeSTT:
    """Drives ``process_live_capture`` end-to-end without real audio."""

    def __init__(self, transcript: str) -> None:
        self.is_available = True
        self._transcript = transcript

    def transcribe(self, wav_path: Path) -> str:
        return self._transcript


def _make_controller(
    db: ChatDatabase,
    *,
    user_id: str = "u1",
    session_id: str = "s1",
    runner: _FakeTurnRunner | None = None,
) -> SessionController:
    """Assemble a minimal :class:`SessionController` for merge tests.

    Bypasses ``__init__`` (too heavy) and patches in only the
    collaborators the merge flow actually consults. Anything irrelevant
    (RAG, prosody, scheduler quirks) is a ``MagicMock`` so attribute
    access doesn't blow up.
    """
    controller = SessionController.__new__(SessionController)
    controller._user_id = user_id
    controller._session_id = session_id
    controller._chat_db = db
    controller._turn_runner = runner if runner is not None else _FakeTurnRunner()
    controller._merge_buffer = {}
    controller._merge_lock = threading.Lock()
    controller._last_vocal_tone = None
    controller._vocal_tone_lock = threading.Lock()
    controller._remember_history = True
    controller._turn_in_progress = False
    controller._compactions_total = 0
    controller._tts_turn_start_at = 0.0
    controller._tts_turn_first_start_at = None
    controller._context_window = 8192
    controller._context_source = "test"
    controller._last_metrics = {}
    controller._metrics_history = []
    controller._stt_partial_listeners = []
    controller._backchannel_listeners = []
    controller._mood_listeners = []
    controller._memory_listeners = []
    controller._last_live_partial = {}
    controller._last_partial_broadcast_at = 0.0
    controller._live_no_speech_streak = 0
    controller._last_listen_extensions = 0
    controller._decision_trace = []
    controller._rag_prefetcher = None
    controller._prebuild_in_flight = False
    controller._listening_window_executor = None
    # Typed-mode proactive timer fields wired by the lean rewrite —
    # the merge code path now disarms / arms the timer at turn
    # boundaries, so these need real values even though the merge
    # tests don't exercise them.
    controller._typed_silence_timer = None
    controller._typed_silence_lock = threading.Lock()
    controller._user_present = True
    controller._typed_silence_armed_at = None
    controller._typed_silence_armed_budget = None
    controller._user_active_app = None
    controller._live_voice_session_active = False
    controller._scheduler = MagicMock()
    controller._backchannel_gate = MagicMock()
    controller._backchannel_gate.consider.return_value = None
    controller._earcons = MagicMock()
    controller._tts = MagicMock()
    controller._prosody = None
    controller._realtime_stt = _FakeRealtimeSTT("")
    settings = MagicMock()
    settings.tts.enabled = False  # Skip prosody/tts wiring for these tests.
    # Disable typed-mode proactive timer wiring for merge tests — they
    # exercise the chat loop end-to-end and we don't want a real
    # ``threading.Timer`` to outlive the test fixture.
    settings.agent.proactive_typed_enabled = False
    settings.agent.proactive_silence_seconds_typed = 0.0
    settings.agent.activity_awareness_enabled = False
    controller._settings = settings
    controller._proactive = MagicMock()
    # Stubs for collaborators called in the metrics tail of
    # ``chat_once_streaming``. These run after ``run()`` returns and
    # have nothing to do with the merge logic.
    controller._post_turn_inner_life = lambda **_: None
    return controller


def _seed_partial_listener_no_op(controller: SessionController) -> None:
    """No-op partial listener so ``feed_stt_partial`` exercises its loop."""
    controller._stt_partial_listeners.append(lambda _t: None)


# ── tests ─────────────────────────────────────────────────────────────────


class _TempDbCase(unittest.TestCase):
    """Base case: a disposable :class:`ChatDatabase` per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = ChatDatabase(Path(self._tmp.name) / "merge.db")

    def tearDown(self) -> None:
        try:
            conn = getattr(self._db._local, "conn", None)
            if conn is not None:
                conn.close()
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass


class ChatDatabaseUpdateMessageContentTests(_TempDbCase):
    """``ChatDatabase.update_message_content`` is the persistence half of
    the merge: it must update the row in place and report whether
    anything was matched."""

    def test_updates_existing_row(self) -> None:
        msg_id = self._db.add_message("u1:s1", "user", "Hey aiko how", 5)
        ok = self._db.update_message_content(msg_id, "Hey aiko how are you")
        self.assertTrue(ok)
        rows = self._db.get_messages("u1:s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "Hey aiko how are you")
        # Token count should have been re-estimated alongside the content.
        self.assertGreater(rows[0].token_count, 0)

    def test_unknown_id_returns_false(self) -> None:
        ok = self._db.update_message_content(99999, "anything")
        self.assertFalse(ok)

    def test_zero_id_is_noop(self) -> None:
        ok = self._db.update_message_content(0, "anything")
        self.assertFalse(ok)


class TurnRunnerResumeTests(_TempDbCase):
    """``TurnRunner.run(resume_user_message_id=N)`` must NOT insert a
    second user row — the row was already updated in place by the merge
    branch."""

    def test_resume_skips_user_message_insert(self) -> None:
        # Pre-create a "phrase A" user row to simulate the merge state.
        msg_id = self._db.add_message("u1:s1", "user", "Hey aiko how", 5)
        self._db.update_message_content(msg_id, "Hey aiko how are you")

        # Stand up a TurnRunner just enough to exercise ``_run_inner``'s
        # insert-vs-skip branch. The PromptAssembler call is mocked.
        from app.core.session.turn_runner import TurnRunner

        ollama = MagicMock()
        ollama.chat_stream.return_value = iter([])
        ollama.last_usage = MagicMock(
            prompt_tokens=10, completion_tokens=0,
            total_duration_ms=0.0, eval_duration_ms=0.0,
            prompt_eval_duration_ms=0.0, tokens_per_second=0.0,
            total_tokens=10, merge=lambda _o: ollama.last_usage,
        )
        prompt = MagicMock()
        telemetry = MagicMock()
        telemetry.compaction_triggered = False
        telemetry.prompt_tokens_estimate = 50
        telemetry.tool_tokens = 0
        prompt.assemble_with_budget.return_value = ([], telemetry)

        runner = TurnRunner(
            ollama, self._db, prompt,
            model="m", context_window=4096,
            max_tokens=128, temperature=0.7,
        )
        runner.run(
            "u1:s1", "Hey aiko how are you",
            resume_user_message_id=msg_id,
        )

        rows = self._db.get_messages("u1:s1")
        # Exactly one user row — the original A row, updated to merged.
        self.assertEqual(
            [(r.role, r.content) for r in rows],
            [("user", "Hey aiko how are you")],
        )


class FeedSttPartialEarlyAbortTests(_TempDbCase):
    """``feed_stt_partial`` must call ``request_stop`` on the merge
    buffer's runner the first time a long-enough partial fires while
    a turn is in flight (and TTS hasn't started)."""

    def _fresh_controller(self) -> tuple[SessionController, _FakeTurnRunner]:
        runner = _FakeTurnRunner()
        controller = _make_controller(self._db, runner=runner)
        return controller, runner

    def test_aborts_on_long_partial_when_window_open(self) -> None:
        controller, runner = self._fresh_controller()
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=42,
            )

        controller.feed_stt_partial("are you doing today")

        self.assertTrue(runner.stop_event.is_set())
        buf = controller._merge_buffer.get(controller.session_key)
        self.assertIsNotNone(buf)
        assert buf is not None
        self.assertTrue(buf.awaiting_phrase_b)

    def test_short_partial_does_not_abort(self) -> None:
        controller, runner = self._fresh_controller()
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=42,
            )
        # < 12 chars: ASR twitch, must not pre-emptively kill phrase A.
        controller.feed_stt_partial("uh")
        self.assertFalse(runner.stop_event.is_set())
        buf = controller._merge_buffer.get(controller.session_key)
        assert buf is not None
        self.assertFalse(buf.awaiting_phrase_b)

    def test_tts_started_locks_window(self) -> None:
        controller, runner = self._fresh_controller()
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=42,
                tts_started=True,
            )
        controller.feed_stt_partial("are you doing today now")
        self.assertFalse(runner.stop_event.is_set())

    def test_final_partial_does_not_abort(self) -> None:
        controller, runner = self._fresh_controller()
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=42,
            )
        # ``final=True`` is the "WAV committed" path; merging happens in
        # ``process_live_capture`` instead, never in ``feed_stt_partial``.
        controller.feed_stt_partial("are you doing today", final=True)
        self.assertFalse(runner.stop_event.is_set())

    def test_no_buffer_no_op(self) -> None:
        controller, runner = self._fresh_controller()
        # No buffer: idle controller, partial just bookkeeping.
        controller.feed_stt_partial("are you doing today")
        self.assertFalse(runner.stop_event.is_set())
        self.assertEqual(controller._merge_buffer, {})


class TtsStartHookTests(_TempDbCase):
    """The wrapped ``on_tts_chunk`` callback installed by
    ``chat_once_streaming`` must flip ``tts_started=True`` and clear the
    buffer on the first chunk, then forward every chunk untouched."""

    def test_first_chunk_closes_window(self) -> None:
        controller = _make_controller(self._db)
        merge_key = controller.session_key
        with controller._merge_lock:
            controller._merge_buffer[merge_key] = _MergeBuffer(
                session_key=merge_key,
                turn_runner=controller._turn_runner,  # type: ignore[arg-type]
                user_text="Hey aiko how",
                user_message_id=1,
            )

        forwarded: list[tuple[str, str]] = []

        def inner(text: str, mood: str) -> None:
            forwarded.append((text, mood))

        wrapped = controller._wrap_tts_chunk_for_merge(inner, merge_key)
        wrapped("hello there", "happy")
        wrapped("how are you", "neutral")

        # Both chunks forwarded, in order.
        self.assertEqual(
            forwarded,
            [("hello there", "happy"), ("how are you", "neutral")],
        )
        # Buffer cleared on first chunk.
        self.assertNotIn(merge_key, controller._merge_buffer)

    def test_first_chunk_with_no_inner_callback(self) -> None:
        # Chat-once-streaming may pass ``inner=None`` when TTS is off; the
        # wrapper must still flip ``tts_started`` and clear the buffer.
        controller = _make_controller(self._db)
        merge_key = controller.session_key
        with controller._merge_lock:
            controller._merge_buffer[merge_key] = _MergeBuffer(
                session_key=merge_key,
                turn_runner=controller._turn_runner,  # type: ignore[arg-type]
                user_text="Hey aiko how",
                user_message_id=1,
            )
        wrapped = controller._wrap_tts_chunk_for_merge(None, merge_key)
        wrapped("anything", "neutral")
        self.assertNotIn(merge_key, controller._merge_buffer)


class ChatOnceStreamingMergeBufferTests(_TempDbCase):
    """``chat_once_streaming`` must install a buffer for live mode and
    skip it for typed mode, and must clear it when the runner returns."""

    def test_live_mode_installs_buffer_during_run(self) -> None:
        observed: dict[str, Any] = {}

        def streaming_action(runner: _FakeTurnRunner, on_tts_chunk):
            # Snapshot the buffer state mid-stream (before TTS fires).
            controller = observed["controller"]
            with controller._merge_lock:
                buf = controller._merge_buffer.get(controller.session_key)
                observed["mid_buffer"] = (
                    None if buf is None else (buf.user_text, buf.user_message_id, buf.tts_started)
                )

        runner = _FakeTurnRunner(streaming_action=streaming_action)
        controller = _make_controller(self._db, runner=runner)
        observed["controller"] = controller

        controller.chat_once_streaming(
            user_text="Hey aiko how", mode="live",
        )

        self.assertIsNotNone(observed["mid_buffer"])
        text, msg_id, tts_started = observed["mid_buffer"]
        self.assertEqual(text, "Hey aiko how")
        self.assertGreater(msg_id, 0)
        self.assertFalse(tts_started)

        # Buffer cleared when run returns (no TTS fired in this test).
        self.assertNotIn(controller.session_key, controller._merge_buffer)
        # Single user row in DB; the runner skipped its own insert via
        # ``resume_user_message_id``.
        rows = self._db.get_messages(controller.session_key)
        self.assertEqual(
            [(r.role, r.content) for r in rows],
            [("user", "Hey aiko how")],
        )

    def test_typed_mode_does_not_install_buffer(self) -> None:
        observed: dict[str, Any] = {}

        def streaming_action(runner: _FakeTurnRunner, on_tts_chunk):
            controller = observed["controller"]
            observed["mid_buffer"] = controller._merge_buffer.get(
                controller.session_key,
            )

        runner = _FakeTurnRunner(streaming_action=streaming_action)
        controller = _make_controller(self._db, runner=runner)
        observed["controller"] = controller

        controller.chat_once_streaming(
            user_text="Hey aiko how", mode="typed",
        )

        self.assertIsNone(observed["mid_buffer"])

    def test_resume_message_id_skips_user_insert(self) -> None:
        # Seed a "phrase A" user row that the merge branch already
        # updated in place. The resumed call must not double-insert.
        msg_id = self._db.add_message(
            "u1:s1", "user", "Hey aiko how", 5,
        )
        self._db.update_message_content(msg_id, "Hey aiko how are you")

        runner = _FakeTurnRunner()
        controller = _make_controller(self._db, runner=runner)

        controller.chat_once_streaming(
            user_text="Hey aiko how are you",
            mode="live",
            _resume_message_id=msg_id,
        )

        rows = self._db.get_messages(controller.session_key)
        self.assertEqual(
            [(r.role, r.content) for r in rows],
            [("user", "Hey aiko how are you")],
        )
        # Runner was called with the resume id so it knows to skip insert.
        self.assertEqual(len(runner.runs), 1)
        self.assertEqual(runner.runs[0]["resume_user_message_id"], msg_id)


class ProcessLiveCaptureMergeTests(_TempDbCase):
    """End-to-end exercise of the merge branch in ``process_live_capture``."""

    def _empty_wav(self) -> Path:
        path = Path(self._tmp.name) / f"phrase_{time.monotonic_ns()}.wav"
        path.write_bytes(b"")
        return path

    def test_happy_merge_combines_into_single_user_row(self) -> None:
        # Phrase A: install a fake "in-flight" buffer matching the state
        # ``chat_once_streaming`` would have installed during phrase A.
        # We simulate phrase A having committed and ``feed_stt_partial``
        # having fired the early abort.
        runner = _FakeTurnRunner()
        controller = _make_controller(self._db, runner=runner)
        controller._realtime_stt = _FakeRealtimeSTT("are you doing today")
        msg_id = self._db.add_message(
            controller.session_key, "user", "Hey aiko how", 5,
        )
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=msg_id,
                awaiting_phrase_b=True,
            )

        result = controller.process_live_capture(
            wav_path=self._empty_wav(),
            capture_ms=120.0,
        )

        self.assertIsNotNone(result)
        assert result is not None
        merged_text, _response = result
        self.assertEqual(merged_text, "Hey aiko how are you doing today")

        # Single user row in DB containing the merged text.
        rows = [r for r in self._db.get_messages(controller.session_key)
                if r.role == "user"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "Hey aiko how are you doing today")

        # Runner ran once with the merged text and the resume id.
        self.assertEqual(len(runner.runs), 1)
        self.assertEqual(
            runner.runs[0]["user_text"], "Hey aiko how are you doing today",
        )
        self.assertEqual(runner.runs[0]["resume_user_message_id"], msg_id)

    def test_no_buffer_runs_normal_turn(self) -> None:
        runner = _FakeTurnRunner()
        controller = _make_controller(self._db, runner=runner)
        controller._realtime_stt = _FakeRealtimeSTT("hello there")
        # No buffer installed → standard path: a brand-new user row.

        result = controller.process_live_capture(
            wav_path=self._empty_wav(),
            capture_ms=80.0,
        )

        assert result is not None
        text, _ = result
        self.assertEqual(text, "hello there")
        rows = self._db.get_messages(controller.session_key)
        self.assertEqual(
            [(r.role, r.content) for r in rows],
            [("user", "hello there")],
        )
        self.assertEqual(len(runner.runs), 1)
        self.assertIsNotNone(runner.runs[0]["resume_user_message_id"])
        # Runner got the row id ``chat_once_streaming`` just persisted —
        # not a merge resume. Either way the runner must skip its own
        # insert.

    def test_tts_started_blocks_merge(self) -> None:
        runner = _FakeTurnRunner()
        controller = _make_controller(self._db, runner=runner)
        controller._realtime_stt = _FakeRealtimeSTT("are you doing today")
        msg_id = self._db.add_message(
            controller.session_key, "user", "Hey aiko how", 5,
        )
        # Buffer says TTS already started → standard barge-in path:
        # phrase B becomes its own user row, no merge.
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=msg_id,
                awaiting_phrase_b=True,
                tts_started=True,
            )

        result = controller.process_live_capture(
            wav_path=self._empty_wav(),
            capture_ms=120.0,
        )

        assert result is not None
        text, _ = result
        self.assertEqual(text, "are you doing today")
        rows = [r for r in self._db.get_messages(controller.session_key)
                if r.role == "user"]
        # Two separate user rows: phrase A and phrase B.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].content, "Hey aiko how")
        self.assertEqual(rows[1].content, "are you doing today")

    def test_triple_merge_combines_three_phrases(self) -> None:
        # A → abort → B → abort → C: a single combined user row "A B C".
        runner = _FakeTurnRunner()
        controller = _make_controller(self._db, runner=runner)
        merge_key = controller.session_key

        # Phrase A → standard path.
        controller._realtime_stt = _FakeRealtimeSTT("Hey aiko how")
        controller.process_live_capture(
            wav_path=self._empty_wav(), capture_ms=80.0,
        )
        # Capture A's id and simulate the abort flag.
        rows = [r for r in self._db.get_messages(merge_key) if r.role == "user"]
        self.assertEqual(len(rows), 1)
        msg_id_a = rows[0].id
        with controller._merge_lock:
            controller._merge_buffer[merge_key] = _MergeBuffer(
                session_key=merge_key,
                turn_runner=runner,
                user_text="Hey aiko how",
                user_message_id=msg_id_a,
                awaiting_phrase_b=True,
            )

        # Phrase B → merges into A.
        controller._realtime_stt = _FakeRealtimeSTT("are you doing")
        controller.process_live_capture(
            wav_path=self._empty_wav(), capture_ms=80.0,
        )
        rows = [r for r in self._db.get_messages(merge_key) if r.role == "user"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "Hey aiko how are you doing")
        # Re-arm for phrase C: in real life the merged restart's
        # ``chat_once_streaming`` would have installed a fresh buffer
        # while phrase B's run was streaming, and ``feed_stt_partial``
        # would have flipped ``awaiting_phrase_b`` before TTS started.
        # The fake runner returns synchronously, so we install the
        # equivalent state here to simulate that race.
        with controller._merge_lock:
            controller._merge_buffer[merge_key] = _MergeBuffer(
                session_key=merge_key,
                turn_runner=runner,
                user_text="Hey aiko how are you doing",
                user_message_id=msg_id_a,
                awaiting_phrase_b=True,
            )

        # Phrase C → merges into A+B.
        controller._realtime_stt = _FakeRealtimeSTT("today")
        controller.process_live_capture(
            wav_path=self._empty_wav(), capture_ms=80.0,
        )
        rows = [r for r in self._db.get_messages(merge_key) if r.role == "user"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "Hey aiko how are you doing today")


class MergeBufferLifecycleTests(_TempDbCase):
    """Buffer must be cleared on session change, on full clear, and on
    shutdown so a stale runner reference can't be ``request_stop()``-ed
    against a torn-down controller."""

    def test_switch_session_clears_buffer(self) -> None:
        controller = _make_controller(self._db)
        old_key = controller.session_key
        with controller._merge_lock:
            controller._merge_buffer[old_key] = _MergeBuffer(
                session_key=old_key,
                turn_runner=controller._turn_runner,  # type: ignore[arg-type]
                user_text="Hey aiko how",
                user_message_id=1,
            )

        controller.switch_session("s2")
        self.assertEqual(controller._merge_buffer, {})

    def test_clear_conversation_memory_clears_buffer(self) -> None:
        controller = _make_controller(self._db)
        with controller._merge_lock:
            controller._merge_buffer[controller.session_key] = _MergeBuffer(
                session_key=controller.session_key,
                turn_runner=controller._turn_runner,  # type: ignore[arg-type]
                user_text="Hey aiko how",
                user_message_id=1,
            )
        controller.clear_conversation_memory()
        self.assertEqual(controller._merge_buffer, {})


if __name__ == "__main__":
    unittest.main()
