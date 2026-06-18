"""Unit tests for :class:`TaskOrchestrationMixin` — chunk 5.

The mixin owns the wiring between the brain queue, task store,
orchestrator, cue store, and escalation manager. These tests
exercise the mixin through a minimal stub-host class so we don't
need to spin up a real :class:`SessionController`.

Coverage:

* :meth:`_init_task_orchestration` populates every component when
  ``agent.tasks_enabled`` is True; with the master switch off it
  installs the disabled stub and skips component construction.
* :meth:`_shutdown_task_orchestration` is idempotent + exception-
  safe.
* Brain-loop handlers (``task_result``, ``task_input_needed``,
  ``task_progress``, ``proactive``) route into the cue store +
  escalation manager correctly.
* The free-to-speak predicate respects the host's
  ``_turn_in_progress`` flag and ``_tts.is_active()`` callable.
* The escalation fire path's proactive-enqueue hook lands on the
  brain queue as a :class:`ProactiveEvent` with the right shape.
* :meth:`drain_task_cues_for_render` renders the parked cues +
  cancels their escalation timers so a cue surfaced naturally
  doesn't also escalate as a proactive.
* Boot recovery surfaces stranded ``running`` rows as
  ``interrupted`` on init.
* The :meth:`task_orchestration_state` debug surface returns the
  expected shape in both enabled and disabled modes.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from app.core.brain import (
    KIND_PROACTIVE,
    KIND_TASK_INPUT_NEEDED,
    KIND_TASK_PROGRESS,
    KIND_TASK_RESULT,
    KIND_USER_MESSAGE,
    ProactiveEvent,
    TaskInputNeededEvent,
    TaskProgressEvent,
    TaskResultEvent,
    UserMessageEvent,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import load_settings
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.core.tasks import (
    CUE_KIND_RESULT,
    STATUS_AWAITING_INPUT,
    STATUS_RUNNING,
    TaskStore,
)


_DEADLINE_S = 2.0


def _wait_for(predicate, *, deadline_s: float = _DEADLINE_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _FakeTts:
    """Minimal stand-in for ``SessionController._tts`` — just an
    ``is_active()`` method whose return value the test can flip."""

    def __init__(self, active: bool = False) -> None:
        self.active = active

    def is_active(self) -> bool:
        return bool(self.active)


class _Host(TaskOrchestrationMixin):
    """Stub host class that satisfies the mixin's read contract.

    The mixin reads exactly five host attributes:
    ``_chat_db``, ``_user_id``, ``_turn_in_progress``, ``_tts``,
    ``_last_user_activity_at``, ``_settings``. Anything else (the
    real :class:`SessionController`'s 100+ attributes) is irrelevant
    here.

    Chunk 7 adds two more contract points used by the
    ``user_message`` handler + :meth:`enqueue_user_message`:

    * ``chat_once_streaming(*, user_text, mode)`` — the controller's
      turn entry point. Stub records every call + returns a
      configurable reply or raises a pre-set exception.
    * ``session_key`` property — used by :meth:`enqueue_user_message`
      to stamp the event. Defaults to ``_user_id``.
    """

    def __init__(
        self,
        *,
        chat_db: ChatDatabase,
        settings: Any,
        user_id: str = "test-user",
    ) -> None:
        self._chat_db = chat_db
        self._settings = settings
        self._user_id = user_id
        self._turn_in_progress: bool = False
        self._tts = _FakeTts(active=False)
        self._last_user_activity_at: float = -float("inf")
        # Chunk 7: per-stub recording for chat_once_streaming.
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_reply: str = "stubbed reply"
        self.chat_raise: Exception | None = None
        # Per-call TTS snapshot so the test can verify ``skip_tts``
        # actually flipped the flag before the call.
        self.tts_seen_during_call: list[bool] = []

    @property
    def session_key(self) -> str:
        return f"session-{self._user_id}"

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        mode: str = "typed",
        on_token: Any = None,
        on_generation_status: Any = None,
        stop_requested: Any = None,
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
        _resume_message_id: int | None = None,
        **_extra: Any,
    ) -> str:
        # Snapshot the live ``tts.enabled`` so the skip_tts test can
        # assert the handler actually flipped it for the duration of
        # the call, then restored it.
        tts_settings = getattr(self._settings, "tts", None)
        tts_enabled = bool(getattr(tts_settings, "enabled", True))
        self.tts_seen_during_call.append(tts_enabled)
        self.chat_calls.append(
            {
                "user_text": user_text,
                "mode": mode,
                "tts_enabled": tts_enabled,
                "on_token": on_token,
                "on_generation_status": on_generation_status,
                "stop_requested": stop_requested,
                # Chunk 11: voice-only metadata the handler threads in.
                # Typed / MCP paths leave these at default; voice paths
                # populate them via the ``UserMessageEvent`` fields.
                "capture_ms": float(capture_ms),
                "stt_ms": float(stt_ms),
                "_resume_message_id": _resume_message_id,
            }
        )
        # Chunk 8: exercise the callbacks the handler threaded in,
        # so tests can verify they survived the queue hop. The
        # stub fires a token + a status + a stop check then
        # returns the configured reply.
        if on_generation_status is not None:
            try:
                on_generation_status("generating...")
            except Exception:
                pass
        if on_token is not None:
            try:
                on_token("hello")
            except Exception:
                pass
        if stop_requested is not None:
            try:
                stop_requested()
            except Exception:
                pass
        if self.chat_raise is not None:
            raise self.chat_raise
        return self.chat_reply


class _Fixture:
    """Per-test temp-dir + chat-db + settings.

    Owns its own SQLite connection so Windows file-locking releases
    cleanly before the temp dir gets cleaned up. The mixin lifecycle
    methods are exposed via ``init_host`` / ``shutdown_host`` for
    convenience.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self.db_path = self.path / "chat.db"
        self.chat_db = ChatDatabase(self.db_path)
        self.settings = load_settings(None)

    def host(self, **settings_overrides: Any) -> _Host:
        """Construct a fresh :class:`_Host` with the given settings overrides.

        The agent-level fields are replaced via ``dataclasses.replace``
        so each test can dial down (e.g.) the escalation windows to
        sub-second without mutating the shared settings.
        """
        if settings_overrides:
            agent = dataclasses.replace(
                self.settings.agent, **settings_overrides
            )
            settings = dataclasses.replace(self.settings, agent=agent)
        else:
            settings = self.settings
        return _Host(chat_db=self.chat_db, settings=settings)

    def cleanup(self) -> None:
        # ``ChatDatabase`` doesn't have an explicit ``close`` — its
        # per-thread connection lives on ``_local.conn``. Drop it
        # so Windows can delete the underlying tempfile.
        conn = getattr(self.chat_db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.chat_db._local.conn = None  # type: ignore[union-attr]
        try:
            self._tmp.cleanup()
        except PermissionError:
            # Best effort — other threads may still be releasing
            # their per-thread connections.
            pass


class InitLifecycleTests(unittest.TestCase):
    """Pin :meth:`_init_task_orchestration` semantics."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_init_wires_every_component(self) -> None:
        host = self.fx.host()
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        self.assertTrue(host._task_orchestration_inited)
        self.assertTrue(host._task_orchestration_enabled)
        self.assertIsNotNone(host._brain_queue)
        self.assertIsNotNone(host._brain_loop)
        self.assertIsNotNone(host._task_store)
        self.assertIsNotNone(host._task_orchestrator)
        self.assertIsNotNone(host._task_cue_store)
        self.assertIsNotNone(host._task_escalation_manager)
        self.assertTrue(host._brain_loop.is_running())

    def test_init_is_idempotent(self) -> None:
        host = self.fx.host()
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        first_loop = host._brain_loop
        host._init_task_orchestration()
        self.assertIs(host._brain_loop, first_loop)

    def test_master_switch_off_installs_disabled_stub(self) -> None:
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()

        self.assertTrue(host._task_orchestration_inited)
        self.assertFalse(host._task_orchestration_enabled)
        self.assertIsNone(host._brain_queue)
        self.assertIsNone(host._brain_loop)
        self.assertIsNone(host._task_store)
        self.assertIsNone(host._task_orchestrator)
        self.assertIsNone(host._task_cue_store)
        self.assertIsNone(host._task_escalation_manager)

    def test_public_properties_match_state(self) -> None:
        host = self.fx.host()
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        self.assertIs(host.task_orchestrator, host._task_orchestrator)
        self.assertIs(host.task_cue_store, host._task_cue_store)
        self.assertIs(host.brain_loop, host._brain_loop)

    def test_disabled_properties_return_none(self) -> None:
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()

        self.assertIsNone(host.task_orchestrator)
        self.assertIsNone(host.task_cue_store)
        self.assertIsNone(host.brain_loop)


class ShutdownLifecycleTests(unittest.TestCase):
    """Pin :meth:`_shutdown_task_orchestration` semantics."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_shutdown_stops_brain_loop(self) -> None:
        host = self.fx.host()
        host._init_task_orchestration()
        self.assertTrue(host._brain_loop.is_running())

        host._shutdown_task_orchestration()
        self.assertFalse(host._brain_loop.is_running())
        self.assertFalse(host._task_orchestration_inited)

    def test_shutdown_when_not_initialised_is_noop(self) -> None:
        host = self.fx.host()
        host._shutdown_task_orchestration()
        self.assertFalse(getattr(host, "_task_orchestration_inited", False))

    def test_shutdown_when_disabled_is_noop(self) -> None:
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()
        host._shutdown_task_orchestration()
        self.assertFalse(host._task_orchestration_inited)

    def test_shutdown_is_idempotent(self) -> None:
        host = self.fx.host()
        host._init_task_orchestration()
        host._shutdown_task_orchestration()
        host._shutdown_task_orchestration()


class FreeToSpeakPredicateTests(unittest.TestCase):
    """Pin the predicate's read of host state."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()

    def test_default_clear(self) -> None:
        self.assertTrue(self.host._brain_loop_free_to_speak())

    def test_turn_in_progress_blocks(self) -> None:
        self.host._turn_in_progress = True
        self.assertFalse(self.host._brain_loop_free_to_speak())

    def test_tts_active_blocks(self) -> None:
        self.host._tts.active = True
        self.assertFalse(self.host._brain_loop_free_to_speak())

    def test_both_clear_passes(self) -> None:
        self.host._turn_in_progress = False
        self.host._tts.active = False
        self.assertTrue(self.host._brain_loop_free_to_speak())

    def test_tts_missing_fails_open(self) -> None:
        # A partially-initialised host without _tts should still
        # report free-to-speak — the gate is permissive by design.
        del self.host._tts
        self.assertTrue(self.host._brain_loop_free_to_speak())

    def test_tts_raising_treated_as_inactive(self) -> None:
        class _Boom:
            def is_active(self):
                raise RuntimeError("boom")

        self.host._tts = _Boom()
        self.assertTrue(self.host._brain_loop_free_to_speak())


class LastUserMessageAtTests(unittest.TestCase):
    """Pin the activity-timestamp reader."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()

    def test_default_is_neg_inf(self) -> None:
        # Default _Host stub initialises to -inf.
        self.assertEqual(
            self.host._task_last_user_message_at(), -float("inf"),
        )

    def test_reads_host_field(self) -> None:
        self.host._last_user_activity_at = 12345.0
        self.assertEqual(
            self.host._task_last_user_message_at(), 12345.0,
        )

    def test_missing_field_returns_neg_inf(self) -> None:
        del self.host._last_user_activity_at
        self.assertEqual(
            self.host._task_last_user_message_at(), -float("inf"),
        )

    def test_garbage_value_returns_neg_inf(self) -> None:
        self.host._last_user_activity_at = "not-a-number"  # type: ignore[assignment]
        self.assertEqual(
            self.host._task_last_user_message_at(), -float("inf"),
        )


class HandlerDispatchTests(unittest.TestCase):
    """Pin the four registered brain-loop handlers."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        # Hold the free-to-speak gate closed so an armed escalation
        # timer re-arms instead of firing — the cue stays parked +
        # pending for the park-side assertions below (the timed
        # windows are gone; an open gate would fire immediately).
        self.host._brain_loop_free_to_speak = lambda: False  # type: ignore[method-assign]
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_task_result_parks_cue(self) -> None:
        event = TaskResultEvent(
            task_id="42",
            session_key="test-user",
            status="done",
            title="file_search",
            result_summary="found 3 matches",
            error=None,
            notify_aiko=True,
        )
        self.host._on_task_result_event(event)

        cues = self.host._task_cue_store.snapshot()
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].task_id, "42")
        self.assertEqual(cues[0].kind, CUE_KIND_RESULT)
        self.assertEqual(cues[0].status, "done")
        self.assertEqual(cues[0].summary, "found 3 matches")

    def test_task_result_arms_escalation(self) -> None:
        event = TaskResultEvent(task_id="t1", session_key="test-user")
        self.host._on_task_result_event(event)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 1,
        )

    def test_task_result_with_notify_aiko_false_skips_park(self) -> None:
        event = TaskResultEvent(
            task_id="silent",
            session_key="test-user",
            notify_aiko=False,
        )
        self.host._on_task_result_event(event)
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 0,
        )

    def test_task_result_wrong_type_is_noop(self) -> None:
        self.host._on_task_result_event("not-an-event")  # type: ignore[arg-type]
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)

    def test_task_input_needed_is_ui_only(self) -> None:
        # Input-needed is UI-only: the TaskStrip surfaces the
        # awaiting_input chip via the orchestrator's listener, so the
        # brain-loop handler parks no chat cue and arms no escalation.
        event = TaskInputNeededEvent(
            task_id="ask1",
            session_key="test-user",
            prompt="which file?",
            options=("a.txt", "b.txt"),
        )
        with self.assertLogs("app.session", level="INFO") as captured:
            self.host._on_task_input_needed_event(event)
        messages = [rec.getMessage() for rec in captured.records]
        self.assertTrue(
            any("task_input_needed UI-only" in m for m in messages),
            f"expected UI-only INFO, got {messages!r}",
        )
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 0,
        )

    def test_task_input_needed_wrong_type_is_noop(self) -> None:
        self.host._on_task_input_needed_event("garbage")  # type: ignore[arg-type]
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)

    def test_task_progress_is_noop(self) -> None:
        event = TaskProgressEvent(
            task_id="p1", progress=0.5, message="halfway"
        )
        # No exception, no state change.
        self.host._on_task_progress_event(event)
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)

    def test_proactive_non_task_source_is_noop(self) -> None:
        # No director wired AND non-task source — handler should
        # drop silently with a DEBUG line (chunk 8 will route
        # voice_silence / typed_silence onto the queue, but in
        # chunk 6 the handler only owns task_escalation).
        with self.assertLogs(
            "app.session", level="DEBUG"
        ) as captured:
            self.host._on_task_proactive_event(
                ProactiveEvent(session_key="x", source="voice_silence")
            )
        messages = [rec.getMessage() for rec in captured.records]
        self.assertTrue(
            any("source=voice_silence" in m for m in messages),
            f"expected source=voice_silence DEBUG, got {messages!r}",
        )

    def test_proactive_task_escalation_without_director(self) -> None:
        # No director wired (stub host) → log INFO + leave cue parked.
        with self.assertLogs("app.session", level="INFO") as captured:
            self.host._on_task_proactive_event(
                ProactiveEvent(
                    session_key="test-user",
                    source="task_escalation",
                    parked_cue_ids=("42", "43"),
                )
            )
        messages = [rec.getMessage() for rec in captured.records]
        self.assertTrue(
            any(
                "task-escalation proactive skipped" in m
                and "no proactive director wired" in m
                for m in messages
            ),
            f"expected 'no director wired' INFO, got {messages!r}",
        )


class ProactiveRoutingTests(unittest.TestCase):
    """Chunk 6: when a director IS wired, task_escalation events
    route into :meth:`ProactiveDirector.notify_task_escalation`.

    Uses a duck-typed stub director so we don't need to build the
    full :class:`ProactiveDirector` (which wants a chat client,
    prompt assembler, prepared-nudge store, etc.). The mixin only
    needs ``notify_task_escalation(session_key)`` on the object it
    finds at ``self._proactive``.
    """

    class _StubDirector:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.raise_next: Exception | None = None

        def notify_task_escalation(self, session_key: str) -> None:
            if self.raise_next is not None:
                exc = self.raise_next
                self.raise_next = None
                raise exc
            self.calls.append(session_key)

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.director = self._StubDirector()
        # Wire the director onto the host before init so the
        # mixin's lookup at handler-call time picks it up.
        self.host._proactive = self.director  # type: ignore[attr-defined]
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_task_escalation_dispatches_to_director(self) -> None:
        event = ProactiveEvent(
            session_key="test-user",
            source="task_escalation",
            parked_cue_ids=("42",),
        )
        self.host._on_task_proactive_event(event)
        self.assertEqual(self.director.calls, ["test-user"])

    def test_other_sources_do_not_dispatch(self) -> None:
        for source in ("voice_silence", "typed_silence"):
            self.host._on_task_proactive_event(
                ProactiveEvent(
                    session_key="test-user",
                    source=source,  # type: ignore[arg-type]
                )
            )
        self.assertEqual(self.director.calls, [])

    def test_wrong_event_type_does_not_dispatch(self) -> None:
        self.host._on_task_proactive_event("not-an-event")  # type: ignore[arg-type]
        self.assertEqual(self.director.calls, [])

    def test_director_exception_swallowed(self) -> None:
        # A buggy director must not bring down the brain loop.
        self.director.raise_next = RuntimeError("director boom")
        with self.assertLogs("app.session", level="ERROR") as captured:
            self.host._on_task_proactive_event(
                ProactiveEvent(
                    session_key="test-user",
                    source="task_escalation",
                    parked_cue_ids=("42",),
                )
            )
        messages = [rec.getMessage() for rec in captured.records]
        self.assertTrue(
            any(
                "task-escalation proactive dispatch failed" in m
                for m in messages
            ),
            f"expected dispatch-failed ERROR, got {messages!r}",
        )

    def test_event_from_queue_routes_to_director(self) -> None:
        # End-to-end: an escalation manager-style enqueue lands on
        # the brain loop, gets dispatched, hits the director.
        self.host._task_enqueue_escalation_proactive(
            "test-user", ("42",)
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.director.calls:
                break
            time.sleep(0.01)
        self.assertEqual(self.director.calls, ["test-user"])


class EscalationEnqueueTests(unittest.TestCase):
    """Verify the escalation fire path's proactive-enqueue hook."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_enqueue_proactive_lands_on_queue(self) -> None:
        # Simulate the escalation manager calling the hook directly.
        self.host._task_enqueue_escalation_proactive(
            "test-user", ("42",)
        )
        # Brain loop picks it up; we wait for the proactive handler
        # to log the receipt.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.host._brain_loop.metrics_snapshot()["dispatched"] >= 1:
                break
            time.sleep(0.01)
        metrics = self.host._brain_loop.metrics_snapshot()
        self.assertGreaterEqual(metrics["dispatched"], 1)

    def test_enqueue_when_disabled_silently_drops(self) -> None:
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()
        # No crash, no queue (loop is None).
        host._task_enqueue_escalation_proactive("user", ("t1",))


class DrainForRenderTests(unittest.TestCase):
    """Pin :meth:`drain_task_cues_for_render`."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        # Gate closed so an armed cue stays pending (re-arming) for the
        # pending-count assertion in ``test_drain_cancels_escalation``.
        self.host._brain_loop_free_to_speak = lambda: False  # type: ignore[method-assign]
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_empty_returns_empty_string(self) -> None:
        self.assertEqual(self.host.drain_task_cues_for_render(), "")

    def test_disabled_returns_empty_string(self) -> None:
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()
        self.assertEqual(host.drain_task_cues_for_render(), "")

    def test_one_cue_renders_block(self) -> None:
        self.host._on_task_result_event(
            TaskResultEvent(
                task_id="42",
                session_key="test-user",
                status="done",
                title="file_search",
                result_summary="found 3 matches",
            )
        )
        block = self.host.drain_task_cues_for_render()
        self.assertNotEqual(block, "")
        # The render module owns the exact format; we just verify
        # the cue text leaked through to the block.
        self.assertIn("found 3 matches", block)

    def test_drain_cancels_escalation(self) -> None:
        self.host._on_task_result_event(
            TaskResultEvent(
                task_id="42",
                session_key="test-user",
                status="done",
                title="file_search",
                result_summary="x",
            )
        )
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 1,
        )
        self.host.drain_task_cues_for_render()
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 0,
        )

    def test_drain_consumes_cue(self) -> None:
        self.host._on_task_result_event(
            TaskResultEvent(
                task_id="42",
                session_key="test-user",
                status="done",
                title="file_search",
                result_summary="x",
            )
        )
        self.host.drain_task_cues_for_render()
        # Second call should be empty — cue was consumed.
        self.assertEqual(self.host.drain_task_cues_for_render(), "")


class InlineResolutionSuppressionTests(unittest.TestCase):
    """Duration-hybrid: an inline-resolved task must not double-report."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_inline_resolved_suppresses_park_and_escalation(self) -> None:
        # mark_task_inline_resolved takes the integer row id (42); the
        # brain event carries the 8-char hex render (_format_task_id(42)
        # == "0000002a"). The consume path parses base-16 to match.
        self.host.mark_task_inline_resolved(42)
        self.host._on_task_result_event(
            TaskResultEvent(task_id="0000002a", session_key="test-user")
        )
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 0,
        )

    def test_suppression_is_one_shot(self) -> None:
        self.host.mark_task_inline_resolved(42)
        self.host._on_task_result_event(
            TaskResultEvent(task_id="0000002a", session_key="test-user")
        )
        # The id was consumed; a second result for the same id parks.
        self.host._on_task_result_event(
            TaskResultEvent(task_id="0000002a", session_key="test-user")
        )
        self.assertEqual(self.host._task_cue_store.pending_count(), 1)


class ReplyOnCompleteRenderTests(unittest.TestCase):
    """Finished ``reply_when_done`` cues render with their full content."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_reply_when_done_renders_full_content(self) -> None:
        import types as _types

        self.host._on_task_result_event(
            TaskResultEvent(
                task_id="7",
                session_key="test-user",
                status="done",
                title="file read: notes.md",
                result_summary="hi",
            )
        )
        # The drain path looks up the row to decide full-content vs terse.
        self.host._task_orchestrator.get = (  # type: ignore[attr-defined]
            lambda tid: _types.SimpleNamespace(
                metadata={
                    "reply_when_done": True,
                    "origin_prompt": "read my notes",
                },
                result={"content": "FULL FILE BODY", "line_count": 1},
                error=None,
            )
        )
        block = self.host.drain_task_cues_for_render()
        self.assertIn("reply now using the result below", block)
        self.assertIn("FULL FILE BODY", block)
        self.assertIn("read my notes", block)

    def test_non_reply_task_uses_terse_cue(self) -> None:
        # Real orchestrator.get returns None for the unknown id, so the
        # cue falls back to the terse bullet render.
        self.host._on_task_result_event(
            TaskResultEvent(
                task_id="8",
                session_key="test-user",
                status="done",
                title="file_search",
                result_summary="found 3 matches",
            )
        )
        block = self.host.drain_task_cues_for_render()
        self.assertIn("Tasks that finished since your last message", block)
        self.assertIn("found 3 matches", block)
        self.assertNotIn("reply now using the result below", block)


class BootRecoveryTests(unittest.TestCase):
    """Verify recovery surfaces stranded ``running`` rows on init."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_running_rows_demoted_to_interrupted(self) -> None:
        # Pre-seed two running tasks via a separate TaskStore handle.
        store = TaskStore(self.fx.chat_db)
        task_a = store.create(
            user_id="test-user",
            handler_name="file_search",
            title="search a",
            args={"q": "a"},
        )
        task_b = store.create(
            user_id="test-user",
            handler_name="file_read",
            title="read b",
            args={"path": "b.txt"},
        )

        host = self.fx.host()
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        # Both rows should now be 'interrupted'.
        row_a = host._task_store.get(task_a)
        row_b = host._task_store.get(task_b)
        assert row_a is not None and row_b is not None
        self.assertEqual(row_a.status, "interrupted")
        self.assertEqual(row_b.status, "interrupted")

    def test_awaiting_input_preserved(self) -> None:
        store = TaskStore(self.fx.chat_db)
        task_id = store.create(
            user_id="test-user",
            handler_name="file_search",
            title="search",
            args={"q": "x"},
        )
        # Demote to awaiting_input.
        store.mark_awaiting_input(
            task_id, prompt="which?", options=("a", "b"),
        )

        host = self.fx.host()
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        row = host._task_store.get(task_id)
        assert row is not None
        self.assertEqual(row.status, STATUS_AWAITING_INPUT)


class DebugSurfaceTests(unittest.TestCase):
    """Pin :meth:`task_orchestration_state` output shape."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_disabled_returns_minimal_dict(self) -> None:
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()
        state = host.task_orchestration_state()
        self.assertEqual(state, {"enabled": False})

    def test_enabled_dict_has_expected_keys(self) -> None:
        host = self.fx.host()
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        state = host.task_orchestration_state()
        self.assertEqual(state["enabled"], True)
        for key in (
            "queue_depth",
            "loop_metrics",
            "cue_metrics",
            "cue_snapshot",
            "escalation_pending",
            "escalation_snapshot",
            "free_to_speak",
        ):
            self.assertIn(key, state)

    def test_state_reflects_parked_cue(self) -> None:
        host = self.fx.host()
        # Gate closed so the armed escalation stays pending for the
        # ``escalation_pending == 1`` assertion below.
        host._brain_loop_free_to_speak = lambda: False  # type: ignore[method-assign]
        host._init_task_orchestration()
        self.addCleanup(host._shutdown_task_orchestration)

        host._on_task_result_event(
            TaskResultEvent(
                task_id="42",
                session_key="test-user",
                status="done",
                title="file_search",
                result_summary="x",
            )
        )
        state = host.task_orchestration_state()
        self.assertEqual(len(state["cue_snapshot"]), 1)
        self.assertEqual(state["cue_snapshot"][0]["task_id"], "42")
        self.assertEqual(state["escalation_pending"], 1)


class UserMessageRoutingTests(unittest.TestCase):
    """Chunk 7: brain-loop ``user_message`` handler + ReplyFuture.

    Pins the contract for the queue-driven user-message path:

    * Handler reads ``UserMessageEvent`` text + mode + skip_tts and
      calls ``chat_once_streaming`` with the right mode mapping.
    * ``reply_future`` is filled with the reply on success.
    * Handler exception → ``reply_future.set_exception``.
    * Empty / whitespace-only text → ``set_result("")`` without
      hitting ``chat_once_streaming``.
    * ``skip_tts=True`` flips ``settings.tts.enabled`` for the
      duration of the call and restores it after.
    """

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_event_routes_to_chat_once_streaming(self) -> None:
        self.host.chat_reply = "hi back"
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="typed",
        )
        self.host._on_user_message_event(event)
        self.assertEqual(len(self.host.chat_calls), 1)
        self.assertEqual(self.host.chat_calls[0]["user_text"], "hello")

    def test_reply_future_fills_on_success(self) -> None:
        self.host.chat_reply = "yes!"
        future = concurrent.futures.Future()
        event = UserMessageEvent(
            session_key="session-test-user",
            text="ping?",
            mode="mcp",
            reply_future=future,
        )
        self.host._on_user_message_event(event)
        self.assertEqual(future.result(timeout=1.0), "yes!")

    def test_reply_future_set_exception_on_handler_crash(self) -> None:
        boom = RuntimeError("turn exploded")
        self.host.chat_raise = boom
        future = concurrent.futures.Future()
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="mcp",
            reply_future=future,
        )
        self.host._on_user_message_event(event)
        with self.assertRaises(RuntimeError) as ctx:
            future.result(timeout=1.0)
        self.assertIs(ctx.exception, boom)

    def test_empty_text_skips_chat_call_and_resolves_future(self) -> None:
        future = concurrent.futures.Future()
        event = UserMessageEvent(
            session_key="session-test-user",
            text="   ",
            mode="mcp",
            reply_future=future,
        )
        self.host._on_user_message_event(event)
        # No chat call.
        self.assertEqual(self.host.chat_calls, [])
        # Future is resolved with empty string so a blocked producer
        # doesn't hang forever.
        self.assertEqual(future.result(timeout=0.5), "")

    def test_no_future_when_producer_does_not_want_reply(self) -> None:
        # WS-style: the producer doesn't care about the return text
        # (already streamed via callbacks elsewhere).
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="typed",
            reply_future=None,
        )
        # Must not crash on the missing future.
        self.host._on_user_message_event(event)
        self.assertEqual(len(self.host.chat_calls), 1)

    def test_mode_mapping_mcp_routes_typed(self) -> None:
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="mcp",
        )
        self.host._on_user_message_event(event)
        self.assertEqual(self.host.chat_calls[0]["mode"], "typed")

    def test_mode_mapping_voice_routes_live(self) -> None:
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="voice",
        )
        self.host._on_user_message_event(event)
        self.assertEqual(self.host.chat_calls[0]["mode"], "live")

    def test_mode_mapping_typed_routes_typed(self) -> None:
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="typed",
        )
        self.host._on_user_message_event(event)
        self.assertEqual(self.host.chat_calls[0]["mode"], "typed")

    def test_skip_tts_disables_then_restores(self) -> None:
        # Pre-condition: TTS is enabled in the stub settings.
        self.host._settings.tts.enabled = True
        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="mcp",
            skip_tts=True,
        )
        self.host._on_user_message_event(event)
        # During the call, TTS was disabled.
        self.assertEqual(self.host.tts_seen_during_call, [False])
        # After the call, TTS is restored.
        self.assertTrue(self.host._settings.tts.enabled)

    def test_wrong_event_type_is_noop(self) -> None:
        self.host._on_user_message_event("not-a-user-message")  # type: ignore[arg-type]
        self.assertEqual(self.host.chat_calls, [])

    def test_registered_on_brain_loop(self) -> None:
        # End-to-end via the brain queue: enqueue a UserMessageEvent
        # and verify the handler picks it up + fills the future.
        self.host.chat_reply = "queue reply"
        future = concurrent.futures.Future()
        event = UserMessageEvent(
            session_key="session-test-user",
            text="ping over the queue",
            mode="mcp",
            reply_future=future,
        )
        self.host._brain_loop.enqueue(event)
        result = future.result(timeout=2.0)
        self.assertEqual(result, "queue reply")
        self.assertEqual(len(self.host.chat_calls), 1)


class EnqueueUserMessageTests(unittest.TestCase):
    """Chunk 7: producer-side :meth:`enqueue_user_message`."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_wait_for_reply_returns_reply_text(self) -> None:
        self.host.chat_reply = "hi from queue"
        out = self.host.enqueue_user_message(
            text="hello",
            mode="mcp",
            wait_for_reply=True,
            timeout=2.0,
        )
        self.assertEqual(out, "hi from queue")
        self.assertEqual(len(self.host.chat_calls), 1)
        self.assertEqual(self.host.chat_calls[0]["mode"], "typed")  # mcp->typed

    def test_no_wait_returns_none_but_handler_still_runs(self) -> None:
        self.host.chat_reply = "fire and forget"
        out = self.host.enqueue_user_message(
            text="hello",
            mode="typed",
            wait_for_reply=False,
        )
        self.assertIsNone(out)
        # Give the brain loop a moment to drain.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.host.chat_calls:
            time.sleep(0.01)
        self.assertEqual(len(self.host.chat_calls), 1)

    def test_empty_text_returns_immediately(self) -> None:
        out = self.host.enqueue_user_message(
            text="   ", wait_for_reply=True, timeout=0.5
        )
        self.assertEqual(out, "")
        self.assertEqual(self.host.chat_calls, [])

    def test_exception_propagates_when_waiting(self) -> None:
        self.host.chat_raise = RuntimeError("nope")
        with self.assertRaises(RuntimeError):
            self.host.enqueue_user_message(
                text="hello",
                mode="mcp",
                wait_for_reply=True,
                timeout=2.0,
            )

    def test_skip_tts_through_enqueue(self) -> None:
        self.host._settings.tts.enabled = True
        out = self.host.enqueue_user_message(
            text="hello",
            mode="mcp",
            skip_tts=True,
            wait_for_reply=True,
            timeout=2.0,
        )
        self.assertEqual(out, self.host.chat_reply)
        self.assertEqual(self.host.tts_seen_during_call, [False])
        self.assertTrue(self.host._settings.tts.enabled)

    def test_disabled_master_switch_falls_back_to_direct(self) -> None:
        # Build a fresh host with tasks_enabled=False so the
        # subsystem stays a stub. The mixin should degrade to a
        # direct chat_once_streaming call.
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()
        host.chat_reply = "direct reply"
        out = host.enqueue_user_message(
            text="hello",
            mode="mcp",
            wait_for_reply=True,
            timeout=2.0,
        )
        self.assertEqual(out, "direct reply")
        self.assertEqual(len(host.chat_calls), 1)
        self.assertEqual(host.chat_calls[0]["mode"], "typed")

    def test_mcp_mode_attaches_future_even_without_explicit_wait(self) -> None:
        # Defensive default for MCP: a producer that forgets
        # ``wait_for_reply=True`` should still get a usable future
        # because the MCP debug tool always wants the reply.
        # We verify this indirectly: the handler should not be
        # racing the test (the future synchronises us).
        out = self.host.enqueue_user_message(
            text="ok",
            mode="mcp",
            wait_for_reply=False,
        )
        self.assertIsNone(out)  # caller didn't ask, didn't get
        # But the handler still ran (the future ensures synchrony).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.host.chat_calls:
            time.sleep(0.01)
        self.assertEqual(len(self.host.chat_calls), 1)

    def test_timeout_raises_when_handler_stalls(self) -> None:
        # Replace chat_once_streaming with a slow stub.
        slow_done = threading.Event()

        def _slow(*, user_text, mode, **_extra):
            slow_done.wait(timeout=3.0)
            return "late"

        self.host.chat_once_streaming = _slow  # type: ignore[method-assign]
        try:
            with self.assertRaises(concurrent.futures.TimeoutError):
                self.host.enqueue_user_message(
                    text="hello",
                    mode="mcp",
                    wait_for_reply=True,
                    timeout=0.1,
                )
        finally:
            slow_done.set()


class StreamingCallbacksRoutingTests(unittest.TestCase):
    """Chunk 8: streaming callbacks survive the queue hop.

    The WS chat handler attaches three callbacks (``on_token``,
    ``on_generation_status``, ``stop_requested``) which used to be
    threaded straight into ``chat_once_streaming`` on the WS worker
    thread. After chunk 8 they're carried in a
    :class:`ProducerCallbacks` bundle on the queued event, unbundled
    by the brain-loop handler, and threaded into the call from the
    loop thread. These tests pin the contract end-to-end so a
    refactor of the queue-side or the kwargs-side regresses loudly.
    """

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_event_carries_callbacks_through_to_chat(self) -> None:
        tokens: list[str] = []
        statuses: list[str] = []
        stops = [False]

        from app.core.brain import ProducerCallbacks, UserMessageEvent

        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="typed",
            callbacks=ProducerCallbacks(
                on_token=lambda t: tokens.append(t),
                on_generation_status=lambda s: statuses.append(s),
                stop_requested=lambda: stops[0],
            ),
        )
        self.host._on_user_message_event(event)
        # The stub stamps one of each, so the callbacks were fired
        # inside the chat_once_streaming impl.
        self.assertEqual(tokens, ["hello"])
        self.assertEqual(statuses, ["generating..."])
        # The stub fires stop_requested (return value ignored) so we
        # verify by checking the chat call snapshot.
        call = self.host.chat_calls[0]
        self.assertIsNotNone(call["on_token"])
        self.assertIsNotNone(call["on_generation_status"])
        self.assertIsNotNone(call["stop_requested"])

    def test_no_callbacks_threads_none_through(self) -> None:
        from app.core.brain import UserMessageEvent

        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="mcp",
            callbacks=None,
        )
        self.host._on_user_message_event(event)
        call = self.host.chat_calls[0]
        self.assertIsNone(call["on_token"])
        self.assertIsNone(call["on_generation_status"])
        self.assertIsNone(call["stop_requested"])

    def test_partial_callbacks_thread_none_for_missing(self) -> None:
        tokens: list[str] = []
        # Only on_token is set; the other two stay None.
        from app.core.brain import ProducerCallbacks, UserMessageEvent

        event = UserMessageEvent(
            session_key="session-test-user",
            text="hello",
            mode="typed",
            callbacks=ProducerCallbacks(on_token=lambda t: tokens.append(t)),
        )
        self.host._on_user_message_event(event)
        self.assertEqual(tokens, ["hello"])
        call = self.host.chat_calls[0]
        self.assertIsNotNone(call["on_token"])
        self.assertIsNone(call["on_generation_status"])
        self.assertIsNone(call["stop_requested"])


