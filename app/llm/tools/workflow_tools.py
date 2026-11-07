"""Aiko-callable tools for nested goal workflows.

These are the brain-facing control surface for the background
:class:`GoalWorkflowHandler`. There is no fast file/browser lane on the
brain anymore — every file, web, and MCP-tool operation runs in the
background workflow lane, so ``start_workflow`` is the single entry point
for anything that touches the outside world:

* ``start_workflow`` — kick off a goal ("find any new files and tell me
  what's in them", "look up X and summarise it", "write a note to the
  file plugin"). The workflow plans, runs the sub-steps in the
  background using whatever plugin tools are available, and reports an
  aggregated answer when it's done.
* ``check_my_work`` — report what Aiko is currently working on: active
  tasks + their progress + anything she recently couldn't do yet
  ("missing capability"). The answer to "what are you up to?" /
  "how's that going?".
* ``cancel_work`` — stop a running task/workflow by id.
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


def _agent_settings(session: "SessionController") -> Any | None:
    return getattr(getattr(session, "_settings", None), "agent", None)


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
                "The way you actually DO things instead of just talking "
                "about them: hand me any goal that needs action and I "
                "plan it, run the steps in the background, and report back "
                "when done. This is how you read, search, write, create, "
                "edit or move files, browse the web, and anything else that "
                "touches the outside world — you do NOT do any of that "
                "inline. You do NOT need to "
                "know which tools exist or what the steps are — just "
                "describe the goal fully in plain language and let me work "
                "it out. Returns "
                "JSON: {task_id, status, note}. CRITICAL: nothing has "
                "happened yet when you call this — never tell the user "
                "something was done / created / written / found until the "
                "result comes back confirming it. Say you're on it and let "
                "it arrive; do NOT invent the result. For longer work, include completion "
                "criteria and constraints from the conversation. You organize the work profiles "
                "yourself; the user does not need to choose a researcher, reader, browser, writer "
                "or reviewer. Saved work can pause, ask questions "
                "and continue with the same task ID."
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
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 12,
                        "description": "What must be true for this request to be complete.",
                    },
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 12,
                        "description": "Relevant limits and preferences from this request.",
                    },
                    "profile": {
                        "type": "string",
                        "enum": [
                            "auto",
                            "general",
                            "reader",
                            "researcher",
                            "browser",
                            "writer",
                            "reviewer",
                        ],
                        "description": "Optional initial work role; auto lets the workflow choose.",
                    },
                },
                "required": ["goal"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        goal = (arguments.get("goal") or "").strip()
        if not goal:
            raise ToolError("start_workflow: 'goal' is required")
        contract = {}
        for field in ("criteria", "constraints"):
            value = arguments.get(field, [])
            if (
                not isinstance(value, list)
                or len(value) > 12
                or any(not isinstance(item, str) or len(item) > 1000 for item in value)
            ):
                raise ToolError(f"start_workflow: {field} must contain at most 12 short strings")
            contract[field] = value
        from app.core.tasks.workflow.profiles import PROFILES

        profile = arguments.get("profile", "auto")
        if profile != "auto" and profile not in PROFILES:
            raise ToolError("start_workflow: unknown work profile")
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError(
                "start_workflow: task subsystem is disabled (agent.tasks_enabled=False)"
            )
        if orch.handler_for(HANDLER_GOAL_WORKFLOW) is None:
            raise ToolError(
                "start_workflow: goal workflows are disabled (agent.workflow_enabled=False)"
            )
        # ``reply_when_done`` is what makes the result render in full when
        # it lands, instead of as a terse bullet. Off = the legacy cue-only
        # behaviour.
        metadata: dict[str, Any] = {}
        if bool(
            getattr(
                _agent_settings(self._session),
                "task_reply_on_complete_enabled",
                True,
            )
        ):
            metadata["reply_when_done"] = True
        origin = (getattr(self._session, "_active_turn_user_text", "") or "").strip()
        if origin:
            metadata["origin_prompt"] = origin[:2000]
        metadata["source_session"] = getattr(self._session, "session_key", None)
        try:
            task_id = orch.start_task(
                user_id=_user_id(self._session),
                handler_name=HANDLER_GOAL_WORKFLOW,
                args={
                    "goal": goal,
                    "user_id": _user_id(self._session),
                    "contract": contract,
                    "profile": profile,
                },
                title=f"workflow: {goal[:60]}",
                initiated_by="aiko",
                metadata=metadata,
            )
        except Exception as exc:
            log.exception("start_workflow: start_task failed: goal=%r", goal)
            raise ToolError(f"start_workflow failed: {exc}") from exc
        if task_id is None:
            raise ToolError("start_workflow: spawn rejected (per-user task cap reached)")
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
                "capability_gaps: [...]}. With status=ok, an empty active list means no active "
                "work was found, not that earlier work succeeded. Use scope=recent to find "
                "recent IDs, or task_id to inspect saved results/errors, including completed, "
                "failed or cancelled tasks. This is read-only and starts nothing. Follow "
                "next_offset with content_offset and revision for long JSON result fragments. "
                "Unavailable/partial reads do not establish absence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "minimum": 1},
                    "scope": {"type": "string", "enum": ["active", "recent"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "content_offset": {"type": "integer", "minimum": 0},
                    "revision": {"type": "string", "maxLength": 128},
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        orch = _orchestrator(self._session)
        if orch is None:
            return json.dumps(
                {
                    "status": "unavailable",
                    "active": [],
                    "capability_gaps": [],
                    "note": "tasks disabled; absence/completion unknown",
                },
                ensure_ascii=False,
            )
        user_id = _user_id(self._session)
        from app.core.tasks.work_inspection import brief, inspect_work

        if "task_id" in arguments:
            if set(arguments) - {"task_id", "content_offset", "revision"}:
                raise ToolError("check_my_work: task_id cannot be combined with list filters")
            try:
                return json.dumps(inspect_work(orch, user_id, **arguments), ensure_ascii=False)
            except Exception as exc:
                raise ToolError(f"check_my_work unavailable: {exc}") from exc
        if "content_offset" in arguments or "revision" in arguments:
            raise ToolError("check_my_work: content_offset/revision require task_id")
        scope = arguments.get("scope", "active")
        limit = arguments.get("limit", 10)
        active: list[dict[str, Any]] = []
        try:
            rows = (
                orch.list_for_user(user_id, limit=limit + 1)
                if scope == "recent"
                else orch.list_running(user_id=user_id)
            )
        except Exception as exc:
            raise ToolError("check_my_work unavailable; current work could not be read") from exc
        rows = [row for row in rows if getattr(row, "user_id", user_id) == user_id]
        active = [brief(row) for row in rows[:limit]]
        gaps: list[dict[str, Any]] = []
        gap_unavailable = False
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
                gap_unavailable = True
        log.info("check_my_work: active=%d gaps=%d", len(active), len(gaps))
        return json.dumps(
            {
                "status": "partial" if gap_unavailable else "ok",
                "read_only": True,
                "active" if scope == "active" else "recent": active,
                "has_more": len(rows) > limit,
                "capability_gaps_unavailable": gap_unavailable,
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
            raise ToolError("cancel_work: 'task_id' must be an integer") from None
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


class ControlWorkTool:
    """Pause or continue the same owned work task from conversation."""

    name = "control_work"

    def __init__(self, session):
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="control_work",
            description=(
                "Pause or resume an existing work task when the user requests it. Returns the "
                "actual saved status synchronously. Pause stops new steps; an operation already "
                "dispatched may finish. Resume preserves results and consumed budget. If a work "
                "budget was exhausted, an explicit user request can add a bounded allowance. "
                "Use cancel_work to cancel, check_my_work to inspect, "
                "and start_workflow for new work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "minimum": 1},
                    "action": {"type": "string", "enum": ["pause", "resume"]},
                },
                "required": ["task_id", "action"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict) -> str:
        task_id, action = arguments.get("task_id"), arguments.get("action")
        if type(task_id) is not int or task_id < 1 or action not in {"pause", "resume"}:
            raise ToolError("control_work: provide task_id and pause or resume")
        orch = _orchestrator(self._session)
        row = orch.get(task_id) if orch else None
        if row is None or row.user_id != _user_id(self._session) or not row.visible_to_user:
            raise ToolError("control_work: work not found")
        changed = orch.pause_work(task_id) if action == "pause" else orch.resume_work(task_id)
        return json.dumps(
            {"task_id": task_id, "changed": changed, "status": orch.get(task_id).status}
        )


# ── factory ────────────────────────────────────────────────────────────


def build_workflow_tools(session: "SessionController") -> list[Tool]:
    """Construct the workflow control tools bound to ``session``."""
    return [
        StartWorkflowTool(session),
        CheckMyWorkTool(session),
        CancelWorkTool(session),
        ControlWorkTool(session),
    ]


__all__ = [
    "StartWorkflowTool",
    "CheckMyWorkTool",
    "CancelWorkTool",
    "ControlWorkTool",
    "build_workflow_tools",
]
