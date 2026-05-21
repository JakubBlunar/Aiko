"""Unit tests for proactive director JSON parsing."""
from __future__ import annotations

from app.llm.proactive_planner import (
    extract_json_object_from_text,
    parse_director_json,
)


def test_parse_director_minimal() -> None:
    raw = '{"speak": true, "utterance_seed": "ask about coffee"}'
    plan = parse_director_json(raw)
    assert plan.speak is True
    assert "coffee" in plan.utterance_seed
    assert plan.draft_line == ""
    assert plan.hints_for_next_user_turn == ""


def test_parse_director_full() -> None:
    raw = """
    {"speak": false, "kind": "nudge", "draft_line": "Hi there.",
     "hints_for_next_user_turn": "User may want weather.",
     "avoid": ["politics"], "suggested_steps": [{"label": "check", "detail": "radar"}]}
    """
    plan = parse_director_json(raw)
    assert plan.speak is False
    assert plan.kind == "nudge"
    assert plan.draft_line == "Hi there."
    assert "weather" in plan.hints_for_next_user_turn
    assert plan.avoid == ["politics"]
    assert len(plan.suggested_steps) == 1
    assert "radar" in plan.suggested_steps[0]


def test_parse_invalid_returns_empty() -> None:
    plan = parse_director_json("not json")
    assert plan.speak is False
    assert plan.utterance_seed == ""


def test_extract_json_from_preamble() -> None:
    text = 'Here you go: {"speak": true} trailing'
    inner = extract_json_object_from_text(text)
    plan = parse_director_json(inner)
    assert plan.speak is True


def test_strip_json_fence() -> None:
    raw = "```json\n{\"speak\": true, \"draft_line\": \"x\"}\n```"
    plan = parse_director_json(raw)
    assert plan.speak is True
    assert plan.draft_line == "x"
