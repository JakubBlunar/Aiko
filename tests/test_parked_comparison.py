from dataclasses import replace

import pytest

from app.core.conversation.conversation_situation import (
    ConversationSituationStore, working_set_message_ids,
)
from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
from app.core.conversation.diagnostic_capture import named_problem
from app.core.infra.chat_database import ChatDatabase
from scripts.diagnostic_return_replay import CASES, RESULT, RecordedObserver, seed


@pytest.fixture
def second_task(tmp_path):
    db = ChatDatabase(tmp_path / "tasks.db")
    host, mic_result = seed(db, "absent")
    store = host._conversation_situation_store
    observer = RecordedObserver()
    worker = ConversationSituationWorker(
        client=observer, chat_db=db, store=store, model="fixture", world_snapshot_provider=dict,
        judgment_shadow_enabled=False,
    )
    session = host.session_key
    remit = "Suggest a name for my blue ceramic bowl."
    bowl = db.add_message(session, "user", remit)
    db.add_message(session, "assistant", "Glacier Blue.")
    observer.payload = {"operation": "replace", "summary": "Naming a ceramic bowl.",
                        "evidence_message_ids": [bowl], "working_set": {
                            "question": {"text": remit, "evidence_message_ids": [bowl]}}}
    state = worker.run(session)
    assert state.working_set.question.evidence_message_ids == (bowl,)
    assert state.parked_comparison.working_set.check_result.evidence_message_ids == (mic_result,)
    assert bowl not in state.parked_comparison.evidence_message_ids
    host._conversation_situation_store = ConversationSituationStore(db)
    return db, host, worker, observer, bowl, mic_result


def test_correction_on_explicit_return_becomes_the_only_current_result(second_task):
    db, host, worker, observer, bowl, mic_result = second_task
    recap = CASES["absent"][1]
    assert RESULT["no"] in host.reviewed_comparison_reply(recap)
    assert "recording" not in host._render_conversation_situation_block("Try a shorter bowl name.")
    correction = db.add_message(host.session_key, "user", "Back to the desk microphone clicks. "
                                "Correction: I checked again. I hear clicks. Which cause fits now?")
    observer.payload = {"operation": "keep", "evidence_message_ids": [correction],
                        "diagnostic_verdict": "An input-side fault"}
    state = worker.run(host.session_key)
    assert state.working_set.decision_check.answer == "yes"
    assert state.working_set.check_result.evidence_message_ids == (correction,)
    assert bowl not in state.evidence_message_ids
    assert mic_result not in working_set_message_ids(state.working_set)
    host._conversation_situation_snapshot_cache = None
    reply = host.reviewed_comparison_reply(recap)
    assert "I hear clicks" in reply and "An input-side fault" in reply
    assert "bowl" not in reply and RESULT["no"] not in reply


@pytest.mark.parametrize("edit_foreground", [False, True])
def test_evidence_edits_only_withdraw_the_affected_problem(second_task, edit_foreground):
    db, host, _, _, bowl, mic_result = second_task
    assert db.update_message_content(bowl if edit_foreground else mic_result, "I take that back.")
    reply = host.reviewed_comparison_reply(CASES["absent"][1])
    assert (RESULT["no"] in reply) == edit_foreground
    state = host._conversation_situation_store.get(host.session_key)
    assert bool(state.working_set) != edit_foreground
    assert host._conversation_situation_store.get("another-session") is None


def test_ambiguous_name_expiry_and_clear(second_task):
    _, host, _, _, _, _ = second_task
    store = host._conversation_situation_store
    state = store.get(host.session_key)
    recap = CASES["absent"][1]
    # Two distinct user goal receipts name the same subject.
    overlapping = replace(state.working_set, question=replace(state.working_set.question,
                          text="What else can cause desk microphone clicks?"))
    assert named_problem(store, replace(state, working_set=overlapping), recap) is None
    expired = replace(state, parked_comparison=replace(state.parked_comparison,
                                                       updated_at="2000-01-01T00:00:00Z"))
    assert named_problem(store, expired, recap) is expired
    assert host.reviewed_comparison_reply("Back to the laptop microphone clicks. "
                                          "Which cause fits better?") == ""
    store.clear(host.session_key)
    host._conversation_situation_snapshot_cache = None
    assert host.reviewed_comparison_reply(recap) == ""


def test_named_return_between_observer_runs_retains_the_checked_problem(second_task):
    db, host, worker, observer, _, mic_result = second_task
    recap = CASES["absent"][1]
    db.add_message(host.session_key, "user", recap)
    db.add_message(host.session_key, "assistant", host.reviewed_comparison_reply(recap))
    follow_up = "Let's go back to the desk microphone clicks. What should I check next?"
    assert RESULT["no"] in host._render_conversation_situation_block(follow_up)
    turn = db.add_message(host.session_key, "user", follow_up)
    observer.payload = {"operation": "keep", "evidence_message_ids": [turn]}
    notes = worker.run(host.session_key).working_set
    assert notes.check_result.evidence_message_ids == (mic_result,)
    assert notes.question.text == "What causes my desk microphone clicks?"
