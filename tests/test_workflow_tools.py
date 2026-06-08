"""Tests for :mod:`app.llm.tools.workflow_tools` — the brain-facing
control surface for nested goal workflows.

The tools (``start_workflow`` / ``check_my_work`` / ``cancel_work``)
are thin glue around :class:`TaskOrchestrator`. These tests verify the
glue with fakes:

* schema shape matches the Ollama tools contract;
* ``start_workflow`` threads the goal through with
  ``handler_name="goal_workflow"`` + ``reply_when_done`` metadata, and
  surfaces clean errors for empty goals / disabled subsystem / missing
  handler / per-user cap rejection;
* ``check_my_work`` projects running rows + capability gaps;
* ``cancel_work`` validates the id and threads through.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.core.tasks.handler_names import HANDLER_GOAL_WORKFLOW
from app.llm.tools.base import ToolError
from app.llm.tools.workflow_tools import (
    CancelWorkTool,
    CheckMyWorkTool,
    StartWorkflowTool,
    build_workflow_tools,
)


class _FakeRow:
    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", 1)
        self.title = kw.get("title", "")
        self.handler_name = kw.get("handler_name", "")
        self.status = kw.get("status", "running")
        self.phase = kw.get("phase")
        self.progress = kw.get("progress")
        self.last_message = kw.get("last_message")
        self.parent_task_id = kw.get("parent_task_id")


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[int] = []
        self.next_task_id: int | None = 42
        self.cancel_outcome = True
        self.running: list[_FakeRow] = []
        self._handlers: dict[str, Any] = {HANDLER_GOAL_WORKFLOW: object()}
        self.start_raise: Exception | None = None

    def handler_for(self, name: str) -> Any | None:
        return self._handlers.get(name)

    def start_task(
        self,
        *,
        user_id: str,
        handler_name: str,
        args: dict[str, Any],
        title: str,
        initiated_by: str = "aiko",
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        if self.start_raise is not None:
            raise self.start_raise
        self.start_calls.append(
            {
                "user_id": user_id,
                "handler_name": handler_name,
                "args": dict(args),
                "title": title,
                "initiated_by": initiated_by,
                "metadata": dict(metadata) if metadata else None,
            }
        )
        return self.next_task_id

    def list_running(self, user_id: str | None = None) -> list[_FakeRow]:
        return list(self.running)

    def cancel(self, task_id: int) -> bool:
        self.cancel_calls.append(int(task_id))
        return bool(self.cancel_outcome)


class _FakeSession:
    def __init__(
        self,
        *,
        orchestrator: _FakeOrchestrator | None,
        user_id: str = "jacob",
        gaps: list[dict[str, Any]] | None = None,
        active_user_text: str = "",
    ) -> None:
        self._user_id = user_id
        self._task_orchestrator = orchestrator
        self._gaps = gaps or []
        self._active_turn_user_text = active_user_text

    def workflow_capability_gaps(self) -> list[dict[str, Any]]:
        return list(self._gaps)


# ── StartWorkflowTool ─────────────────────────────────────────────────


class StartWorkflowSchemaTests(unittest.TestCase):
    def test_schema(self) -> None:
        tool = StartWorkflowTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        self.assertEqual(tool.name, "start_workflow")
        params = tool.schema().parameters
        self.assertEqual(params["required"], ["goal"])
        self.assertIn("goal", params["properties"])


class StartWorkflowRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.session = _FakeSession(
            orchestrator=self.orch, active_user_text="go find new files"
        )
        self.tool = StartWorkflowTool(self.session)

    def test_happy_path(self) -> None:
        out = json.loads(self.tool.run({"goal": "find new files and read them"}))
        self.assertEqual(out["task_id"], 42)
        self.assertEqual(out["status"], "running")
        call = self.orch.start_calls[0]
        self.assertEqual(call["handler_name"], HANDLER_GOAL_WORKFLOW)
        self.assertEqual(call["user_id"], "jacob")
        self.assertEqual(call["args"]["goal"], "find new files and read them")
        self.assertEqual(call["initiated_by"], "aiko")
        self.assertTrue(call["metadata"]["reply_when_done"])
        self.assertEqual(call["metadata"]["origin_prompt"], "go find new files")

    def test_empty_goal_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"goal": "   "})

    def test_missing_goal_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({})

    def test_disabled_subsystem(self) -> None:
        tool = StartWorkflowTool(_FakeSession(orchestrator=None))
        with self.assertRaises(ToolError):
            tool.run({"goal": "x"})

    def test_missing_handler(self) -> None:
        self.orch._handlers.clear()
        with self.assertRaises(ToolError):
            self.tool.run({"goal": "x"})

    def test_cap_rejection(self) -> None:
        self.orch.next_task_id = None
        with self.assertRaises(ToolError):
            self.tool.run({"goal": "x"})

    def test_start_raise_wrapped(self) -> None:
        self.orch.start_raise = RuntimeError("boom")
        with self.assertRaises(ToolError):
            self.tool.run({"goal": "x"})


# ── CheckMyWorkTool ────────────────────────────────────────────────────


class CheckMyWorkRunTests(unittest.TestCase):
    def test_empty(self) -> None:
        orch = _FakeOrchestrator()
        tool = CheckMyWorkTool(_FakeSession(orchestrator=orch))
        out = json.loads(tool.run({}))
        self.assertEqual(out["active"], [])
        self.assertEqual(out["capability_gaps"], [])

    def test_active_and_gaps(self) -> None:
        orch = _FakeOrchestrator()
        orch.running = [
            _FakeRow(
                id=5,
                title="workflow: find files",
                handler_name="goal_workflow",
                status="running",
                phase="acting",
                progress=0.4,
                last_message="reading file 2/3",
            ),
            _FakeRow(id=6, handler_name="file_read", parent_task_id=5),
        ]
        session = _FakeSession(
            orchestrator=orch,
            gaps=[{"capability": "send email", "goal": "email Bob"}],
        )
        tool = CheckMyWorkTool(session)
        out = json.loads(tool.run({}))
        self.assertEqual(len(out["active"]), 2)
        self.assertEqual(out["active"][0]["task_id"], 5)
        self.assertEqual(out["active"][0]["phase"], "acting")
        self.assertEqual(out["active"][1]["parent_task_id"], 5)
        self.assertEqual(out["capability_gaps"][0]["capability"], "send email")

    def test_disabled_subsystem(self) -> None:
        tool = CheckMyWorkTool(_FakeSession(orchestrator=None))
        out = json.loads(tool.run({}))
        self.assertEqual(out["active"], [])


# ── CancelWorkTool ─────────────────────────────────────────────────────


class CancelWorkRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.tool = CancelWorkTool(_FakeSession(orchestrator=self.orch))

    def test_happy_path(self) -> None:
        out = json.loads(self.tool.run({"task_id": 9}))
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["task_id"], 9)
        self.assertEqual(self.orch.cancel_calls, [9])

    def test_bad_id(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": 0})
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": "nope"})

    def test_disabled_subsystem(self) -> None:
        tool = CancelWorkTool(_FakeSession(orchestrator=None))
        with self.assertRaises(ToolError):
            tool.run({"task_id": 1})


# ── factory ────────────────────────────────────────────────────────────


class BuildWorkflowToolsTests(unittest.TestCase):
    def test_builds_three(self) -> None:
        tools = build_workflow_tools(_FakeSession(orchestrator=_FakeOrchestrator()))
        names = {t.name for t in tools}
        self.assertEqual(
            names, {"start_workflow", "check_my_work", "cancel_work"}
        )


if __name__ == "__main__":
    unittest.main()
