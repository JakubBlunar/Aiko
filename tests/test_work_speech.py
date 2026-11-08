"""Local voice learning and task reports never need a cloud reporting turn."""

import json
from types import SimpleNamespace

import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.persona.speech_guide import SpeechGuide
from app.core.tasks.task_store import TaskStore
from app.core.tasks.task_events import TaskEventStore
from app.core.tasks.task_orchestrator import TaskOrchestrator
from app.core.tasks.work_reports import WorkReports
from app.llm.turn_outcome import TurnOutcome


@pytest.fixture
def env(tmp_path):
    db = ChatDatabase(tmp_path / "speech.db")
    store = TaskStore(db)
    orch = TaskOrchestrator(store, event_store=TaskEventStore(db), heartbeat_enabled=False)
    yield db, store, orch
    orch.shutdown(wait=False)
    db._get_conn().close()


@pytest.mark.parametrize("invalidation", ["delete", "edit", "owner"])
def test_guide_uses_completed_teacher_replies_not_its_own_reports(env, invalidation):
    db, _, _ = env
    teacher = []
    for i in range(26):
        question = db.add_message("u:s", "user", f"Explain switch {i}")
        mid = db.add_message(
            "u:s",
            "assistant",
            f"Switch {i} controls the garden lighting.",
            outcome=TurnOutcome(provider_id="brain", status="completed", user_message_id=question),
        )
        teacher.append(mid)
    db.add_message(
        "u:s", "assistant", "LOCAL REPORT SHOULD NEVER TEACH THE GUIDE", dialogue_act="work_report"
    )
    db.add_message("u:s", "assistant", "LEGACY UNKNOWN SHOULD NOT BE INFERRED")
    db.add_message("other:s", "assistant", "OTHER OWNER IS EXCLUDED")
    calls, persona = [], ["You are Aiko.\n\nHow you talk:\n- Natural, specific sentences."]
    owner, session = ["u"], ["u:s"]

    def learn(messages, **kwargs):
        calls.append(kwargs)
        frame = json.loads(messages[1]["content"])
        assert "LOCAL REPORT" not in str(frame) and "LEGACY UNKNOWN" not in str(frame)
        ids = [row["task_id"] for row in frame["evidence"][:3]]
        if invalidation == "owner":
            owner[0], session[0] = "other", "other:s"
        return json.dumps(
            {
                "modes": [
                    {
                        "when": "explaining controls",
                        "guidance": "Name the concrete effect first.",
                        "source_ids": ids,
                    }
                ],
                "examples": [],
            }
        ), None

    guide = SpeechGuide(
        db,
        user=lambda: owner[0],
        session=lambda: session[0],
        persona=lambda: persona[0],
        client=lambda: SimpleNamespace(chat_json=learn),
        model=lambda: "small",
        window=lambda: 8192,
    )
    result = guide.run()
    if invalidation == "owner":
        assert result["skipped"]
        assert db._get_conn().execute("SELECT COUNT(*) FROM work_speech_guides").fetchone()[0] == 0
        return
    assert result["learned"]
    context = guide.context()
    assert context["learned_delivery"]["modes"][0]["when"] == "explaining controls"
    from unittest.mock import patch

    with patch.object(guide, "_samples", side_effect=AssertionError("Unneeded transcript scan")):
        assert guide.run()["skipped"] and len(calls) == 1
    assert not calls[0]["think"] and calls[0]["surface"] == "work_speech_guide"
    saved = guide._stored()
    if invalidation == "delete":
        db.delete_message_rows("u:s", [saved["sources"][0]["id"]])
    else:
        db.update_message_content(saved["sources"][0]["id"], "Edited source reply.")
    assert guide._stored() is None
    assert guide.context()["learned_delivery"] is None
    persona[0] = "You are Aiko, speaking more directly."
    assert guide.context()["revision"] != context["revision"]


def make_reports(env, *, fail=False):
    db, store, orch = env
    tid = store.create(
        user_id="u",
        handler_name="goal_workflow",
        title="Orchard",
        metadata={"source_session": "u:s"},
        state={
            "harness_version": 1,
            "local_reporting": True,
            "epoch": 1,
            "contract": {"objective": "Find the orchard colour"},
        },
    )
    store.mark_done(
        tid,
        result={
            "outcome": "success",
            "content": "The orchard is blue.",
            "verification": {"status": "supported"},
        },
    )
    calls, queued, shown, session = [], [], [], ["u:s"]

    def local(messages, **kwargs):
        calls.append(kwargs)
        assert "orchard" in messages[1]["content"]
        if fail:
            raise RuntimeError("local model unavailable")
        return json.dumps({"text": "[[reaction:cheerful]]\nThe orchard is blue. Found it."}), None

    reports = WorkReports(
        db,
        orch,
        None,
        user=lambda: "u",
        session=lambda: session[0],
        client=lambda: SimpleNamespace(chat_json=local),
        model=lambda: "small",
        window=lambda: 8192,
        context=lambda: {"revision": "one", "persona": "Specific, warm speech."},
        enqueue=lambda *args: queued.append(args),
        present=lambda *args: shown.append(args),
    )
    return reports, tid, calls, queued, shown, session


