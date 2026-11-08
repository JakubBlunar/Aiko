"""Recover omitted tool evidence without redispatching an operation."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.handlers.mcp_tool import McpToolHandler
from app.core.tasks.result_artifacts import ResultArtifacts
from app.core.tasks.task_store import TaskStore
from app.core.tasks.workflow.harness_results import inspect_result, verify_completion


def setup(tmp_path, **limits):
    db = ChatDatabase(tmp_path / "results.db")
    store = TaskStore(db)
    parent = store.create(user_id="u", handler_name="goal_workflow", title="Read")
    child = store.create(
        user_id="u", handler_name="mcp_tool", title="Source", parent_task_id=parent
    )
    artifacts = ResultArtifacts(db, **limits)
    return db, store, parent, child, artifacts


def test_handler_preserves_raw_tail_and_review_can_use_owned_excerpt(tmp_path, monkeypatch):
    db, store, parent, child, artifacts = setup(tmp_path)
    body = "prefix " * 18000 + "TAIL_FACT: blue orchard"
    calls = []

    def call(*a, **kw):
        calls.append(1)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=body)])

    monkeypatch.setattr(
        "app.core.tasks.workflow.goal_workflow_handler._task_id_from_context", lambda: child
    )
    events = []
    McpToolHandler(manager=SimpleNamespace(call_tool=call), result_artifacts=artifacts).start(
        {"server_id": "files", "tool_name": "read", "tool_args": {}}, events.append
    )
    payload = events[-1].result
    assert payload["truncated"] and "TAIL_FACT" not in payload["content"]
    assert payload["source_artifact"]["available"]
    store.mark_done(child, result=payload)
    host = SimpleNamespace(get=store.get, result_artifacts=artifacts)
    excerpt = inspect_result(host, parent, {"task_id": child, "query": "TAIL_FACT"})
    assert "TAIL_FACT: blue orchard" in excerpt["content"]
    assert not excerpt["source_partial"] and excerpt["view"] == "source"

    def review(messages, **kwargs):
        assert "TAIL_FACT: blue orchard" in messages[1]["content"]
        return json.dumps({"supported": True}), None

    verdict, _ = verify_completion(
        SimpleNamespace(chat_json=review),
        None,
        {
            "contract": {"objective": "Read orchard"},
            "inspections": [excerpt],
            "steps": [{"child_id": child, "status": "done"}],
        },
        host,
        parent,
        "Blue orchard",
        [child],
        30,
        context_window=8192,
    )
    assert verdict["status"] == "supported"
    assert calls == [1]
    assert artifacts.read(parent + 999, child) is None
    assert "error" in inspect_result(host, parent + 999, {"task_id": child})
    assert "error" in inspect_result(host, parent, {"task_id": child, "revision": "old"})
    db._get_conn().close()


def test_unicode_quotas_are_atomic_and_cleanup_follows_owner(tmp_path):
    db, store, parent, child, artifacts = setup(tmp_path, per_result_bytes=100, per_job_bytes=120)
    other = store.create(user_id="u", handler_name="mcp_tool", title="Other", parent_task_id=parent)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda cid: artifacts.save(cid, "猫" * 100), [child, other]))
    assert all(item["source_partial"] for item in results)
    total = (
        db._get_conn()
        .execute("SELECT SUM(length(CAST(content AS BLOB))) FROM work_result_artifacts")
        .fetchone()[0]
    )
    assert total <= 120
    assert artifacts.read(parent, child)["content"].strip("猫") == ""
    store.delete(parent)
    assert db._get_conn().execute("SELECT COUNT(*) FROM work_result_artifacts").fetchone()[0] == 0
    db._get_conn().close()


def test_artifact_rest_paging_keeps_parent_owner_and_revision_checks(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.web.rest.work_routes import register_work_routes

    db, store, parent, child, artifacts = setup(tmp_path)
    body = "source " * 20000 + "TAIL"
    artifacts.save(child, body)
    store.mark_done(child, result={"content": "compact view", "truncated": True})
    session = SimpleNamespace(
        user_id="u",
        tasks=SimpleNamespace(
            store=store, event_store=None, orchestrator=SimpleNamespace(result_artifacts=artifacts)
        ),
    )
    app = FastAPI()
    register_work_routes(app, session)
    with TestClient(app) as client:
        response = client.get(f"/api/tasks/{child}/result?view=source&offset=139998")
        assert response.status_code == 200 and "TAIL" in response.json()["content"]
        assert not response.json()["truncated"]
        assert client.get(f"/api/tasks/{child}/result?view=source&revision=old").status_code == 409
        session.user_id = "other"
        assert client.get(f"/api/tasks/{child}/result?view=source").status_code == 404
    db._get_conn().close()
