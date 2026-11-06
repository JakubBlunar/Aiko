import json
from datetime import datetime, timezone

import pytest

from app.core.memory.promise_evidence import supported_deadline
from tests.test_promise_source_grounding import response
from tests.test_promise_worker import _build_world


def test_unstated_deadline_is_removed_without_losing_the_actual_commitment():
    worker, store, _, _ = _build_world(responses=[response(deadline="2026-11-06T00:30:00Z")])
    assert worker.run()["persisted"] == 1
    saved = store.calls[0]
    assert "promise_deadline" not in saved["metadata"] and "(by " not in saved["content"]


def test_same_source_replay_cannot_create_another_promise_even_with_a_new_clock():
    worker, store, llm, _ = _build_world(responses=[response(), response(
        what="call dentist about the filling", deadline="2026-11-06T00:30:00Z")])
    assert worker.run()["persisted"] == 1
    assert worker.run()["reason"] == "unchanged_source_window"
    assert len(llm.chat_calls) == 1
    worker._chat_db.add_message("u1:session-1", "user", "Another message changes the window.")
    assert worker.run()["persisted"] == 0
    assert len(store.calls) == 1


def test_immediate_enactment_is_not_a_follow_up_commitment():
    candidate = json.loads(response())
    candidate["promises"][0]["commitment_type"] = "immediate"
    worker, store, _, _ = _build_world(responses=[json.dumps(candidate)])
    assert worker.run()["persisted"] == 0 and not store.calls


@pytest.mark.parametrize("quote,source,expected", [
    ("tomorrow", "I'll call tomorrow.", "day"),
    ("tomorrow at 18:30", "I'll call tomorrow at 18:30.", "minute"),
    ("tomorrow", "I'll call soon.", ""),
    ("next week", "I'll call next week.", ""),
    ("soon", "I'll call soon.", ""),
])
def test_timing_precision_never_exceeds_the_literal_source(quote, source, expected):
    anchor = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    when, precision = supported_deadline(quote, source, anchor)
    assert precision == expected
    assert (when is not None) == bool(expected)
    if precision == "day":
        assert when.astimezone().date().isoformat() == "2026-10-02"
        assert (when.hour, when.minute) == (23, 59)
