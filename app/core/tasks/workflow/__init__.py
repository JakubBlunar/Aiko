"""Nested goal-workflow subsystem.

A goal workflow is a parent task that drives a small plan→act→observe
loop: an LLM planner picks the next :class:`WorkflowSkill`, the handler
spawns it as a child task, waits for the child to finish, folds the
result onto a shared blackboard, and repeats until the planner decides
to ``finish`` (or hits a capability gap, or a cap).

This package owns three pieces:

* :mod:`skill_registry` — the :class:`WorkflowSkill` / :class:`SpawnContext`
  dataclasses and the :class:`WorkflowSkillRegistry` (name → skill,
  arg-schema, child-spawn function). MCP-pluggable: new skills (e.g.
  ``browser_mcp``) register here without touching the planner.
* ``workflow_planner`` (later chunk) — the budgeted blackboard render
  + ``worker_client.chat_json`` action decision.
* ``goal_workflow_handler`` (later chunk) — the daemon-thread loop.
"""
from __future__ import annotations

from app.core.tasks.workflow.skill_registry import (
    WORKFLOW_SKILL_FINISH,
    SpawnContext,
    WorkflowSkill,
    WorkflowSkillRegistry,
    build_builtin_skill_registry,
)
from app.core.tasks.workflow.goal_workflow_handler import (
    EVENT_WORKFLOW_BLOCKED,
    EVENT_WORKFLOW_LOOP_DETECTED,
    OUTCOME_BLOCKED,
    OUTCOME_MISSING_CAPABILITY,
    GoalWorkflowHandler,
)
from app.core.tasks.workflow.workflow_planner import (
    ACTION_FINISH,
    ACTION_MISSING_CAPABILITY,
    ACTION_SKILL,
    OUTCOME_NOTHING_FOUND,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    PlannerDecision,
    PlannerInput,
    PlannerStep,
    decide_next_action,
    parse_planner_response,
    render_planner_messages,
)


__all__ = [
    "WORKFLOW_SKILL_FINISH",
    "SpawnContext",
    "WorkflowSkill",
    "WorkflowSkillRegistry",
    "build_builtin_skill_registry",
    "GoalWorkflowHandler",
    "OUTCOME_MISSING_CAPABILITY",
    "OUTCOME_BLOCKED",
    "EVENT_WORKFLOW_BLOCKED",
    "EVENT_WORKFLOW_LOOP_DETECTED",
    "ACTION_SKILL",
    "ACTION_FINISH",
    "ACTION_MISSING_CAPABILITY",
    "OUTCOME_SUCCESS",
    "OUTCOME_PARTIAL",
    "OUTCOME_NOTHING_FOUND",
    "PlannerStep",
    "PlannerInput",
    "PlannerDecision",
    "render_planner_messages",
    "parse_planner_response",
    "decide_next_action",
]
