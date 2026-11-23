"""Run a fictional chat handoff, worker comparison and source follow-up on loopback."""
import argparse
from contextlib import closing
import json
import multiprocessing
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace as NS

import requests

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import OllamaSettings
from app.core.tasks.handlers.web_search import WebSearchHandler
from app.core.tasks.task_events import TaskEventStore
from app.core.tasks.task_handler import TaskCompleted
from app.core.tasks.task_orchestrator import TaskOrchestrator
from app.core.tasks.task_store import TaskStore
from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler
from app.core.tasks.workflow.skill_registry import build_builtin_skill_registry, WorkflowSkill
from app.core.tasks.work_outcome import task_outcome
from app.core.tasks.work_reports import WorkReports
from app.core.tasks.workflow.work_discussion import WorkDiscussion
from app.llm.tools.workflow_tools import CheckMyWorkTool, StartWorkflowTool
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


def matches_fixture_prices(text):
    """Score the English fixture and leave its full wording in the report for inspection."""
    amounts = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", text))
    if not {"25", "40"} <= amounts <= {"15", "25", "40"}:
        return False
    # A single mention of each number is insufficient if the cities are swapped.
    for city, price in (("Alderwick", "25"), ("Fernford", "40")):
        gap = r"(?:(?!Alderwick|Fernford|\d|[.!?]).){0,90}"
        if not re.search(rf"\b{city}\b{gap}\b{price}\b|\b{price}\b{gap}\b{city}\b",
                         text, re.I):
            return False
    return True


def run(db, client, model, large_catalogue=False):
    store = TaskStore(db)
    orch = TaskOrchestrator(store, event_store=TaskEventStore(db), heartbeat_enabled=False)
    places = {"Alderwick": 25, "Fernford": 40, "Birch Quay": 90}
    listings = [NS(title=f"Night Loom in {city}", url=f"https://example.test/{city.split()[0]}",
                   snippet=f"The Night Loom workshop in {city} costs EUR {cost}.")
                for city, cost in places.items()]
    orch.register_handler(WebSearchHandler(provider=NS(search=lambda *args: listings)))
    registry = build_builtin_skill_registry()
    unrelated_calls = []
    if large_catalogue:
        def unrelated(args, ctx):
            unrelated_calls.append(ctx.parent_task_id)
            return None

        for number in range(512):
            registry.register(WorkflowSkill(
                name=f"star_chart_{number:03}", group=f"observatory_{number:03}", effect="read",
                description="Look up stellar coordinates.", spawn=unrelated,
                input_schema={"type": "object", "properties": {"constellation": {"type": "string"}},
                              "required": ["constellation"]},
            ))

    class Page:
        name = "fixture_page"

        def start(self, args, emit):
            url = args["url"]
            listing = next(item for item in listings if item.url == url)
            emit(TaskCompleted(result={"content": listing.snippet, "postcondition": {
                "requested_resource": url, "resource": url,
            }}))
            return {}

    orch.register_handler(Page())
    registry.register(WorkflowSkill(
        name="read_workshop_page", group="web", effect="read",
        description="Read a workshop's original catalogue page to compare its listed price.",
        input_schema={"type": "object", "properties": {"url": {"type": "string",
                      "enum": [item.url for item in listings]}}, "required": ["url"]},
        spawn=lambda args, ctx: orch.start_task(user_id=ctx.user_id, handler_name=Page.name,
                                               args=args, title=args["url"],
                                               parent_task_id=ctx.parent_task_id),
    ))
    worker = GoalWorkflowHandler(
        orchestrator=orch, skill_registry=registry,
        worker_client_provider=lambda: client, model_provider=lambda: model,
        context_window_provider=lambda: 65536, max_iterations=8, max_wall_seconds=600,
    )
    orch.register_handler(worker)
    request = "Compare the prices of the two Night Loom workshops I mentioned."
    session = NS(_user_id="replay", session_key="replay:workshops", _chat_db=db,
                 _active_turn_user_text=request, _task_orchestrator=orch)
    first = "I'm interested in Night Loom in Alderwick."
    second = "The other one is Night Loom in Fernford."
    for turn in (first, second, request):
        db.add_message(session.session_key, "user", turn)
    tool = StartWorkflowTool(session)
    queued = []
    reports = WorkReports(db, orch, None, user=lambda: session._user_id,
                          session=lambda: session.session_key, client=lambda: client,
                          model=lambda: model, window=lambda: 65536,
                          context=lambda: {"revision": "fixture", "persona": "Be concise."},
                          enqueue=lambda *args: queued.append(args), present=lambda *args: None)
    session._work_reports = reports
    try:
        prompt = [
            {"role": "system", "content": "Prepare arguments for start_workflow. Return JSON. "
             "Include exact reference_quotes from the earlier user messages for referenced items. "
             "Today's date and conversation follow. " + json.dumps(tool.schema().parameters)},
            {"role": "user", "content": time_anchor_and_transcript(db, session.session_key)},
        ]
        raw, _ = client.chat_json(prompt, model=model, format_json=True, think=False,
                                  options={"num_predict": 600}, surface="workshop_handoff")
        args = json.loads(raw)
        print(json.dumps({"handoff": args}), flush=True)
        tid = json.loads(tool.run(args))["task_id"]
        deadline = time.monotonic() + 620
        while time.monotonic() < deadline:
            row = orch.get(tid)
            if row.status not in {"running", "paused"} or row.phase == "budget_exhausted":
                break
            time.sleep(0.25)
        row = orch.get(tid)
        reports.tick()
        delivered = bool(queued and reports.deliver(*queued[0]))
        report = db.get_messages(session.session_key)[-1].content if delivered else ""
        question = "Where did you find that?"
        db.add_message(session.session_key, "user", question)
        proof = json.loads(CheckMyWorkTool(session).run({"view": "sources"}))
        sources = (proof.get("record") or {}).get("sources", [])
        mid = orch.append_work_message(tid, role="user", kind="discussion", content=question)
        discussion = WorkDiscussion(worker)
        try:
            discussion._answer(tid, mid)
        finally:
            discussion.shutdown()
        source_reply = orch.work_messages(tid)[-1].data.get("content", "")
        result = row.result or {}
        refs = row.state.get("contract", {}).get("conversation_references", [])
        urls = {item.get("url") for item in sources}
        passed = (delivered and task_outcome(row)["verified"]
                  and not unrelated_calls
                  and len({r["message_id"] for r in refs}) == 2
                  and urls == {listings[0].url, listings[1].url}
                  and all(item.url in source_reply for item in listings[:2])
                  and all(matches_fixture_prices(text) for text in (result.get("content", ""),
                                                                    report)))
        print(json.dumps({"task": task_outcome(row), "refs": refs, "answer": result.get("content"),
                          "report": report, "sources": sources, "drawer_reply": source_reply,
                          "iterations": row.state.get("iteration"),
                          "registered_skills": len(registry.names()),
                          "unrelated_calls": len(unrelated_calls), "passed": passed}), flush=True)
        return passed
    finally:
        reports.shutdown()
        worker.shutdown()
        orch.shutdown()


