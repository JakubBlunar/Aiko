"""Conversation references and saved source answers share the real task and message stores."""
import json
from types import SimpleNamespace as NS

import pytest

from app.core.tasks.work_evidence import owned_records
from app.core.tasks.work_reports import WorkReports
from app.core.tasks.task_store import TaskStore
from app.core.infra.chat_database import ChatDatabase
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
        reply = orch.work_messages(tid)[-1].data
        assert reply["evidence_ids"] == [child] and listing["url"] in reply["content"]
        assert "example.test/b" not in reply["content"]
        msg = orch.append_work_message(tid, role="user", kind="discussion",
                                       content='What is the source for "free parking"?')
        discussion._answer(tid, msg)
        assert listing["url"] not in orch.work_messages(tid)[-1].data["content"]
    finally:
        discussion.shutdown()
    db.add_message("u:s", "assistant", "Let's talk about something else.")
    assert json.loads(tool.run({"view": "sources"}))["status"] == "needs_task"
    db.execute_commit("UPDATE tasks SET result='{}' WHERE id=?", (child,))
    assert not answer_sources(orch, store.get(tid))["sources"]
    session._user_id = "other"
    assert json.loads(tool.run({"view": "sources", "task_id": tid}))["status"] == "not_found"


def test_reworded_report_traces_its_own_sentence_after_restart(env):
    db, store, orch = env
    reports, tid, _, queued, shown, _ = make_reports(env)
    original = store.get(tid).result
    first = original["steps"][0]["child_id"]
    second = store.create(user_id="u", handler_name="test_reader", title="Access note",
                          parent_task_id=tid)
    store.mark_done(second, result={"content": "Visitors should use the east gate."})
    check = certificate(original["content"], owned_records(orch, tid, original["steps"]))
    check["certificate"]["claims"] = [
        {"part": 0, "task_id": first, "quote": "The orchard is blue."}
    ]
    db.execute_commit("UPDATE tasks SET result=? WHERE id=?",
                      (json.dumps({**original, "verification": check}), tid))
    draft = "The orchard is blue. Enter through the eastern gate."

    def model(messages, **kwargs):
        if kwargs["surface"] == "work_report":
            return json.dumps({"text": "[[reaction:calm]]\n" + draft}), None
        assert json.loads(messages[1]["content"])["proposed_report"] == draft
        return json.dumps({"supported": True, "missing": [],
                           "criteria": [{"index": 0, "supported": True}], "claims": [
                               {"part": 0, "task_id": first, "quote": "The orchard is blue."},
                               {"part": 1, "task_id": second,
                                "quote": "Visitors should use the east gate."},
                           ]}), None

    reports.client = lambda: NS(chat_json=model)
    reports.tick()
    assert reports.deliver(*queued[0]) and shown[0][1] == draft
    reopened = ChatDatabase(db._db_path)
    reader = WorkReports(reopened, TaskStore(reopened), None, user=lambda: "u",
                         session=lambda: "u:s", client=None, model=None, window=None,
                         context=None, enqueue=None, present=None)
    try:
        session = NS(_user_id="u", session_key="u:s", _task_orchestrator=orch, _work_reports=reader)
        tool = CheckMyWorkTool(session)
        query = {"view": "sources", "passage": "eastern gate"}
        source, = json.loads(tool.run(query))["record"]["sources"]
        assert source["task_id"] == second
        assert source["supports"] == ["Enter through the eastern gate."]
        assert json.loads(tool.run({**query, "task_id": tid}))["status"] == "unavailable"
        assert json.loads(tool.run({**query, "passage": "western gate"}))["status"] == "unavailable"
        db.execute_commit("UPDATE tasks SET result=? WHERE id=?",
                          (json.dumps({"content": "Visitors should use the west gate."}), second))
        assert json.loads(tool.run(query))["status"] == "unavailable"
        db.execute_commit("DELETE FROM work_report_sources WHERE report_id=?", (queued[0][1],))
        db.execute_commit("UPDATE tasks SET result=? WHERE id=?",
                          (json.dumps({"content": "Visitors should use the east gate."}), second))
        assert json.loads(tool.run({"view": "sources"}))["status"] == "unavailable"
    finally:
        reopened._get_conn().close()