class EnqueueWithCallbacksTests(unittest.TestCase):
    """Chunk 8: ``enqueue_user_message`` accepts the WS-shape loose
    kwargs **and** the explicit :class:`ProducerCallbacks` bundle.
    """

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_loose_kwargs_bundle_into_producer_callbacks(self) -> None:
        tokens: list[str] = []
        statuses: list[str] = []

        reply = self.host.enqueue_user_message(
            text="hello",
            mode="typed",
            wait_for_reply=True,
            timeout=2.0,
            on_token=lambda t: tokens.append(t),
            on_generation_status=lambda s: statuses.append(s),
            stop_requested=lambda: False,
        )
        self.assertEqual(reply, self.host.chat_reply)
        self.assertEqual(tokens, ["hello"])
        self.assertEqual(statuses, ["generating..."])

    def test_explicit_callbacks_arg_wins_over_loose_kwargs(self) -> None:
        # Producer passes both — the explicit bundle wins (kwargs
        # are a convenience for the WS handler shape).
        from app.core.brain import ProducerCallbacks

        winning_tokens: list[str] = []
        losing_tokens: list[str] = []

        self.host.enqueue_user_message(
            text="hello",
            mode="typed",
            wait_for_reply=True,
            timeout=2.0,
            on_token=lambda t: losing_tokens.append(t),
            callbacks=ProducerCallbacks(
                on_token=lambda t: winning_tokens.append(t)
            ),
        )
        self.assertEqual(winning_tokens, ["hello"])
        self.assertEqual(losing_tokens, [])

    def test_no_callbacks_attached_when_all_none(self) -> None:
        # MCP-shape call: no streaming callbacks at all. The
        # ``callbacks`` field on the event should be None so the
        # handler doesn't allocate a bundle for nothing.
        self.host.enqueue_user_message(
            text="hello",
            mode="mcp",
            wait_for_reply=True,
            timeout=2.0,
        )
        call = self.host.chat_calls[0]
        self.assertIsNone(call["on_token"])
        self.assertIsNone(call["on_generation_status"])
        self.assertIsNone(call["stop_requested"])

    def test_disabled_subsystem_threads_callbacks_through_direct_path(self) -> None:
        # ``agent.tasks_enabled=False`` falls back to a direct
        # ``chat_once_streaming`` call. The streaming callbacks have
        # to survive that path too or the WS handler would silently
        # lose its per-token broadcast whenever the user disables
        # tasks.
        host = self.fx.host(tasks_enabled=False)
        host._init_task_orchestration()
        tokens: list[str] = []
        host.enqueue_user_message(
            text="hello",
            mode="typed",
            wait_for_reply=True,
            on_token=lambda t: tokens.append(t),
        )
        self.assertEqual(tokens, ["hello"])

    def test_stop_requested_threads_through(self) -> None:
        # Sanity: the stop_requested predicate is what the WS
        # cancel-button uses to abort an in-flight turn. The handler
        # has to thread it through verbatim.
        calls = []

        def _stop() -> bool:
            calls.append(time.monotonic())
            return False

        self.host.enqueue_user_message(
            text="hello",
            mode="typed",
            wait_for_reply=True,
            timeout=2.0,
            stop_requested=_stop,
        )
        # Stub fires stop_requested once; the WS production case
        # may fire it many times during the LLM stream.
        self.assertGreaterEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
