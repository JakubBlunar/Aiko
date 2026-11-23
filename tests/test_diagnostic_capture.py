import json
from types import SimpleNamespace

import pytest

from app.core.conversation.conversation_situation import parse_extraction
from app.core.conversation.diagnostic_capture import diagnostic_capture, proposed_test
from scripts.diagnostic_return_replay import EXAMPLES, RESULT, microphone_reply_fits


def rows_for(name):
    return [SimpleNamespace(id=i, role=role, content=text)
            for i, (role, text) in enumerate(EXAMPLES[name][2], 1)]


def test_proposal_excludes_a_report_in_the_same_turn():
    proposal = EXAMPLES["microphone"][2][-1][1]
    assert proposed_test(proposal).hypotheses == ["An input fault", "A call transport fault"]
    assert proposed_test(proposal + " " + RESULT["yes"]) is None
    assert proposed_test(RESULT["yes"] + " " + proposal) is None
    assert proposed_test(RESULT["yes"]) is None


@pytest.mark.parametrize(("reply", "reported", "expected"), [
    ("Since you've confirmed the clicks in the local recording, it's an input fault. "
     "Would you like to check the cable?", False, False),
    ("Are the clicks audible in the local recording?", False, True),
    ("The clicks are audible in the local recording. Should we check the cable?", False, False),
    ("If you hear clicks in that recording, an input fault fits better.", False, True),
    ("Given the clicks in the recording, an input fault fits better.", True, True),
    ("Given the clicks in the recording, a call fault fits better than an input fault.",
     True, False),
    ("Are the clicks audible in the local recording?", True, False),
])
def test_replay_distinguishes_predictions_from_reported_results(reply, reported, expected):
    assert microphone_reply_fits(reply, reported=reported) is expected


@pytest.mark.parametrize("name,verdict,yes,check_id", [
    ("microphone", "input fault", 0, 2), ("press", "A faulty roller", 1, 3),
])
def test_only_sources_supply_questions_and_hypotheses(name, verdict, yes, check_id):
    rows = rows_for(name)
    capture = diagnostic_capture(rows, None)
    raw = json.dumps({"operation": "keep", "summary": "Comparing possible faults.",
                      "diagnostic_verdict": verdict, "evidence_message_ids": [1, rows[-1].id]})
    extraction = parse_extraction(capture.bind(raw), valid_message_ids={r.id for r in rows},
                                  valid_user_message_ids={r.id for r in rows if r.role == "user"})
    notes = extraction.working_set
    assert extraction.operation == "replace"
    assert (notes.question.text, notes.next_check.text) == EXAMPLES[name][:2]
    assert notes.question.evidence_message_ids == (1,)
    assert notes.next_check.evidence_message_ids == (check_id,)
    assert notes.causal_check.expects_yes == yes and not notes.causal_check.result_applies
    assert notes.check_result is None and not notes.facts


def test_abstentions_unknown_labels_and_posthoc_explanations_do_not_create_a_record():
    rows = rows_for("microphone")
    capture = diagnostic_capture(rows, None)
    for verdict in ("neither", "a third fault", {}, None):
        raw = json.dumps({"operation": "replace", "diagnostic_verdict": verdict})
        assert capture.bind(raw) == raw
    rows[-1].content += " I already tested this and heard clicks."
    assert diagnostic_capture(rows, None) is None
    assert diagnostic_capture(rows_for("fan"), None) is None


def test_assistant_suggestion_keeps_hypothesis_separate_from_reported_fact():
    raw = json.dumps({"operation": "replace", "summary": "Considering the fan noise.",
                      "evidence_message_ids": [1, 2], "working_set": {
                          "question": {"text": "Why does my fan hum?", "evidence_message_ids": [1]},
                          "hypotheses": [{"text": "A loose grille", "evidence_message_ids": [2]}],
                          "facts": [{"text": "A loose grille causes it.", "evidence_message_ids":
                                     [2]}, {"text": "The fan hums.", "evidence_message_ids": [1]}],
                      }})
    notes = parse_extraction(raw, valid_message_ids={1, 2}, valid_user_message_ids={1}).working_set
    assert [f.text for f in notes.facts] == ["The fan hums."]
    assert [h.text for h in notes.hypotheses] == ["A loose grille"]


def test_recap_uses_corrected_user_result_after_worker_restart(tmp_path):
    from app.core.conversation.conversation_situation import ConversationSituationStore
    from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
    from app.core.infra.chat_database import ChatDatabase
    from scripts.diagnostic_return_replay import Host, RecordedObserver

    db = ChatDatabase(tmp_path / "ordinary.db")
    observer = RecordedObserver()
    session = Host.session_key
    def observe(verdict, *ids, **extra):
        observer.payload = {"operation": "replace", "summary": "Comparing causes.",
                            "evidence_message_ids": list(ids), "diagnostic_verdict": verdict,
                            **extra}
        store = ConversationSituationStore(db)
        worker = ConversationSituationWorker(client=observer, chat_db=db, store=store,
                                             model="fixture", world_snapshot_provider=dict,
                                             judgment_shadow_enabled=False)
        worker.run(session)
        return store.get(session).working_set

    ids = [db.add_message(session, role, text) for role, text in EXAMPLES["press"][2]]
    assert observe("faulty roller", *ids).decision_check.answer is None
    report = db.add_message(session, "user", "I tested the fresh plate. It leaves streaks.")
    notes = observe("faulty roller", report)
    assert notes.decision_check.answer == "yes" and notes.causal_check.result_applies
    detour = db.add_message(session, "user", "New topic. I'm glazing a bowl.")
    assert observe(None, detour, working_set={"facts": "invalid"}).suspended
    recap = db.add_message(session, "user", "Back to the press prints. Which cause fits better?")
    assert observe(None, recap).check_result.evidence_message_ids == (report,)
    correction = db.add_message(session, "user", "Back to the press prints. Correction: the "
                                "fresh plate prints clean. Which cause fits now?")
    notes = observe("damaged plate", correction)
    assert notes.check_result.evidence_message_ids == (correction,)
    assert notes.decision_check.answer == "no" and notes.causal_check.result_applies
    assert db.update_message_content(correction, "I have not checked.")
    assert ConversationSituationStore(db).get(session) is None


def test_changed_required_input_keeps_report_without_scoring_the_cause(tmp_path):
    from app.core.conversation.conversation_situation import ConversationSituationStore
    from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
    from app.core.infra.chat_database import ChatDatabase
    from scripts.diagnostic_return_replay import Host, RecordedObserver

    db = ChatDatabase(tmp_path / "different-input.db")
    session = Host.session_key
    observer = RecordedObserver()
    worker = ConversationSituationWorker(
        client=observer, chat_db=db, store=ConversationSituationStore(db), model="fixture",
        world_snapshot_provider=dict, judgment_shadow_enabled=False,
    )
    ids = [db.add_message(session, role, text) for role, text in EXAMPLES["microphone"][2]]
    observer.payload = {"operation": "replace", "summary": "Checking microphone clicks.",
                        "evidence_message_ids": ids, "diagnostic_verdict": "input fault"}
    assert worker.run(session).working_set.causal_check
    result = db.add_message(session, "user", "The recording is clean but used a different input.")
    observer.payload.update(evidence_message_ids=[result],
                            diagnostic_verdict="call transport fault")
    notes = worker.run(session).working_set
    assert notes.check_result.evidence_message_ids == (result,)
    assert notes.decision_check.answer is None and not notes.causal_check.result_applies
