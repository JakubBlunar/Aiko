"""Small-context tasks keep their contract, current evidence and dispatch boundaries."""

import json
import threading
from types import SimpleNamespace

import pytest

from app.core.tasks.workflow.context_budget import (
    TaskBudget,
    TaskContextOverflow,
    dumps,
    message_tokens,
    pack_planner,
    pack_review,
)
from app.core.tasks.workflow.workflow_planner import PlannerInput, PlannerStep, decide_next_action
from app.llm.call_scope import CallScope, call_scope, LlmCallCancelled, LlmCallDeferred
from app.llm.llm_gate import CONVERSATION_WORKER, TASK, GatedChatClient, LlmPriorityGate


def skill(name="browser__click"):
    return {
        "name": name,
        "description": "Use a current observed target. Never guess a ref.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
            "additionalProperties": False,
        },
        "args": {"ref": {"type": "string", "required": True}},
        "effect": "unknown",
    }


def test_task_system_prefix_is_stable_across_clock_changes(monkeypatch):
    from app.core.infra import timephrase
    from app.core.tasks.workflow.workflow_planner import render_planner_messages

    ctx = context()
    monkeypatch.setattr(timephrase, "today_anchor", lambda: "TIME_ONE")
    first, _, _ = pack_planner(ctx, TaskBudget.resolve(8192))
    monkeypatch.setattr(timephrase, "today_anchor", lambda: "TIME_TWO")
    second, _, _ = pack_planner(ctx, TaskBudget.resolve(8192))
    assert first[0] == second[0]
    assert "TIME_ONE" in first[1]["content"] and "TIME_TWO" in second[1]["content"]
    assert first[1]["content"].startswith("CONTRACT:")
    assert first[1]["content"].count('"constraints"') == 1
    assert timephrase.STORED_TEXT_TIME_RULE in first[0]["content"]
    assert render_planner_messages(ctx)[1]["content"].count("TIME_TWO") == 1


def context(**patch):
    contract = {
        "objective": "Find the orchard settings",
        "criteria": ["Read the saved state"],
        "constraints": ["Do not submit a payment", "Do not alter another tab"],
        "created_at": "2026-11-07T12:00:00Z",
    }
    return PlannerInput(
        goal=contract["objective"],
        skills=[skill()],
        work={
            "contract": contract,
            "profile": "browser",
            "profile_instructions": "Inspect then act",
            "user_guidance": "Keep the original constraints.",
            **patch,
        },
    )


@pytest.mark.parametrize("window", [4096, 8192, 16384, 65536])
def test_whole_prompt_budget_keeps_goal_and_constraints_under_history_pressure(window):
    ctx = context(
        notes="irrelevant notes " * 20000,
        plan=["large plan " * 1000] * 12,
        inspected_results=[{"content": "old text " * 10000}] * 3,
    )
    object.__setattr__(ctx, "steps", [PlannerStep("old", {}, "done", "data " * 1000)] * 30)
    budget = TaskBudget.resolve(window, routine=True)
    messages, selected, report = pack_planner(ctx, budget)
    rendered = "\n".join(row["content"] for row in messages)
    for value in [
        ctx.goal,
        *ctx.work["contract"]["constraints"],
        *ctx.work["contract"]["criteria"],
    ]:
        assert value in rendered
    assert selected[0]["input_schema"] == ctx.skills[0]["input_schema"]
    assert message_tokens(messages) <= budget.input_limit
    assert (
        report["estimated_input_tokens"] + budget.answer + budget.reasoning + budget.margin
        <= window
    )
    assert len(dumps(ctx.work)) > len(rendered)  # durable state was not rewritten/shortened


def test_oversize_original_contract_fails_before_calling_model():
    ctx = context()
    ctx.work["contract"]["constraints"].append("IMMUTABLE " * 20000)
    with pytest.raises(TaskContextOverflow):
        decide_next_action(
            SimpleNamespace(chat_json=lambda *a, **k: pytest.fail("must not infer")),
            ctx,
            valid_skill_names={"browser__click"},
            context_window=4096,
        )
    assert ctx.work["contract"]["constraints"][-1].endswith("IMMUTABLE ")


def test_current_page_keeps_late_action_ref_instead_of_600_character_history_prefix():
    page = "Page: orchard\n" + "\n".join(
        f'[button] "Orchard action {i}" ref=e{i}' for i in range(1, 41)
    )
    ctx = context(current_observation={"status": "current", "source_task_id": 7, "content": page})
    messages, _, _ = pack_planner(ctx, TaskBudget.resolve(8192, routine=True))
    assert "ref=e40" in messages[1]["content"]
    assert "ref=e1" in messages[1]["content"]


