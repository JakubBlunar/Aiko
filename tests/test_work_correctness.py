"""Host proof and outcome contracts must hold even when a model always approves."""
import json
from types import SimpleNamespace as NS

import pytest

from app.core.tasks.work_evidence import (
    certificate_current, check_postconditions, evidence_record, owned_records,
)
from app.core.tasks.work_outcome import task_outcome
from app.core.tasks.workflow.harness_results import verify_completion
from app.llm.call_scope import current_call_scope
from work_review_helpers import certificate, supported_review


def row(cid=2, *, effect="read", content="Job J17 is completed.", status="done", **result):
    return NS(id=cid, parent_task_id=1, status=status, created_at=f"2026-11-15T10:00:{cid:02d}Z",
              completed_at=f"2026-11-15T10:00:{cid:02d}Z",
              error=None, args={"_config_generation": "one"},
              result={"content": content, "receipt": {"effect": effect}, **result})


def run_review(rows, change=None, *, partial=False):
    calls = []
    steps = [{"child_id": item.id, "status": item.status} for item in rows]
    state = {"steps": steps, "contract": {"objective": "Check job J17",
                                          "criteria": ["Return its actual status."]}}
    orch = NS(get=lambda cid: next((item for item in rows if item.id == cid), None),
              list_children=lambda _: rows)

    def chat(messages, **kwargs):
        calls.append((messages, kwargs))
        assert kwargs["think"] is True
        assert current_call_scope().reasoning_required
        assert current_call_scope().reasoning_tokens == 8192
        answer = json.loads(supported_review(messages))
        usage = NS(done_reason="stop")
        if change:
            change(answer, usage)
        return json.dumps(answer), usage

    check, _ = verify_completion(NS(chat_json=chat), "local", state, orch, 1,
                                 "Job J17 is completed.", [2], 10,
                                 context_window=65536, partial=partial)
    return check, calls


@pytest.mark.parametrize("mutation", [
    lambda a, u: setattr(u, "done_reason", "length"),
    lambda a, u: a.update(missing=["No proof"]),
    lambda a, u: a.update(criteria=[]),
    lambda a, u: a.update(criteria=[{"index": 0, "supported": "true"}]),
    lambda a, u: a.update(claims=[]),
    lambda a, u: a["claims"][0].update(text="Only the supported portion"),
    lambda a, u: a["claims"][0].update(task_id=999),
    lambda a, u: a["claims"][0].update(quote="Invented source quotation"),
])
def test_approving_boolean_cannot_waive_review_contract(mutation):
    check, calls = run_review([row()], mutation)
    assert calls and check["status"] == "unsupported"
    assert "certificate" not in check


def test_valid_read_certificate_binds_answer_and_all_source_revisions():
    rows = [row(), row(3, status="failed", content="", error_note="Another read failed")]
    check, calls = run_review(rows)
    assert check["status"] == "supported"
    assert set(check["certificate"]["sources"]) == {"2", "3"}
    sources = owned_records(NS(list_children=lambda _: rows), 1, [])
    assert certificate_current(check["certificate"], sources)
    rows[0].result["content"] = "Job J17 is queued."
    changed = owned_records(NS(list_children=lambda _: rows), 1, [])
    assert not certificate_current(check["certificate"], changed)
    frame = json.loads(calls[0][0][1]["content"])
    assert frame["outcome_index"][1]["status"] == "failed"


def test_write_acknowledgement_cannot_reach_model_approval():
    check, calls = run_review([row(effect="write", content="success")])
    assert check["status"] == "unsupported" and not calls


def mutation_and_observation():
    write = row(effect="write", content="queued", postcondition={
        "resource": "notes:shopping", "expected": {"checked": True},
    })
    read = row(3, postcondition={"resource": "notes:shopping", "observed": {"checked": True}})
    return write, read


