"""Schema v17 recovery interaction with the input store.

When a ``running`` row is demoted to ``interrupted`` on boot the
orchestrator's :meth:`register_recovered` hook also cancels any
orphan pending input rows. ``awaiting_input`` rows are preserved
as-is — their pending input row stays valid because the user's
answer hasn't been given yet.
"""
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.brain import BrainEventQueue
from app.core.infra.chat_database import ChatDatabase
from app.core.tasks import (
    INPUT_STATUS_CANCELLED,
    INPUT_STATUS_PENDING,
    STATUS_INTERRUPTED,
    TaskEventStore,
    TaskInputStore,
    TaskOrchestrator,
    TaskStore,
    recover_interrupted_tasks,
)


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)
        self.event_store = TaskEventStore(self.db)
        self.input_store = TaskInputStore(self.db)
        self.queue = BrainEventQueue()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="recover-test"
        )
        self.orch = TaskOrchestrator(
            self.store,
            queue=self.queue,
            executor=self.executor,
            event_store=self.event_store,
            input_store=self.input_store,
            heartbeat_enabled=False,
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


class RecoveryInputsTests(unittest.TestCase):
    def test_running_recovery_cancels_orphan_pending_input(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            # Manually create a pending input row (handler crashed
            # mid-question).
            iid = f.input_store.create(tid, prompt="?")
            self.assertEqual(
                f.input_store.get(iid).status,  # type: ignore[union-attr]
                INPUT_STATUS_PENDING,
            )
            report = recover_interrupted_tasks(
                f.store, orchestrator=f.orch, resume_on_boot=True
            )
            self.assertIn(tid, report.interrupted)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_INTERRUPTED)
            inp = f.input_store.get(iid)
            assert inp is not None
            self.assertEqual(inp.status, INPUT_STATUS_CANCELLED)
        finally:
            f.close()

    def test_awaiting_input_recovery_preserves_pending_row(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            # Manually flip status to awaiting_input (simulates the
            # state at crash time).
            f.store.mark_awaiting_input(tid, prompt="?", options=None)
            iid = f.input_store.create(tid, prompt="?")
            report = recover_interrupted_tasks(
                f.store, orchestrator=f.orch, resume_on_boot=True
            )
            self.assertIn(tid, report.preserved)
            inp = f.input_store.get(iid)
            assert inp is not None
            # awaiting_input rows are NOT touched by recovery, so
            # the pending input stays pending.
            self.assertEqual(inp.status, INPUT_STATUS_PENDING)
        finally:
            f.close()

    def test_recovery_report_counts(self) -> None:
        f = _Fixture()
        try:
            t1 = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            t2 = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            f.store.mark_awaiting_input(t2, prompt="?", options=None)
            report = recover_interrupted_tasks(
                f.store, orchestrator=f.orch, resume_on_boot=True
            )
            self.assertEqual(report.total_scanned, 2)
            self.assertIn(t1, report.interrupted)
            self.assertIn(t2, report.preserved)
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