def test_report_commits_once_without_cloud_and_survives_restart(env):
    reports, tid, calls, queued, shown, _ = make_reports(env)
    reports.tick()
    assert len(calls) == len(queued) == 1
    session, rid = queued[0]
    assert reports.deliver(session, rid)
    assert not reports.deliver(session, rid)
    db, _, _ = env
    messages = db.get_messages(session)
    assert len(messages) == 1 and "[[" not in messages[0].content
    assert shown[0][2] == messages[0].id and shown[0][3] == "cheerful"
    assert db.get_turn_outcomes(session) == []  # cannot become teacher evidence
    reports._notified.clear()
    reports.tick()
    assert len(calls) == 1 and len(shown) == 1
    db.delete_message_rows(session, [messages[0].id])
    reports.tick()
    assert db.get_messages(session) == []  # deletion is not permission to replay


def test_reporting_failure_has_no_cloud_fallback_or_duplicate_cost(env):
    reports, _, calls, queued, shown, _ = make_reports(env, fail=True)
    reports.tick()
    reports.tick()
    assert len(calls) == 1 and not queued and not shown
    for _ in range(5):
        reports._retry.clear()
        reports.tick()
    assert len(calls) == 3  # persisted cap survives service reconstruction too


def test_route_changed_while_waiting_cannot_dispatch_old_sized_prompt():
    from app.llm.call_scope import bind_route, CallScope, call_scope, LlmCallDeferred
    from app.llm.llm_gate import GatedChatClient, LlmPriorityGate, TASK

    calls, window = [], [8192]
    client = GatedChatClient(
        SimpleNamespace(chat_json=lambda *a, **k: calls.append(1)), LlmPriorityGate(), TASK
    )
    route = bind_route(lambda: client, lambda: "small", lambda: window[0])

    def change():
        window[0] = 4096

    with (
        pytest.raises(LlmCallDeferred),
        call_scope(
            CallScope(
                on_wait=change,
                on_acquired=route.acquired,
            )
        ),
    ):
        route.client.chat_json([])
    assert calls == []


def test_new_guidance_or_session_switch_blocks_stale_report(env):
    reports, tid, calls, queued, shown, session = make_reports(env)
    reports.tick()
    source, rid = queued[0]
    session[0] = "u:other"
    assert not reports.deliver(source, rid)
    session[0] = source
    env[2].append_work_message(tid, role="user", content="Check another source", kind="guidance")
    assert not reports.deliver(source, rid)
    assert not shown


def test_brain_result_handler_leaves_local_reporting_to_outbox(env):
    from app.core.brain.events import TaskResultEvent
    from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin

    reports, tid, _, _, _, _ = make_reports(env)

    class Host(TaskOrchestrationMixin):
        _work_reports = reports
        _task_orchestrator = env[2]

        def _consume_task_inline_resolved(self, task_id):
            return False

        def _dispatch_task_report(self, *args):
            pytest.fail("Cloud-oriented C6 reporting must not be invoked")

    Host()._on_task_result_event(TaskResultEvent(task_id=f"{tid:08x}", status="done"))


def test_already_historical_jobs_are_not_reannounced_on_upgrade(env):
    reports, tid, calls, queued, _, _ = make_reports(env)
    state = env[1].get(tid).state
    state.pop("local_reporting")
    env[1].update_state(tid, state)
    reports.tick()
    assert not calls and not queued


def test_large_editable_persona_has_a_bounded_speaking_projection(env):
    from app.core.tasks.workflow.context_budget import tokens

    guide = SpeechGuide(
        env[0],
        user=lambda: "u",
        session=lambda: "u:s",
        persona=lambda: "Aiko's voice: " + "specific natural phrasing " * 1000,
        client=lambda: None,
        model=lambda: "small",
        window=lambda: 4096,
    )
    context = guide.context(max_tokens=256)
    assert context["persona_partial"]
    assert tokens(context["persona"]) <= 256


def test_direct_presentation_uses_existing_delivery_identity():
    from app.core.conversation.delivery import DeliveryLedger, current_delivery_id
    from app.core.session.work_reporting_mixin import WorkReportingMixin

    ledger = DeliveryLedger(lambda: ("u:s", 1))
    host, seen = WorkReportingMixin(), []
    host.delivery_ledger = lambda: ledger

    def present(session, text, mid, reaction):
        seen.append((current_delivery_id.get(), ledger.offer_text(mid, "web")))

    host._proactive = SimpleNamespace(present_work_report=present)
    before = current_delivery_id.get()
    host._present_work_report("u:s", "Read the source.", 7, "calm")
    assert seen[0][0] and seen[0][0] == seen[0][1]
    assert current_delivery_id.get() == before
