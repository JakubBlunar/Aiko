"""Operation admission preserves schemas and prevents unsupported dispatch."""

import json
from types import SimpleNamespace

from app.core.tasks.workflow.mcp_skills import _skill_from_descriptor
from app.core.tasks.workflow.skill_registry import SpawnContext, WorkflowSkillRegistry
from app.core.tasks.workflow.workflow_planner import PlannerInput, render_planner_messages
from app.mcp.client.manager import McpToolDescriptor


def test_full_schema_is_disclosed_and_invalid_arguments_cannot_spawn():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["brief", "full"], "description": "Display mode"},
            "options": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "required": ["count"],
                "additionalProperties": False,
            },
        },
        "required": ["mode", "options"],
        "additionalProperties": False,
    }
    skill = _skill_from_descriptor(McpToolDescriptor("test", "read", "Read", schema, "read"))
    registry = WorkflowSkillRegistry()
    registry.register(skill)
    assert registry.schema_for(skill.name) == schema
    prompt = render_planner_messages(
        PlannerInput(goal="read", skills=registry.describe_for_planner())
    )
    assert '"minimum":1' in prompt[1]["content"]
    assert "Display mode" in prompt[1]["content"]
    assert '"brief","full"' in prompt[1]["content"]
    assert registry.validate_call(skill.name, {"mode": "other", "options": {"count": 0}})
    assert registry.validate_call(skill.name, {"mode": "brief", "options": {"count": 1}}, set())
    assert (
        registry.spawn_child(skill.name, {"mode": "other"}, SpawnContext(None, "user", 1)) is None
    )
    assert not registry.validate_call(
        skill.name, json.loads('{"mode":"full","options":{"count":2}}'), {skill.name}
    )


def test_cancel_during_planning_does_not_dispatch():
    from scripts.probe_agent_harness import _scenario

    assert _scenario(cancel_during_plan=True)["children_dispatched"] == 0


def test_large_catalogue_is_bounded_and_undisclosed_tools_are_rejected():
    from app.core.tasks.workflow.profiles import catalogue
    from app.core.tasks.workflow.skill_registry import WorkflowSkill

    registry = WorkflowSkillRegistry()
    for i in range(120):
        registry.register(
            WorkflowSkill(
                name=f"source_{i}",
                description="Read source " + str(i),
                effect="read",
                group="sources",
                spawn=lambda *_: 1,
                input_schema={"type": "object", "properties": {}},
            )
        )
    first = catalogue(registry, "reader")
    assert len(first) == 12
    assert len(json.dumps(first)) <= 18000
    assert registry.validate_call("source_119", {}, {row["name"] for row in first})
    found = catalogue(registry, "reader", query="source_119")
    assert found[0]["name"] == "source_119"


def test_mcp_approval_live_contract_and_uncertain_mutation():
    from app.core.tasks.handlers.mcp_tool import McpToolHandler
    from app.core.tasks.task_handler import TaskFailed, TaskInputNeeded

    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }
    descriptor = McpToolDescriptor("test", "save", "Save requested content", schema, "unknown")

    class Manager:
        calls = 0

        def list_available_tools(self):
            return [descriptor]

        def call_tool(self, *_args):
            self.calls += 1
            raise TimeoutError("unknown remote outcome")

    manager = Manager()
    handler = McpToolHandler(
        manager=manager, resolve_approval=lambda _cap: "ask", can_dispatch=lambda: True
    )
    args = {"server_id": "test", "tool_name": "save", "tool_args": {"content": "requested"}}
    outcomes = []
    state = handler.start(args, outcomes.append)
    assert isinstance(outcomes[-1], TaskInputNeeded)
    assert "requested" in outcomes[-1].prompt
    assert manager.calls == 0
    handler.on_input(state, "approve", outcomes.append)
    assert manager.calls == 1
    assert isinstance(outcomes[-1], TaskFailed)
    assert outcomes[-1].uncertain_effect

    # A changed schema/effect cannot inherit approval for the prior contract.
    outcomes.clear()
    state = handler.start(args, outcomes.append)
    descriptor.input_schema["properties"]["content"]["maxLength"] = 100
    handler.on_input(state, "approve", outcomes.append)
    assert manager.calls == 1
    assert "contract changed" in outcomes[-1].error


def test_mcp_cancel_before_dispatch_and_nontext_is_partial():
    from app.core.tasks.handlers.mcp_tool import McpToolHandler
    from app.core.tasks.task_handler import TaskCompleted, TaskFailed

    descriptor = McpToolDescriptor("test", "read", "Read", {"type": "object"}, "read")
    calls = []
    manager = SimpleNamespace(
        list_available_tools=lambda: [descriptor],
        call_tool=lambda *args: (
            calls.append(args)
            or SimpleNamespace(
                isError=False, content=[SimpleNamespace(text="text"), SimpleNamespace(data="image")]
            )
        ),
    )
    args = {"server_id": "test", "tool_name": "read", "tool_args": {}}
    outcomes = []
    McpToolHandler(
        manager=manager, resolve_approval=lambda _: "auto", can_dispatch=lambda: False
    ).start(args, outcomes.append)
    assert isinstance(outcomes[-1], TaskFailed)
    assert not calls
    McpToolHandler(
        manager=manager, resolve_approval=lambda _: "auto", can_dispatch=lambda: True
    ).start(args, outcomes.append)
    assert isinstance(outcomes[-1], TaskCompleted)
    assert outcomes[-1].result["truncated"]
    assert outcomes[-1].result["non_text_blocks"] == 1