def time_anchor_and_transcript(db, session_key):
    from app.core.infra import timephrase
    transcript = timephrase.format_transcript(db.get_messages(session_key))
    return timephrase.today_anchor() + "\n" + transcript


def replay_process(tmp, model, large_catalogue):
    base = "http://127.0.0.1:11434"
    with requests.Session() as session:
        class LocalWorker(LoopbackPolicyClient):
            def chat_json(self, messages, *, options=None, **kwargs):
                raw, usage = super().chat_json(
                    messages, options={**(options or {}), "temperature": 0, "seed": 71}, **kwargs,
                )
                print(json.dumps({"surface": kwargs.get("surface"), "input": usage.prompt_tokens,
                                  "generated": usage.completion_tokens}), flush=True)
                return raw, usage

        client = LocalWorker(OllamaSettings(base, chat_model=model, context_window=65536),
                             base, session)
        require_local_model(session, base, model)
        db = ChatDatabase(Path(tmp) / "work.db")
        with closing(db._get_conn()):
            raise SystemExit(0 if run(db, client, model, large_catalogue) else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--large-catalogue", action="store_true",
                        help="Register 512 unrelated fictional lookup skills.")
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=scratch, prefix="workshops-") as tmp:
        # Worker threads own SQLite connections. Process exit closes them before cleanup.
        proc = multiprocessing.get_context("spawn").Process(target=replay_process,
                                                            args=(tmp, args.model,
                                                                  args.large_catalogue))
        proc.start()
        try:
            proc.join()
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join()
        return proc.exitcode


if __name__ == "__main__":
    raise SystemExit(main())
