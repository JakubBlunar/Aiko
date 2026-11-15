"""Source-valid output must also retain the source's limited meaning."""
import json
from types import SimpleNamespace

import pytest

from app.core.memory.claim_fidelity import refusal
from app.core.memory.memory_admission import admit_memory
from app.core.memory.promise_evidence import ownership_refusal, same_source_actions, source_clause
from app.core.memory.promise_extractor import Promise
from app.core.memory.promise_worker import PromiseExtractionWorker
from tests.test_goal_reflection_evidence import setup
from tests.test_promise_source_grounding import response
from tests.test_promise_worker import _build_world
from tests.test_memory_extractor_watermark import _FakeDb, _FakeStore, _FakeOllama, _build


@pytest.mark.parametrize("evidence", [None, []])
@pytest.mark.parametrize("basis,content", [
    ("reported", "Noah promised to deliver a covered cup."),
    ("inferred", "Noah may prefer covered cups."),
])
def test_extractor_cannot_bypass_source_checks_by_omitting_evidence(evidence, basis, content):
    row = SimpleNamespace(id=1, session_id="s", role="user",
                          content="I will bring a covered cup.", created_at="2026-11-01T12:00Z")
    result = admit_memory({"kind": "fact", "content": content, "basis": basis,
                          "evidence": evidence, "event_time": "2026-11-01T20:34Z"},
                         [row], session_key="s", writer="extractor")
    assert not result.accepted
    assert result.metadata["claim_fidelity_refusal"] == "missing_evidence"


def test_supported_paraphrase_and_tentative_inference_still_have_source_receipts():
    row = SimpleNamespace(id=1, session_id="s", role="user", created_at="2026-11-01T12:00Z",
                          content="I will bring a covered cup.")
    for basis, content in [
        ("reported", "Noah intends to bring a covered cup."),
        ("inferred", "Noah may prefer covered cups."),
    ]:
        result = admit_memory({"kind": "fact", "content": content, "basis": basis,
                               "evidence": [{"message_id": 1, "quote": row.content}],
                               "event_time": "2026-11-01T20:34Z"},
                              [row], session_key="s", writer="extractor")
        assert result.accepted
        assert result.metadata["claim_fidelity"]["event_time"] is None
        assert result.source_message_id == 1


def test_inline_subjective_self_note_keeps_its_separate_admission_contract():
    result = admit_memory({"kind": "self", "content": "I want to explore quieter music."},
                          [], session_key="s", writer="inline")
    assert result.accepted


@pytest.mark.parametrize("source,content", [
    ("I am back. Are we reading?", "Noah is back and asks if they are reading."),
    ("I finished the report and sent it to Mira.", "Noah finished a report and sent it to Mira."),
])
def test_useful_extractor_output_reaches_storage_without_a_model_invented_clock(source, content):
    db = _FakeDb()
    db.append("user", source)
    for _ in range(5):
        db.append()
    payload = json.dumps({"memories": [{
        "content": content, "kind": "event", "salience": .5, "temporal_type": "past_event",
        "provenance": "stated", "basis": "reported", "event_time": "2026-11-01T19:45:00Z",
        "destination": "memory", "evidence": [{"message_id": 1, "quote": source}],
    }]})
    store = _FakeStore()
    worker = _build(db, _FakeOllama([payload]), store=store)
    assert worker.extract_for_session("s") == 1
    assert store.added[0]["content"] == content
    assert store.added[0]["source_message_id"] == 1
    assert store.added[0]["event_time"] is None


@pytest.mark.parametrize("source", [
    "Hopefully I am not too demanding.",
    "I hope I am not too demanding.",
    "I hope I'll get around to sending those notes.",
    "Tell me the project you chose. I want to actually pay attention this time.",
])
def test_a_first_person_pronoun_does_not_establish_an_owned_commitment(source):
    assert ownership_refusal(source, source)


@pytest.mark.parametrize("source", [
    "I'll review the project with you before the meeting.",
    "I keep meaning to call the dentist.",
    "Tomorrow night we go over the route and timing.",
    "I promise to bring the covered cup.",
    "I won't share the private notes.",
    "I promise not to share the private notes.",
    "I hope I'll have time, but I'll send the notes either way.",
])
def test_meaningful_owned_follow_up_is_retained(source):
    assert not ownership_refusal(source, source)


@pytest.mark.parametrize("source,quote", [
    ("The launch is in three hours. I'll send you my notes when you return.",
     "I'll send you my notes"),
    ("I'll send you my notes. The launch is in three hours.", "I'll send you my notes."),
])
def test_deadline_in_an_unrelated_sentence_cannot_date_a_commitment(source, quote):
    worker, store, _, _ = _build_world(messages=[("assistant", source)], responses=[response(
        who="assistant", what="send Noah the project notes", source_quote=quote,
        deadline="2026-11-02", deadline_quote="in three hours",
    )])
    assert worker.run()["persisted"] == 1
    assert "promise_deadline" not in store.calls[0]["metadata"]
    assert store.calls[0]["metadata"]["deadline_quote"] == ""


