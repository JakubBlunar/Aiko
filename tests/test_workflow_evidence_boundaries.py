"""The host enforces provenance even when the planner and reviewer agree on a mistake."""
import json
from types import SimpleNamespace as NS

import pytest

from app.core.tasks.handlers.mcp_tool import McpToolHandler
from app.core.tasks.work_evidence import owned_records, submission_receipts
from app.core.tasks.work_outcome import task_outcome
from app.core.tasks.work_report_grounding import fallback
from app.core.tasks.workflow.harness_results import inspect_result, verify_completion
from app.core.tasks.workflow.mcp_skills import register_mcp_skills
from app.mcp.client.manager import McpToolDescriptor
from test_workflow_harness import PARTIAL, ScriptedClient, start_work, wait_status, work as work


@pytest.mark.parametrize("handler", ["web_search", "fast_tool"])
@pytest.mark.parametrize("quote,field,links,expected", [
    ("Directions only", "2.snippet", [0], True),
    ("Directions only", None, [0], True),
    ("Directions only", "1.snippet", [0], False),
    ("Claim from the request", None, [0], False),
    ("Derived summary", None, [0], False),
    ("Directions only", None, [], False),
    ("Directions only", None, [True], False),
])
def test_search_review_admits_only_returned_fields_and_linked_criteria(
    handler, quote, field, links, expected,
):
    payload = {"query": "Claim from the request", "summary": "Derived summary",
               "results": [{"title": "First"}, {"snippet": "Directions only"}]}
    row = NS(id=2, parent_task_id=1, status="done", completed_at="2026-11-23T10:00:00Z",
             handler_name=handler, args={"tool_name": "web_search"},
             result=payload if handler == "web_search" else {"content": json.dumps(payload)})
    orch = NS(get=lambda cid: row if cid == 2 else None, list_children=lambda _: [row])
    state = {"steps": [{"child_id": 2, "status": "done"}],
             "contract": {"objective": "Check venue", "criteria": ["Hours", "Access"]},
             "inspections": [inspect_result(orch, 1, {"task_id": 2})]}
    claim = {"part": 0, "quote": quote, "task_id": 2}
    if field:
        claim["field"] = field

    def review(messages, **kwargs):
        for source in json.loads(messages[1]["content"])["evidence"]:
            assert "Claim from the request" not in source["content"]
            assert "Derived summary" not in source["content"]
        return json.dumps({"supported": True, "missing": [], "claims": [claim], "criteria": [
            {"index": 0, "supported": True, "claim_indices": [0]},
            {"index": 1, "supported": True, "claim_indices": links},
        ]}), None

    check, _ = verify_completion(NS(chat_json=review), "scripted", state, orch, 1,
                                 "Directions only", [2], 10, context_window=65536)
    assert check["status"] == ("supported" if expected else "unsupported")
    if expected:
        assert check["certificate"]["claims"][0]["field"] == "2.snippet"


@pytest.mark.parametrize("finding,extra_source", [
    ("The venue has a lift.", False),
    ("The venue has a lift.", True),
    ("See our venue map.", False),
])
def test_irrelevant_snippet_or_dropped_qualification_cannot_certify(finding, extra_source):
    snippet = "See our venue map. The listed route is temporarily closed."
    row = NS(id=2, parent_task_id=1, status="done", completed_at=None, args={},
             handler_name="web_search",
             result={"query": finding, "results": [{"snippet": snippet}]})
    rows = [row]
    if extra_source:
        rows.append(NS(id=3, parent_task_id=1, status="done", completed_at=None, args={},
                       handler_name="fast_tool", result={"content": "Local time is 14:30."}))
    orch = NS(get=lambda n: next((r for r in rows if r.id == n), None),
              list_children=lambda _: rows)
    state = {"steps": [{"child_id": 2, "status": "done"}], "contract": {"objective": "Access"}}
    calls = []

    def review(*args, **kwargs):
        calls.append(True)
        return json.dumps({"supported": True, "missing": [],
                           "criteria": [{"index": 0, "supported": True}],
                           "claims": [{"part": 0, "task_id": 2, "quote": snippet}]}), None

    check, _ = verify_completion(NS(chat_json=review), "scripted", state, orch, 1, finding,
                                 [2], 10, context_window=65536)
    assert check["status"] == "unsupported"
    assert bool(calls) == extra_source


@pytest.fixture
def notes(work):
    _, _, orch, registry = work
    schema = {"type": "object"}
    descriptors = [McpToolDescriptor("filesystem", name, name, schema)
                   for name in ("read_text_file", "write_file", "preview")]
    contents = {"note": "original"}
    calls = []

    def call(server, name, args):
        calls.append(name)
        if name == "write_file":
            contents[args["path"]] = args["content"]
            text = "Acknowledged."
        else:
            text = contents[args["path"]]
        return NS(isError=False, content=[NS(text=text)])

    manager = NS(list_available_tools=lambda: descriptors, call_tool=call)
    orch.register_handler(McpToolHandler(manager=manager, resolve_approval=lambda _: "auto"))
    register_mcp_skills(registry, manager)
    return work, calls


READ = {"action": "filesystem__read_text_file", "args": {"path": "note"}}
WRITE = {"action": "filesystem__write_file", "args": {"path": "note", "content": "changed"}}


@pytest.mark.parametrize("operation,expected", [
    (READ, "partial"), (WRITE, "submitted"),
    ({"action": "filesystem__preview", "args": {"path": "note"}}, "partial"),
])
def test_only_an_owned_dispatched_write_can_be_reported_as_submitted(notes, operation, expected):
    (store, _, orch, _), calls = notes

    def submit(_):
        child = next(row for row in store.list_for_user("u") if row.parent_task_id)
        return {"action": "finish", "outcome": "submitted", "evidence_ids": [child.id]}

    tid, _ = start_work(notes[0], ScriptedClient(operation, submit))
    root = wait_status(store, tid, {"done"})
    assert len(calls) == 1
    assert task_outcome(root)["state"] == expected
    if expected == "submitted":
        child, = store.list_children(tid)
        assert root.result["verification"]["submissions"][0]["task_id"] == child.id
        sources = owned_records(orch, tid, root.result["steps"])
        assert operation["action"] in fallback(root, sources)
        assert "could not verify" in fallback(root, [])
        child.args["tool_args"]["path"] = "different-note"
        assert not submission_receipts(NS(get=lambda _: child), tid, root.state, [child.id])


def test_read_back_observes_change_and_duplicate_write_is_blocked(notes):
    (store, _, _, _), calls = notes
    tid, _ = start_work(notes[0], ScriptedClient(READ, WRITE, READ, WRITE))
    result = wait_status(store, tid, {"done"}).result
    assert calls == ["read_text_file", "write_file", "read_text_file"]
    assert [child.result["content"] for child in store.list_children(tid)] == [
        "original", "Acknowledged.", "changed",
    ]
    assert result["outcome"] == "blocked"


def test_repeated_unchanged_reads_still_hit_observation_limit(notes):
    (store, _, _, _), calls = notes
    tid, _ = start_work(notes[0], ScriptedClient(READ, READ, READ, PARTIAL))
    assert wait_status(store, tid, {"done"}).result["outcome"] == "blocked"
    assert calls == ["read_text_file"] * 3
