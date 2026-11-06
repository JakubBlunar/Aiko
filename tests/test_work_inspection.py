import json
from types import SimpleNamespace as NS

import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_store import TaskStore
from app.llm.tools.base import ToolRegistry
from app.llm.tools.workflow_tools import CheckMyWorkTool


@pytest.fixture
def env(tmp_path):
    db = ChatDatabase(tmp_path / "work.db")
    store = TaskStore(db)
    registry = ToolRegistry()
    registry.register(CheckMyWorkTool(NS(_user_id="u", _task_orchestrator=store)))
    return store, registry


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled", "awaiting_input"])
def test_terminal_and_pending_work_is_readable_without_restarting(env, outcome):
    store, registry = env
    tid = store.create(user_id="u", handler_name="test", title="A recorded task")
    if outcome == "done":
        store.mark_done(tid, result={"answer": "The task finished with this exact result."})
    elif outcome == "failed":
        store.mark_failed(tid, error="The input could not be opened.")
    elif outcome == "cancelled":
        store.mark_cancelled(tid)
    else:
        store.mark_awaiting_input(tid, prompt="Which folder?")
    before = store.get(tid)
    result = registry.dispatch("check_my_work", {"task_id": tid})
    assert result.ok
    task = json.loads(result.content)["task"]
    assert task["status"] == outcome
    assert task["result"] == before.result and task["error"] == before.error
    assert task["input_request"] == before.input_request
    assert store.get(tid).updated_at == before.updated_at


def test_owned_recent_history_includes_completed_tasks(env):
    store, registry = env
    own = store.create(user_id="u", handler_name="test", title="Owned task")
    store.mark_done(own, result={"answer": "done"})
    foreign = store.create(user_id="other", handler_name="test", title="Private task")
    result = json.loads(registry.dispatch("check_my_work", {"scope": "recent"}).content)
    assert [row["task_id"] for row in result["recent"]] == [own]
    result = json.loads(registry.dispatch("check_my_work", {"task_id": foreign}).content)
    assert result["status"] == "not_found" and "task" not in result


def test_result_pages_cannot_silently_mix_task_revisions(env):
    store, registry = env
    tid = store.create(user_id="u", handler_name="test", title="Long result")
    store.mark_done(tid, result={"answer": "A long detail. " * 600 + "Except when declined."})
    first = json.loads(registry.dispatch("check_my_work", {"task_id": tid}).content)["task"]
    assert first["is_fragment"]
    second = json.loads(registry.dispatch("check_my_work", {
        "task_id": tid, "content_offset": first["next_offset"], "revision": first["revision"],
    }).content)["task"]
    combined = json.loads(first["details_fragment"] + second["details_fragment"])
    assert combined["result"]["answer"].endswith("Except when declined.")
    store.mark_failed(tid, error="A new outcome")
    assert not registry.dispatch("check_my_work", {
        "task_id": tid, "content_offset": first["next_offset"], "revision": first["revision"],
    }).ok


def test_failed_work_lookup_is_not_a_successful_empty_list():
    registry = ToolRegistry()
    def unavailable(**kwargs):
        raise RuntimeError("database unavailable")
    registry.register(CheckMyWorkTool(NS(_user_id="u",
        _task_orchestrator=NS(list_running=unavailable))))
    result = registry.dispatch("check_my_work", {})
    assert not result.ok and "unavailable" in result.content