def test_complete_sentence_quote_does_not_borrow_the_next_sentences_condition():
    quote = "I'll send the notes."
    source = quote + " If you want a diagram, I can add one later."
    assert source_clause(quote, source) == quote
    assert not ownership_refusal(quote, source)


@pytest.mark.parametrize("source", [
    "I'll send you my notes. By tomorrow.",
    "By tomorrow. I'll send you my notes.",
])
def test_adjacent_timing_fragment_still_dates_the_genuine_commitment(source):
    worker, store, _, _ = _build_world(messages=[("assistant", source)], responses=[response(
        who="assistant", what="send Noah the project notes", source_quote="I'll send you my notes",
        deadline="2026-11-02", deadline_quote="tomorrow",
    )])
    assert worker.run()["persisted"] == 1
    assert store.calls[0]["metadata"]["deadline_precision"] == "day"
    assert "promise_deadline" in store.calls[0]["metadata"]


def test_same_commitment_quote_cannot_mint_a_differently_worded_obligation():
    quote = "I'll collect on that correction later"
    memory = SimpleNamespace(source_message_id=7, content="Aiko promised: remember being wrong",
        metadata={"promise_who": "assistant", "source_quote": quote})
    promise = Promise(who="assistant", text="collect on your correction", source_turn_id=7,
                      raw_match="collect on that correction later")
    assert same_source_actions(promise, [memory], PromiseExtractionWorker._is_duplicate)


def test_one_message_can_still_contain_two_distinct_commitments():
    memory = SimpleNamespace(source_message_id=7, content="Aiko promised: call the dentist",
        metadata={"promise_who": "assistant", "source_quote": "I'll call the dentist"})
    promise = Promise(who="assistant", text="send the notes", source_turn_id=7,
                      raw_match="I'll send the notes")
    assert not same_source_actions(promise, [memory], PromiseExtractionWorker._is_duplicate)


@pytest.mark.parametrize("source,claim,accepted", [
    ("I am back. Are we going to read?", "Noah returned from his bathroom routine.", False),
    ("I'm back!", "Noah returned after brushing his teeth.", False),
    ("I am back. Are we reading?", "Noah intends to read after washing up.", False),
    ("I returned from the bathroom.", "Noah returned from the bathroom.", True),
    ("I finished brushing my teeth and returned.", "Noah returned after brushing his teeth.", True),
    ("I am back. I finished brushing my teeth.", "Noah returned after brushing his teeth.", True),
    ("I am back.", "Noah returned.", True),
    ("I am back. Are we reading?", "Noah asked whether we are reading.", True),
])
def test_bare_return_reports_do_not_certify_the_preceding_plan(source, claim, accepted):
    assert (refusal(claim, [source], "reported") is None) == accepted


@pytest.mark.parametrize("kind,clock,expected", [
    ("reflection", "", 0),
    ("reflection", " at 19:45", 1),
    ("intention", "", 1),
])
def test_goal_notes_cannot_turn_recording_time_into_an_unstated_event_clock(kind, clock, expected):
    goals, worker, model, goal = setup({"note": ""})
    row = goals._memory_store.add("Noah promised to bring a tool example" + clock,
                                 "event", goal.embedding, temporal_type="future_plan")
    model.payload = json.dumps({
        "note": "I want to review the tool example at 19:45." if kind == "intention"
                else "I reflect on Noah's promise to bring the tool example at 19:45.",
        "note_kind": kind, "evidence_memory_ids": [row.id],
    })
    result = worker.run()
    assert result["wrote"] == expected
    assert len(goals.list_progress(goal.id)) == expected


def test_reflection_label_cannot_turn_a_plan_into_a_reported_outcome():
    goals, worker, model, goal = setup({"note": ""})
    row = goals._memory_store.add("Noah will finish the clear tool example",
                                 "event", goal.embedding, temporal_type="future_plan")
    model.payload = json.dumps({"note": "Noah completed the clear tool example; I feel encouraged.",
                               "note_kind": "reflection", "evidence_memory_ids": [row.id]})
    assert worker.run()["wrote"] == 0
    assert not goals.list_progress(goal.id)


@pytest.mark.parametrize("source,claim,accepted", [
    ("I will call later.", "Noah will call at 19:45.", False),
    ("I will call at 7:45pm.", "Noah will call at 19:45.", True),
    ("I will call at 7:45.", "Noah will call at 19:45.", False),
    ("I will call at 8am.", "Noah will call at 08:00.", True),
])
def test_event_clock_needs_stated_timing_not_a_recording_timestamp(source, claim, accepted):
    assert (refusal(claim, [source], "reported") is None) == accepted
