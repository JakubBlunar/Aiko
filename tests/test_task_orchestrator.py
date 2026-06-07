"""Tests for :mod:`app.core.tasks.task_orchestrator`.

The orchestrator is where every important contract lives:

* Handler registration round-trips by ``name``.
* ``start_task`` persists + spawns; the synchronous return is the
  ``task_id`` (matches the "I'll start that for you" UX latency
  requirement).
* Per-user cap rejects spawn #N+1.
* Handler emits drive state transitions:
    - TaskProgress -> row.progress / row.last_message + queue event
    - TaskInputNeeded -> row.status='awaiting_input' + queue event
    - TaskCompleted -> row.status='done' + result blob + queue event
    - TaskFailed -> row.status='failed' + error string + queue event
* ``answer`` requires the row to be ``awaiting_input``; otherwise
  rejected.
* ``cancel`` wins the race against late emits (handler emit after
  cancel is suppressed silently).
* Boot recovery via :func:`recover_interrupted_tasks` demotes
  stranded ``running`` rows to ``interrupted`` and fires the cue
  event.
* Handler exceptions become TaskFailed automatically — the worker
  thread never propagates raw exceptions back into the orchestrator
  surface.
* The ``task_id`` ContextVar is set inside the worker so log lines
  from the handler carry ``task=...``.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.brain import BrainEventQueue
from app.core.brain.events import (
    KIND_TASK_INPUT_NEEDED,
    KIND_TASK_PROGRESS,
    KIND_TASK_RESULT,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.log_context import get_task_id
from app.core.tasks import (
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    TaskCompleted,
    TaskFailed,
    TaskInputNeeded,
    TaskOrchestrator,
    TaskProgress,
    TaskStore,
    recover_interrupted_tasks,
)


class _Fixture:
    def __init__(self, *, per_user_cap: int = 8) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)
        self.queue = BrainEventQueue()
        # Single-worker pool for deterministic ordering in tests.
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="test-task"
        )
        self.orch = TaskOrchestrator(
            self.store,
            queue=self.queue,
            executor=self.executor,
            per_user_cap=per_user_cap,
        )

    def close(self) -> None:
        try:
            self.orch.shutdown(wait=True)
        except Exception:
            pass
        try:
            self.executor.shutdown(wait=True)
        except Exception:
            pass
        try:
            self.queue.close()
        except Exception:
            pass
        if self.db is not None:
            conn = getattr(self.db._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self.db._local.conn = None
            self.db = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def drain_queue(self) -> list:
        events = []
        while True:
            e = self.queue.get(timeout=0.05)
            if e is None:
                break
            events.append(e)
        return events


# ── reusable test handlers ──────────────────────────────────────────


class CompletingHandler:
    """Emits two progress events + TaskCompleted. Always succeeds."""

    name = "completing"

    def __init__(self) -> None:
        self.start_calls = 0

    def start(self, args, emit):
        self.start_calls += 1
        emit(TaskProgress(progress=0.3, message="scanning"))
        emit(TaskProgress(progress=0.7, message="filtering"))
        emit(TaskCompleted(result={"summary": "found 3", "count": 3}))
        return {"args": args, "phase": "done"}

    def resume(self, state, emit):
        emit(TaskFailed(error="resume not used"))
        return state

    def on_input(self, state, answer, emit):
        emit(TaskFailed(error="on_input not used"))
        return state

    def cancel(self, state):
        pass


class FailingHandler:
    name = "failing"

    def start(self, args, emit):
        emit(TaskFailed(error="db is on fire"))
        return {"args": args, "phase": "errored"}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class RaisingHandler:
    """Raises an unhandled exception. The orchestrator must convert
    that into a TaskFailed terminal emit so the row reaches a final
    state."""

    name = "raising"

    def start(self, args, emit):
        raise RuntimeError("boom")

    def resume(self, state, emit):
        raise RuntimeError("boom")

    def on_input(self, state, answer, emit):
        raise RuntimeError("boom")

    def cancel(self, state):
        pass


class AskingHandler:
    """Emits TaskInputNeeded on start, then TaskCompleted on the
    answer. Exercises the awaiting_input -> on_input -> done path."""

    name = "asking"

    def __init__(self) -> None:
        self.last_answer: str | None = None

    def start(self, args, emit):
        emit(TaskInputNeeded(prompt="which one?", options=["a", "b"]))
        return {"args": args, "phase": "awaiting"}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        self.last_answer = answer
        emit(TaskCompleted(result={"chosen": answer, "summary": f"picked {answer}"}))
        return {**state, "phase": "done", "chosen": answer}

    def cancel(self, state):
        pass


class StateCheckingHandler:
    """Captures the ``task_id`` contextvar value seen from inside the
    handler body, so a test can assert propagation."""

    name = "state_checking"

    def __init__(self) -> None:
        self.observed_task_id: str | None = None

    def start(self, args, emit):
        self.observed_task_id = get_task_id()
        emit(TaskCompleted(result={"observed": self.observed_task_id}))
        return {"args": args}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class SlowAwaitingHandler:
    """Sleeps briefly then emits TaskInputNeeded. Used for the cancel
    race test — a short sleep makes the timing predictable without
    being flaky."""

    name = "slow_await"

    def __init__(self, sleep_s: float = 0.05) -> None:
        self.sleep_s = float(sleep_s)
        self.cancel_called = False

    def start(self, args, emit):
        time.sleep(self.sleep_s)
        emit(TaskInputNeeded(prompt="will be cancelled"))
        return {"args": args}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        self.cancel_called = True


# ── tests ───────────────────────────────────────────────────────────


class HandlerRegistryTests(unittest.TestCase):
    def test_register_round_trip(self) -> None:
        f = _Fixture()
        try:
            handler = CompletingHandler()
            f.orch.register_handler(handler)
            self.assertIs(f.orch.handler_for("completing"), handler)
            self.assertEqual(f.orch.list_handlers(), ["completing"])
        finally:
            f.close()

    def test_register_overwrites_existing(self) -> None:
        f = _Fixture()
        try:
            h1 = CompletingHandler()
            h2 = CompletingHandler()
            f.orch.register_handler(h1)
            f.orch.register_handler(h2)
            self.assertIs(f.orch.handler_for("completing"), h2)
        finally:
            f.close()

    def test_register_rejects_empty_name(self) -> None:
        f = _Fixture()
        try:
            class Anon:
                name = ""

                def start(self, *a, **k):
                    pass

                def resume(self, *a, **k):
                    pass

                def on_input(self, *a, **k):
                    pass

                def cancel(self, *a, **k):
                    pass

            with self.assertRaises(ValueError):
                f.orch.register_handler(Anon())
        finally:
            f.close()


class StartTaskTests(unittest.TestCase):
    def test_start_returns_task_id_synchronously(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="jacob",
                handler_name="completing",
                args={"q": "notes"},
                title="search 'notes'",
            )
            self.assertIsNotNone(tid)
            assert tid is not None
            self.assertGreater(tid, 0)
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.user_id, "jacob")
            self.assertEqual(row.title, "search 'notes'")
        finally:
            f.close()

    def test_unknown_handler_returns_none(self) -> None:
        f = _Fixture()
        try:
            tid = f.orch.start_task(
                user_id="jacob",
                handler_name="nope",
                args={},
                title="t",
            )
            self.assertIsNone(tid)
        finally:
            f.close()

    def test_per_user_cap_rejects_overflow(self) -> None:
        f = _Fixture(per_user_cap=2)
        try:
            # Asking handlers stay awaiting_input -> always counted as active.
            f.orch.register_handler(AskingHandler())
            tid1 = f.orch.start_task(
                user_id="jacob", handler_name="asking",
                args={}, title="a",
            )
            tid2 = f.orch.start_task(
                user_id="jacob", handler_name="asking",
                args={}, title="b",
            )
            # Wait for both to reach awaiting_input (counted as active).
            self._wait_until_status(f, tid1, STATUS_AWAITING_INPUT)
            self._wait_until_status(f, tid2, STATUS_AWAITING_INPUT)
            tid3 = f.orch.start_task(
                user_id="jacob", handler_name="asking",
                args={}, title="c",
            )
            self.assertIsNone(tid3, "third spawn should hit the cap")
        finally:
            f.close()

    def _wait_until_status(self, f, tid, target, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = f.orch.get(tid)
            if row and row.status == target:
                return
            time.sleep(0.005)
        self.fail(f"task {tid} never reached {target}")


class EmitOutcomeTests(unittest.TestCase):
    def test_completed_flow(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={"q": "x"}, title="t",
            )
            final = f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_DONE)
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_DONE)
            self.assertEqual(row.result, {"summary": "found 3", "count": 3})
            self.assertIsNotNone(row.completed_at)
            # Progress should reflect last reported value.
            self.assertAlmostEqual(row.progress or 0.0, 0.7)
            self.assertEqual(row.last_message, "filtering")
        finally:
            f.close()

    def test_completed_emits_brain_events(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            events = f.drain_queue()
            kinds = [e.kind for e in events]
            self.assertEqual(kinds.count(KIND_TASK_PROGRESS), 2)
            self.assertEqual(kinds.count(KIND_TASK_RESULT), 1)
            result_evt = next(e for e in events if e.kind == KIND_TASK_RESULT)
            self.assertEqual(result_evt.status, STATUS_DONE)
            self.assertEqual(result_evt.notify_aiko, True)
        finally:
            f.close()

    def test_failed_flow(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(FailingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="failing",
                args={}, title="t",
            )
            final = f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_FAILED)
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_FAILED)
            self.assertEqual(row.error, "db is on fire")
            self.assertIsNone(row.result)
        finally:
            f.close()

    def test_failed_emits_task_result_with_failed_status(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(FailingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="failing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            events = f.drain_queue()
            result_evts = [e for e in events if e.kind == KIND_TASK_RESULT]
            self.assertEqual(len(result_evts), 1)
            self.assertEqual(result_evts[0].status, STATUS_FAILED)
            self.assertEqual(result_evts[0].error, "db is on fire")
        finally:
            f.close()

    def test_raising_handler_becomes_failed(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(RaisingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="raising",
                args={}, title="t",
            )
            final = f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_FAILED)
            row = f.orch.get(tid)
            assert row is not None
            self.assertIn("RuntimeError", row.error or "")
            self.assertIn("boom", row.error or "")
        finally:
            f.close()


class AwaitingInputTests(unittest.TestCase):
    def test_input_needed_persists_prompt(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="t",
            )
            self._wait_until_status(f, tid, STATUS_AWAITING_INPUT)
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.input_request["prompt"], "which one?")
            self.assertEqual(row.input_request["options"], ["a", "b"])
        finally:
            f.close()

    def test_input_needed_emits_brain_event(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="t",
            )
            self._wait_until_status(f, tid, STATUS_AWAITING_INPUT)
            events = f.drain_queue()
            input_evts = [e for e in events if e.kind == KIND_TASK_INPUT_NEEDED]
            self.assertEqual(len(input_evts), 1)
            self.assertEqual(input_evts[0].prompt, "which one?")
            self.assertEqual(input_evts[0].options, ("a", "b"))
        finally:
            f.close()

    def test_answer_resumes_to_completion(self) -> None:
        f = _Fixture()
        try:
            handler = AskingHandler()
            f.orch.register_handler(handler)
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="t",
            )
            self._wait_until_status(f, tid, STATUS_AWAITING_INPUT)
            ok = f.orch.answer(tid, "a")
            self.assertTrue(ok)
            final = f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_DONE)
            self.assertEqual(handler.last_answer, "a")
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.result, {"chosen": "a", "summary": "picked a"})
        finally:
            f.close()

    def test_answer_rejected_on_wrong_status(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)  # status=done
            ok = f.orch.answer(tid, "anything")
            self.assertFalse(ok)
        finally:
            f.close()

    def test_answer_unknown_task_returns_false(self) -> None:
        f = _Fixture()
        try:
            self.assertFalse(f.orch.answer(999999, "x"))
        finally:
            f.close()

    def _wait_until_status(self, f, tid, target, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = f.orch.get(tid)
            if row and row.status == target:
                return
            time.sleep(0.005)
        self.fail(f"task {tid} never reached {target}")


class CancelTests(unittest.TestCase):
    def test_cancel_marks_row_and_emits(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="t",
            )
            self._wait_until_status(f, tid, STATUS_AWAITING_INPUT)
            ok = f.orch.cancel(tid)
            self.assertTrue(ok)
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_CANCELLED)
            events = f.drain_queue()
            result_evts = [e for e in events if e.kind == KIND_TASK_RESULT]
            statuses = [e.status for e in result_evts]
            self.assertIn(STATUS_CANCELLED, statuses)
        finally:
            f.close()

    def test_cancel_already_terminal_returns_false(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            self.assertFalse(f.orch.cancel(tid))
        finally:
            f.close()

    def test_cancel_invokes_handler_cancel(self) -> None:
        f = _Fixture()
        try:
            handler = SlowAwaitingHandler(sleep_s=0.02)
            f.orch.register_handler(handler)
            tid = f.orch.start_task(
                user_id="u", handler_name="slow_await",
                args={}, title="t",
            )
            self._wait_until_status(f, tid, STATUS_AWAITING_INPUT)
            f.orch.cancel(tid)
            # cancel is submitted to the executor; give it a beat.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not handler.cancel_called:
                time.sleep(0.005)
            self.assertTrue(handler.cancel_called)
        finally:
            f.close()

    def test_cancel_unknown_task_returns_false(self) -> None:
        f = _Fixture()
        try:
            self.assertFalse(f.orch.cancel(999999))
        finally:
            f.close()

    def _wait_until_status(self, f, tid, target, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = f.orch.get(tid)
            if row and row.status == target:
                return
            time.sleep(0.005)
        self.fail(f"task {tid} never reached {target}")


class ContextVarPropagationTests(unittest.TestCase):
    """The ``task_id`` ContextVar must be set inside the handler
    invocation so all per-handler log lines auto-correlate."""

    def test_task_id_observed_inside_handler(self) -> None:
        f = _Fixture()
        try:
            handler = StateCheckingHandler()
            f.orch.register_handler(handler)
            tid = f.orch.start_task(
                user_id="u", handler_name="state_checking",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            assert handler.observed_task_id is not None
            # Format is 8-char zero-padded hex of the task id.
            self.assertEqual(handler.observed_task_id, f"{tid:08x}")
        finally:
            f.close()

    def test_task_id_not_set_outside_handler(self) -> None:
        f = _Fixture()
        try:
            self.assertIsNone(get_task_id())
            f.orch.register_handler(StateCheckingHandler())
            f.orch.start_task(
                user_id="u", handler_name="state_checking",
                args={}, title="t",
            )
            # Test thread's contextvar still empty even after start.
            self.assertIsNone(get_task_id())
        finally:
            f.close()


class NotifyOverrideTests(unittest.TestCase):
    """A handler can override ``notify_aiko`` on a per-outcome basis,
    e.g. silencing a "found 0 results" completion that the row was
    originally set to surface."""

    class SilenceableHandler:
        name = "silenceable"

        def start(self, args, emit):
            emit(TaskCompleted(result={"summary": "0 found"}, notify_aiko=False))
            return {"args": args}

        def resume(self, state, emit):
            return state

        def on_input(self, state, answer, emit):
            return state

        def cancel(self, state):
            pass

    def test_per_outcome_notify_override(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(NotifyOverrideTests.SilenceableHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="silenceable",
                args={}, title="t",
                notify_aiko=True,  # row default
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            events = f.drain_queue()
            result_evt = next(e for e in events if e.kind == KIND_TASK_RESULT)
            self.assertFalse(result_evt.notify_aiko)
        finally:
            f.close()


class RecoveryTests(unittest.TestCase):
    def test_recovery_demotes_running_to_interrupted(self) -> None:
        f = _Fixture()
        try:
            # Simulate stranded rows by inserting them directly via the
            # store (no handler involved).
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            report = recover_interrupted_tasks(f.store, orchestrator=f.orch)
            self.assertEqual(report.interrupted, [tid])
            row = f.orch.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_INTERRUPTED)
        finally:
            f.close()

    def test_recovery_emits_cue_when_orchestrator_wired(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="my stalled job",
                args={}, state={},
            )
            recover_interrupted_tasks(f.store, orchestrator=f.orch)
            events = f.drain_queue()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event.kind, KIND_TASK_RESULT)
            # task_id is the 8-char hex form, NOT the int.
            self.assertEqual(event.task_id, f"{tid:08x}")
            self.assertEqual(event.title, "my stalled job")
        finally:
            f.close()

    def test_recovery_preserves_awaiting_input(self) -> None:
        f = _Fixture()
        try:
            tid_run = f.store.create(
                user_id="u", handler_name="h", title="will be interrupted",
                args={}, state={},
            )
            tid_wait = f.store.create(
                user_id="u", handler_name="h", title="still waiting",
                args={}, state={},
            )
            f.store.mark_awaiting_input(tid_wait, prompt="?")
            report = recover_interrupted_tasks(f.store, orchestrator=f.orch)
            self.assertEqual(report.interrupted, [tid_run])
            self.assertEqual(report.preserved, [tid_wait])
            self.assertEqual(
                f.orch.get(tid_wait).status, STATUS_AWAITING_INPUT,
            )
        finally:
            f.close()

    def test_recovery_skips_cue_when_resume_disabled(self) -> None:
        f = _Fixture()
        try:
            f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            recover_interrupted_tasks(
                f.store, orchestrator=f.orch, resume_on_boot=False
            )
            events = f.drain_queue()
            self.assertEqual(events, [], "no cue should fire when disabled")
        finally:
            f.close()

    def test_recovery_no_op_when_nothing_stranded(self) -> None:
        f = _Fixture()
        try:
            report = recover_interrupted_tasks(f.store, orchestrator=f.orch)
            self.assertEqual(report.interrupted, [])
            self.assertEqual(report.preserved, [])
            self.assertEqual(report.failed, [])
            self.assertEqual(report.total_scanned, 0)
        finally:
            f.close()


class QueueIsolationTests(unittest.TestCase):
    """The orchestrator can run without a queue (e.g. boot recovery
    that hasn't wired the brain loop yet). ``last_emitted_event``
    carries the most recent event for assertion convenience."""

    def test_works_without_queue(self) -> None:
        tmp = TemporaryDirectory()
        db_path = Path(tmp.name) / "chat.db"
        db: ChatDatabase | None = ChatDatabase(db_path)
        store = TaskStore(db)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iso")
        orch = TaskOrchestrator(store, queue=None, executor=executor)
        try:
            orch.register_handler(CompletingHandler())
            tid = orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            final = orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_DONE)
            # last_emitted_event survives even with no queue.
            self.assertIsNotNone(orch.last_emitted_event)
            self.assertEqual(orch.last_emitted_event.kind, KIND_TASK_RESULT)
        finally:
            orch.shutdown(wait=True)
            executor.shutdown(wait=True)
            conn = getattr(db._local, "conn", None)
            if conn is not None:
                conn.close()
                db._local.conn = None
            try:
                tmp.cleanup()
            except PermissionError:
                # The orchestrator worker thread may still hold its
                # per-thread sqlite connection open even after the
                # executor's main reference is released. Cleanup will
                # succeed at GC time; the temp dir is harmless either way.
                pass


# ── Chunk 13: listener fan-out + snapshot helper ─────────────────────


class _Recorder:
    """Listener that records every (kind, payload) it sees.

    Used by :class:`ListenerFanOutTests` to assert the orchestrator
    fires the right kinds in the right order with the right payloads.
    Thread-safe — the orchestrator dispatches from a worker thread.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, kind: str, payload: dict) -> None:
        with self._lock:
            self.events.append((kind, dict(payload)))

    def kinds(self) -> list[str]:
        with self._lock:
            return [k for k, _ in self.events]

    def latest(self, kind: str) -> dict | None:
        with self._lock:
            for k, p in reversed(self.events):
                if k == kind:
                    return p
        return None


