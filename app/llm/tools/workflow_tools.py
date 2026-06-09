"""Aiko-callable tools for nested goal workflows.

These are the brain-facing control surface for the background
:class:`GoalWorkflowHandler`. They sit alongside the fast-lane file
tools (``start_file_search`` / ``start_file_read``) but cover a
different shape of request:

* ``start_workflow`` — kick off a MULTI-STEP goal ("find any new files
  and tell me what's in them", "look up X and summarise it"). The
  workflow plans, runs several sub-steps in the background, and reports
  an aggregated answer when it's done. Use this when one tool call
  isn't enough.
* ``check_my_work`` — report what Aiko is currently working on: active
  tasks + their progress + anything she recently couldn't do yet
  ("missing capability"). The answer to "what are you up to?" /
  "how's that going?".
* ``cancel_work`` — stop a running task/workflow by id.

The fast-lane ``start_file_search`` / ``start_file_read`` stay for
single, direct operations where the result is wanted in the same reply.
The tool descriptions below draw the line explicitly so the LLM routes
"read this one file" to the fast lane and "go find and read whatever's
new" to ``start_workflow``.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from app.core.tasks.handler_names import HANDLER_GOAL_WORKFLOW
from app.llm.tools.base import Tool, ToolError, ToolSchema


if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.tools.workflow")


def _user_id(session: "SessionController") -> str:
    return str(getattr(session, "_user_id", "default") or "default")


def _orchestrator(session: "SessionController") -> Any | None:
    return getattr(session, "_task_orchestrator", None)


# ── start_workflow ────────────────────────────────────────────────────


class StartWorkflowTool:
    """Spawn a multi-step background goal workflow."""

    name = "start_workflow"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="start_workflow",
            description=(
                "Start a MULTI-STEP background task to accomplish a goal "
                "that needs several actions chained together. Reach for "
                "this WHENEVER fulfilling a file/research request takes "
                "more than one step — e.g. 'find "
                "any new files and tell me what's in them', 'look up X "
                "online and summarise it', 'search my notes for Y then "
                "read the best match'. I'll plan the steps, run them in "
                "the background (file search, file read, web search, …), "
                "and report back with an aggregated answer when done — you "
                "don't need to do the steps yourself. Use this instead of "
                "start_file_search / start_file_read when ONE tool call "
                "isn't enough. For a single direct file read or one quick "
                "search, use the fast file tools instead. Returns JSON: "
                "{task_id, status, note}. Tell the user you're on it; the "
                "result arrives automatically — do NOT invent it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "A clear, self-contained description of what "
                            "to accomplish, in plain language. Include "
                            "everything I need to know to work "
                            "independently."
                        ),
                    },
                },
                "required": ["goal"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        goal = (arguments.get("goal") or "").strip()
        if not goal:
            raise ToolError("start_workflow: 'goal' is required")
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError(
                "start_workflow: task subsystem is disabled "
                "(agent.tasks_enabled=False)"
            )
        if orch.handler_for(HANDLER_GOAL_WORKFLOW) is None:
            raise ToolError(
                "start_workflow: goal workflows are disabled "
                "(agent.workflow_enabled=False)"
            )
        metadata: dict[str, Any] = {"reply_when_done": True}
        origin = (getattr(self._session, "_active_turn_user_text", "") or "").strip()
        if origin:
            metadata["origin_prompt"] = origin[:500]
        try:
            task_id = orch.start_task(
                user_id=_user_id(self._session),
                handler_name=HANDLER_GOAL_WORKFLOW,
                args={"goal": goal, "user_id": _user_id(self._session)},
                title=f"workflow: {goal[:60]}",
                initiated_by="aiko",
                metadata=metadata,
            )
        except Exception as exc:
            log.exception("start_workflow: start_task failed: goal=%r", goal)
            raise ToolError(f"start_workflow failed: {exc}") from exc
        if task_id is None:
            raise ToolError(
                "start_workflow: spawn rejected (per-user task cap reached)"
            )
        log.info("start_workflow spawned: task_id=%d goal=%r", task_id, goal[:80])
        return json.dumps(
            {
                "task_id": task_id,
                "status": "running",
                "note": (
                    "Workflow started. I'll work through the steps in the "
                    "background and report the result automatically when "
                    "it's ready. Tell the user you're on it and move on — "
                    "do NOT invent the result or start another workflow for "
                    "the same goal."
                ),
            },
            ensure_ascii=False,
        )


# ── check_my_work ─────────────────────────────────────────────────────


class CheckMyWorkTool:
    """Report active tasks/workflows + recent capability gaps."""

    name = "check_my_work"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="check_my_work",
            description=(
                "Report what you're currently working on in the "
                "background: active tasks and workflows with their "
                "progress + phase, plus anything you recently could NOT "
                "do because you lack the capability. Use this when the "
                "user asks 'what are you up to?', 'how's that going?', "
                "'are you still working on it?', or 'is there anything you "
                "couldn't do?'. Returns JSON: {active: [...], "
                "capability_gaps: [...]}. If active is empty, you're not "
                "working on anything right now."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        orch = _orchestrator(self._session)
        if orch is None:
            return json.dumps(
                {"active": [], "capability_gaps": [], "note": "tasks disabled"},
                ensure_ascii=False,
            )
        user_id = _user_id(self._session)
        active: list[dict[str, Any]] = []
        try:
            rows = orch.list_running(user_id=user_id)
        except Exception:
            log.debug("check_my_work: list_running failed", exc_info=True)
            rows = []
        for row in rows:
            active.append(
                {
                    "task_id": int(getattr(row, "id", 0)),
                    "title": str(getattr(row, "title", "") or ""),
                    "handler": str(getattr(row, "handler_name", "") or ""),
                    "status": str(getattr(row, "status", "") or ""),
                    "phase": getattr(row, "phase", None),
                    "progress": getattr(row, "progress", None),
                    "last_message": getattr(row, "last_message", None),
                    "parent_task_id": getattr(row, "parent_task_id", None),
                }
            )
        gaps: list[dict[str, Any]] = []
        gap_fn = getattr(self._session, "workflow_capability_gaps", None)
        if callable(gap_fn):
            try:
                for gap in gap_fn():
                    gaps.append(
                        {
                            "capability": gap.get("capability", ""),
                            "goal": gap.get("goal", ""),
                        }
                    )
            except Exception:
                log.debug("check_my_work: gap read failed", exc_info=True)
        log.info(
            "check_my_work: active=%d gaps=%d", len(active), len(gaps)
        )
        return json.dumps(
            {
                "active": active,
                "capability_gaps": gaps,
                "note": (
                    "Report these naturally. If capability_gaps is "
                    "non-empty, tell the user you don't know how to do "
                    "those parts yet and name what you'd need."
                ),
            },
            ensure_ascii=False,
        )


# ── cancel_work ───────────────────────────────────────────────────────


class CancelWorkTool:
    """Cancel a running task/workflow by id."""

    name = "cancel_work"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="cancel_work",
            description=(
                "Cancel a running background task or workflow by its id "
                "(from start_workflow or check_my_work). Cancelling a "
                "workflow also stops its sub-steps. Use when the user says "
                "'never mind', 'stop that', 'forget it'. Returns JSON: "
                "{cancelled, task_id}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The id of the task/workflow to cancel.",
                    },
                },
                "required": ["task_id"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id", 0))
        except (TypeError, ValueError):
            raise ToolError("cancel_work: 'task_id' must be an integer")
        if task_id <= 0:
            raise ToolError("cancel_work: 'task_id' must be a positive integer")
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError("cancel_work: task subsystem is disabled")
        try:
            ok = orch.cancel(task_id)
        except Exception as exc:
            log.exception("cancel_work: cancel failed: task_id=%d", task_id)
            raise ToolError(f"cancel_work failed: {exc}") from exc
        log.info("cancel_work: task_id=%d cancelled=%s", task_id, ok)
        return json.dumps(
            {"cancelled": bool(ok), "task_id": int(task_id)},
            ensure_ascii=False,
        )


# ── factory ────────────────────────────────────────────────────────────


def build_workflow_tools(session: "SessionController") -> list[Tool]:
    """Construct the workflow control tools bound to ``session``."""
    return [
        StartWorkflowTool(session),
        CheckMyWorkTool(session),
        CancelWorkTool(session),
    ]


__all__ = [
    "StartWorkflowTool",
    "CheckMyWorkTool",
    "CancelWorkTool",
    "build_workflow_tools",
]