@pytest.mark.parametrize("failure", ["resource", "generation", "earlier", "value", "partial"])
def test_postcondition_requires_same_resource_generation_and_later_matching_observation(failure):
    write, read = mutation_and_observation()
    if failure == "resource":
        read.result["postcondition"]["resource"] = "notes:someone-else"
    elif failure == "generation":
        read.args["_config_generation"] = "two"
    elif failure == "earlier":
        read.completed_at = "2026-11-15T09:00:00Z"
    elif failure == "value":
        read.result["postcondition"]["observed"]["checked"] = 1  # bool is not an integer
    else:
        read.result["source_artifact"] = {"source_partial": True}
    check, calls = run_review([write, read])
    assert check["status"] == "unsupported" and not calls


def test_latest_contradictory_observation_cannot_be_hidden_by_not_citing_it():
    write, read = mutation_and_observation()
    assert check_postconditions([evidence_record(write), evidence_record(read)]) == []
    newer = row(4, postcondition={"resource": "notes:shopping", "observed": {"checked": False}})
    check, calls = run_review([write, read, newer])
    assert check["status"] == "unsupported" and not calls


def test_envelope_keeps_receipt_partiality_and_resource():
    item = row(source_artifact={"source_partial": True})
    record = evidence_record(item)
    assert record["receipt"] == {"effect": "read"}
    assert record["partial"] is True
    assert record["revision"]


@pytest.mark.parametrize("outcome", ["blocked", "partial", "uncertain", "submitted",
                                     "missing_capability"])
def test_execution_done_does_not_promote_objective_outcome(outcome):
    item = row()
    item.handler_name = "goal_workflow"
    item.result.update(outcome=outcome)
    observed = task_outcome(item)
    assert observed["state"] == outcome and not observed["verified"]
    assert observed["label"] != "Completed"


def test_old_supported_boolean_is_unverified_and_changed_answer_invalidates_certificate():
    item = row()
    item.result.update(outcome="success", verification={"status": "supported"})
    assert task_outcome(item)["state"] == "unverified"
    item.result["verification"] = certificate(item.result["content"], [evidence_record(row())])
    assert task_outcome(item)["verified"]
    item.result["content"] = "A new unsupported answer."
    assert not task_outcome(item)["verified"]


def test_partial_fact_review_does_not_certify_whole_objective():
    check, _ = run_review([row()], lambda a, u: a["criteria"][0].update(supported=False),
                          partial=True)
    assert check["status"] == "supported"
    item = row()
    item.result.update(outcome="partial", verification=check)
    assert task_outcome(item)["state"] == "partial"
    assert not task_outcome(item)["verified"]


def test_a_read_started_before_the_mutation_cannot_verify_it_even_if_it_finishes_later():
    write, read = mutation_and_observation()
    read.created_at = "2026-11-15T09:59:00Z"
    assert check_postconditions([evidence_record(write), evidence_record(read)])


@pytest.mark.parametrize("verified", [False, True])
def test_task_promise_resolution_requires_verified_outcome_and_never_uses_title(verified):
    from app.core.brain.events import TaskResultEvent
    from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin

    seen = []

    class Host(TaskOrchestrationMixin):
        def _maybe_resolve_promises(self, text, *, source):
            seen.append(text)

        def _consume_task_inline_resolved(self, task_id):
            return True

    Host()._on_task_result_event(TaskResultEvent(
        title="Unproven objective in the title", result_summary="Only the proved result",
        objective_outcome={"verified": verified},
    ))
    assert seen == (["Only the proved result"] if verified else [])


@pytest.mark.parametrize("first_ids", [None, [999]])
def test_missing_finish_ids_are_repaired_once_without_repeating_operations(first_ids):
    from app.core.tasks.workflow.workflow_planner import (
        PlannerInput, PlannerStep, decide_next_action,
    )

    calls = []

    def client(messages, **kwargs):
        calls.append((messages, kwargs))
        return json.dumps({"action": "finish", "outcome": "success", "findings": "Read result.",
                           **({"evidence_ids": [17]} if len(calls) == 2 else
                              ({"evidence_ids": first_ids} if first_ids else {}))}), None

    ctx = PlannerInput(goal="Read source", skills=[],
                       steps=[PlannerStep("read", status="done", child_id=17)],
                       work={"profile": "reader"})
    decision = decide_next_action(NS(chat_json=client), ctx, valid_skill_names=set(),
                                  context_window=65536)
    assert len(calls) == 2 and decision.evidence_ids == [17]
    assert "[17]" in calls[1][0][-1]["content"]
    assert all(kwargs["think"] is True for _, kwargs in calls)


