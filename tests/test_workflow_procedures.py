"""Compound tools use the existing child admission/checkpoint path without extra inference."""

from pathlib import Path

import pytest

from app.core.tasks.workflow.skill_registry import WorkflowSkill
from app.plugins.sdk import PluginApi
from test_workflow_harness import start_work, wait_status, ScriptedClient, PARTIAL
from test_workflow_harness import work as work


@pytest.fixture
def procedure_work(work):
    store, events, orchestrator, registry = work
    for name in ("one", "two"):
        registry.register(
            WorkflowSkill(
                name=f"files__{name}",
                group="mcp:files",
                generation="same",
                description="Read source",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                effect="read",
                spawn=lambda args, ctx: ctx.orchestrator.start_task(
                    user_id=ctx.user_id,
                    handler_name="test_reader",
                    title="Read",
                    args=args,
                    parent_task_id=ctx.parent_task_id,
                    notify_aiko=False,
                ),
            )
        )
    api = PluginApi(plugin_id="files", plugin_root=Path("."), config={})
    api.register_procedure(
        "read_pair",
        "Read two sources",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        steps=[
            {"tool": "one", "arguments": {"path": {"$input": "path"}}},
            {"tool": "two", "arguments": {"path": "second"}},
        ],
    )
    definition = {
        **api.procedures[0],
        "name": "files:read_pair",
        "owner": "files",
        "generation": "same",
    }
    return work, {"procedures": {definition["name"]: definition}}


def test_compound_calls_need_only_one_planning_turn_and_retain_children(procedure_work):
    work, extensions = procedure_work
    client = ScriptedClient(
        {
            "action": "run_procedure",
            "args": {"name": "files:read_pair", "inputs": {"path": "first"}},
        },
        PARTIAL,
    )
    task_id, _ = start_work(work, client, extensions_provider=lambda: extensions)
    done = wait_status(work[0], task_id, {"done", "failed"})
    assert done.status == "done", done.error
    assert client.calls == 2
    children = work[0].list_children(task_id)
    assert [row.args["path"] for row in children] == ["first", "second"]
    assert len({row.metadata["harness_step_id"] for row in children}) == 2
    assert done.state["children"] == 2 and not done.state.get("procedure")


def test_new_guidance_stops_remaining_procedure_calls(procedure_work):
    from app.core.tasks.task_handler import TaskCompleted

    work, extensions = procedure_work
    store, events, orch, _ = work

    class Reader:
        name = "test_reader"

        def start(self, args, emit):
            parent = next(row for row in store.list_for_user("u") if row.parent_task_id is None)
            events.append(
                parent.id,
                type="work_message",
                data={
                    "role": "user",
                    "kind": "guidance",
                    "content": "Stop before the second source",
                },
            )
            emit(TaskCompleted(result={"content": "First source read"}))
            return {}

        def cancel(self, state):
            pass

    orch.register_handler(Reader())
    client = ScriptedClient(
        {
            "action": "run_procedure",
            "args": {"name": "files:read_pair", "inputs": {"path": "first"}},
        },
        PARTIAL,
    )
    task_id, _ = start_work(work, client, extensions_provider=lambda: extensions)
    done = wait_status(store, task_id, {"done", "failed"})
    assert done.status == "done", done.error
    assert len(store.list_children(task_id)) == 1
    assert done.state["guidance"][0]["content"] == "Stop before the second source"


def test_uncertain_procedure_effect_is_not_repeated(procedure_work):
    from app.core.tasks.task_handler import TaskFailed

    work, extensions = procedure_work
    calls = []

    class Reader:
        name = "test_reader"

        def start(self, args, emit):
            calls.append(args)
            emit(TaskFailed(error="Response lost after dispatch", uncertain_effect=True))
            return {}

        def cancel(self, state):
            pass

    work[2].register_handler(Reader())
    client = ScriptedClient(
        {
            "action": "run_procedure",
            "args": {"name": "files:read_pair", "inputs": {"path": "first"}},
        }
    )
    task_id, _ = start_work(work, client, extensions_provider=lambda: extensions)
    done = wait_status(work[0], task_id, {"done", "failed"})
    assert done.status == "done" and done.result["outcome"] == "uncertain"
    assert len(calls) == client.calls == 1


def test_configuration_change_stops_between_procedure_calls(procedure_work):
    from app.core.tasks.task_handler import TaskCompleted

    work, extensions = procedure_work

    class Reader:
        name = "test_reader"

        def start(self, args, emit):
            extensions["procedures"]["files:read_pair"]["generation"] = "reconfigured"
            emit(TaskCompleted(result={"content": "First source read"}))
            return {}

        def cancel(self, state):
            pass

    work[2].register_handler(Reader())
    client = ScriptedClient(
        {
            "action": "run_procedure",
            "args": {"name": "files:read_pair", "inputs": {"path": "first"}},
        },
        PARTIAL,
    )
    task_id, _ = start_work(work, client, extensions_provider=lambda: extensions)
    done = wait_status(work[0], task_id, {"done", "failed"})
    assert done.status == "done"
    assert len(work[0].list_children(task_id)) == 1
