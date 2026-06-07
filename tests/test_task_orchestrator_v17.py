"""Schema v17 orchestrator integration tests.

Pin the new contracts added in Brain Orchestration Phase 2:

* Event-log append on every emit (started / progress / input_question
  / input_answer / completed / failed / cancelled / interrupted).
* ``parent_task_id`` propagation + ``EVENT_CHILD_SPAWNED`` on parent.
* ``TaskInputNeeded`` writes a row in the new ``task_inputs`` table
  and supersedes any prior pending row.
* ``answer`` resolves the latest pending input row.
* Cascade-cancel walks the tree depth-first.
* Heartbeat is bumped on every emit.
* ``TaskEventEmit`` outcome appends without touching row state.
* ``TaskProgress.phase`` promotes to the ``phase`` column.

Tests use a single-worker pool for deterministic ordering.
"""
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.brain import BrainEventQueue
from app.core.infra.chat_database import ChatDatabase
from app.core.tasks import (
    EVENT_CANCELLED,
    EVENT_CHILD_SPAWNED,
    EVENT_COMPLETED,
    EVENT_FAILED,
    EVENT_INPUT_ANSWER,
    EVENT_INPUT_QUESTION,
    EVENT_PHASE_CHANGE,
    EVENT_PROGRESS,
    EVENT_STARTED,
    INPUT_STATUS_ANSWERED,
    INPUT_STATUS_PENDING,
    INPUT_STATUS_SUPERSEDED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_RUNNING,
    TaskCompleted,
    TaskEventEmit,
    TaskEventStore,
    TaskFailed,
    TaskInputNeeded,
    TaskInputStore,
    TaskOrchestrator,
    TaskProgress,
    TaskStore,
)


