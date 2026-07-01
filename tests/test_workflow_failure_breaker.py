"""Tests for the GoalWorkflowHandler robustness limits (Phase 2b).

Covers the consecutive-failure circuit breaker (with the browser-aware
message), the streak reset on success, and the wall-clock budget.
"""
from __future__ import annotations

import json
import threading
import unittest
from typing import Any
from unittest import mock

from app.core.infra.log_context import reset_task_id, set_task_id
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskEventEmit,
    TaskFailed,
    TaskOutcome,
)
from app.core.tasks.workflow.goal_workflow_handler import (
    EVENT_WORKFLOW_BLOCKED,
    GoalWorkflowHandler,
)
from app.core.tasks.workflow.skill_registry import (
    SpawnContext,
    WorkflowSkill,
    build_builtin_skill_registry,
)


class _Row:
    def __init__(self, rid: int, *, status: str, result=None, error=None) -> None:
        self.id = rid
        self.status = status
        self.result = result
        self.error = error


class _Orchestrator:
    """Children fail when handler_name == 'mcp_tool', else done."""

    def __init__(self) -> None:
        self.rows: dict[int, _Row] = {}
        self._next = 1000
        self.spawned: list[str] = []

    def set_parent(self, task_id: int) -> None:
        self.rows[task_id] = _Row(task_id, status="running")

    def start_task(self, *, handler_name: str, args: dict, **kw: Any) -> int | None:
        self._next += 1
        cid = self._next
        if handler_name == "mcp_tool":
            self.rows[cid] = _Row(cid, status="failed", error="MCP call failed")
        else:
            self.rows[cid] = _Row(cid, status="done", result={"summary": "ok"})
        self.spawned.append(handler_name)
        return cid

    def wait_for_task(self, task_id: int, *, timeout: float = 5.0) -> str:
        row = self.rows.get(task_id)
        return row.status if row is not None else "failed"

    def get(self, task_id: int) -> _Row | None:
        return self.rows.get(task_id)

    def cancel(self, task_id: int) -> bool:
        if task_id in self.rows:
            self.rows[task_id].status = "cancelled"
        return True


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def chat_json(self, messages, **kw):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx], None


class _Harness:
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


def _browser_skill(name: str) -> WorkflowSkill:
    def _spawn(args: dict[str, Any], ctx: SpawnContext) -> int | None:
        return ctx.orchestrator.start_task(
            user_id=ctx.user_id,
            handler_name="mcp_tool",
            args={"server_id": "browser", "tool_name": name, "tool_args": args},
            parent_task_id=ctx.parent_task_id,
        )

    return WorkflowSkill(
        name=f"browser__{name}",
        description=f"browser {name}",
        spawn=_spawn,
        group="mcp:browser",
    )


def _run(
    orch: _Orchestrator,
    client: _ScriptedClient,
    *,
    parent_id: int = 42,
    max_consecutive_failures: int = 2,
    max_wall_seconds: float = 300.0,
) -> _Harness:
    orch.set_parent(parent_id)
    registry = build_builtin_skill_registry()
    registry.register(_browser_skill("browser_snapshot"))
    registry.register(_browser_skill("browser_click"))
    handler = GoalWorkflowHandler(
        orchestrator=orch,
        skill_registry=registry,
        worker_client_provider=lambda: client,
        model_provider=lambda: "worker-model",
        user_name_provider=lambda: "Jacob",
        child_wait_timeout_seconds=5.0,
        max_consecutive_failures=max_consecutive_failures,
        max_wall_seconds=max_wall_seconds,
    )
    h = _Harness()
    tok = set_task_id(f"{parent_id:08x}")
    try:
        handler.start({"goal": "open my email", "user_id": "u1"}, h.emit)
        h.done.wait(timeout=5.0)
    finally:
        reset_task_id(tok)
    return h