class SnapshotHelperTests(unittest.TestCase):
    """Pinning the JSON-safe shape of ``task_snapshot``.

    The frontend + REST consumers depend on every field name being
    stable. Adding a field is fine; renaming or dropping one is a
    wire-protocol break.
    """

    def test_snapshot_round_trips_running_row(self) -> None:
        from app.core.tasks import task_snapshot
        from app.core.tasks.task_store import TaskRow

        row = TaskRow(
            id=42,
            user_id="alice",
            handler_name="file_search",
            args={"query": "memory"},
            state={"phase": "scanning"},
            status="running",
            title="search memory",
            progress=0.5,
            last_message="halfway",
            input_request=None,
            result=None,
            error=None,
            notify_aiko=True,
            visible_to_user=True,
            initiated_by="aiko",
            created_at="2026-06-07T12:00:00+00:00",
            updated_at="2026-06-07T12:01:00+00:00",
            completed_at=None,
            metadata={"source": "tool"},
        )
        snap = task_snapshot(row)
        # Pinned field set — change is a wire break.
        self.assertEqual(
            set(snap.keys()),
            {
                "id", "user_id", "handler_name", "title", "status",
                "progress", "last_message", "initiated_by", "args",
                "input_request", "result", "error", "notify_aiko",
                "visible_to_user", "created_at", "updated_at",
                "completed_at", "metadata",
                # v17 — Brain Orchestration Phase 2
                "phase", "parent_task_id", "heartbeat_at",
            },
        )
        self.assertEqual(snap["id"], 42)
        self.assertEqual(snap["user_id"], "alice")
        self.assertEqual(snap["handler_name"], "file_search")
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["progress"], 0.5)
        self.assertEqual(snap["last_message"], "halfway")
        self.assertEqual(snap["args"], {"query": "memory"})
        self.assertEqual(snap["metadata"], {"source": "tool"})
        self.assertTrue(snap["notify_aiko"])
        self.assertTrue(snap["visible_to_user"])

    def test_snapshot_handles_none_optional_fields(self) -> None:
        from app.core.tasks import task_snapshot
        from app.core.tasks.task_store import TaskRow

        row = TaskRow(
            id=1, user_id="u", handler_name="h", title="t",
            status="running",
        )
        snap = task_snapshot(row)
        self.assertIsNone(snap["progress"])
        self.assertIsNone(snap["last_message"])
        self.assertIsNone(snap["input_request"])
        self.assertIsNone(snap["result"])
        self.assertIsNone(snap["error"])
        self.assertIsNone(snap["completed_at"])
        self.assertIsNone(snap["metadata"])

    def test_snapshot_is_json_safe(self) -> None:
        """``json.dumps`` over the snapshot must round-trip cleanly."""
        import json as _json

        from app.core.tasks import task_snapshot
        from app.core.tasks.task_store import TaskRow

        row = TaskRow(
            id=7, user_id="u", handler_name="file_read",
            args={"path": "docs/notes.md"},
            state={"opaque": [1, 2, 3]},
            status="awaiting_input",
            title="read notes",
            input_request={
                "prompt": "which root?",
                "options": ["Documents:notes.md", "Notes:notes.md"],
            },
            metadata={"label": "Documents"},
        )
        payload = task_snapshot(row)
        text = _json.dumps(payload)
        again = _json.loads(text)
        self.assertEqual(again["input_request"]["prompt"], "which root?")
        self.assertEqual(again["args"]["path"], "docs/notes.md")

    def test_snapshot_decouples_dicts(self) -> None:
        """Mutating the snapshot must not affect the source row."""
        from app.core.tasks import task_snapshot
        from app.core.tasks.task_store import TaskRow

        row = TaskRow(
            id=1, user_id="u", handler_name="h", title="t",
            status="running",
            args={"k": "v"},
            metadata={"m": 1},
        )
        snap = task_snapshot(row)
        snap["args"]["k"] = "MUTATED"
        snap["metadata"]["m"] = 999
        self.assertEqual(row.args["k"], "v")
        self.assertEqual(row.metadata["m"], 1)