def test_large_current_state_selects_goal_relevant_whole_lines_and_marks_partial():
    page = "Page: orchard\n" + "\n".join(
        f'[button] "Unrelated entry {i} details details" ref=e{i}' for i in range(1000)
    )
    page += '\n[button] "Find the orchard settings" ref=orchard-target'
    ctx = context(current_observation={"status": "current", "source_task_id": 7, "content": page})
    messages, _, report = pack_planner(ctx, TaskBudget.resolve(4096, routine=True))
    assert "ref=orchard-target" in messages[1]["content"]
    assert "Partial excerpt" in messages[1]["content"]
    assert report["estimated_input_tokens"] <= report["input_limit"]


def test_review_bounds_every_evidence_record_and_keeps_full_contract():
    budget = TaskBudget.resolve(8192, routine=True)
    frame = {"contract": context().work["contract"], "findings": "The orchard setting is enabled."}
    evidence = [
        {"task_id": n, "result": "long result " * 20000, "partial": False} for n in range(12)
    ]
    messages, report = pack_review(
        "Review only supplied evidence; do not infer missing facts.", frame, evidence, budget
    )
    packed = json.loads(messages[1]["content"])
    assert packed["contract"] == frame["contract"]
    assert message_tokens(messages) <= budget.input_limit
    assert all(row["partial"] for row in packed["evidence"])
    assert set(report["evidence_ids"]).issubset(set(range(12)))


def test_huge_page_does_not_expand_optional_history_to_fill_the_whole_model():
    ctx = context(
        notes="old notes " * 50000,
        current_observation={
            "content": "irrelevant line\n" * 10000,
            "source_task_id": 7,
            "status": "current",
        },
    )
    messages, _, report = pack_planner(ctx, TaskBudget.resolve(65536, routine=True))
    assert report["input_target"] <= 12288
    assert message_tokens(messages) <= 12288


def test_context_limit_never_silently_turns_an_oversized_schema_into_missing_capability():
    oversized = skill()
    oversized["input_schema"]["description"] = "Necessary contract " * 5000
    ctx = context()
    object.__setattr__(ctx, "skills", [oversized])
    with pytest.raises(TaskContextOverflow):
        pack_planner(ctx, TaskBudget.resolve(4096, routine=True))


def test_unknown_model_estimates_are_conservative_for_unicode_and_full_goal_survives():
    from app.core.tasks.workflow.context_budget import tokens

    text = "原始目标与限制条件" * 30
    assert tokens(text) >= len(text) * 2
    ctx = context()
    ctx.work["contract"]["constraints"].append(text)
    messages, _, report = pack_planner(ctx, TaskBudget.resolve(8192, routine=True))
    assert text in messages[1]["content"]
    assert report["estimated_input_tokens"] <= report["input_limit"]


