"""Pin the structured-log field shape for the brain-orchestration tier.

``docs/brain-orchestration.md`` *Logging* section lists exactly which
``key=value`` fields each lifecycle line must carry. Operators rely on
these names — ``task=`` for the contextvar correlation, ``from=`` /
``to=`` for state transitions, ``handler=`` / ``initiated_by=`` /
``running_count=`` for spawn metrics — to grep across log files and
ring-buffer dumps when something goes wrong.

Any change to a log line shape must update this test in lockstep. The
test captures every INFO record emitted to the ``app.task_*`` loggers
during a curated set of lifecycle scenarios and asserts that:

* Each scenario emits the *named* line at least once.
* The expected key-value tokens are present (regex match on the
  formatted message body).
* No banned legacy field name slips into a new line.

Field shape is enforced via :func:`logging.Formatter` so a bug in the
crash_logging adapter would also surface here.
"""
from __future__ import annotations

import logging
import re
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.brain import BrainEventQueue
from app.core.infra.chat_database import ChatDatabase
from app.core.tasks import (
    STATUS_AWAITING_INPUT,
    STATUS_DONE,
    STATUS_FAILED,
    TaskCompleted,
    TaskFailed,
    TaskInputNeeded,
    TaskOrchestrator,
    TaskProgress,
    TaskStore,
    recover_interrupted_tasks,
)


# ── log-capture helpers ─────────────────────────────────────────────


