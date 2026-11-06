"""Advertised grounding/work requests reach the matching tool schema family."""
import pytest

from app.core.session.tool_pass_gate import (
    GateContext, _TOOL_FAMILY, select_active_tool_names, should_run_tool_pass,
)


@pytest.mark.parametrize("text,needed", [
    ("Why do you think that about me?", "recall_concept"),
    ("What have you figured out about me?", "recall_concept"),
    ("How has your picture of me changed?", "recall_self_history"),
    ("How has our relationship changed?", "recall_self_history"),
    ("What is your current reading of this?", "recall_beliefs"),
    ("What do you think I need from this conversation?", "recall_beliefs"),
    ("What are you up to?", "check_my_work"),
    ("Can you write a file with those notes?", "start_workflow"),
    ("Please read the notes.txt file", "start_workflow"),
    ("Could you move that document into a folder?", "start_workflow"),
    ("Read notes.txt", "start_workflow"),
])
def test_request_reaches_the_schema_not_only_the_decision_pass(text, needed):
    names = list(_TOOL_FAMILY)
    decision = should_run_tool_pass(text, names, context=GateContext())
    assert decision.run
    allowed = select_active_tool_names(decision, names, router_enabled=True)
    assert allowed is None or needed in allowed


@pytest.mark.parametrize("text", [
    "Could you write a short poem?", "I wrote a file earlier", "I like how you think",
    "That sounds lovely", "I feel like writing something",
])
def test_ordinary_text_and_inline_writing_do_not_gain_a_workflow_pass(text):
    decision = should_run_tool_pass(text, list(_TOOL_FAMILY), context=GateContext())
    assert not decision.run


def test_new_inspection_read_is_available_in_a_premise_pass_without_mutations():
    names = ["recall", "inspect_memory", "move_to", "write_diary", "start_workflow"]
    decision = should_run_tool_pass("Those lenses", names, context=GateContext(recall_need=True))
    assert select_active_tool_names(decision, names, router_enabled=False) == {
        "recall", "inspect_memory",
    }