def test_reasoning_required_scope_never_silently_retries_without_thinking(monkeypatch):
    from app.core.infra.settings import OllamaSettings
    from app.llm.call_scope import CallScope, call_scope
    from app.llm.ollama_client import OllamaClient

    client = OllamaClient(OllamaSettings(base_url="http://unused.test"))
    monkeypatch.setattr("app.llm.ollama_client.requests.post",
                        lambda *a, **k: pytest.fail("must not dispatch an unreasoned fallback"))
    with call_scope(CallScope(reasoning_required=True)):
        assert client._retry_body_without_think({}, timeout=1, model="local",
                                                 surface="workflow_verify", label="audit") is None


def test_requested_resource_mismatch_is_rejected_before_model_even_for_partial_answers():
    item = row(postcondition={"resource": "notes:b", "requested_resource": "notes:a",
                              "observed": {"content": "hello"}})
    for partial in (False, True):
        check, calls = run_review([item], partial=partial)
        assert check["status"] != "supported"
        # Full completion is rejected without inference. Partial claim admission
        # also rejects the resource even when the scripted model approves.
        if not partial:
            assert not calls


@pytest.mark.parametrize("handler", ["web_search", "fast_tool"])
def test_empty_search_is_not_factual_evidence_even_when_model_would_approve(handler):
    item = row(content=json.dumps({"results": []}))
    item.handler_name = handler
    item.args["tool_name"] = "web_search"
    if handler == "web_search":
        item.result["results"] = []
    check, calls = run_review([item])
    assert check["status"] == "unsupported" and not calls


@pytest.mark.parametrize("part,expected", [(0, "supported"), (1, "unsupported"),
                                          (True, "unsupported")])
def test_indexed_claims_cover_original_answer_without_copying_or_rewriting_it(part, expected):
    def change(answer, usage):
        answer["claims"] = [{"part": part, "task_id": 2, "quote": "Job J17 is completed."}]

    check, _ = run_review([row()], change)
    assert check["status"] == expected


def test_speaking_facts_do_not_duplicate_large_child_sources_or_certificates():
    from app.core.tasks.work_report_grounding import speech_facts

    facts = speech_facts({"kind": "progress", "evidence": [{"result": "x" * 1000000}],
                          "finding": "Useful result", "result": {"verification": {
                              "certificate": {"claims": ["y" * 1000000]}, "status": "supported",
                          }}})
    assert len(json.dumps(facts)) < 1000
    assert facts["finding_claims"] == "Useful result"


def test_model_excerpt_truncation_does_not_erase_an_exact_host_postcondition():
    write, read = mutation_and_observation()
    records = [evidence_record(write, limit=1), evidence_record(read, limit=1)]
    assert all(record["partial"] for record in records)
    assert not any(record["source_partial"] for record in records)
    assert check_postconditions(records) == []


def test_result_parent_link_cannot_waive_user_ownership():
    child = row()
    child.user_id = "other"
    orch = NS(get=lambda cid: NS(user_id="owner") if cid == 1 else child,
              list_children=lambda _: [child])
    assert owned_records(orch, 1, [{"child_id": 2}]) == []


def test_pre_envelope_host_reads_keep_their_declared_effect_but_unknown_mcp_does_not():
    item = row()
    item.result.pop("receipt")
    item.handler_name = "fast_tool"
    assert evidence_record(item)["effect"] == "read"
    item.handler_name = "mcp_tool"
    assert evidence_record(item)["effect"] == "unknown"


@pytest.mark.parametrize("verification", [True, "verified", [], {"certificate": "ok"},
                                          {"certificate": {"criteria": {}}}])
def test_arbitrary_legacy_result_fields_cannot_crash_or_certify_task_reads(verification):
    item = row()
    item.result.update(outcome="success", verification=verification)
    assert task_outcome(item)["state"] == "unverified"


def test_lean_work_list_rows_without_result_remain_readable_and_unverified():
    item = NS(status="done", handler_name="goal_workflow")
    assert task_outcome(item)["state"] == "unverified"
    item.status = "running"
    assert task_outcome(item)["state"] == "running"