class _Fixture:
    def __init__(self, *, cascade: bool = True) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)
        self.event_store = TaskEventStore(self.db)
        self.input_store = TaskInputStore(self.db)
        self.queue = BrainEventQueue()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="test-task"
        )
        self.orch = TaskOrchestrator(
            self.store,
            queue=self.queue,
            executor=self.executor,
            per_user_cap=8,
            event_store=self.event_store,
            input_store=self.input_store,
            cascade_cancel_children=cascade,
            heartbeat_enabled=False,  # tests drive the sweep directly
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


class _PhaseCompletingHandler:
    name = "phase_completing"

    def start(self, args, emit):
        emit(TaskProgress(progress=0.3, message="scanning", phase="scanning"))
        emit(TaskProgress(progress=0.7, message="matching", phase="matching"))
        emit(TaskCompleted(result={"summary": "found 3"}))
        return {"phase": "done"}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class _InputThenCompleteHandler:
    name = "input_then_complete"

    def start(self, args, emit):
        emit(TaskInputNeeded(prompt="confirm?", options=["yes", "no"]))
        return {"phase": "waiting"}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        emit(TaskCompleted(result={"answer": answer}))
        return state

    def cancel(self, state):
        pass


class _FailingHandler:
    name = "failing"

    def start(self, args, emit):
        emit(TaskFailed(error="boom"))
        return {}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class _EmitCustomHandler:
    name = "emit_custom"

    def start(self, args, emit):
        emit(TaskEventEmit(type="visited_url", data={"url": "https://x"}))
        emit(TaskCompleted(result={"ok": True}))
        return {}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class _LongRunningHandler:
    """A handler that just emits a progress beat then blocks until told to stop."""

    name = "long_running"

    def __init__(self) -> None:
        import threading
        self._stop = threading.Event()
        self.cancel_calls = 0

    def start(self, args, emit):
        emit(TaskProgress(progress=0.1, message="working", phase="early"))
        # Wait for cancel — never naturally completes.
        self._stop.wait(timeout=3.0)
        return {}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        self.cancel_calls += 1
        self._stop.set()


# ── tests ───────────────────────────────────────────────────────────


class EventLogTests(unittest.TestCase):
    def test_started_event_appended_on_spawn(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="t",
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            types = [e.type for e in f.event_store.list_for_task(tid)]
            self.assertEqual(types[0], EVENT_STARTED)
        finally:
            f.close()

    def test_progress_emits_append_event(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="t",
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            types = [e.type for e in f.event_store.list_for_task(tid)]
            self.assertGreaterEqual(types.count(EVENT_PROGRESS), 2)
            self.assertIn(EVENT_PHASE_CHANGE, types)
            self.assertIn(EVENT_COMPLETED, types)
        finally:
            f.close()

    def test_failed_event_appended(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_FailingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="failing", args={}, title="t"
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            latest = f.event_store.latest_for_task(tid, type=EVENT_FAILED)
            self.assertIsNotNone(latest)
        finally:
            f.close()

    def test_taskeventemit_appends_custom_event(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_EmitCustomHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="emit_custom", args={}, title="t"
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            latest = f.event_store.latest_for_task(tid, type="visited_url")
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.data, {"url": "https://x"})
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_DONE)
        finally:
            f.close()


class PhasePromotionTests(unittest.TestCase):
    def test_phase_promoted_to_column(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="t",
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            row = f.store.get(tid)
            assert row is not None
            # Last emit declared phase="matching" before completion;
            # the column should reflect it.
            self.assertEqual(row.phase, "matching")
        finally:
            f.close()

    def test_phase_change_event_records_transition(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="t",
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            phase_events = [
                e for e in f.event_store.list_for_task(tid)
                if e.type == EVENT_PHASE_CHANGE
            ]
            self.assertGreaterEqual(len(phase_events), 2)
            self.assertEqual(
                phase_events[0].data, {"from": None, "to": "scanning"}
            )
            self.assertEqual(
                phase_events[1].data, {"from": "scanning", "to": "matching"}
            )
        finally:
            f.close()


class HeartbeatBumpTests(unittest.TestCase):
    def test_emit_bumps_heartbeat(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="t",
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            row = f.store.get(tid)
            assert row is not None
            # heartbeat_at must be at least the created_at value;
            # for a handler that emitted multiple beats it should be
            # bumped past it.
            self.assertIsNotNone(row.heartbeat_at)
            self.assertGreaterEqual(
                str(row.heartbeat_at), str(row.created_at)
            )
        finally:
            f.close()


class InputStoreIntegrationTests(unittest.TestCase):
    def test_input_needed_creates_pending_row(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_InputThenCompleteHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="input_then_complete",
                args={},
                title="t",
            )
            assert tid is not None
            # Give the handler thread time to land the emit.
            import time
            for _ in range(20):
                if f.input_store.latest_pending(tid) is not None:
                    break
                time.sleep(0.05)
            row = f.input_store.latest_pending(tid)
            assert row is not None
            self.assertEqual(row.prompt, "confirm?")
            self.assertEqual(row.options, ["yes", "no"])
            self.assertEqual(row.status, INPUT_STATUS_PENDING)
        finally:
            f.close()

    def test_answer_marks_pending_row_answered(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_InputThenCompleteHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="input_then_complete",
                args={},
                title="t",
            )
            assert tid is not None
            import time
            for _ in range(20):
                if f.input_store.latest_pending(tid) is not None:
                    break
                time.sleep(0.05)
            pending = f.input_store.latest_pending(tid)
            assert pending is not None
            self.assertTrue(f.orch.answer(tid, "yes"))
            f.orch.wait_for_task(tid, timeout=2.0)
            row = f.input_store.get(pending.id)
            assert row is not None
            self.assertEqual(row.status, INPUT_STATUS_ANSWERED)
            self.assertEqual(row.response, "yes")
            answer_event = f.event_store.latest_for_task(
                tid, type=EVENT_INPUT_ANSWER
            )
            self.assertIsNotNone(answer_event)
        finally:
            f.close()


class CascadeCancelTests(unittest.TestCase):
    def test_cascade_cancels_active_children(self) -> None:
        f = _Fixture(cascade=True)
        try:
            f.orch.register_handler(_LongRunningHandler())
            parent_id = f.orch.start_task(
                user_id="u", handler_name="long_running", args={}, title="parent"
            )
            assert parent_id is not None
            # Spawn a child handler explicitly tied to parent.
            child_id = f.orch.start_task(
                user_id="u",
                handler_name="long_running",
                args={},
                title="child",
                parent_task_id=parent_id,
            )
            assert child_id is not None
            # Wait until child has fired its first progress beat so the
            # active table sees it.
            import time
            time.sleep(0.1)
            self.assertTrue(f.orch.cancel(parent_id))
            f.orch.wait_for_task(parent_id, timeout=2.0)
            f.orch.wait_for_task(child_id, timeout=2.0)
            parent = f.store.get(parent_id)
            child = f.store.get(child_id)
            assert parent is not None and child is not None
            self.assertEqual(parent.status, STATUS_CANCELLED)
            self.assertEqual(child.status, STATUS_CANCELLED)
            # Audit event records the cascade.
            cancel_event = f.event_store.latest_for_task(
                parent_id, type=EVENT_CANCELLED
            )
            self.assertIsNotNone(cancel_event)
            assert cancel_event is not None
            cascaded = (
                cancel_event.data.get("cascaded_children", [])
                if cancel_event.data
                else []
            )
            self.assertIn(child_id, cascaded)
        finally:
            f.close()

    def test_cascade_off_leaves_children(self) -> None:
        f = _Fixture(cascade=False)
        try:
            f.orch.register_handler(_LongRunningHandler())
            parent_id = f.orch.start_task(
                user_id="u", handler_name="long_running", args={}, title="parent"
            )
            assert parent_id is not None
            child_id = f.orch.start_task(
                user_id="u",
                handler_name="long_running",
                args={},
                title="child",
                parent_task_id=parent_id,
            )
            assert child_id is not None
            import time
            time.sleep(0.1)
            self.assertTrue(f.orch.cancel(parent_id))
            parent = f.store.get(parent_id)
            child = f.store.get(child_id)
            assert parent is not None and child is not None
            self.assertEqual(parent.status, STATUS_CANCELLED)
            self.assertEqual(child.status, STATUS_RUNNING)
        finally:
            f.close()


class ParentChildSpawnTests(unittest.TestCase):
    def test_parent_id_persisted(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            parent_id = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="p",
            )
            assert parent_id is not None
            f.orch.wait_for_task(parent_id, timeout=2.0)
            child_id = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="c",
                parent_task_id=parent_id,
            )
            assert child_id is not None
            f.orch.wait_for_task(child_id, timeout=2.0)
            child = f.store.get(child_id)
            assert child is not None
            self.assertEqual(child.parent_task_id, parent_id)
        finally:
            f.close()

    def test_child_spawned_event_on_parent(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            parent_id = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="p",
            )
            assert parent_id is not None
            f.orch.wait_for_task(parent_id, timeout=2.0)
            child_id = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="c",
                parent_task_id=parent_id,
            )
            assert child_id is not None
            event = f.event_store.latest_for_task(
                parent_id, type=EVENT_CHILD_SPAWNED
            )
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event.data.get("child_task_id"), child_id)
        finally:
            f.close()


class TaskSnapshotV17Tests(unittest.TestCase):
    def test_snapshot_includes_new_fields(self) -> None:
        from app.core.tasks import task_snapshot

        f = _Fixture()
        try:
            f.orch.register_handler(_PhaseCompletingHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="phase_completing",
                args={},
                title="t",
            )
            assert tid is not None
            f.orch.wait_for_task(tid, timeout=2.0)
            row = f.store.get(tid)
            assert row is not None
            snap = task_snapshot(row)
            self.assertIn("phase", snap)
            self.assertIn("parent_task_id", snap)
            self.assertIn("heartbeat_at", snap)
            self.assertEqual(snap["phase"], "matching")
        finally:
            f.close()


class SupersedeOnReaskTests(unittest.TestCase):
    def test_second_question_supersedes_first(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_InputThenCompleteHandler())
            tid = f.orch.start_task(
                user_id="u",
                handler_name="input_then_complete",
                args={},
                title="t",
            )
            assert tid is not None
            import time
            for _ in range(20):
                if f.input_store.latest_pending(tid) is not None:
                    break
                time.sleep(0.05)
            first = f.input_store.latest_pending(tid)
            assert first is not None
            # Directly invoke the helper that the orchestrator uses
            # via the input-needed path (without going through the
            # handler again).
            superseded = f.input_store.supersede_pending_for_task(tid)
            self.assertEqual(superseded, 1)
            # First row flipped to SUPERSEDED.
            first_after = f.input_store.get(first.id)
            assert first_after is not None
            self.assertEqual(first_after.status, INPUT_STATUS_SUPERSEDED)
            # Now answer should fail because the handler thread is
            # still waiting on its own on_input — cancel the task to
            # tear down cleanly.
            f.orch.cancel(tid)
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