class FailureBreakerTests(unittest.TestCase):
    def test_two_browser_failures_break_with_browser_message(self) -> None:
        orch = _Orchestrator()
        client = _ScriptedClient(
            [
                {"action": "browser__browser_snapshot", "args": {}},
                {"action": "browser__browser_click", "args": {"ref": "e1"}},
                {"action": "browser__browser_snapshot", "args": {"again": True}},
            ]
        )
        h = _run(orch, client, max_consecutive_failures=2)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        # Consecutive-failure breaker now reports BLOCKED (stuck / needs
        # help), not the budget-exhaustion "partial".
        self.assertEqual(term.result["outcome"], "blocked")
        self.assertIn("Chrome", term.result["content"])
        # Broke after the 2nd consecutive failure — only 2 children spawned.
        self.assertEqual(len(orch.spawned), 2)
        # A workflow_blocked audit event was appended before the finish.
        blocked = [
            o
            for o in h.outcomes
            if isinstance(o, TaskEventEmit) and o.type == EVENT_WORKFLOW_BLOCKED
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].data.get("reason"), "consecutive_failures")

    def test_success_resets_streak(self) -> None:
        orch = _Orchestrator()
        client = _ScriptedClient(
            [
                {"action": "browser__browser_snapshot", "args": {}},  # fail
                {"action": "web_search", "args": {"query": "x"}},  # ok -> reset
                {"action": "browser__browser_click", "args": {"ref": "e1"}},  # fail
                {"action": "finish", "findings": "done", "outcome": "success"},
            ]
        )
        h = _run(orch, client, max_consecutive_failures=2)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        # Never broke: reached the explicit finish.
        self.assertEqual(term.result["outcome"], "success")
        self.assertEqual(
            orch.spawned, ["mcp_tool", "web_search", "mcp_tool"]
        )

    def test_non_browser_failure_uses_generic_message(self) -> None:
        orch = _Orchestrator()

        # Two distinct non-browser skills that always fail (distinct names
        # so the exact-(skill,args) repeat guard doesn't trip first).
        def _spawn(args, ctx):
            return ctx.orchestrator.start_task(
                user_id=ctx.user_id,
                handler_name="mcp_tool",
                args={},
                parent_task_id=ctx.parent_task_id,
            )

        registry = build_builtin_skill_registry()
        registry.register(
            WorkflowSkill(name="flaky", description="flaky", spawn=_spawn, group="x")
        )
        orch.set_parent(7)
        # Break after the first failure so the generic (non-browser)
        # message path is exercised without the repeat guard interfering.
        handler = GoalWorkflowHandler(
            orchestrator=orch,
            skill_registry=registry,
            worker_client_provider=lambda: _ScriptedClient(
                [{"action": "flaky", "args": {}}]
            ),
            child_wait_timeout_seconds=5.0,
            max_consecutive_failures=1,
        )
        h = _Harness()
        tok = set_task_id("00000007")
        try:
            handler.start({"goal": "g", "user_id": "u"}, h.emit)
            h.done.wait(timeout=5.0)
        finally:
            reset_task_id(tok)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertIn("tool kept failing", term.result["content"])


class PlannerGuidanceTests(unittest.TestCase):
    """_planner_guidance surfaces plugin / captured guidance for the
    ``mcp:<server_id>`` groups actually present in the menu."""

    def _handler(self, *, group_guidance: dict[str, str]) -> GoalWorkflowHandler:
        registry = build_builtin_skill_registry()
        registry.register(_browser_skill("browser_snapshot"))
        return GoalWorkflowHandler(
            orchestrator=_Orchestrator(),
            skill_registry=registry,
            worker_client_provider=lambda: _ScriptedClient([{"action": "finish"}]),
            group_guidance_provider=lambda: dict(group_guidance),
        )

    def test_guidance_present_for_browser_menu(self) -> None:
        handler = self._handler(
            group_guidance={"mcp:browser": "Snapshot FIRST playbook"}
        )
        skills = [
            {"name": "browser__browser_snapshot", "group": "mcp:browser"},
            {"name": "web_search", "group": "web"},
        ]
        out = handler._planner_guidance(skills)
        self.assertIn("Snapshot FIRST", out)

    def test_no_guidance_without_matching_group(self) -> None:
        handler = self._handler(
            group_guidance={"mcp:browser": "Snapshot FIRST playbook"}
        )
        skills = [{"name": "web_search", "group": "web"}]
        self.assertEqual(handler._planner_guidance(skills), "")

    def test_no_guidance_without_group_guidance(self) -> None:
        handler = self._handler(group_guidance={})
        skills = [{"name": "browser__browser_snapshot", "group": "mcp:browser"}]
        self.assertEqual(handler._planner_guidance(skills), "")

    def test_filesystem_guidance_from_plugin(self) -> None:
        # A filesystem MCP group in the menu surfaces its plugin guidance.
        handler = self._handler(
            group_guidance={"mcp:fs": "use absolute paths under the root"}
        )
        skills = [
            {"name": "fs__write_file", "group": "mcp:fs"},
            {"name": "fs__list_allowed_directories", "group": "mcp:fs"},
        ]
        out = handler._planner_guidance(skills)
        self.assertIn("absolute paths", out)


class WallClockTests(unittest.TestCase):
    def test_budget_exceeded_finishes_before_planning(self) -> None:
        orch = _Orchestrator()
        client = _ScriptedClient([{"action": "finish"}])
        # monotonic(): first call = start (0), second = loop check (100) > 10.
        with mock.patch(
            "app.core.tasks.workflow.goal_workflow_handler.time.monotonic",
            side_effect=[0.0, 100.0, 100.0, 100.0],
        ):
            h = _run(orch, client, max_wall_seconds=10.0)
        term = h.terminal()
        self.assertIsInstance(term, TaskCompleted)
        self.assertEqual(term.result["outcome"], "partial")
        self.assertIn("time budget", term.result["content"])
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