class ListenerFanOutTests(unittest.TestCase):
    """Verify ``add_task_listener`` fires for every lifecycle moment.

    The REST + WS bridge in ``app/web/server.py`` depends on this
    contract. Adding a new lifecycle event must also add a listener
    fire and a test here.
    """

    def test_listener_fires_task_started_with_snapshot(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={"q": "x"}, title="search",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            kinds = rec.kinds()
            self.assertIn("task_started", kinds)
            self.assertEqual(kinds[0], "task_started")
            started = rec.events[0][1]
            self.assertIn("task", started)
            self.assertEqual(started["task"]["id"], tid)
            self.assertEqual(started["task"]["handler_name"], "completing")
            self.assertEqual(started["task"]["status"], "running")
        finally:
            f.close()

    def test_listener_fires_task_progress_with_patch(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            progress_events = [
                (k, p) for k, p in rec.events if k == "task_progress"
            ]
            # CompletingHandler emits 2 progress events.
            self.assertEqual(len(progress_events), 2)
            for _, payload in progress_events:
                self.assertEqual(payload["task_id"], tid)
                self.assertIn("patch", payload)
                self.assertEqual(payload["patch"]["status"], "running")
                self.assertIsNotNone(payload["patch"].get("progress"))
                self.assertIsNotNone(payload["patch"].get("last_message"))
        finally:
            f.close()

    def test_listener_fires_task_input_needed_with_snapshot(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="ask",
            )
            # Poll for the event to land — fixed sleeps race under
            # parallel test load.
            deadline = time.monotonic() + 2.0
            input_needed = None
            while time.monotonic() < deadline:
                input_needed = rec.latest("task_input_needed")
                if input_needed is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(input_needed)
            self.assertIn("task", input_needed)
            snap = input_needed["task"]
            self.assertEqual(snap["id"], tid)
            self.assertEqual(snap["status"], "awaiting_input")
            self.assertIsNotNone(snap["input_request"])
            self.assertEqual(snap["input_request"]["prompt"], "which one?")
            self.assertEqual(
                snap["input_request"]["options"], ["a", "b"]
            )
        finally:
            f.close()

    def test_listener_fires_task_completed_on_done(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            completed = rec.latest("task_completed")
            self.assertIsNotNone(completed)
            snap = completed["task"]
            self.assertEqual(snap["id"], tid)
            self.assertEqual(snap["status"], "done")
            self.assertIsNotNone(snap["result"])
            self.assertEqual(snap["result"]["count"], 3)
        finally:
            f.close()

    def test_listener_fires_task_completed_on_failure(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(FailingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="failing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            completed = rec.latest("task_completed")
            self.assertIsNotNone(completed)
            snap = completed["task"]
            self.assertEqual(snap["status"], "failed")
            self.assertEqual(snap["error"], "db is on fire")
        finally:
            f.close()

    def test_listener_fires_task_completed_on_cancel(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.05)
            ok = f.orch.cancel(tid)
            self.assertTrue(ok)
            time.sleep(0.05)
            completed = rec.latest("task_completed")
            self.assertIsNotNone(completed)
            self.assertEqual(completed["task"]["status"], "cancelled")
        finally:
            f.close()

    def test_full_input_needed_then_answer_then_completed_sequence(self) -> None:
        """The full ``running -> awaiting_input -> running -> done`` arc.

        Verifies that the listener sees exactly four kinds, in this
        order: task_started, task_input_needed, task_completed.
        ``task_progress`` is not emitted by AskingHandler so it must
        not appear.
        """
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.1)
            ok = f.orch.answer(tid, "a")
            self.assertTrue(ok)
            f.orch.wait_for_task(tid, timeout=2.0)
            kinds = rec.kinds()
            self.assertEqual(
                kinds,
                ["task_started", "task_input_needed", "task_completed"],
            )
        finally:
            f.close()

    def test_remove_task_listener_unsubscribes(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            removed = f.orch.remove_task_listener(rec)
            self.assertTrue(removed)
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(rec.events, [])
        finally:
            f.close()

    def test_remove_task_listener_missing_returns_false(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            self.assertFalse(f.orch.remove_task_listener(rec))
        finally:
            f.close()

    def test_add_task_listener_idempotent(self) -> None:
        f = _Fixture()
        rec = _Recorder()
        try:
            f.orch.add_task_listener(rec)
            f.orch.add_task_listener(rec)
            f.orch.add_task_listener(rec)
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            # Only one started event despite three add_task_listener calls.
            self.assertEqual(rec.kinds().count("task_started"), 1)
        finally:
            f.close()

    def test_buggy_listener_does_not_break_siblings(self) -> None:
        """A listener that raises must not poison sibling listeners
        or break the lifecycle path."""
        f = _Fixture()
        good = _Recorder()
        try:
            def bad(kind: str, payload: dict) -> None:
                raise RuntimeError("listener boom")

            f.orch.add_task_listener(bad)
            f.orch.add_task_listener(good)
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            final = f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_DONE)
            self.assertIn("task_started", good.kinds())
            self.assertIn("task_completed", good.kinds())
        finally:
            f.close()

    def test_listener_skips_invalid_callbacks(self) -> None:
        """Non-callable / None values must be silently ignored."""
        f = _Fixture()
        try:
            f.orch.add_task_listener(None)  # type: ignore[arg-type]
            f.orch.add_task_listener("not callable")  # type: ignore[arg-type]
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            final = f.orch.wait_for_task(tid, timeout=2.0)
            self.assertEqual(final, STATUS_DONE)
        finally:
            f.close()

    def test_listener_kind_constants_match_payload_kinds(self) -> None:
        """The exported string constants must match the dispatched kinds."""
        from app.core.tasks import (
            TASK_LISTENER_COMPLETED,
            TASK_LISTENER_INPUT_NEEDED,
            TASK_LISTENER_PROGRESS,
            TASK_LISTENER_STARTED,
        )

        self.assertEqual(TASK_LISTENER_STARTED, "task_started")
        self.assertEqual(TASK_LISTENER_PROGRESS, "task_progress")
        self.assertEqual(TASK_LISTENER_INPUT_NEEDED, "task_input_needed")
        self.assertEqual(TASK_LISTENER_COMPLETED, "task_completed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
