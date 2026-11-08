"""Conversation-started work persists evidence and resumes the same task."""

import json
import time
import threading
from types import SimpleNamespace

import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_events import TaskEventStore
from app.core.tasks.task_handler import TaskCompleted
from app.core.tasks.task_inputs import TaskInputStore
from app.core.tasks.task_orchestrator import TaskOrchestrator
from app.core.tasks.task_store import TaskStore
from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler
from app.core.tasks.workflow.skill_registry import WorkflowSkill, WorkflowSkillRegistry


class Reader:
    name = "test_reader"

    def start(self, args, emit):
        emit(TaskCompleted(result={"content": "prefix " * 1500 + "TAIL_FACT: blue orchard"}))
        return {}

    def cancel(self, state):
        pass


@pytest.fixture
def work(tmp_path):
    db = ChatDatabase(tmp_path / "work.db")
    store, events = TaskStore(db), TaskEventStore(db)
    orchestrator = TaskOrchestrator(
        store=store, event_store=events, input_store=TaskInputStore(db), heartbeat_enabled=False
    )
    orchestrator.register_handler(Reader())
    registry = WorkflowSkillRegistry()
    registry.register(
        WorkflowSkill(
            name="read_source",
            description="Read a permitted test source.",
            effect="read",
            group="files",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            spawn=lambda args, ctx: ctx.orchestrator.start_task(
                user_id=ctx.user_id,
                handler_name="test_reader",
                title="Read source",
                args=args,
                parent_task_id=ctx.parent_task_id,
                notify_aiko=False,
            ),
        )
    )
    yield store, events, orchestrator, registry
    orchestrator.shutdown(wait=False)


