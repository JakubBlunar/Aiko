"""A commitment's actor, literal source and time must survive extraction."""
import json

import pytest

from tests.test_promise_worker import _build_world, _seed_promise


def response(**changes):
    return json.dumps({"promises": [{"who": "user", "what": "call the dentist",
        "deadline": None, "source_message_id": 1,
        "source_quote": "meaning to call the dentist", **changes}]})


@pytest.mark.parametrize("changes", [
    {"source_message_id": None}, {"source_message_id": True},
    {"source_message_id": 999}, {"source_message_id": "1"},
    {"source_message_id": []}, {"source_quote": ""},
    {"source_quote": "I'll visit the dentist"}, {"who": "assistant"},
    {"what": "call the dentist tomorrow"},
])
def test_missing_fabricated_wrong_actor_or_unanchored_source_cannot_persist(changes):
    worker, store, _, _ = _build_world(responses=[response(**changes)])
    assert worker.run()["persisted"] == 0
    assert not store.calls


def test_exact_citation_reaches_storage_and_old_source_sets_relative_deadline():
    payload = response(deadline="tomorrow", deadline_quote="tomorrow")
    worker, store, llm, _ = _build_world(responses=[payload], messages=[
        ("user", "I keep meaning to call the dentist about my filling tomorrow."),
    ])
    worker._chat_db.execute_commit(
        "UPDATE messages SET created_at = ? WHERE id = ?", ("2026-10-01T12:00:00+00:00", 1))
    assert worker.run()["persisted"] == 1
    saved = store.calls[0]
    assert saved["source_message_id"] == 1
    assert saved["metadata"]["source_scope"] == "validated_message_quote"
    assert saved["metadata"]["promise_deadline"].startswith("2026-10-02")
    assert "2026" in saved["content"]
    prompt = llm.chat_calls[0]["messages"][-1]["content"]
    assert "message_id=1" in prompt and "2026-10-01T12:00" in prompt


def test_other_speaker_same_action_does_not_suppress_a_real_commitment():
    existing = [_seed_promise("Aiko promised: call the dentist", who="assistant")]
    worker, store, _, _ = _build_world(responses=[response()], existing=existing)
    assert worker.run()["persisted"] == 1
    assert store.calls[0]["metadata"]["promise_who"] == "user"


def test_same_actor_action_with_a_distinct_known_deadline_is_kept():
    existing = [_seed_promise("Jacob promised: call the dentist")]
    existing[0].metadata["promise_deadline"] = "2026-10-01T12:00:00+00:00"
    worker, store, _, _ = _build_world(responses=[response(deadline="2026-10-02",
        deadline_quote="tomorrow")], existing=existing, messages=[
            ("user", "I keep meaning to call the dentist tomorrow."),
        ])
    worker._chat_db.execute_commit(
        "UPDATE messages SET created_at=? WHERE id=1", ("2026-10-01T12:00:00+00:00",))
    assert worker.run()["persisted"] == 1
    assert len(store.calls) == 1


@pytest.mark.parametrize("role,text,quote,action", [
    ("assistant", "You just have to get to Thursday and survive the appointment.",
     "get to Thursday and survive the appointment", "survive the appointment"),
    ("assistant", "I'm glad you're here. Now play the episode before I get maudlin.",
     "Now play the episode", "play the episode"),
    ("user", "I look forward to your cookies on Thursday.",
     "look forward to your cookies on Thursday", "make cookies together"),
    ("user", "Both would be lovely. I don't know which one would be more tasty.",
     "I don't know which one would be more tasty", "be the taste tester"),
])
def test_advice_commands_and_anticipation_do_not_become_owned_promises(role, text, quote, action):
    worker, store, _, _ = _build_world(messages=[(role, text)], responses=[response(
        who=role, what=action, source_quote=quote,
    )])
    assert worker.run()["persisted"] == 0
    assert not store.calls


@pytest.mark.parametrize("role,text,quote,action", [
    ("assistant", "I'll review the route with you before the appointment.",
     "review the route with you", "review the route with Jacob"),
    ("user", "I will let you know when I am sitting inside the bus.",
     "I will let you know when I am sitting inside the bus", "text Aiko from the bus"),
    ("assistant", "Tomorrow night we go over the route and timing.",
     "Tomorrow night we go over the route and timing", "review the route and timing"),
])
def test_owned_follow_up_survives_shortened_quotes_and_shared_actions(role, text, quote, action):
    worker, store, _, _ = _build_world(messages=[(role, text)], responses=[response(
        who=role, what=action, source_quote=quote,
    )])
    assert worker.run()["persisted"] == 1
    assert store.calls[0]["metadata"]["promise_who"] == role