def test_repair_deferred_by_higher_priority_still_charges_the_completed_attempt():
    calls = []

    def answer(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise LlmCallDeferred()
        return "invalid", SimpleNamespace(total_tokens=37)

    scope = CallScope()
    with pytest.raises(LlmCallDeferred), call_scope(scope):
        decide_next_action(
            SimpleNamespace(chat_json=answer),
            context(),
            valid_skill_names={"browser__click"},
            context_window=8192,
        )
    assert scope.reported_tokens == 37


def test_expired_or_cancelled_task_cannot_start_a_provider_thinking_retry(monkeypatch):
    import time
    from app.llm.ollama_client import OllamaClient

    client = object.__new__(OllamaClient)
    monkeypatch.setattr(
        "app.llm.ollama_client.requests.post", lambda *a, **k: pytest.fail("expired")
    )
    with call_scope(CallScope(request_deadline=time.monotonic() - 1)):
        assert (
            client._retry_body_without_think(
                {}, timeout=30, model="test", surface="workflow_planner", label="test"
            )
            is None
        )


def test_owned_result_inventory_reopens_ids_omitted_from_history():
    from app.core.tasks.workflow.harness_results import inspect_result

    rows = [SimpleNamespace(id=n, title=f"Read {n}", status="done") for n in range(18)]
    parent = SimpleNamespace(list_children=lambda owner: rows if owner == 4 else [])
    first = inspect_result(parent, 4, {"list": True})
    second = inspect_result(parent, 4, {"list": True, "offset": first["next_offset"]})
    assert [row["task_id"] for row in first["results"] + second["results"]] == list(range(18))
    assert inspect_result(parent, 5, {"list": True})["results"] == []
    assert inspect_result(parent, 4, {"list": True, "offset": -1}).get("error")


def test_reported_token_counts_adjust_the_next_task_estimate_without_changing_num_ctx():
    calls = []

    def answer(messages, **kwargs):
        calls.append(kwargs)
        return '{"action":"browser__click","args":{"ref":"e40"}}', SimpleNamespace(
            total_tokens=200, prompt_tokens=message_tokens(messages) * 2
        )

    decision = decide_next_action(
        SimpleNamespace(chat_json=answer),
        context(),
        valid_skill_names={"browser__click"},
        context_window=65536,
    )
    assert decision.budget["next_estimate_scale"] >= 2
    assert calls[0]["think"] is False
    assert calls[0]["options"]["num_predict"] <= 384
    assert "num_ctx" not in calls[0]["options"]


def test_parse_repair_is_once_inference_only_and_keeps_goal():
    calls = []

    def answer(messages, **kwargs):
        calls.append((messages, kwargs))
        raw = "not json" if len(calls) == 1 else '{"action":"browser__click","args":{"ref":"e2"}}'
        return raw, SimpleNamespace(total_tokens=7)

    result = decide_next_action(
        SimpleNamespace(chat_json=answer),
        context(),
        valid_skill_names={"browser__click"},
        context_window=8192,
    )
    assert result.skill == "browser__click"
    assert result.usage_tokens == 14
    assert len(calls) == 2
    assert context().goal in calls[-1][0][1]["content"]
    assert "No operation was executed" in calls[-1][0][-1]["content"]


def test_invalid_response_does_not_loop_indefinitely():
    calls = []

    def answer(*a, **k):
        calls.append(True)
        return "invalid", None

    result = decide_next_action(
        SimpleNamespace(chat_json=answer),
        context(),
        valid_skill_names={"browser__click"},
        context_window=8192,
    )
    assert result.outcome == "partial"
    assert len(calls) == 2


def test_task_thinking_headroom_is_local_to_call_scope():
    from app.llm.ollama_client import OllamaClient

    client = object.__new__(OllamaClient)
    client._think_headroom = 8192
    with call_scope(CallScope(reasoning_tokens=128)):
        task_options = {"num_predict": 400}
        client._apply_think_headroom(task_options, True, surface="workflow_planner")
    cognitive_options = {"num_predict": 400}
    client._apply_think_headroom(cognitive_options, True, surface="memory")
    assert task_options["num_predict"] == 528
    assert cognitive_options["num_predict"] == 8592


@pytest.mark.parametrize("content", ["button ref=e40", "x" * 100001], ids=["small", "truncated"])
def test_plugin_projection_reaches_owned_result_through_generic_sdk_middleware(content):
    from app.core.tasks.handlers.mcp_tool import McpToolHandler
    from app.plugins.sdk import MiddlewareResult

    projection = {"kind": "current_page", "resource": "tab", "content": content}
    middleware = SimpleNamespace(
        claims=lambda *a: True,
        transform=lambda *a: MiddlewareResult(
            content="button ref=e40", summary="Current page", planner_context=projection
        ),
    )
    handler = McpToolHandler(
        manager=SimpleNamespace(
            call_tool=lambda *a, **k: SimpleNamespace(
                content=[SimpleNamespace(text="raw")], isError=False
            )
        ),
        middlewares=[middleware],
    )
    events = []
    handler.start({"server_id": "plugin", "tool_name": "observe", "tool_args": {}}, events.append)
    stored = events[-1].result["planner_context"]
    assert stored["content"] == content[:100000]
    assert stored["kind"] == projection["kind"]
    assert stored.get("partial", False) == (len(content) > 100000)
    assert projection["content"] == content


def test_measured_estimate_can_relax_slowly_after_context_mix_changes():
    def answer(messages, **kwargs):
        return '{"action":"finish","outcome":"partial"}', SimpleNamespace(
            total_tokens=100, prompt_tokens=message_tokens(messages)
        )

    result = decide_next_action(
        SimpleNamespace(chat_json=answer),
        context(),
        valid_skill_names={"browser__click"},
        context_window=8192,
        estimate_scale=2,
    )
    assert 1.9 < result.budget["next_estimate_scale"] < 2


def test_cancelled_queue_wait_removes_waiter_and_never_calls_provider():
    gate, stop, queued = LlmPriorityGate(), threading.Event(), threading.Event()
    gate.acquire(CONVERSATION_WORKER)
    calls, errors = [], []
    client = GatedChatClient(
        SimpleNamespace(chat_json=lambda *a, **k: calls.append(True)), gate, TASK
    )
    scope = CallScope(cancelled=stop.is_set, on_wait=queued.set)

    def run():
        try:
            with call_scope(scope):
                client.chat_json([])
        except LlmCallCancelled:
            errors.append("cancelled")

    thread = threading.Thread(target=run)
    thread.start()
    assert queued.wait(2)
    stop.set()
    thread.join(2)
    gate.release(CONVERSATION_WORKER)
    assert errors == ["cancelled"]
    assert calls == []
    assert gate.stats()["queued"] == 0
    assert scope.queue_seconds > 0
    assert scope.active_seconds < scope.elapsed_seconds


def test_expired_queue_wait_defers_and_does_not_poison_gate():
    gate = LlmPriorityGate()
    gate.acquire(CONVERSATION_WORKER)
    client = GatedChatClient(
        SimpleNamespace(chat_json=lambda *a, **k: pytest.fail("no slot")), gate, TASK
    )
    with pytest.raises(LlmCallDeferred), call_scope(CallScope(queue_timeout=0)):
        client.chat_json([])
    assert gate.stats()["queued"] == 0
    gate.release(CONVERSATION_WORKER)
    assert gate.acquire(TASK, timeout=0) is not None
    gate.release(TASK)


def test_failed_cancellation_callback_also_removes_waiter():
    gate = LlmPriorityGate()
    gate.acquire(CONVERSATION_WORKER)

    def fail():
        raise RuntimeError("owner unavailable")

    with pytest.raises(RuntimeError):
        gate.acquire(TASK, cancelled=fail)
    assert gate.stats()["queued"] == 0
    gate.release(CONVERSATION_WORKER)


def test_retarget_during_inference_releases_captured_gate_and_preserves_client():
    old_gate, new_gate = LlmPriorityGate(), LlmPriorityGate()
    entered, release = threading.Event(), threading.Event()

    def answer(*a, **k):
        entered.set()
        assert release.wait(2)
        return "old"

    client = GatedChatClient(SimpleNamespace(chat_json=answer), old_gate, TASK)
    results = []
    thread = threading.Thread(target=lambda: results.append(client.chat_json([])))
    thread.start()
    assert entered.wait(2)
    client.retarget(SimpleNamespace(chat_json=lambda *a, **k: "new"), new_gate)
    release.set()
    thread.join(2)
    assert results == ["old"]
    assert old_gate.stats()["inflight"] == new_gate.stats()["inflight"] == 0
    assert client.chat_json([]) == "new"


def test_same_resource_reconfiguration_keeps_holders_and_waiters_in_one_gate():
    from app.core.session.llm_clients_mixin import LlmClientsMixin

    class Host(LlmClientsMixin):
        def _find_llm_provider(self, provider_id):
            return SimpleNamespace(kind="ollama", base_url="http://localhost:11434")

        def _build_workflow_client(self, gate, task_priority):
            return GatedChatClient(self._worker_client_inner, gate, task_priority)

        def _install_live_policy_client(self):
            pass

    host = Host()
    host._settings = SimpleNamespace(
        agent=SimpleNamespace(worker_llm_max_concurrency=1),
        llm=SimpleNamespace(
            routes={
                "worker_default": SimpleNamespace(
                    provider_id="local", model="model", contention_group=""
                )
            }
        ),
    )
    host._install_worker_clients(SimpleNamespace())
    original = host._worker_llm_gate
    original.acquire(TASK)
    host._settings.agent.worker_llm_max_concurrency = 2
    host._install_worker_clients(SimpleNamespace())
    assert host._worker_llm_gate is original
    assert original.stats()["inflight"] == 1
    assert original.stats()["max_concurrency"] == 2
    original.release(TASK)


def test_divergent_workflow_context_matches_actual_local_transport_and_keeps_resource_gate():
    from app.core.infra.settings import LlmProvider, LlmRoute, transport_for_provider
    from app.core.session.llm_clients_mixin import LlmClientsMixin
    from app.llm.ollama_client import OllamaClient

    provider = LlmProvider(
        id="local", name="Local", kind="ollama", base_url="http://localhost:11434"
    )
    worker = LlmRoute(provider_id="local", model="same", context_window=8192)
    workflow = LlmRoute(provider_id="local", model="same", context_window=4096)

    class Host(LlmClientsMixin):
        def _find_llm_provider(self, provider_id):
            return provider

    host = Host()
    host._settings = SimpleNamespace(
        llm=SimpleNamespace(
            providers=[provider], routes={"worker_default": worker, "workflow": workflow}
        )
    )
    host._worker_client_inner = OllamaClient(transport_for_provider(provider, route=worker))
    gate = LlmPriorityGate()
    host._workflow_client = host._build_workflow_client(gate, TASK)
    assert host._workflow_client._gate is gate
    assert host._workflow_context_window() == 4096
    assert host._worker_client_inner.request_context_window == 8192


def test_unknown_local_context_does_not_claim_the_declared_workflow_window():
    from app.core.infra.settings import OllamaSettings
    from app.core.session.llm_clients_mixin import LlmClientsMixin
    from app.llm.ollama_client import OllamaClient

    host = LlmClientsMixin()
    host._workflow_client = GatedChatClient(
        OllamaClient(OllamaSettings(base_url="http://localhost:11434", context_window=None)),
        None,
        TASK,
    )
    assert host._workflow_context_window() is None
