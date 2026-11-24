from unittest.mock import MagicMock

import pytest

from app.core.conversation.outcome_comparison import check_outcomes
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from scripts.practical_reasoning_replay import DRAWINGS, OUTCOMES


@pytest.mark.parametrize(("case", "required"), [
    ("totals", ["higher for morning", "6 drawings", "4 drawings"]),
    ("opposite winners", ["higher for morning", "higher for evening", "4/5 evening sessions"]),
    ("denominator reversal", ["higher for morning", "6/10 morning", "4/8 evening"]),
    ("tie", ["higher for morning", "was tied", "6/9", "4/6"]),
    ("missing opportunities", ["higher for morning", "6/10", "number of evening sessions"]),
    ("multiple finishes", ["higher for morning", "morning sessions with a finish",
                           "evening sessions with a finish"]),
    ("throughput", ["higher for evening", "0.6 drawings per morning", "0.8 drawings per evening"]),
    ("no opportunities", ["0.5 sketches per weekday", "no weekend sessions"]),
    ("follow-up", ["higher for evening", "0.6 drawings", "0.8 drawings"]),
    ("known successes", ["higher for morning", "5/10", "2/5"]),
    ("causal claim", ["can't establish", "time spent", "0.8 pages", "0.6 pages"]),
    ("labelled runs", ["higher for indoor", "4 laps per indoor run", "2 laps per outdoor run"]),
])
def test_requested_quantity_and_units(case, required):
    _, history, question, _ = next(row for row in OUTCOMES if row[0] == case)
    answer = check_outcomes(question, next(reversed(history), "")).reply
    for phrase in required:
        assert phrase in answer
    if case in ("denominator reversal", "no opportunities"):
        assert "higher for evening" not in answer
        assert "higher for weekday" not in answer


@pytest.mark.parametrize("extra", [
    "Actually I lost track on Fridays.", "Please send this to the group.",
    "The units are estimates.", "Each session had at most one page.",
    "In the evenings 7 sessions had a finish.", "In the mornings 0 sessions had a finish.",
])
def test_unrecognized_or_inconsistent_report_stays_in_chat(extra):
    assert not check_outcomes(f"{DRAWINGS} {extra} When did I finish more?").reply


def test_followup_uses_only_the_requested_metric_and_item():
    prior = DRAWINGS + " When did I finish more?"
    assert "total" not in check_outcomes("Which had higher throughput?", prior).reply
    assert "0.8 drawings" in check_outcomes("Which had higher throughput?", prior).reply
    assert not check_outcomes("Which group averaged more pages per session?", prior).guidance
    assert not check_outcomes("Which hat looks better?", prior).guidance
    assert not check_outcomes(DRAWINGS + " Which hat looks better?").guidance
    assert not check_outcomes("Which had higher throughput?", "I bought a hat.").guidance


def test_success_counts_unknown_without_single_output_condition():
    answer = check_outcomes(DRAWINGS + " When was I more likely to finish one?").reply
    assert answer.count("with a finish") == 2
    assert "higher" not in answer


def test_first_group_can_have_the_missing_denominator():
    reply = check_outcomes("I finished 12 tasks in the mornings and 10 in 5 evening shifts. "
                           "When did I finish more tasks per shift?").reply
    assert "2 tasks per evening shift" in reply and "number of morning shifts" in reply


def test_checked_comparison_streamed_and_saved(tmp_path):
    db = ChatDatabase(tmp_path / "outcomes.db")
    persona = tmp_path / "persona.txt"
    persona.write_text("Answer briefly.", encoding="utf-8")
    model = MagicMock()
    runner = TurnRunner(model, db, PromptAssembler(db, persona_path=persona),
                        model="fixture", context_window=8192, max_tokens=200,
                        temperature=0, filler_enabled=False)
    db.add_message("fiction", "user", DRAWINGS)
    db.add_message("fiction", "assistant", "Go on.")
    tokens = []
    result = runner.run("fiction", "Which had higher throughput?", on_token=tokens.append)
    assert "0.8 drawings per evening session" in result.text
    assert "".join(tokens) == db.get_message_row(result.assistant_message_id).content == result.text
    model.chat_stream.assert_not_called()
