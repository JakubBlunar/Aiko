import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.conversation.digest_fidelity import parse_claims
from app.core.infra import timephrase
from app.core.rag.rag_retriever import _temporal_filter_drops


def row(mid=1, text="Noah reported a headache", temporal="past_event", age=1, kind="event"):
    return SimpleNamespace(id=mid, content=text, temporal_type=temporal, kind=kind, metadata={},
        created_at=(timephrase.utcnow() - timedelta(hours=age)).isoformat(),
        relevance_until=None)


def draft(scope="history", **changes):
    return json.dumps({"claims": [{"text": "Noah reported a headache on November 5.",
        "source_ids": [1], "subject": "user", "scope": scope, **changes}]})


def test_new_schema_stores_only_cited_clauses_not_unchecked_digest_text():
    data = json.loads(draft())
    data["digest"] = "A fabricated extra conclusion."
    value = parse_claims(json.dumps(data), [row()], timephrase.utcnow())
    assert value and "fabricated" not in value["text"]
    assert not parse_claims('{"digest":"An unreviewed old shape"}', [row()], timephrase.utcnow())


@pytest.mark.parametrize("text", [
    "Noah reported a headache four weeks ago.",
    "Noah reported a headache one week ago.",
    "Noah plans an appointment in May next year.",
])
def test_relative_intervals_cannot_enter_stored_summary_clauses(text):
    assert parse_claims(draft(text=text), [row()], timephrase.utcnow()) is None


@pytest.mark.parametrize("payload,sources", [
    (draft(source_ids=[99]), [row()]),
    (draft(subject="aiko"), [row()]),
    (draft("history"), [row(temporal="future_plan")]),
    (draft("current_state"), [row(temporal="ongoing", age=48)]),
    (draft("current_state"), [row(temporal="past_event")]),
    (draft("reported_pattern"), [row(), row(2)]),
    (draft("history", text="Noah's ongoing state is a headache."), [row()]),
    (draft("history"), [row(kind="goal_progress")]),
])
def test_summary_refuses_unlicensed_current_pattern_subject_and_execution(payload, sources):
    assert parse_claims(payload, sources, timephrase.utcnow()) is None


def test_current_state_cannot_ignore_later_resolution_and_expires_without_new_data():
    state = row(temporal="ongoing", age=3)
    resolved = row(2, "Noah's headache went away", age=1)
    now = timephrase.utcnow()
    assert parse_claims(draft("current_state"), [state, resolved], now) is None
    other_actor = row(2, "Aiko's headache went away", age=1, kind="self")
    assert parse_claims(draft("current_state"), [state, other_actor], now)
    unresolved = row(2, "Noah's headache has not resolved", age=1)
    assert parse_claims(draft("current_state"), [state, unresolved], now)
    value = parse_claims(draft("current_state"), [state], now)
    assert value and value["valid_until"]
    memory = SimpleNamespace(kind="topic_digest", metadata={"summary_claims": value},
                             temporal_type="durable")
    assert not _temporal_filter_drops(memory, now)
    assert _temporal_filter_drops(memory, now + timedelta(days=2))


def test_legitimate_intention_and_explicit_pattern_are_retained():
    now = timephrase.utcnow()
    assert parse_claims(draft("intention", text="Noah intended to visit the museum."),
                        [row(text="Noah intended to visit the museum", temporal="future_plan")],
                        now)
    assert parse_claims(draft("reported_pattern", text="Noah often goes walking."),
                        [row(text="Noah often goes walking")], now)


def test_expired_ongoing_memories_are_not_rendered_as_a_current_state():
    state = row(temporal="ongoing", age=72)
    state.relevance_until = (timephrase.utcnow() - timedelta(days=1)).isoformat()
    assert _temporal_filter_drops(state, timephrase.utcnow())
    assert "current applicability unknown" in timephrase.format_memory_line(state)
