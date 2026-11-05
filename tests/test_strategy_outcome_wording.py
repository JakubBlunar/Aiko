"""Natural outcomes preserve source, condition and observed-order requirements."""
from types import SimpleNamespace

import pytest

from app.core.memory.learned_strategies import admit_strategy
from app.core.memory.strategy_outcomes import outcome_schema_needed
from tests.test_learned_strategies import episode


@pytest.mark.parametrize("text,success", [
    ("Now it is working.", True), ("Now it's fine.", True),
    ("now it s fine", True), ("It works now.", True),
    ("It is still failing.", False), ("It did not help.", False),
    ("It didn\u2019t help.", False),
])
def test_explicit_user_status_can_report_an_outcome(text, success):
    rows, candidate = episode()
    rows[2].content = text
    candidate["strategy"]["outcome"].update(text=text, quote=text, success=success)
    assert outcome_schema_needed(rows)
    admitted = admit_strategy(candidate, rows, session_key="s", owner="u")
    assert admitted["metadata"]["learned_strategy"]["success"] is success


@pytest.mark.parametrize("text", [
    "Sounds nice", "I am all worked up", "Now it is fine to try", "It would have worked",
    "Imagine it worked", "Now it is working?",
])
def test_consent_hypothetical_arousal_and_questions_are_not_results(text):
    rows, candidate = episode()
    rows[2].content = text
    candidate["strategy"]["outcome"].update(text=text, quote=text)
    assert admit_strategy(candidate, rows, session_key="s", owner="u") is None


def test_assistant_outcome_is_neither_an_offering_signal_nor_user_testimony():
    rows, candidate = episode()
    rows[2].role = "assistant"
    assert not outcome_schema_needed(rows)
    assert admit_strategy(candidate, rows, session_key="s", owner="u") is None


@pytest.mark.parametrize("text,accepted", [
    ("I isolated one service and now it's working.", True),
    ("Now it's working. I isolated one service.", False),
    ("I isolated one service then isolated one service and now it's working.", False),
])
def test_same_message_requires_unambiguous_literal_action_before_result(text, accepted):
    rows, candidate = episode()
    rows[2].content = text
    candidate["strategy"]["approach"] = {
        "message_id": 3, "quote": text, "text": "isolated one service",
    }
    result = "Now it's working" if text.startswith("Now") else "now it's working"
    candidate["strategy"]["outcome"].update(text=result, quote=text)
    assert bool(admit_strategy(candidate, rows, session_key="s", owner="u")) == accepted


def test_natural_outcome_still_requires_goal_and_conditions():
    rows, candidate = episode()
    rows[2].content = "Now it's fine."
    candidate["strategy"]["outcome"].update(text=rows[2].content, quote=rows[2].content)
    candidate["strategy"]["constraints"] = []
    assert admit_strategy(candidate, rows, session_key="s", owner="u") is None


def test_user_result_among_context_can_offer_schema_for_existing_call():
    assert outcome_schema_needed([SimpleNamespace(role="user", content="It works now.")])
