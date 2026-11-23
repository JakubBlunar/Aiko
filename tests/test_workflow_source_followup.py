"""Conversation references and saved source answers share the real task and message stores."""
import json
from types import SimpleNamespace as NS

import pytest

from app.core.tasks.work_evidence import owned_records
from app.core.tasks.work_sources import answer_sources
from app.core.tasks.workflow.work_discussion import WorkDiscussion
from app.llm.tools.base import ToolError
from app.llm.tools.workflow_tools import CheckMyWorkTool, StartWorkflowTool
from test_work_speech import env as env, make_reports
from test_workflow_tools import _FakeOrchestrator, _FakeSession
from work_review_helpers import certificate


def test_quote_binding_preserves_qualifiers_and_excludes_other_conversations(env):
    db, _, _ = env
    source = "I ruled out Night Loom in Alderwick. I meant Night Loom in Fernford."
    mid = db.add_message("u:s", "user", source)
    db.add_message("u:other", "user", source)
    db.add_message("u:s", "assistant", source)
    request = "Look up the one I meant."
    db.add_message("u:s", "user", request)
    orch = _FakeOrchestrator()
    session = _FakeSession(orchestrator=orch, user_id="u", active_user_text=request)
    session.session_key, session._chat_db = "u:s", db
    StartWorkflowTool(session).run({"goal": "Compare", "reference_quotes": [
        '"Night Loom in Fernford"', request,
    ]})
    ref, = orch.start_calls[0]["args"]["contract"]["conversation_references"]
    assert ref["message_id"] == mid and ref["selected"] == "Night Loom in Fernford"
    assert source in ref["at_handoff"] and ref["message_revision"] and not ref["excerpt"]


@pytest.mark.parametrize("quotes,private", [
    ([], False), (["Night Loom"], False), (["Night Loom in Valewood"], False),
    (["Night Loom in Fernford"], False), (["Night Loom in Fernford", "Fernford"], False),
    (["Night Loom in Fernford"], True),
])
def test_unresolved_references_never_queue_work(env, quotes, private):
    db, _, _ = env
    request = "Compare the two workshops I mentioned."
    for city in ("Fernford", "Alderwick"):
        db.add_message("u:s", "user", "Night Loom in " + city)
    db.add_message("other:s", "user", "Night Loom in Valewood")
    db.add_message("u:s", "user", request)
    orch = _FakeOrchestrator()
    session = _FakeSession(orchestrator=orch, user_id="u", active_user_text=request)
    session.session_key, session._chat_db, session._remember_history = "u:s", db, not private
    with pytest.raises(ToolError):
        StartWorkflowTool(session).run({"goal": "Compare", "reference_quotes": quotes})
    assert not orch.start_calls


def test_chat_and_drawer_trace_the_prior_report_to_an_older_search_hit(env):
    db, store, orch = env
    reports, tid, _, queued, _, _ = make_reports(env)
    child = store.create(user_id="u", handler_name="web_search", title="Workshops",
                         parent_task_id=tid)
    quote = "Night Loom in Alderwick costs EUR 25."
    listing = {"title": "Alderwick workshop", "snippet": quote, "url": "https://example.test/a"}
    store.mark_done(child, result={"results": [
        {"title": "Other town", "url": "https://example.test/b"}, listing,
    ]})
    for i in range(9):
        cid = store.create(user_id="u", handler_name="fast_tool", title="Other",
                           parent_task_id=tid)
        store.mark_done(cid, result={"content": str(i)})
    check = certificate(quote, owned_records(orch, tid, []))
    check["certificate"].update(method="search_extract", claims=[
        {"part": 0, "task_id": child, "field": "2.snippet", "quote": quote},
    ])
    result = {"outcome": "success", "content": quote, "verification": check}
    db.execute_commit("UPDATE tasks SET result=? WHERE id=?", (json.dumps(result), tid))
    reports.tick()
    assert reports.deliver(*queued[0])
    session = NS(_user_id="u", session_key="u:s", _task_orchestrator=orch, _work_reports=reports)
    tool = CheckMyWorkTool(session)
    page = json.loads(tool.run({"view": "sources"}))
    source, = page["record"]["sources"]
    assert source["url"] == listing["url"] and source["quote"] == quote
    msg = orch.append_work_message(tid, role="user", kind="discussion",
                                   content="Where did you find that price?")
    discussion = WorkDiscussion(NS(_orchestrator=orch))
    try:
        discussion._answer(tid, msg)
    finally:
        discussion.shutdown()
    reply = orch.work_messages(tid)[-1].data
    assert reply["evidence_ids"] == [child] and listing["url"] in reply["content"]
    assert "example.test/b" not in reply["content"]
    db.add_message("u:s", "assistant", "Let's talk about something else.")
    assert json.loads(tool.run({"view": "sources"}))["status"] == "needs_task"
    db.execute_commit("UPDATE tasks SET result='{}' WHERE id=?", (child,))
    assert not answer_sources(orch, store.get(tid))["sources"]
    session._user_id = "other"
    assert json.loads(tool.run({"view": "sources", "task_id": tid}))["status"] == "not_found"
