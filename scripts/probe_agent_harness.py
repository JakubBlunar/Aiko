"""Observe current workflow contracts using simulated tasks and planner decisions.

No live app, model, tool, database or external side effect is used. This is an
audit probe, not an assertion that current model behavior causes these failures.
Run again after harness changes to compare the boundary behavior.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.tasks.task_handler import TaskCompleted
from app.core.tasks.workflow import goal_workflow_handler as workflow
from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler
from app.core.tasks.workflow.mcp_skills import _arg_schema_from_input
from app.core.tasks.workflow.skill_registry import (
    SpawnContext, WorkflowSkill, WorkflowSkillRegistry,
)
from app.core.tasks.workflow.workflow_planner import (
    PlannerDecision, PlannerInput, PlannerStep, parse_planner_response, render_planner_messages,
)


class _Orchestrator:
    def __init__(self, *, failed: bool = False) -> None:
        self.cancelled = False
        self.failed = failed

    def get(self, task_id: int) -> SimpleNamespace:
        if task_id == 1:
            return SimpleNamespace(status="cancelled" if self.cancelled else "running")
        return SimpleNamespace(
            status="failed" if self.failed else "done",
            result=None if self.failed else {"content": "simulated read receipt"},
            error="simulated tool failure" if self.failed else None,
        )

    def wait_for_task(self, task_id: int, *, timeout: float) -> str:
        return "failed" if self.failed else "done"


def _scenario(*, cancel_during_plan: bool = False, failed_child: bool = False) -> dict:
    orchestrator = _Orchestrator(failed=failed_child)
    registry = WorkflowSkillRegistry()
    spawned: list[dict] = []
    registry.register(WorkflowSkill(
        name="probe_read", description="Simulated read; no external work.",
        spawn=lambda args, ctx: spawned.append(dict(args)) or 2,
    ))
    handler = GoalWorkflowHandler(
        orchestrator=orchestrator, skill_registry=registry,
        worker_client_provider=lambda: None, loop_detection_enabled=False,
    )
    decisions = iter([
        PlannerDecision(action="skill", skill="probe_read"),
        PlannerDecision(action="finish", outcome="success", findings="Claimed success"),
    ])

    def decide(*args, **kwargs):
        if cancel_during_plan:
            orchestrator.cancelled = True
        return next(decisions)

    events = []
    with patch.object(workflow, "decide_next_action", side_effect=decide):
        handler._run_loop(1, "probe", "read and verify", 2, events.append)
    completed = [event.result for event in events if isinstance(event, TaskCompleted)]
    return {
        "children_dispatched": len(spawned),
        "completed_outcomes": [result["outcome"] for result in completed],
        "child_statuses": [step["status"] for result in completed for step in result["steps"]],
    }


def probe() -> dict:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["brief", "detailed"],
                     "description": "Choose a display mode"},
            "count": {"type": "integer", "minimum": 1},
        },
        "required": ["mode"], "additionalProperties": False,
    }
    registry = WorkflowSkillRegistry()
    spawned = []
    registry.register(WorkflowSkill(
        name="probe_read", description="Simulated constrained read.",
        arg_schema=_arg_schema_from_input(schema),
        spawn=lambda args, ctx: spawned.append(args) or 2,
    ))
    messages = render_planner_messages(PlannerInput(
        goal="test", skills=registry.describe_for_planner(),
    ))
    registry.spawn_child(
        "probe_read", {"mode": "invalid", "count": "not-an-integer", "extra": True},
        SpawnContext(orchestrator=None, user_id="probe", parent_task_id=1),
    )
    long_history = render_planner_messages(PlannerInput(
        goal="test", skills=[], steps=[PlannerStep(
            skill="probe_read", observation="prefix " * 1000 + "EVIDENCE_AT_END",
        )],
    ))
    return {
        "mode": "offline_simulated_contract_probes",
        "cancel_during_planning": _scenario(cancel_during_plan=True),
        "success_after_failed_child": _scenario(failed_child=True),
        "invalid_finish_outcome": parse_planner_response(
            '{"action":"finish","outcome":"typo"}', valid_skill_names={"finish"},
        ).outcome,
        "enum_survives_mcp_projection": (
            "enum" in registry.describe_for_planner()[0]["args"]["mode"]
        ),
        "argument_type_in_model_catalogue": "integer" in messages[1]["content"],
        "argument_description_in_model_catalogue": (
            "Choose a display mode" in messages[1]["content"]
        ),
        "invalid_arguments_reach_spawn": bool(spawned),
        "child_observation_characters": len(workflow._summarize_child(
            SimpleNamespace(result={"content": "x" * 1500}, error=None), "done",
        )),
        "long_result_tail_in_planner_context": "EVIDENCE_AT_END" in long_history[1]["content"],
        "registered_but_undisclosed_action_accepted": parse_planner_response(
            '{"action":"hidden_tool"}',
            valid_skill_names={"probe_read", "hidden_tool", "finish"},
        ).action == "skill",
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