class _ListHandler(logging.Handler):
    """In-memory handler that buffers full formatted lines."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.formatted: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            self.formatted.append(self.format(record))
        except Exception:
            self.formatted.append(record.getMessage())


def _attach_handler(handler: _ListHandler, *names: str) -> None:
    formatter = logging.Formatter("%(name)s %(message)s")
    handler.setFormatter(formatter)
    for name in names:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        # Make sure INFO survives the per-test root-level threshold.
        if logger.level == 0 or logger.level > logging.DEBUG:
            logger.setLevel(logging.DEBUG)


def _detach_handler(handler: _ListHandler, *names: str) -> None:
    for name in names:
        logging.getLogger(name).removeHandler(handler)


def _captured_text(handler: _ListHandler) -> str:
    """All buffered lines concatenated with newlines so a single
    ``re.search`` walks every record."""
    return "\n".join(handler.formatted)


# ── helper handlers reused across scenarios ─────────────────────────


class CompletingHandler:
    name = "completing"

    def start(self, args, emit):
        emit(TaskProgress(progress=0.5, message="halfway"))
        emit(TaskCompleted(result={"summary": "ok"}))
        return {"args": args}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class FailingHandler:
    name = "failing"

    def start(self, args, emit):
        emit(TaskFailed(error="something broke"))
        return {"args": args}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class AskingHandler:
    name = "asking"

    def start(self, args, emit):
        emit(TaskInputNeeded(prompt="which one?", options=["a", "b"]))
        return {"args": args}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        emit(TaskCompleted(result={"chosen": answer}))
        return {**state, "answer": answer}

    def cancel(self, state):
        pass


# ── fixtures ────────────────────────────────────────────────────────


class _Fixture:
    LOGGERS = ("app.task_orchestrator", "app.task_store")

    def __init__(self) -> None:
        # Attach the log handler FIRST so the orchestrator's
        # ``task-orchestrator init:`` line is captured along with the
        # rest of the lifecycle.
        self.handler = _ListHandler()
        _attach_handler(self.handler, *self.LOGGERS)
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)
        self.queue = BrainEventQueue()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="brainlog"
        )
        self.orch = TaskOrchestrator(
            self.store,
            queue=self.queue,
            executor=self.executor,
            per_user_cap=8,
        )

    def close(self) -> None:
        _detach_handler(self.handler, *self.LOGGERS)
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

    def wait(self, tid: int, target: str, *, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.orch.get(tid)
            if row and row.status == target:
                return
            time.sleep(0.005)
        raise AssertionError(f"task {tid} never reached {target}")


# ── tests ───────────────────────────────────────────────────────────


class StoreLogFieldsTests(unittest.TestCase):
    def test_create_log_carries_required_fields(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="jacob",
                handler_name="file_search",
                title="t",
                args={}, state={},
                notify_aiko=False,
                visible_to_user=True,
                initiated_by="background",
            )
            text = _captured_text(f.handler)
            self.assertRegex(text, rf"task created: task={tid} ")
            self.assertIn("user=jacob", text)
            self.assertIn("handler=file_search", text)
            self.assertIn("initiated_by=background", text)
            self.assertIn("notify_aiko=0", text)
            self.assertIn("visible_to_user=1", text)
        finally:
            f.close()

    def test_done_log_carries_result_size(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_done(tid, result={"x": 1})
            text = _captured_text(f.handler)
            self.assertRegex(text, rf"task done: task={tid} result_size=\d+")
        finally:
            f.close()

    def test_failed_log_carries_error_string(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_failed(tid, error="boom")
            text = _captured_text(f.handler)
            self.assertRegex(text, rf"task failed: task={tid} error=boom")
        finally:
            f.close()

    def test_cancelled_log_carries_task_id(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_cancelled(tid)
            self.assertRegex(
                _captured_text(f.handler), rf"task cancelled: task={tid}"
            )
        finally:
            f.close()

    def test_awaiting_input_log_carries_prompt_len_and_options(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_awaiting_input(tid, prompt="abcde", options=["x", "y"])
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                rf"task awaiting input: task={tid} prompt_len=5 options=2",
            )
        finally:
            f.close()


class OrchestratorLogFieldsTests(unittest.TestCase):
    """The orchestrator owns the higher-level lifecycle lines:

    * ``task spawned:`` — after a successful ``start()``.
    * ``task transition:`` — every state change with ``from=`` / ``to=``.
    * ``task completed:`` — terminal state (done / failed / cancelled).
    * ``task spawn rejected:`` — per-user cap / unknown-handler WARNINGs.
    * ``task recovered on boot:`` — recovery hook.
    """

    def test_spawned_line_carries_handler_initiated_and_count(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="jacob",
                handler_name="completing",
                args={}, title="t",
                initiated_by="aiko",
                notify_aiko=True,
                visible_to_user=True,
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            text = _captured_text(f.handler)
            self.assertRegex(text, rf"task spawned: task={tid} ")
            self.assertIn("handler=completing", text)
            self.assertIn("initiated_by=aiko", text)
            self.assertRegex(text, r"notify_aiko=1\b")
            self.assertRegex(text, r"visible_to_user=1\b")
            self.assertRegex(text, r"running_count=\d+\b")
        finally:
            f.close()

    def test_transition_lines_carry_from_to(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(AskingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="asking",
                args={}, title="t",
            )
            f.wait(tid, STATUS_AWAITING_INPUT)
            f.orch.answer(tid, "a")
            f.orch.wait_for_task(tid, timeout=2.0)
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                rf"task transition: task={tid} from=running to=awaiting_input",
            )
            self.assertRegex(
                text,
                rf"task transition: task={tid} from=awaiting_input to=running",
            )
        finally:
            f.close()

    def test_completed_line_carries_status_notify_result_size(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                rf"task completed: task={tid} status={STATUS_DONE} "
                rf"notify_aiko=1 result_size=\d+",
            )
        finally:
            f.close()

    def test_failed_completed_line_carries_error_field(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(FailingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="failing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                rf"task completed: task={tid} status={STATUS_FAILED} "
                rf"notify_aiko=1 error=something broke",
            )
        finally:
            f.close()

    def test_spawn_rejected_logs_reason_and_user(self) -> None:
        f = _Fixture()
        try:
            # Force cap=1 by inserting a stub active row.
            f.orch._per_user_cap = 1
            f.store.create(
                user_id="alice", handler_name="h", title="t",
                args={}, state={},
            )
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="alice", handler_name="completing",
                args={}, title="t",
            )
            self.assertIsNone(tid)
            text = _captured_text(f.handler)
            self.assertIn("task spawn rejected:", text)
            self.assertIn("reason=per_user_cap", text)
            self.assertIn("user=alice", text)
            self.assertRegex(text, r"running_count=\d+ cap=1")
        finally:
            f.close()

    def test_spawn_rejected_unknown_handler(self) -> None:
        f = _Fixture()
        try:
            tid = f.orch.start_task(
                user_id="u", handler_name="nope",
                args={}, title="t",
            )
            self.assertIsNone(tid)
            text = _captured_text(f.handler)
            self.assertIn("reason=unknown_handler", text)
            self.assertIn("handler=nope", text)
        finally:
            f.close()

    def test_recovered_on_boot_line(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            recover_interrupted_tasks(f.store, orchestrator=f.orch)
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                rf"task recovered on boot: task={tid} was_status=running "
                rf"now_status=interrupted",
            )
            # Also check the summary log.
            self.assertRegex(
                text,
                r"task recovery: scanned=1 interrupted=1 preserved=0 failed=0 "
                r"resume_on_boot=1",
            )
        finally:
            f.close()

    def test_init_line_carries_handlers_and_cap(self) -> None:
        f = _Fixture()
        try:
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                r"task-orchestrator init: handlers=0 per_user_cap=8 queue=wired",
            )
        finally:
            f.close()


class ContextVarFieldTests(unittest.TestCase):
    """The ``task_id`` contextvar must reach the formatter the live
    app uses. Logging captured at the raw-message layer doesn't see
    it (the contextvar is stamped via a logging filter in
    crash_logging) — but we can verify the orchestrator's INFO lines
    *do* include the literal ``task=NNN`` substring in the body so a
    grep target works either way.
    """

    def test_every_lifecycle_line_carries_task_field(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(CompletingHandler())
            tid = f.orch.start_task(
                user_id="u", handler_name="completing",
                args={}, title="t",
            )
            f.orch.wait_for_task(tid, timeout=2.0)
            text = _captured_text(f.handler)
            # Filter to orchestrator lines only (drop store + init).
            orch_lines = [
                line for line in text.split("\n")
                if "app.task_orchestrator " in line
                and "task=" in line.split("app.task_orchestrator ")[1]
            ]
            self.assertGreater(len(orch_lines), 0)
            for line in orch_lines:
                self.assertRegex(line, rf"task={tid}\b")
        finally:
            f.close()


# ── chunk-3 BrainLoop log-field tests ───────────────────────────────


class _BrainLoopFixture:
    """Minimal ``BrainLoop`` + log-capture fixture, separate from the
    orchestrator's ``_Fixture`` because the loop tests don't need
    SQLite or a thread pool.
    """

    LOGGERS = ("app.brain_loop", "app.brain_queue")

    def __init__(self, **loop_kwargs) -> None:
        from app.core.brain import BrainLoop

        self.handler = _ListHandler()
        _attach_handler(self.handler, *self.LOGGERS)
        self.loop = BrainLoop(**loop_kwargs)

    def close(self) -> None:
        try:
            self.loop.stop()
        finally:
            _detach_handler(self.handler, *self.LOGGERS)


def _wait_for(predicate, *, deadline_s: float = 1.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class BrainLoopLogFieldsTests(unittest.TestCase):
    """``app.brain_loop`` INFO + ERROR lines are part of the
    operator-facing log contract documented in
    ``docs/brain-orchestration.md`` *Logging*.

    Pinned shapes — change here means a doc + ops update:

    * ``brain-loop init: handlers=N``
    * ``brain-loop start: consumer_active=True``
    * ``brain-loop stop: drained=N deferred=N total_dispatched=N``
    * ``brain-loop register: kind=K handlers=N`` (or
      ``brain-loop register replaced: kind=K handlers=N``)
    * ``brain-loop dispatched: kind=K route=R elapsed_ms=F gate_waited_ms=F``
    * ``brain-loop deferred: kind=K reason=R deferred_count=N``
    * ``brain-loop no handler: kind=K``
    * ``brain-loop handler error: kind=K route=R elapsed_ms=F exc=...``
    """

    def test_init_line_present_on_construction(self) -> None:
        f = _BrainLoopFixture()
        try:
            text = _captured_text(f.handler)
            self.assertRegex(text, r"brain-loop init: handlers=0")
        finally:
            f.close()

    def test_start_line_marks_consumer_active(self) -> None:
        f = _BrainLoopFixture()
        try:
            f.loop.start()
            text = _captured_text(f.handler)
            self.assertRegex(
                text, r"brain-loop start: consumer_active=True"
            )
        finally:
            f.close()

    def test_stop_line_carries_drained_deferred_dispatched(self) -> None:
        from app.core.brain import KIND_USER_MESSAGE, UserMessageEvent

        f = _BrainLoopFixture()
        try:
            f.loop.register_handler(KIND_USER_MESSAGE, lambda e: None)
            f.loop.start()
            f.loop.enqueue(UserMessageEvent(text="hi"))
            self.assertTrue(
                _wait_for(lambda: f.loop.queue.depth() == 0)
            )
            f.loop.stop()
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                r"brain-loop stop: drained=\d+ deferred=\d+ "
                r"total_dispatched=1",
            )
        finally:
            f.close()

    def test_register_first_logs_register_line(self) -> None:
        from app.core.brain import KIND_USER_MESSAGE

        f = _BrainLoopFixture()
        try:
            f.loop.register_handler(KIND_USER_MESSAGE, lambda e: None)
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                r"brain-loop register: kind=user_message handlers=1",
            )
        finally:
            f.close()

    def test_register_again_logs_replaced_line(self) -> None:
        from app.core.brain import KIND_USER_MESSAGE

        f = _BrainLoopFixture()
        try:
            f.loop.register_handler(KIND_USER_MESSAGE, lambda e: None)
            f.loop.register_handler(KIND_USER_MESSAGE, lambda e: None)
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                r"brain-loop register replaced: kind=user_message "
                r"handlers=1",
            )
        finally:
            f.close()

    def test_dispatched_line_carries_all_fields(self) -> None:
        from app.core.brain import KIND_USER_MESSAGE, UserMessageEvent

        f = _BrainLoopFixture()
        try:
            done = __import__("threading").Event()

            def named_handler(event: object) -> None:
                done.set()

            f.loop.register_handler(KIND_USER_MESSAGE, named_handler)
            f.loop.start()
            f.loop.enqueue(UserMessageEvent(text="hello"))
            self.assertTrue(done.wait(timeout=1.0))
            f.loop.stop()
            text = _captured_text(f.handler)
            # Single regex covers all required fields in order.
            self.assertRegex(
                text,
                r"brain-loop dispatched: kind=user_message "
                r"route=named_handler elapsed_ms=[\d.]+ "
                r"gate_waited_ms=[\d.]+",
            )
        finally:
            f.close()

    def test_deferred_line_carries_reason_and_count(self) -> None:
        from app.core.brain import KIND_PROACTIVE, ProactiveEvent

        gate = {"open": False}
        f = _BrainLoopFixture(free_to_speak=lambda: gate["open"])
        try:
            f.loop.register_handler(KIND_PROACTIVE, lambda e: None)
            f.loop.start()
            f.loop.enqueue(ProactiveEvent(source="typed_silence"))
            self.assertTrue(
                _wait_for(lambda: f.loop.pending_deferred_count() == 1)
            )
            f.loop.stop()
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                r"brain-loop deferred: kind=proactive reason=gate_closed "
                r"deferred_count=1",
            )
        finally:
            f.close()

    def test_no_handler_line_carries_kind(self) -> None:
        from app.core.brain import UserMessageEvent

        f = _BrainLoopFixture()
        try:
            f.loop.start()
            f.loop.enqueue(UserMessageEvent(text="orphan"))
            self.assertTrue(
                _wait_for(lambda: f.loop.queue.depth() == 0)
            )
            f.loop.stop()
            text = _captured_text(f.handler)
            self.assertRegex(
                text, r"brain-loop no handler: kind=user_message"
            )
        finally:
            f.close()

    def test_handler_error_line_carries_kind_route_exc(self) -> None:
        from app.core.brain import KIND_USER_MESSAGE, UserMessageEvent

        f = _BrainLoopFixture()
        try:
            def my_boom(event: object) -> None:
                raise RuntimeError("boom!")

            f.loop.register_handler(KIND_USER_MESSAGE, my_boom)
            f.loop.start()
            f.loop.enqueue(UserMessageEvent(text="x"))
            self.assertTrue(
                _wait_for(
                    lambda: f.loop.metrics_snapshot()["errors"] == 1
                )
            )
            f.loop.stop()
            text = _captured_text(f.handler)
            self.assertRegex(
                text,
                r"brain-loop handler error: kind=user_message "
                r"route=my_boom elapsed_ms=[\d.]+ "
                r"exc=RuntimeError\('boom!'\)",
            )
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
