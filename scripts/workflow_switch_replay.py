"""Exercise a note-to-register handoff with overlapping fictional read capabilities."""
import argparse
from functools import partial
import json
import re
import time
from types import SimpleNamespace

from app.core.tasks.task_events import TaskEventStore
from app.core.tasks.task_handler import TaskCompleted
from app.core.tasks.task_orchestrator import TaskOrchestrator
from app.core.tasks.task_store import TaskStore
from app.core.tasks.work_outcome import task_outcome
from app.core.tasks.work_reports import WorkReports
from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler
from app.core.tasks.workflow.skill_registry import build_builtin_skill_registry, WorkflowSkill
from app.llm.tools.workflow_tools import CheckMyWorkTool, StartWorkflowTool
from scripts.workflow_replay_runner import run_isolated

REQUEST = ("Read my latest pickup note for Juniper. Where do I collect it according to the "
           "entry the note references? If the note has no reference, tell me that.")
LOCATIONS = {"waybill": "Copper Alcove", "claimcheck": "Pearl Rotunda"}
REFERENCE = "JN-4827"
NOTE = "pickup_note_latest"
REFERENCE_CASES = ("resolved", "ambiguous-waybill", "ambiguous-claimcheck",
                   "indirect", "paraphrase")
PURPOSE = {"waybill": "Tracks freight entrusted to a carrier.",
           "claimcheck": "Tracks personal property left with an attendant."}


