"""Fast-tool discovery leads to real owned evidence, never undisclosed dispatch."""

import json
from types import SimpleNamespace as NS

import pytest

from app.core.tasks.handlers.fast_tool import FastToolHandler
from app.core.tasks.task_handler import TaskCompleted, TaskFailed
from app.core.tasks.workflow.fast_tools import shared_fast_skills
from app.core.tasks.workflow.skill_registry import build_builtin_skill_registry, WorkflowSkill
from app.core.tasks.workflow.workflow_planner import PlannerInput, decide_next_action
from app.llm.tools.base import ToolRegistry
from app.llm.tools.web_search_brain import BrainWebSearchTool
import test_workflow_harness as harness_tests

work = harness_tests.work
wait_status = harness_tests.wait_status


def search_registry(*, scope="u:s", sink=None):
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        return [NS(title="Official episode list", url="https://example.test/episodes",
                   snippet="The series has ten episodes. Episode titles have not been announced.")]

    registry = ToolRegistry(workflow_scope=scope)
    registry.register(BrainWebSearchTool(
        NS(search=search), user_names_provider=lambda: ["Jacob"], on_results=sink,
    ), workflow_read_only=True)
    return registry, calls


def test_hidden_operation_becomes_discovery_without_executing_its_arguments():
    calls = []

    def answer(messages, **kwargs):
        calls.append(messages)
        return '{"action":"web_search","args":{"query":"unadmitted"}}', None

    decision = decide_next_action(
        NS(chat_json=answer),
        PlannerInput(goal="Find episode titles", skills=[], work={"profile": "general"}),
        valid_skill_names=set(), discoverable_skill_names={"web_search"}, context_window=8192,
    )
    assert decision.action == "discover" and decision.args == {"names": ["web_search"]}
    assert len(calls) == 1


def test_invalid_json_is_bounded_even_before_an_action_was_read():
    calls = []

    def answer(*args, **kwargs):
        calls.append(True)
        return '{"action":broken}', None

    decision = decide_next_action(
        NS(chat_json=answer),
        PlannerInput(goal="Find episode titles", skills=[], work={"profile": "general"}),
        valid_skill_names=set(), discoverable_skill_names={"web_search"}, context_window=8192,
    )
    assert decision.is_finish and decision.outcome == "partial" and len(calls) == 2


def test_fast_tools_overlay_native_fallback_and_keep_complete_schema():
    brain, _ = search_registry()
    skills = build_builtin_skill_registry()
    native = skills.get("web_search")
    skills.replace_fast(shared_fast_skills(brain))
    assert skills.get("web_search").group == "fast"
    assert skills.validate_call("web_search", {"query": "test", "max_results": 99})
    assert skills.validate_call("web_search", {"query": "test", "extra": True})
    assert skills.names().count("web_search") == 1
    skills.replace_fast([])
    assert skills.get("web_search") is native
    skills.set_web_search_enabled(False)
    assert skills.get("web_search") is None


@pytest.mark.parametrize("invalid", ["schema", "generation", "private", "owner"])
def test_shared_call_revalidates_schema_settings_privacy_and_owner(invalid):
    registry, calls = search_registry()
    args = {"tool_name": "web_search", "tool_args": {"query": "Episode titles"},
            "generation": registry.workflow_revision("web_search"),
            "user_id": "u", "source_session": "u:s"}
    if invalid == "schema":
        args["tool_args"]["extra"] = 1
    elif invalid == "generation":
        args["generation"] = "stale"
    elif invalid == "private":
        args["tool_args"]["query"] = "What does Jacob want to watch?"
    handler = FastToolHandler(lambda *a: None if invalid == "owner" else registry)
    events = []
    handler.start(args, events.append)
    assert isinstance(events[0], TaskFailed) and not calls


