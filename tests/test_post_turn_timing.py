"""P16 -- ``post_turn_ms`` instrumentation on the post-turn cascade.

The cascade runs *after* the reply has finished streaming and after
``embedder.end_turn()``, so none of its cost showed up in any existing
metric. These tests pin the three things that make the number
trustworthy: it measures the cascade (not the turn), it survives a
cascade that raises, and it lands on the metrics dict.

Harness follows the ``SessionController.__new__`` pattern used by
``test_voice_merge.py`` -- the real ``__init__`` spins up Ollama clients,
RAG stores and background workers.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.core.infra.chat_database import ChatDatabase
from app.core.session.session_controller import SessionController
from app.core.session.turn_runner import TurnResult


class _FakeTurnRunner:
    def request_stop(self) -> None:
        pass

    def run(self, session_key, user_text, **_kw) -> TurnResult:
        return TurnResult(text="ok", reaction="neutral")


def _make_controller(db: ChatDatabase) -> SessionController:
    controller = SessionController.__new__(SessionController)
    controller._user_id = "u1"
    controller._session_id = "s1"
    controller._chat_db = db
    controller._turn_runner = _FakeTurnRunner()
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
    controller._last_system_prompt = SessionController._zero_system_prompt()
    controller._stt_partial_listeners = []
    controller._backchannel_listeners = []
    controller._mood_listeners = []
    controller._memory_listeners = []
    controller._metrics_listeners = []
    controller._last_live_partial = {}
    controller._last_partial_broadcast_at = 0.0
    controller._live_no_speech_streak = 0
    controller._last_listen_extensions = 0
    controller._decision_trace = []
    controller._rag_prefetcher = None
    controller._prebuild_in_flight = False
    controller._listening_window_executor = None
    controller._typed_silence_timer = None
    controller._typed_silence_lock = threading.Lock()
    controller._user_present = True
    controller._typed_silence_armed_at = None
    controller._typed_silence_armed_budget = None
    controller._user_active_app = None
    controller._live_voice_session_active = False
    controller._current_turn_gestures = []
    controller._active_turn_attachments = []
    controller._scheduler = MagicMock()
    controller._backchannel_gate = MagicMock()
    controller._backchannel_gate.consider.return_value = None
    controller._earcons = MagicMock()
    controller._tts = MagicMock()
    controller._prosody = None
    controller._realtime_stt = None
    settings = MagicMock()
    settings.tts.enabled = False
    settings.agent.proactive_typed_enabled = False
    settings.agent.proactive_silence_seconds_typed = 0.0
    settings.agent.activity_awareness_enabled = False
    controller._settings = settings
    controller._proactive = MagicMock()
    return controller


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = ChatDatabase(Path(self._tmp.name) / "pt.db")
        self.controller = _make_controller(self._db)

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


class PostTurnTimingTests(_Case):
    def test_measures_the_cascade_not_the_turn(self) -> None:
        self.controller._post_turn_inner_life = lambda **_: time.sleep(0.05)
        self.controller.chat_once_streaming(user_text="hi", mode="typed")
        metrics = self.controller.get_last_metrics()
        self.assertGreaterEqual(metrics["post_turn_ms"], 45.0)
        # And it must not have been folded into the turn total, which was
        # measured before the cascade ran.
        self.assertLess(metrics["total_ms"], metrics["post_turn_ms"])

    def test_a_cheap_cascade_reports_near_zero(self) -> None:
        self.controller._post_turn_inner_life = lambda **_: None
        self.controller.chat_once_streaming(user_text="hi", mode="typed")
        self.assertLess(self.controller.get_last_metrics()["post_turn_ms"], 50.0)

    def test_a_raising_cascade_is_still_timed(self) -> None:
        # A cascade that fails *slowly* is still latency the user waited
        # through, so the timer has to wrap the except path too.
        def _boom(**_kw):
            time.sleep(0.03)
            raise RuntimeError("cascade broke")

        self.controller._post_turn_inner_life = _boom
        self.controller.chat_once_streaming(user_text="hi", mode="typed")
        self.assertGreaterEqual(
            self.controller.get_last_metrics()["post_turn_ms"], 25.0,
        )

    def test_metric_is_always_present(self) -> None:
        self.controller._post_turn_inner_life = lambda **_: None
        self.controller.chat_once_streaming(user_text="hi", mode="typed")
        self.assertIn("post_turn_ms", self.controller.get_last_metrics())


class PostTurnLoggingTests(_Case):
    def test_slow_cascade_escalates_to_info(self) -> None:
        from app.core.session import chat_turn_mixin as ctm

        self.controller._post_turn_inner_life = lambda **_: time.sleep(0.02)
        with mock_slow_threshold(ctm, 10.0):
            with self.assertLogs("app.session", level="INFO") as logs:
                self.controller.chat_once_streaming(user_text="hi", mode="typed")
        self.assertTrue(
            any("post-turn done:" in line for line in logs.output),
            f"expected an INFO post-turn line, got {logs.output}",
        )

    def test_fast_cascade_stays_at_debug(self) -> None:
        from app.core.session import chat_turn_mixin as ctm

        self.controller._post_turn_inner_life = lambda **_: None
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("app.session")
        handler = _Capture(level=logging.DEBUG)
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with mock_slow_threshold(ctm, 5000.0):
                self.controller.chat_once_streaming(user_text="hi", mode="typed")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        lines = [r for r in records if "post-turn done:" in r.getMessage()]
        self.assertTrue(lines, "the line should still be emitted at DEBUG")
        self.assertTrue(all(r.levelno == logging.DEBUG for r in lines))


class mock_slow_threshold:
    """Temporarily move the DEBUG->INFO escalation threshold."""

    def __init__(self, module, value: float) -> None:
        self._module = module
        self._value = value
        self._prev = module._POST_TURN_SLOW_MS

    def __enter__(self):
        self._module._POST_TURN_SLOW_MS = self._value
        return self

    def __exit__(self, *exc):
        self._module._POST_TURN_SLOW_MS = self._prev
        return False


if __name__ == "__main__":
    unittest.main()