def run(db, client, model, case):
    store = TaskStore(db)
    orch = TaskOrchestrator(store, event_store=TaskEventStore(db), heartbeat_enabled=False)
    registry = build_builtin_skill_registry(web_search_enabled=False)
    calls = []
    records = {}

    class Lookup:
        name = "fixture_lookup"

        def start(self, args, emit):
            name, key = args["name"], args["key"]
            calls.append([name, key])
            expected_key, content = records[name]
            matched = key.casefold() == expected_key.casefold()
            returned = content if matched else "No matching entry."
            emit(TaskCompleted(result={"content": returned}))
            return {}

    def spawn(name, parameter, args, ctx):
        return orch.start_task(user_id=ctx.user_id, handler_name=Lookup.name, title=name,
                               args={"name": name, "key": args[parameter]},
                               parent_task_id=ctx.parent_task_id)

    def register(name, description, group, parameter, key, content):
        records[name] = (key, content)
        registry.register(WorkflowSkill(
            name=name, description=description, group=group, effect="read",
            input_schema={"type": "object", "properties": {parameter: {"type": "string"}},
                          "required": [parameter], "additionalProperties": False},
            spawn=partial(spawn, name, parameter),
        ))

    orch.register_handler(Lookup())
    ambiguous = case.startswith("ambiguous-")
    target = case.removeprefix("ambiguous-") if ambiguous else case
    if ambiguous or case == "resolved":
        note = f"Juniper pickup: {REFERENCE} appears in both the waybill and the claimcheck. "
        if ambiguous:
            note += "The box indicating which one applies to this pickup was left blank."
        else:
            note += "The signed instruction assigns the claimcheck to this pickup."
            target = "claimcheck"
    elif case == "indirect":
        target = "waybill"
        note = (f"Juniper pickup: use the paper the carrier signed when it took our freight. "
                f"The number on it is {REFERENCE}.")
    elif case == "paraphrase":
        target = "waybill"
        note = f"Juniper pickup: look under {REFERENCE} on the document accompanying the shipment."
    elif case in LOCATIONS:
        note = f"Juniper pickup: the linked {case} entry is {REFERENCE}."
    else:
        note = "Juniper pickup: no register reference has been assigned."
    register(NOTE, "Read the latest personal pickup note for a project.",
             "notebook", "project", "Juniper", note)
    for edition in ("archived", "template", "draft", "team", "sample"):
        register(f"pickup_note_{edition}", f"Read the {edition} pickup note for a project.",
                 "notebook", "project", "Juniper", "Example pickup location: Slate Annex.")
    for register_name, place in LOCATIONS.items():
        for edition in ("current", "archive", "preview"):
            content = (f"{register_name.title()} {REFERENCE}: collect at {place}."
                       if edition == "current" else "Superseded location: Slate Annex.")
            description = f"Read the {edition} {register_name} entry by its identifier."
            if case in REFERENCE_CASES:
                description += " " + PURPOSE[register_name]
            register(f"{register_name}_{edition}", description,
                     register_name, "identifier", REFERENCE, content)

    worker = GoalWorkflowHandler(
        orchestrator=orch, skill_registry=registry, worker_client_provider=lambda: client,
        model_provider=lambda: model, context_window_provider=lambda: 65536,
        max_iterations=9, max_wall_seconds=480,
    )
    orch.register_handler(worker)
    session = SimpleNamespace(_user_id="replay", session_key="replay:switch", _chat_db=db,
                              _active_turn_user_text=REQUEST, _task_orchestrator=orch)
    db.add_message(session.session_key, "user", REQUEST)
    queued = []
    reports = WorkReports(db, orch, None, user=lambda: "replay",
                          session=lambda: session.session_key,
                          client=lambda: client, model=lambda: model, window=lambda: 65536,
                          context=lambda: {"revision": "fixture", "persona": "Be concise."},
                          enqueue=lambda *args: queued.append(args), present=lambda *args: None)
    session._work_reports = reports
    try:
        start = StartWorkflowTool(session).run({"goal": REQUEST, "profile": "reader"})
        tid = json.loads(start)["task_id"]
        deadline = time.monotonic() + 500
        row = orch.get(tid)
        question = ""
        clarified = False
        while time.monotonic() < deadline:
            if row.status == "awaiting_input":
                if question:
                    break
                question = (row.input_request or {}).get("prompt", "")
                print(json.dumps({"question": question, "calls_before_question": calls}),
                      flush=True)
                if (not ambiguous or calls != [[NOTE, "Juniper"]]
                        or not all(name in question.lower() for name in LOCATIONS)):
                    break
                clarified = orch.answer(tid, f"Use the {target} for this pickup.")
                if not clarified:
                    break
            elif row.status not in {"running", "paused"}:
                break
            if row.phase == "budget_exhausted":
                break
            time.sleep(0.2)
            row = orch.get(tid)
        reports.tick()
        delivered = bool(queued and reports.deliver(*queued[-1]))
        report = db.get_messages(session.session_key)[-1].content if delivered else ""
        proof = json.loads(CheckMyWorkTool(session).run({"view": "sources"}))
        sources = (proof.get("record") or {}).get("sources", [])
        quotes = [s["quote"] for s in sources]
        children = {child.title: child.id for child in orch.list_children(tid)}
        answer = (row.result or {}).get("content", "")
        expected_calls = [[NOTE, "Juniper"]]
        if target in LOCATIONS:
            expected_calls.append([f"{target}_current", REFERENCE])
            expected = LOCATIONS[target]
            correct = (all(expected.casefold() in text.casefold() for text in (answer, report))
                       and any(s["task_id"] == children.get(f"{target}_current")
                               and expected.casefold() in s["quote"].casefold() for s in sources)
                       and any(s["task_id"] == children.get(NOTE) and REFERENCE in s["quote"]
                               and s["quote"] in note for s in sources))
        else:
            correct = all(re.search(r"\b(?:no|without|missing|not|hasn't|hasn’t)\b.{0,40}reference"
                                    r"|reference.{0,30}\b(?:missing|not|absent)\b", text, re.I)
                          for text in (answer, report)) and any("no register reference" in quote
                                                                for quote in quotes)
        wrong_places = {"Slate Annex", *LOCATIONS.values()} - {LOCATIONS.get(target)}
        correct = correct and not any(place.casefold() in text.casefold() for place in wrong_places
                                      for text in (answer, report))
        passed = bool(correct and delivered and task_outcome(row)["verified"]
                      and calls == expected_calls and clarified == ambiguous
                      and not row.state.get("reference_query"))
        print(json.dumps({"case": case, "calls": calls, "outcome": task_outcome(row),
                          "answer": answer, "report": report, "quotes": quotes,
                          "passed": passed}), flush=True)
        return passed
    finally:
        reports.shutdown()
        worker.shutdown()
        orch.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    cases = [*LOCATIONS, "missing", *REFERENCE_CASES]
    parser.add_argument("--case", choices=[*cases, "references", "all"], default="all")
    args = parser.parse_args()
    if args.case != "all":
        cases = REFERENCE_CASES if args.case == "references" else [args.case]
    results = [run_isolated(run, args.model, "switch-", case) for case in cases]
    raise SystemExit(int(any(results)))
