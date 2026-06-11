"""Tests for FileWriteHandler, the write_file skill gating, and the
workflow wait-through-awaiting_input behaviour."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.core.tasks.approval import APPROVE, APPROVE_ALL, DENY
from app.core.tasks.capabilities import CAPABILITY_FILE_WRITE
from app.core.tasks.handlers.file_write import FileWriteHandler
from app.core.tasks.sandbox import FileTaskRoot
from app.core.tasks.task_handler import (
    STATUS_AWAITING_INPUT,
    TaskCompleted,
    TaskFailed,
    TaskInputNeeded,
)
from app.core.tasks.workflow import GoalWorkflowHandler, WorkflowSkillRegistry
from app.core.tasks.workflow.skill_registry import (
    WORKFLOW_SKILL_WRITE_FILE,
    build_builtin_skill_registry,
)


class _Emitter:
    """Collects emitted outcomes for assertions."""

    def __init__(self) -> None:
        self.outcomes: list[Any] = []

    def __call__(self, outcome: Any) -> None:
        self.outcomes.append(outcome)

    def last(self) -> Any:
        return self.outcomes[-1] if self.outcomes else None

    def has(self, cls: type) -> bool:
        return any(isinstance(o, cls) for o in self.outcomes)


class FileWriteHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root_dir = Path(self._tmp.name) / "writable"
        self.root_dir.mkdir()
        self.ro_dir = Path(self._tmp.name) / "readonly"
        self.ro_dir.mkdir()
        self.writable = FileTaskRoot(
            label="Notes", path=str(self.root_dir), read_only=False
        )
        self.readonly = FileTaskRoot(
            label="Docs", path=str(self.ro_dir), read_only=True
        )

    def _handler(self, *, approval: str = "auto", **kw: Any) -> FileWriteHandler:
        self.session_approved: list[str] = []
        return FileWriteHandler(
            roots=[self.writable, self.readonly],
            max_bytes=kw.get("max_bytes", 262144),
            allowed_extensions=kw.get(
                "allowed_extensions", (".txt", ".md")
            ),
            resolve_approval=lambda _cap: approval,
            mark_session_approved=lambda cap: self.session_approved.append(cap),
        )

    # ── create (non-destructive) ─────────────────────────────────────

    def test_create_new_file_no_approval(self) -> None:
        h = self._handler(approval="ask")  # ask, but new file isn't destructive
        emit = _Emitter()
        h.start({"path": "Notes:hello.txt", "content": "hi there"}, emit)
        self.assertTrue(emit.has(TaskCompleted))
        self.assertFalse(emit.has(TaskInputNeeded))
        self.assertEqual(
            (self.root_dir / "hello.txt").read_text(encoding="utf-8"),
            "hi there",
        )

    # ── overwrite (destructive) gating ───────────────────────────────

    def test_overwrite_auto_writes_immediately(self) -> None:
        (self.root_dir / "todo.txt").write_text("old", encoding="utf-8")
        h = self._handler(approval="auto")
        emit = _Emitter()
        h.start({"path": "Notes:todo.txt", "content": "new"}, emit)
        self.assertTrue(emit.has(TaskCompleted))
        self.assertEqual(
            (self.root_dir / "todo.txt").read_text(encoding="utf-8"), "new"
        )

    def test_overwrite_ask_emits_approval_then_approve(self) -> None:
        (self.root_dir / "todo.txt").write_text("old", encoding="utf-8")
        h = self._handler(approval="ask")
        emit = _Emitter()
        state = h.start({"path": "Notes:todo.txt", "content": "new"}, emit)
        self.assertTrue(emit.has(TaskInputNeeded))
        self.assertEqual(state.get("phase"), "awaiting_approval")
        # File unchanged until approved.
        self.assertEqual(
            (self.root_dir / "todo.txt").read_text(encoding="utf-8"), "old"
        )
        emit2 = _Emitter()
        h.on_input(state, APPROVE, emit2)
        self.assertTrue(emit2.has(TaskCompleted))
        self.assertEqual(
            (self.root_dir / "todo.txt").read_text(encoding="utf-8"), "new"
        )

    def test_overwrite_ask_deny_leaves_file(self) -> None:
        (self.root_dir / "todo.txt").write_text("old", encoding="utf-8")
        h = self._handler(approval="ask")
        emit = _Emitter()
        state = h.start({"path": "Notes:todo.txt", "content": "new"}, emit)
        emit2 = _Emitter()
        out_state = h.on_input(state, DENY, emit2)
        self.assertTrue(emit2.has(TaskCompleted))
        completed = [o for o in emit2.outcomes if isinstance(o, TaskCompleted)][0]
        self.assertTrue(completed.result.get("declined"))
        self.assertEqual(out_state.get("phase"), "declined")
        self.assertEqual(
            (self.root_dir / "todo.txt").read_text(encoding="utf-8"), "old"
        )

    def test_approve_all_marks_session(self) -> None:
        (self.root_dir / "todo.txt").write_text("old", encoding="utf-8")
        h = self._handler(approval="ask")
        emit = _Emitter()
        state = h.start({"path": "Notes:todo.txt", "content": "new"}, emit)
        h.on_input(state, APPROVE_ALL, _Emitter())
        self.assertEqual(self.session_approved, [CAPABILITY_FILE_WRITE])

    # ── append ───────────────────────────────────────────────────────

    def test_append_to_new_file(self) -> None:
        h = self._handler(approval="ask")
        emit = _Emitter()
        h.start({"path": "Notes:log.txt", "op": "append", "content": "a"}, emit)
        self.assertTrue(emit.has(TaskCompleted))
        self.assertEqual(
            (self.root_dir / "log.txt").read_text(encoding="utf-8"), "a"
        )

    def test_append_to_existing_is_destructive(self) -> None:
        (self.root_dir / "log.txt").write_text("a", encoding="utf-8")
        h = self._handler(approval="ask")
        emit = _Emitter()
        state = h.start(
            {"path": "Notes:log.txt", "op": "append", "content": "b"}, emit
        )
        self.assertTrue(emit.has(TaskInputNeeded))
        h.on_input(state, APPROVE, _Emitter())
        self.assertEqual(
            (self.root_dir / "log.txt").read_text(encoding="utf-8"), "ab"
        )

    # ── replace ──────────────────────────────────────────────────────

    def test_replace_in_existing(self) -> None:
        (self.root_dir / "doc.md").write_text("hello world", encoding="utf-8")
        h = self._handler(approval="auto")
        emit = _Emitter()
        h.start(
            {
                "path": "Notes:doc.md",
                "op": "replace",
                "find": "world",
                "replace": "there",
            },
            emit,
        )
        self.assertTrue(emit.has(TaskCompleted))
        self.assertEqual(
            (self.root_dir / "doc.md").read_text(encoding="utf-8"),
            "hello there",
        )

    def test_replace_find_not_found_fails(self) -> None:
        (self.root_dir / "doc.md").write_text("hello", encoding="utf-8")
        h = self._handler(approval="auto")
        emit = _Emitter()
        h.start(
            {"path": "Notes:doc.md", "op": "replace", "find": "xyz", "replace": "q"},
            emit,
        )
        self.assertTrue(emit.has(TaskFailed))

    def test_replace_missing_file_fails(self) -> None:
        h = self._handler(approval="auto")
        emit = _Emitter()
        h.start(
            {"path": "Notes:nope.md", "op": "replace", "find": "a", "replace": "b"},
            emit,
        )
        self.assertTrue(emit.has(TaskFailed))

    # ── gating / safety ──────────────────────────────────────────────

    def test_readonly_root_rejected(self) -> None:
        h = self._handler(approval="auto")
        emit = _Emitter()
        h.start({"path": "Docs:x.txt", "content": "no"}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_extension_not_allowed(self) -> None:
        h = self._handler(approval="auto", allowed_extensions=(".txt",))
        emit = _Emitter()
        h.start({"path": "Notes:script.py", "content": "x"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertFalse((self.root_dir / "script.py").exists())

    def test_byte_cap_enforced(self) -> None:
        h = self._handler(approval="auto", max_bytes=8)
        emit = _Emitter()
        h.start({"path": "Notes:big.txt", "content": "x" * 100}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_ambiguous_bare_path_rejected(self) -> None:
        # Two writable roots + a bare path -> ask for a label.
        other = Path(self._tmp.name) / "writable2"
        other.mkdir()
        h = FileWriteHandler(
            roots=[
                self.writable,
                FileTaskRoot(label="More", path=str(other), read_only=False),
            ],
            allowed_extensions=(".txt",),
            resolve_approval=lambda _cap: "auto",
        )
        emit = _Emitter()
        h.start({"path": "ambiguous.txt", "content": "x"}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_no_writable_root(self) -> None:
        h = FileWriteHandler(
            roots=[self.readonly],
            allowed_extensions=(".txt",),
            resolve_approval=lambda _cap: "auto",
        )
        emit = _Emitter()
        h.start({"path": "Docs:x.txt", "content": "x"}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_atomic_write_no_temp_left_behind(self) -> None:
        h = self._handler(approval="auto")
        h.start({"path": "Notes:a.txt", "content": "data"}, _Emitter())
        leftovers = [
            p for p in os.listdir(self.root_dir) if p.startswith(".aiko_write_")
        ]
        self.assertEqual(leftovers, [])


class WriteFileSkillGatingTests(unittest.TestCase):
    def test_skill_absent_by_default(self) -> None:
        reg = build_builtin_skill_registry(file_write_enabled=False)
        self.assertNotIn(WORKFLOW_SKILL_WRITE_FILE, reg.names())

    def test_skill_present_when_enabled(self) -> None:
        reg = build_builtin_skill_registry(file_write_enabled=True)
        self.assertIn(WORKFLOW_SKILL_WRITE_FILE, reg.names())


class _Row:
    def __init__(self, status: str, result: Any = None, error: str | None = None):
        self.status = status
        self.result = result
        self.error = error


class _FakeOrch:
    """Minimal orchestrator stub for _wait_child tests."""

    def __init__(self) -> None:
        self.rows: dict[int, _Row] = {}
        self.wait_script: dict[int, list[str]] = {}
        self.cancelled: list[int] = []

    def get(self, tid: int) -> Any:
        return self.rows.get(tid)

    def cancel(self, tid: int) -> bool:
        self.cancelled.append(tid)
        row = self.rows.get(tid)
        if row is not None:
            row.status = "cancelled"
        return True

    def wait_for_task(self, tid: int, timeout: float) -> str:
        seq = self.wait_script.get(tid)
        status = seq.pop(0) if seq else "done"
        if status in ("done", "failed", "cancelled") and tid in self.rows:
            self.rows[tid].status = status
        return status


class WaitThroughAwaitingInputTests(unittest.TestCase):
    def _handler(self, orch: _FakeOrch) -> GoalWorkflowHandler:
        return GoalWorkflowHandler(
            orchestrator=orch,
            skill_registry=WorkflowSkillRegistry(),
            worker_client_provider=lambda: None,
            child_wait_timeout_seconds=5.0,
        )

    def test_waits_through_awaiting_input_until_done(self) -> None:
        orch = _FakeOrch()
        orch.rows[1] = _Row("running")  # parent
        orch.rows[2] = _Row(
            STATUS_AWAITING_INPUT, result={"summary": "wrote it"}
        )
        # First wait times out (child parked on approval), second resolves.
        orch.wait_script[2] = ["timeout", "done"]
        status, row = self._handler(orch)._wait_child(1, 2)
        self.assertEqual(status, "done")
        self.assertNotIn(2, orch.cancelled)

    def test_real_timeout_cancels_running_child(self) -> None:
        orch = _FakeOrch()
        orch.rows[1] = _Row("running")
        orch.rows[2] = _Row("running")  # not awaiting input
        orch.wait_script[2] = ["timeout"]
        status, _row = self._handler(orch)._wait_child(1, 2)
        self.assertEqual(status, "timeout")
        self.assertIn(2, orch.cancelled)

    def test_parent_cancel_while_awaiting_cancels_child(self) -> None:
        orch = _FakeOrch()
        orch.rows[1] = _Row("cancelled")  # parent already terminal
        orch.rows[2] = _Row(STATUS_AWAITING_INPUT)
        orch.wait_script[2] = ["timeout"]
        status, _row = self._handler(orch)._wait_child(1, 2)
        self.assertEqual(status, "cancelled")
        self.assertIn(2, orch.cancelled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