@pytest.mark.parametrize("changed", [False, True])
def test_background_search_does_not_pollute_turn_sink_and_rechecks_rebuild(changed):
    sink = []
    first, calls = search_registry(sink=lambda *args: sink.append(args))
    replacement, _ = search_registry(scope="u:other" if changed else "u:s")
    generations = iter([first, replacement])
    events = []
    FastToolHandler(lambda *a: next(generations)).start(
        {"tool_name": "web_search", "tool_args": {"query": "Episode titles"},
         "generation": first.workflow_revision("web_search")}, events.append,
    )
    assert isinstance(events[0], TaskFailed if changed else TaskCompleted)
    if not changed:
        assert "ten episodes" in events[0].result["content"]
    assert calls and not sink


def test_real_session_builder_shares_reads_revokes_disabled_tools_and_excludes_writes():
    from test_fast_tool_registration import host

    value = host()
    value._workflow_skill_registry = build_builtin_skill_registry()
    value.rebuild_tool_registry()
    shared = value._workflow_skill_registry
    assert shared.get("recall").group == "fast" and shared.get("inspect_memory")
    assert not shared.get("write_diary") and not shared.get("start_workflow")
    generation = shared.get("recall").generation
    value.rebuild_tool_registry()
    assert shared.get("recall").generation == generation
    value._settings.tools.enabled = False
    value.rebuild_tool_registry()
    assert not shared.get("recall") and not shared.get("web_search")


def test_plugin_must_explicitly_share_a_read_and_generation_is_part_of_contract():
    from app.plugins.sdk import PluginApi
    from app.llm.tools.plugin_tool import PluginFastTool
    from pathlib import Path

    api = PluginApi(plugin_id="math", plugin_root=Path("."), config={})
    api.register_fast_tool(name="calculate", description="Compute", parameters={"type": "object"},
                           handler=lambda args: "42", workflow_read_only=True)
    spec = api.fast_tools[0]
    assert spec.workflow_read_only
    registry = ToolRegistry()
    registry.register(PluginFastTool(spec), workflow_read_only=True, workflow_generation="one")
    first = registry.workflow_revision("calculate")
    registry.register(PluginFastTool(spec), workflow_read_only=True, workflow_generation="two")
    assert registry.workflow_revision("calculate") != first
    registry.register(PluginFastTool(spec))
    assert not registry.workflow_tools()


def test_hidden_search_recovers_to_owned_evidence_and_supported_finish(work):
    from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler

    store, _, orch, skills = work
    brain, calls = search_registry()
    skills.replace_fast(shared_fast_skills(brain))
    # Reproduce a crowded initial menu whose lexical matches crowd out web_search.
    for i in range(7):
        skills.register(WorkflowSkill(
            name=f"a_source_{i}", description="episode titles", effect="read",
            spawn=lambda args, ctx: None,
        ))
    orch.register_handler(FastToolHandler(lambda *a: brain))

    class Client:
        planning = 0

        def chat_json(self, messages, **kwargs):
            if kwargs.get("surface") != "workflow_planner":
                assert "ten episodes" in messages[1]["content"]
                return '{"supported":true,"reason":"Matches the returned source"}', None
            self.planning += 1
            if self.planning <= 2:
                answer = {"action": "web_search", "args": {"query": "Episode titles"}}
            else:
                child = store.list_children(tid)[0]
                answer = {"action": "finish", "outcome": "success",
                          "findings": "Ten episodes; titles have not been announced.",
                          "evidence_ids": [child.id]}
            return json.dumps(answer), None

    handler = GoalWorkflowHandler(orchestrator=orch, skill_registry=skills,
                                  worker_client_provider=lambda: Client())
    client = Client()
    handler._worker_client_provider = lambda: client
    orch.register_handler(handler)
    tid = orch.start_task(user_id="u", handler_name=handler.name, title="Episode titles",
                          args={"goal": "episode titles"},
                          metadata={"source_session": "u:s"})
    row = wait_status(store, tid, {"done", "failed"})
    assert row.result["outcome"] == "success", row.error
    assert row.result["verification"]["status"] == "supported"
    assert len(calls) == 1 and len(store.list_children(tid)) == 1
    child = store.list_children(tid)[0]
    assert child.handler_name == "fast_tool" and child.parent_task_id == tid
    assert "Official episode list" in child.result["content"]
    assert client.planning == 3