def wait_status(store, task_id, statuses, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = store.get(task_id)
        if row.status in statuses:
            return row
        time.sleep(0.02)
    raise AssertionError(f"task {task_id}: expected {statuses}; got {store.get(task_id)}")


def test_profile_evidence_inspection_and_independent_review(work):
    store, events, orchestrator, registry = work

    class Client:
        calls = 0
        inspected = False

        def chat_json(self, messages, **kwargs):
            if kwargs["surface"] == "workflow_verify":
                assert "TAIL_FACT: blue orchard" in messages[1]["content"]
                return json.dumps(
                    {"supported": True, "reason": "The inspected source supports it."}
                ), None
            self.calls += 1
            child = next((r for r in store.list_for_user("u") if r.parent_task_id), None)
            if self.calls == 1:
                answer = {
                    "action": "read_source",
                    "profile": "reader",
                    "args": {"path": "first"},
                    "plan": [
                        "Read the source",
                        "Inspect the relevant passage",
                        "Review the answer",
                    ],
                }
            elif self.calls == 2:
                answer = {
                    "action": "inspect_result",
                    "args": {"task_id": child.id, "query": "TAIL_FACT"},
                }
            else:
                self.inspected = "TAIL_FACT: blue orchard" in messages[1]["content"]
                answer = {
                    "action": "finish",
                    "outcome": "success",
                    "findings": "The orchard is blue.",
                    "evidence_ids": [child.id],
                }
            return json.dumps(answer), None

    client = Client()
    handler = GoalWorkflowHandler(
        orchestrator=orchestrator, skill_registry=registry, worker_client_provider=lambda: client
    )
    orchestrator.register_handler(handler)
    task_id = orchestrator.start_task(
        user_id="u",
        handler_name=handler.name,
        title="Find orchard detail",
        args={"goal": "Find the orchard detail"},
    )
    row = wait_status(store, task_id, {"done", "failed"})
    assert row.status == "done", row.error
    assert row.result["outcome"] == "success"
    assert row.result["verification"]["status"] == "supported"
    assert row.state["profile"] == "reader"
    assert row.state["steps"][0]["child_id"] == store.list_children(task_id)[0].id
    assert client.inspected
    assert events.list_after(task_id, type="work_message")
    assert row.state["pending"] is None


def start_work(work, client, **options):
    _, _, orchestrator, registry = work
    handler = GoalWorkflowHandler(
        orchestrator=orchestrator,
        skill_registry=registry,
        worker_client_provider=lambda: client,
        **options,
    )
    orchestrator.register_handler(handler)
    task_id = orchestrator.start_task(
        user_id="u",
        handler_name=handler.name,
        title="Inspect source",
        args={"goal": "Inspect the source"},
    )
    return task_id, handler


class ScriptedClient:
    def __init__(self, *decisions, supported=True):
        self.decisions = iter(decisions)
        self.supported = supported
        self.calls = 0

    def chat_json(self, messages, **kwargs):
        if kwargs["surface"] == "workflow_verify":
            return json.dumps({"supported": self.supported, "reason": "Evidence checked."}), None
        self.calls += 1
        decision = next(self.decisions)
        if callable(decision):
            decision = decision(messages)
        return json.dumps(decision), SimpleNamespace(total_tokens=7)


READ = {"action": "read_source", "args": {"path": "first"}}
PARTIAL = {"action": "finish", "outcome": "partial", "findings": "Saved what I found."}


def test_budget_resume_preserves_totals_and_reuses_child_after_restart(work):
    from app.core.tasks.recovery import recover_interrupted_tasks

    store, events, old, registry = work
    task_id, _ = start_work(work, ScriptedClient(READ), max_iterations=1)
    paused = wait_status(store, task_id, {"paused"})
    # A child wait can briefly pause too; wait for the budget boundary.
    end = time.monotonic() + 6
    while paused.phase != "budget_exhausted" and time.monotonic() < end:
        time.sleep(0.03)
        paused = store.get(task_id)
    assert paused.phase == "budget_exhausted"
    assert paused.state["tokens"] == 7
    assert len(store.list_children(task_id)) == 1
    old.shutdown(wait=False)
    restarted = TaskOrchestrator(store=store, event_store=events, heartbeat_enabled=False)
    try:
        handler = GoalWorkflowHandler(
            orchestrator=restarted,
            skill_registry=registry,
            worker_client_provider=lambda: ScriptedClient(PARTIAL),
        )
        restarted.register_handler(handler)
        report = recover_interrupted_tasks(store, orchestrator=restarted)
        assert task_id in report.preserved
        assert restarted.resume_work(task_id)
        done = wait_status(store, task_id, {"done", "failed"})
        assert done.status == "done", done.error
        assert done.state["tokens"] == 14
        assert done.state["iteration"] == 2
        assert done.state["epoch"] == paused.state["epoch"] + 1
        assert len(store.list_children(task_id)) == 1
    finally:
        restarted.shutdown(wait=False)


def test_pause_during_planner_fences_old_decision_and_stale_emit(work):
    from app.core.tasks.task_handler import TaskFailed

    store, _, orchestrator, _ = work
    entered, release = threading.Event(), threading.Event()

    def blocked(_messages):
        entered.set()
        assert release.wait(5)
        return READ

    task_id, _ = start_work(work, ScriptedClient(blocked, PARTIAL))
    try:
        assert entered.wait(3)
        assert orchestrator.pause_work(task_id)
        assert orchestrator.resume_work(task_id)
        # A delayed emit from epoch one must not fail epoch two.
        orchestrator.emit_work_outcome(task_id, 1, TaskFailed(error="stale"))
        release.set()
        done = wait_status(store, task_id, {"done", "failed"})
        assert done.status == "done", done.error
        assert store.list_children(task_id) == []
    finally:
        release.set()


def test_question_answer_continues_same_work_and_is_saved(work):
    store, events, orchestrator, _ = work
    client = ScriptedClient(
        {"action": "ask_user", "args": {"question": "Which source file?"}}, PARTIAL
    )
    task_id, _ = start_work(work, client)
    wait_status(store, task_id, {"awaiting_input"})
    assert orchestrator.answer(task_id, "orchard.txt")
    row = wait_status(store, task_id, {"done", "failed"})
    assert row.status == "done", row.error
    assert row.state["guidance"][0]["content"] == "orchard.txt"
    assert any(
        (event.data or {}).get("content") == "orchard.txt"
        for event in events.latest_work_messages(task_id)
    )


def test_guidance_arriving_during_planning_discards_old_action(work):
    store, events, orchestrator, _ = work
    entered, release = threading.Event(), threading.Event()

    def blocked(_messages):
        entered.set()
        assert release.wait(5)
        return READ

    def check_guidance(messages):
        assert "Do not open that source" in messages[1]["content"]
        return PARTIAL

    task_id, _ = start_work(work, ScriptedClient(blocked, check_guidance))
    try:
        assert entered.wait(3)
        events.append(
            task_id,
            type="work_message",
            data={"role": "user", "kind": "guidance", "content": "Do not open that source"},
        )
        release.set()
        done = wait_status(store, task_id, {"done", "failed"})
        assert done.status == "done", done.error
        assert store.list_children(task_id) == []
        assert done.state["iteration"] == 2
    finally:
        release.set()


def test_claimed_success_without_owned_evidence_becomes_partial(work):
    finish = {"action": "finish", "outcome": "success", "findings": "Everything is done."}
    task_id, _ = start_work(work, ScriptedClient(finish, finish, finish))
    row = wait_status(work[0], task_id, {"done", "failed"})
    assert row.status == "done", row.error
    assert row.result["outcome"] == "partial"
    assert row.result["verification"]["status"] == "unsupported"


def test_terminal_transitions_cannot_overwrite_cancellation_or_success(work):
    store = work[0]
    tid = store.create(
        user_id="u", handler_name="test_reader", title="cancel race", args={}, state={}
    )
    assert store.mark_cancelled(tid)
    assert not store.mark_done(tid, result={"content": "late result"})
    assert not store.mark_failed(tid, error="late error")
    assert store.get(tid).status == "cancelled"
    tid = store.create(
        user_id="u", handler_name="test_reader", title="done race", args={}, state={}
    )
    assert store.mark_done(tid, result={"content": "completed"})
    assert not store.mark_failed(tid, error="late error")
    assert store.get(tid).result["content"] == "completed"


def test_checkpoints_fence_epoch_and_atomic_child_registration(work):
    store = work[0]
    state = {"harness_version": 1, "epoch": 3, "pending": {"step_id": "step-1"}}
    root = store.create(
        user_id="u", handler_name="goal_workflow", title="root", args={}, state=state
    )
    child = store.create(
        user_id="u",
        handler_name="test_reader",
        title="child",
        args={},
        state={},
        parent_task_id=root,
        metadata={"harness_step_id": "step-1", "epoch": 3},
    )
    assert store.get(root).state["pending"]["child_id"] == child
    assert store.child_for_step(root, "step-1").id == child
    assert not store.write_checkpoint(root, {**state, "epoch": 2})
    with pytest.raises(ValueError, match="no longer admitted"):
        store.create(
            user_id="u",
            handler_name="test_reader",
            title="stale",
            args={},
            state={},
            parent_task_id=root,
            metadata={"harness_step_id": "wrong", "epoch": 2},
        )
    assert len(store.list_children(root)) == 1


def test_work_routes_ownership_active_history_paging_and_message_dedupe(work):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.web.rest.work_routes import register_work_routes

    store, events, orchestrator, _ = work
    discussions = []
    orchestrator.register_handler(
        SimpleNamespace(
            name="goal_workflow",
            start=lambda _args, _emit: {},
            discuss_work=lambda *args: discussions.append(args),
        )
    )
    root = store.create(
        user_id="u", handler_name="goal_workflow", title="old active work", args={}, state={}
    )
    other = store.create(
        user_id="other", handler_name="goal_workflow", title="private", args={}, state={}
    )
    for i in range(35):
        tid = store.create(user_id="u", handler_name="test_reader", title=str(i), args={}, state={})
        store.mark_done(tid, result={"content": "complete"})
    child = store.create(
        user_id="u",
        handler_name="test_reader",
        title="large result",
        args={},
        state={},
        parent_task_id=root,
    )
    store.mark_done(child, result={"content": "a" * 13000})
    for i in range(105):
        events.append(root, type="work_message", data={"role": "assistant", "content": str(i)})
    session = SimpleNamespace(
        user_id="u",
        tasks=SimpleNamespace(store=store, event_store=events, orchestrator=orchestrator),
    )
    app = FastAPI()
    register_work_routes(app, session)
    with TestClient(app) as client:
        page = client.get("/api/work").json()
        assert page["has_more"]
        assert root in {row["id"] for row in page["tasks"]}
        assert other not in {row["id"] for row in page["tasks"]}
        assert client.get(f"/api/tasks/{other}/work").status_code == 404
        assert (
            client.post(f"/api/tasks/{other}/work/control", json={"action": "pause"}).status_code
            == 404
        )
        detail = client.get(f"/api/tasks/{root}/work").json()
        assert len(detail["messages"]) == 100
        assert detail["has_older"]
        earlier = client.get(f"/api/tasks/{root}/work?before={detail['oldest_event_id']}").json()
        assert len(earlier["messages"]) == 5
        result = client.get(f"/api/tasks/{child}/result").json()
        assert len(result["content"]) == 12000
        assert result["next_offset"] == 12000
        assert (
            client.get(
                f"/api/tasks/{child}/result?offset=12000&revision={result['revision']}"
            ).json()["content"]
            == "a" * 1000
        )
        assert (
            client.get(f"/api/tasks/{child}/result?offset=12000&revision=stale").status_code == 409
        )
        payload = {"content": "What did you find?", "mode": "discussion", "client_id": "retry-1"}
        first = client.post(f"/api/tasks/{root}/work/messages", json=payload).json()
        second = client.post(f"/api/tasks/{root}/work/messages", json=payload).json()
        assert first["created"] and not second["created"]
        assert first["message_id"] == second["message_id"]
        assert len(discussions) == 1


def test_unknown_effect_failure_is_not_retried_and_remains_uncertain(work):
    from app.core.tasks.task_handler import TaskFailed

    store, _, orchestrator, registry = work

    class Writer:
        name = "test_writer"

        def start(self, args, emit):
            emit(TaskFailed(error="The reply was lost after dispatch", uncertain_effect=True))
            return {}

    orchestrator.register_handler(Writer())
    registry.register(
        WorkflowSkill(
            name="save_source",
            description="Save requested result",
            group="files",
            effect="write",
            input_schema={"type": "object"},
            spawn=lambda args, ctx: ctx.orchestrator.start_task(
                user_id=ctx.user_id,
                handler_name="test_writer",
                title="Save source",
                args=args,
                parent_task_id=ctx.parent_task_id,
                notify_aiko=False,
            ),
        )
    )
    client = ScriptedClient({"action": "save_source", "args": {}})
    task_id, _ = start_work(work, client)
    row = wait_status(store, task_id, {"done", "failed"})
    assert row.status == "done", row.error
    assert row.result["outcome"] == "uncertain"
    assert row.result["verification"]["status"] == "uncertain"
    assert client.calls == 1
    assert len(store.list_children(task_id)) == 1


def test_boot_adopts_child_committed_before_observation_checkpoint(work):
    from copy import deepcopy
    from app.core.tasks.recovery import recover_interrupted_tasks
    from app.core.tasks.workflow.harness_mixin import _signature

    store, events, old, registry = work
    seed_id, _ = start_work(work, ScriptedClient(READ), max_iterations=1)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        seed = store.get(seed_id)
        if seed and seed.phase == "budget_exhausted":
            break
        time.sleep(0.02)
    assert seed.phase == "budget_exhausted"
    old.shutdown(wait=False)
    state = deepcopy(seed.state)
    state.update(
        epoch=1,
        steps=[],
        seen=[],
        iteration=1,
        children=1,
        pending={
            "step_id": "crash-step",
            "skill": "read_source",
            "args": {"path": "first"},
            "child_id": None,
            "effect": "read",
            "attempt": 0,
            "reserved": True,
            "profile": "reader",
            "schema_revision": _signature(registry.get("read_source")),
        },
    )
    state["limits"]["iterations"] = 32
    root = store.create(
        user_id="u",
        handler_name="goal_workflow",
        title="Recover linked source",
        args=state["args"],
        state=state,
    )
    child = store.create(
        user_id="u",
        handler_name="test_reader",
        title="Already read",
        args={},
        state={},
        parent_task_id=root,
        metadata={"harness_step_id": "crash-step", "epoch": 1},
    )
    store.mark_done(child, result={"content": "The orchard is blue."})
    restarted = TaskOrchestrator(store=store, event_store=events, heartbeat_enabled=False)
    client = ScriptedClient(
        {
            "action": "finish",
            "outcome": "success",
            "findings": "The orchard is blue.",
            "evidence_ids": [child],
        }
    )
    try:
        restarted.register_handler(
            GoalWorkflowHandler(
                orchestrator=restarted,
                skill_registry=registry,
                worker_client_provider=lambda: client,
            )
        )
        recover_interrupted_tasks(store, orchestrator=restarted)
        done = wait_status(store, root, {"done", "failed"})
        assert done.status == "done", done.error
        assert done.result["outcome"] == "success"
        assert [row.id for row in store.list_children(root)] == [child]
        assert done.state["steps"][0]["child_id"] == child
        assert client.calls == 1
    finally:
        restarted.shutdown(wait=False)


@pytest.mark.parametrize("different_tail", [False, True])
def test_progress_uses_full_result_not_common_header(work, different_tail):
    store, _, orchestrator, _ = work

    class SourceReader(Reader):
        def start(self, args, emit):
            emit(
                TaskCompleted(
                    result={
                        "content": "shared header " * 100
                        + (args["path"] if different_tail else "same entire result")
                    }
                )
            )
            return {}

    orchestrator.register_handler(SourceReader())
    client = ScriptedClient(
        *[{"action": "read_source", "args": {"path": str(i)}} for i in range(3)]
    )
    task_id, _ = start_work(work, client, max_iterations=3)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        row = store.get(task_id)
        if row.status in {"done", "failed"} or row.phase == "budget_exhausted":
            break
        time.sleep(0.02)
    assert len(store.list_children(task_id)) == 3
    if different_tail:
        assert row.status == "paused" and row.phase == "budget_exhausted"
    else:
        assert row.status == "done" and row.result["outcome"] == "blocked"


def test_plugin_configuration_changed_during_planning_requires_fresh_plan(work):
    from dataclasses import replace

    store, _, orchestrator, registry = work
    registry.register(replace(registry.get("read_source"), generation="old"))

    class Client:
        def chat_json(self, messages, **kwargs):
            registry.register(replace(registry.get("read_source"), generation="new"))
            return json.dumps(
                {
                    "action": "read_source",
                    "profile": "reader",
                    "args": {"path": "first"},
                    "reason": "Inspect source",
                }
            ), None

    task_id, _ = start_work(work, Client())
    finished = wait_status(store, task_id, {"done", "failed"})
    assert finished.result["outcome"] == "blocked"
    assert not store.list_children(task_id)


def test_pause_while_queued_never_dispatches_inference_or_reserves_wait_time(work):
    from app.llm.llm_gate import CONVERSATION_WORKER, TASK, GatedChatClient, LlmPriorityGate

    store, _, orchestrator, _ = work
    gate, calls = LlmPriorityGate(), []
    gate.acquire(CONVERSATION_WORKER)
    client = GatedChatClient(
        SimpleNamespace(chat_json=lambda *a, **k: calls.append(True)), gate, TASK
    )
    task_id, _ = start_work(work, client)
    try:
        deadline = time.monotonic() + 3
        while gate.stats()["queued"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.stats()["queued"] == 1
        assert store.get(task_id).state["active_reserved"] == 0
        assert orchestrator.pause_work(task_id)
        deadline = time.monotonic() + 3
        while gate.stats()["queued"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.stats()["queued"] == 0
        assert calls == []
        assert store.get(task_id).state["active_reserved"] == 0
    finally:
        gate.release(CONVERSATION_WORKER)


def test_route_edit_while_queued_replans_with_new_context_and_preserves_contract(work):
    from app.llm.llm_gate import CONVERSATION_WORKER, TASK, GatedChatClient, LlmPriorityGate

    store, _, _, _ = work
    gate, old_calls, new_calls, window = LlmPriorityGate(), [], [], [4096]
    gate.acquire(CONVERSATION_WORKER)

    def old(*a, **k):
        old_calls.append(True)
        return json.dumps(PARTIAL), None

    def new(*a, **k):
        new_calls.append(True)
        return json.dumps(PARTIAL), None

    client = GatedChatClient(SimpleNamespace(chat_json=old), gate, TASK)
    task_id, _ = start_work(work, client, context_window_provider=lambda: window[0])
    deadline = time.monotonic() + 3
    while gate.stats()["queued"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    window[0] = 8192
    client.retarget(SimpleNamespace(chat_json=new), None)
    gate.release(CONVERSATION_WORKER)
    finished = wait_status(store, task_id, {"done", "failed"}, timeout=7)
    assert finished.status == "done", finished.error
    assert old_calls == [] and new_calls == [True]
    assert finished.state["contract"]["objective"] == "Inspect the source"
    assert finished.state["context_budget"]["context_window"] == 8192
    assert finished.state["iteration"] == 1


def test_deferred_review_resumes_without_repeating_planner(work, monkeypatch):
    from app.llm.call_scope import CallScope
    from app.llm.llm_gate import CONVERSATION_WORKER, TASK, GatedChatClient, LlmPriorityGate

    monkeypatch.setattr(
        "app.core.tasks.workflow.harness_mixin.CallScope",
        lambda **kwargs: CallScope(queue_timeout=0.05, **kwargs),
    )
    store, _, _, _ = work
    gate, finish_entered, occupied, release = (
        LlmPriorityGate(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    calls = {"planner": 0, "review": 0}

    def holder():
        assert finish_entered.wait(4)
        gate.acquire(CONVERSATION_WORKER)
        occupied.set()
        release.wait(4)
        gate.release(CONVERSATION_WORKER)

    thread = threading.Thread(target=holder)
    thread.start()

    class Client:
        def chat_json(self, messages, **kwargs):
            if kwargs["surface"] == "workflow_verify":
                calls["review"] += 1
                return '{"supported":true,"reason":"Saved evidence supports the answer."}', None
            calls["planner"] += 1
            if calls["planner"] == 1:
                return json.dumps(READ), None
            assert calls["planner"] == 2
            child = next(row for row in store.list_for_user("u") if row.parent_task_id)
            finish_entered.set()
            deadline = time.monotonic() + 3
            while not gate.stats()["queued"] and time.monotonic() < deadline:
                time.sleep(0.01)
            return json.dumps(
                {
                    "action": "finish",
                    "outcome": "success",
                    "findings": "The orchard is blue.",
                    "evidence_ids": [child.id],
                }
            ), None

    task_id, _ = start_work(work, GatedChatClient(Client(), gate, TASK))
    try:
        assert occupied.wait(4)
        deadline = time.monotonic() + 3
        row = store.get(task_id)
        while row.phase != "waiting_model" or row.status != "paused":
            assert time.monotonic() < deadline
            time.sleep(0.01)
            row = store.get(task_id)
        assert row.state["review_request"]["findings"] == "The orchard is blue."
        assert row.state["active_reserved"] == 0
        release.set()
        done = wait_status(store, task_id, {"done", "failed"}, timeout=7)
        assert done.status == "done", done.error
        assert done.result["outcome"] == "success"
        assert calls == {"planner": 2, "review": 1}
        assert done.state["iteration"] == 2
    finally:
        release.set()
        thread.join(3)


def test_browser_projection_survives_compaction_then_becomes_stale_after_action(work):
    store, _, orchestrator, registry = work
    page = "Page: orchard\n" + "\n".join(f'[button] "Action {n}" ref=e{n}' for n in range(1, 41))

    class Browser:
        name = "fake_browser"

        def start(self, args, emit):
            result = {"content": "Clicked", "summary": "Click acknowledged"}
            if args["operation"] == "snapshot":
                result = {
                    "content": page,
                    "summary": "Page observed",
                    "planner_context": {
                        "kind": "current_page",
                        "resource": "tab-one",
                        "content": page,
                    },
                }
            emit(TaskCompleted(result=result))
            return {}

        def cancel(self, state):
            pass

    orchestrator.register_handler(Browser())
    for operation in ("snapshot", "click"):
        registry.register(
            WorkflowSkill(
                name=f"browser__{operation}",
                description=operation,
                group="mcp:browser",
                generation="one",
                effect="read",
                input_schema={"type": "object"},
                spawn=lambda args, ctx, op=operation: ctx.orchestrator.start_task(
                    user_id=ctx.user_id,
                    handler_name="fake_browser",
                    title=op,
                    args={"operation": op, "server_id": "browser", "_config_generation": "one"},
                    parent_task_id=ctx.parent_task_id,
                    notify_aiko=False,
                ),
            )
        )

    class Client:
        calls = 0

        def chat_json(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"action": "browser__snapshot", "profile": "browser"}), None
            if self.calls == 2:
                assert "ref=e40" in messages[1]["content"]
                return json.dumps({"action": "browser__click", "args": {"ref": "e40"}}), None
            assert '"status": "stale"' in messages[1]["content"]
            assert "ref=e40" not in messages[1]["content"]
            return json.dumps(PARTIAL), None

    task_id, _ = start_work(work, Client(), context_window_provider=lambda: 8192)
    done = wait_status(store, task_id, {"done", "failed"})
    assert done.status == "done", done.error
    assert done.state["current_observation"]["status"] == "stale"
    assert store.list_children(task_id)[0].result["planner_context"]["content"] == page


def test_guidance_arriving_during_review_discards_old_completion(work):
    store, events, _, _ = work
    entered, release = threading.Event(), threading.Event()

    class Client:
        calls = 0

        def chat_json(self, messages, **kwargs):
            if kwargs["surface"] == "workflow_verify":
                entered.set()
                assert release.wait(5)
                return '{"supported":true,"reason":"The original answer is supported."}', None
            self.calls += 1
            if self.calls == 1:
                return json.dumps(READ), None
            if self.calls == 2:
                child = next(row for row in store.list_for_user("u") if row.parent_task_id)
                return json.dumps(
                    {
                        "action": "finish",
                        "outcome": "success",
                        "findings": "Original answer",
                        "evidence_ids": [child.id],
                    }
                ), None
            assert "Also check the second source" in messages[1]["content"]
            return json.dumps(PARTIAL), None

    client = Client()
    task_id, _ = start_work(work, client)
    try:
        assert entered.wait(3)
        events.append(
            task_id,
            type="work_message",
            data={"role": "user", "kind": "guidance", "content": "Also check the second source"},
        )
        release.set()
        done = wait_status(store, task_id, {"done", "failed"})
        assert done.status == "done", done.error
        assert done.result["outcome"] == "partial"
        assert done.result["content"] == PARTIAL["findings"]
        assert done.state["verification"]["status"] == "outdated"
        assert client.calls == 3
    finally:
        release.set()


def test_all_explicit_guidance_is_preserved_even_after_ten_messages(work):
    _, _, orchestrator, registry = work
    handler = GoalWorkflowHandler(
        orchestrator=orchestrator, skill_registry=registry, worker_client_provider=lambda: None
    )
    state = {"message_cursor": 0, "guidance": []}
    orchestrator.work_messages = lambda *a, **k: [
        SimpleNamespace(
            id=n,
            created_at="2026-11-07",
            data={"role": "user", "kind": "guidance", "content": f"Constraint {n}"},
        )
        for n in range(12)
    ]
    assert handler._guidance(1, state)
    assert [row["content"] for row in state["guidance"]] == [f"Constraint {n}" for n in range(12)]


def test_work_discussion_uses_saved_evidence_without_dispatching_operations(work):
    store, events, orchestrator, _ = work

    class Client(ScriptedClient):
        def chat_json(self, messages, **kwargs):
            if kwargs["surface"] == "work_discussion":
                assert "cannot run tools" in messages[0]["content"]
                assert "How did this go?" in messages[1]["content"]
                return json.dumps({"reply": "Saved partial findings. [[reaction:happy]]"}), None
            return super().chat_json(messages, **kwargs)

    task_id, handler = start_work(work, Client(PARTIAL))
    wait_status(store, task_id, {"done"})
    message_id, created = orchestrator.post_work_message(
        task_id, "How did this go?", mode="discussion", client_id="question-one"
    )
    assert created
    assert orchestrator.post_work_message(
        task_id, "How did this go?", mode="discussion", client_id="question-one"
    ) == (message_id, False)
    end = time.monotonic() + 4
    while time.monotonic() < end:
        reply = events.latest_for_task(task_id, type="work_message")
        if (
            reply
            and (reply.data or {}).get("kind") == "discussion"
            and (reply.data or {}).get("role") == "assistant"
        ):
            break
        time.sleep(0.02)
    assert reply.data["content"] == "Saved partial findings."
    assert not store.list_children(task_id)
    end = time.monotonic() + 2
    while handler._discussions.job_locks and time.monotonic() < end:
        time.sleep(0.01)
    assert not handler._discussions.job_locks
