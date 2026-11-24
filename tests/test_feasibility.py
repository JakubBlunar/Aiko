"""Public plan advice must honor both the arithmetic and the permitted change."""
import pytest

from app.core.conversation.feasibility import check_feasibility
from scripts.practical_reasoning_replay import FEASIBILITY


@pytest.mark.parametrize(("case", "expected"), [
    ("fixed start", "Arriving earlier won't help"), ("on demand", "5:45 pm"),
    ("flexible finish", "leave 15 minutes later, at 7:00 pm"),
    ("both fixed", "won't fit"), ("exact fit", "fits exactly"),
    ("follow-up", "5:45 pm"), ("material", "one continuous piece of at least 90 cm"),
    ("equipment", "accepts at least 34 cm"),
])
def test_fictional_plans(case, expected):
    _, history, question, _ = next(row for row in FEASIBILITY if row[0] == case)
    assert expected in check_feasibility(question, reversed(history)).reply


def test_changed_time_and_duration():
    answer = check_feasibility(
        "I arrive at 09:50. The tour starts whenever I arrive and lasts 20 minutes. "
        "I must leave at 10:05. I can arrive earlier. Will it fit?")
    assert "5 minutes earlier, at 9:45" in answer.reply


def test_unstated_start_does_not_grant_flexibility():
    answer = check_feasibility("I can get there at 6. The last bus leaves at 6:45. "
                               "Can I see the whole hour-long show?")
    assert "15 minutes short" in answer.reply
    assert "only if you could start then" in answer.reply


@pytest.mark.parametrize("extra", [
    "It takes 10 minutes to get to the stop.", "The clocks use different time zones.",
    "Please book a taxi too.", "Actually I can leave at 7:15 pm.",
])
def test_extra_constraints_and_actions_stay_in_chat(extra):
    assert not check_feasibility(
        f"I arrive at 6:00 pm. The show takes 60 minutes. I must leave at 6:45 pm. {extra} "
        "Will it fit?").reply


def test_unknown_equipment_permission_and_units():
    plan = ("My printer fits sheets up to 8 inches wide. The photo is 10 inches wide "
            "and must stay intact. Will it fit?")
    assert "You'd need a printer that accepts at least 10 inches" in check_feasibility(plan).reply
    assert not check_feasibility(plan.replace("8 inches", "20 cm")).reply


def test_topic_switch_has_no_feasibility_answer():
    assert not check_feasibility(FEASIBILITY[-1][2]).guidance
    assert not check_feasibility("How much earlier would I need to be?",
                                 ["The garden looks great.", FEASIBILITY[0][2]]).guidance
