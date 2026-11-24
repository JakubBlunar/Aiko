from unittest.mock import MagicMock

import pytest

from app.core.conversation.contingency import check_contingency
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from scripts.narrative_reasoning_replay import CAKE, PLANS


@pytest.mark.parametrize(("case", "expected"), [
    ("before cutoff", "you'll pick up"), ("at cutoff", "you'll pick up"),
    ("after cutoff", "Sam will bake"), ("time unknown", "Did the bakery confirm by noon?"),
    ("cake not observed", "haven't mentioned receiving"),
    ("proposed exception", "Sam will bake"), ("accepted exception", "you'll pick up"),
    ("finished fallback", "Yes"), ("text without confirmation", "Did the bakery confirm"),
    ("strict cutoff", "Nina will bring"), ("held-out equipment", "Nina will bring"),
    ("current outcome only", "Yes"), ("explicitly missed", "Sam will bake"),
])
def test_conditional_outcomes(case, expected):
    _, history, q, _ = next(row for row in PLANS if row[0] == case)
    answer = check_contingency(q, reversed(history)).reply
    assert expected in answer
    if case in {"cake not observed", "current outcome only"}:
        assert "?" not in answer


def test_no_stale_or_unrelated_answers():
    assert not check_contingency("Do we have a bike yet?", [CAKE]).guidance
    assert not check_contingency("Whose turn is it?", ["We started playing cards.", CAKE]).guidance
    assert not check_contingency("What makes a good birthday card?", [CAKE]).guidance
    assert not check_contingency("Whose turn is it?", ["Nothing happened."] * 4 + [CAKE]).guidance


@pytest.mark.parametrize("update", [
    "Sam vetoed that plan.", "The cafe confirmed at 10 am.",
    "The bakery confirmed at 9 am yesterday.", "Please order it too.",
    "The bakery confirmed at 9 am. The bakery confirmed at 4 pm.",
    "The bakery declined to confirm by noon.",
])
def test_unrecognized_updates_do_not_settle_the_plan(update):
    assert not check_contingency(update + " Whose turn is it?", [CAKE]).reply


def test_ambiguous_clock_and_missing_completion():
    assert "Did the bakery confirm" in check_contingency(
        "The bakery first confirmed at 2:10. Who is responsible?", [CAKE]).reply
    answer = check_contingency("Do we have a cake yet?", [CAKE])
    assert "haven't mentioned receiving" in answer.reply


def test_single_message_and_public_followup(tmp_path):
    given = CAKE + " The bakery's first confirmation arrived at 12:20."
    assert "Sam will bake" in check_contingency(given + " Who is responsible?").reply
    db = ChatDatabase(tmp_path / "plan.db")
    persona = tmp_path / "persona.txt"
    persona.write_text("Answer briefly.", encoding="utf-8")
    model = MagicMock()
    runner = TurnRunner(model, db, PromptAssembler(db, persona_path=persona),
                        model="fixture", context_window=8192, max_tokens=200,
                        temperature=0, filler_enabled=False)
    db.add_message("fiction", "user", given)
    db.add_message("fiction", "assistant", "Go on.")
    tokens = []
    result = runner.run("fiction", "Do we have a cake yet?", on_token=tokens.append)
    assert "haven't mentioned receiving the cake" in result.text
    assert result.telemetry.practical_reply_kind == "conditional_agreement"
    assert "".join(tokens) == db.get_message_row(result.assistant_message_id).content == result.text
    model.chat_stream.assert_not_called()
