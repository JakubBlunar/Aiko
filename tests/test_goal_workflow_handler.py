"""Tests for the GoalWorkflowHandler plan→act→observe loop."""
from __future__ import annotations

import json
import threading
import unittest
from typing import Any

from app.core.infra.log_context import reset_task_id, set_task_id
from app.core.tasks.task_handler import TaskCompleted, TaskFailed, TaskOutcome
from app.core.tasks.workflow.goal_workflow_handler import (
    OUTCOME_MISSING_CAPABILITY,
    GoalWorkflowHandler,
)
from app.core.tasks.workflow.skill_registry import build_builtin_skill_registry


class _Row:
    def __init__(
        self,
        rid: int,
        *,
        status: str = "running",
        result: dict | None = None,
        error: str | None = None,
        handler_name: str = "",
    ) -> None:
        self.id = rid
        self.status = status
        self.result = result
        self.error = error
        self.handler_name = handler_name


def _canned(handler_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if handler_name == "file_search":
        return {"summary": "found 2 files", "match_count": 2, "matches": ["a", "b"]}
    if handler_name == "file_read":
        return {"summary": "a preview", "content": "the file body"}
    if handler_name == "web_search":
        return {"summary": "3 web results", "result_count": 3}
    return {"summary": "done"}


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.rows: dict[int, _Row] = {}
        self._next = 1000
        self.spawned: list[tuple[str, dict, int]] = []
        self.cancelled: list[int] = []

    def set_parent(self, task_id: int, status: str = "running") -> None:
        self.rows[task_id] = _Row(task_id, status=status)

    def start_task(self, *, handler_name: str, args: dict, **kw: Any) -> int | None:
        self._next += 1
        cid = self._next
        self.rows[cid] = _Row(
            cid, status="done", result=_canned(handler_name, args),
            handler_name=handler_name,
        )
        self.spawned.append((handler_name, args, cid))
        return cid

    def wait_for_task(self, task_id: int, *, timeout: float = 5.0) -> str:
        row = self.rows.get(task_id)
        return row.status if row is not None else "failed"

    def get(self, task_id: int) -> _Row | None:
        return self.rows.get(task_id)

    def cancel(self, task_id: int) -> bool:
        self.cancelled.append(task_id)
        if task_id in self.rows:
            self.rows[task_id].status = "cancelled"
        return True


class _ScriptedClient:
    """Returns scripted planner JSON responses in order."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def chat_json(self, messages, **kw):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx], None


class _Harness:
    """Captures emits + signals a terminal outcome via an Event."""

    def __init__(self) -> None:
        self.outcomes: list[TaskOutcome] = []
        self.done = threading.Event()

    def emit(self, outcome: TaskOutcome) -> None:
        self.outcomes.append(outcome)
        if isinstance(outcome, (TaskCompleted, TaskFailed)):
            self.done.set()

    def terminal(self) -> TaskOutcome | None:
        for o in reversed(self.outcomes):
            if isinstance(o, (TaskCompleted, TaskFailed)):
                return o
        return None


def _run(
    orch: _FakeOrchestrator,
    client: _ScriptedClient,
    *,
    parent_id: int = 42,
    goal: str = "find new files and read them",
    max_iterations: int = 6,
    max_children: int = 8,
    on_gap: Any = None,
) -> _Harness:
    orch.set_parent(parent_id)
    registry = build_builtin_skill_registry()
    handler = GoalWorkflowHandler(
        orchestrator=orch,
        skill_registry=registry,
        worker_client_provider=lambda: client,
        model_provider=lambda: "worker-model",
        user_name_provider=lambda: "Jacob",
        on_capability_gap=on_gap,
        max_iterations=max_iterations,
        max_children=max_children,
        child_wait_timeout_seconds=5.0,
    )
    h = _Harness()
    tok = set_task_id(f"{parent_id:08x}")
    try:
        state = handler.start({"goal": goal, "user_id": "u1"}, h.emit)
        assert state["phase"] == "planning"
        h.done.wait(timeout=5.0)
    finally:
        reset_task_id(tok)
    return h


class HappyPathTests(unittest.TestCase):
    def test_search_then_read_then_finish(self) -> None:
        orch = _FakeOrchestrator()
        client = _ScriptedClient(
            [
                {"action": "search_files", "args": {"only_new": True}, "reason": "go"},
                {"action": "read_file", "args": {"path": "Documents:a.md"}},
                {"action": "finish", "findings": "Found and read 1 new note.",
                 "outcome": "success"},
            ]
        )
        h = _run(orch, client)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertEqual(term.result["outcome"], "success")
        self.assertIn("Found and read", term.result["content"])
        # Two children spawned: file_search + file_read.
        kinds = [s[0] for s in orch.spawned]
        self.assertEqual(kinds, ["file_search", "file_read"])
        self.assertEqual(len(term.result["steps"]), 2)

    def test_empty_goal_fails_immediately(self) -> None:
        orch = _FakeOrchestrator()
        orch.set_parent(7)
        handler = GoalWorkflowHandler(
            orchestrator=orch,
            skill_registry=build_builtin_skill_registry(),
            worker_client_provider=lambda: _ScriptedClient([]),
        )
        h = _Harness()
        tok = set_task_id("00000007")
        try:
            handler.start({"goal": "  "}, h.emit)
        finally:
            reset_task_id(tok)
        self.assertIsInstance(h.terminal(), TaskFailed)


class MissingCapabilityTests(unittest.TestCase):
    def test_missing_capability_records_gap_and_finishes(self) -> None:
        orch = _FakeOrchestrator()
        gaps: list[dict] = []
        client = _ScriptedClient(
            [
                {
                    "action": "missing_capability",
                    "missing_capability": "open and click around a web page",
                    "reason": "no browser skill",
                }
            ]
        )
        h = _run(orch, client, on_gap=gaps.append)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertEqual(term.result["outcome"], OUTCOME_MISSING_CAPABILITY)
        self.assertIn("open and click", term.result["content"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["capability"], "open and click around a web page")
        # No child was spawned.
        self.assertEqual(orch.spawned, [])


class GuardTests(unittest.TestCase):
    def test_repeat_guard_finishes_partial(self) -> None:
        orch = _FakeOrchestrator()
        client = _ScriptedClient(
            [
                {"action": "search_files", "args": {"query": "x"}},
                {"action": "search_files", "args": {"query": "x"}},  # exact repeat
            ]
        )
        h = _run(orch, client)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertEqual(term.result["outcome"], "partial")
        # Only the first search ran.
        self.assertEqual(len(orch.spawned), 1)

    def test_child_cap_finishes_partial(self) -> None:
        orch = _FakeOrchestrator()
        # Planner always wants a (distinct) search; cap children at 1.
        client = _ScriptedClient(
            [
                {"action": "search_files", "args": {"query": "a"}},
                {"action": "search_files", "args": {"query": "b"}},
                {"action": "search_files", "args": {"query": "c"}},
            ]
        )
        h = _run(orch, client, max_children=1)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertEqual(term.result["outcome"], "partial")
        self.assertEqual(len(orch.spawned), 1)

    def test_iteration_cap_finishes_partial(self) -> None:
        orch = _FakeOrchestrator()
        # Distinct searches every time; cap iterations at 2.
        client = _ScriptedClient(
            [
                {"action": "search_files", "args": {"query": "a"}},
                {"action": "search_files", "args": {"query": "b"}},
                {"action": "search_files", "args": {"query": "c"}},
            ]
        )
        h = _run(orch, client, max_iterations=2)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertEqual(term.result["outcome"], "partial")
        self.assertEqual(len(orch.spawned), 2)


class CancelTests(unittest.TestCase):
    def test_cancel_before_loop_emits_nothing(self) -> None:
        orch = _FakeOrchestrator()
        client = _ScriptedClient([{"action": "finish"}])
        orch.set_parent(99, status="cancelled")
        registry = build_builtin_skill_registry()
        handler = GoalWorkflowHandler(
            orchestrator=orch,
            skill_registry=registry,
            worker_client_provider=lambda: client,
            child_wait_timeout_seconds=5.0,
        )
        h = _Harness()
        tok = set_task_id(f"{99:08x}")
        try:
            handler.start({"goal": "g", "user_id": "u"}, h.emit)
            # Give the daemon thread a moment to observe cancellation.
            h.done.wait(timeout=1.0)
        finally:
            reset_task_id(tok)
        self.assertIsNone(h.terminal())
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
