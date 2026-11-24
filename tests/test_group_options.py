from itertools import combinations
from unittest.mock import MagicMock

import pytest

from app.core.conversation.group_options import check_group_options
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from scripts.everyday_reasoning_replay import GROUPS, TRIO


@pytest.mark.parametrize(("case", "phrases"), [
    ("pairwise only", ("no confirmed day",)), ("subset", ("Thursday works for Lea and Ben",)),
    ("possible switch", ("no confirmed day", "Wednesday could work", "confirmation from Ben")),
    ("confirmed switch", ("Wednesday works",)), ("withdrawn", ("no confirmed day",)),
    ("multiple common options", ("Monday and Tuesday work",)), ("unknown attendee", ("Val",)),
    ("films", ("no confirmed film",)), ("films subset", ("Moss works for Ben and you",)),
])
def test_intersections_and_group_members(case, phrases):
    _, history, question, _ = next(row for row in GROUPS if row[0] == case)
    answer = check_group_options(question, reversed(history))
    assert all(p in answer.reply for p in phrases)


def test_individual_turns_keep_whole_group_and_open_changes():
    history = ["I can do Friday.", "Nia can do Monday or Friday.", "Ken can do Monday."]
    assert "no confirmed day" in check_group_options("When can we all meet?", history).reply
    assert "Ken and you" in check_group_options("I might do Monday. Ken might do Friday. "
                                                "Which night works for Ken and me?", history).reply
    assert not check_group_options("When can we all meet?", ["We finished rehearsal.", TRIO]).reply


def test_different_pairs_of_excluded_days():
    days = ("Monday", "Tuesday", "Friday")
    for excluded in combinations(days, 2):
        a, b = (set(days) - {d} for d in excluded)
        listings = f"Nia can do {' or '.join(sorted(a))}. Tao can do {' or '.join(sorted(b))}."
        result = check_group_options(listings + " Which nights work for everyone?").reply
        assert result == f"{(a & b).pop()} works for Nia and Tao."


def test_two_people_in_one_turn_do_not_hide_an_earlier_participant():
    history = ["Ben can do Thursday. I can do Thursday.", "Lea can do Monday."]
    assert "no confirmed day" in check_group_options("When can we all meet?", history).reply


def test_incomplete_roster_ambiguous_update_and_quoted_titles():
    assert not check_group_options("Which night works for all four of us?", [TRIO]).reply
    assert "What works for you" in check_group_options(
        "When can we all meet?", ["Lea can do Monday. Val can do Monday."]).reply
    assert not check_group_options("Ben confirmed I can do Wednesday. When can we all meet?",
                                   [TRIO]).reply
    assert not check_group_options("I can do Friday after six. When can we all meet?", [TRIO]).reply
    assert not check_group_options("Write a limerick about Friday.", [TRIO]).guidance
    films = ('Lea can watch "Pride and Prejudice" or "Friday". '
             'Ben can watch "Friday" or "Love, Actually". Which film works for everyone?')
    assert check_group_options(films).reply == "Friday works for Lea and Ben."


def test_checked_reply_streams_and_persists(tmp_path):
    db = ChatDatabase(tmp_path / "group.db")
    persona = tmp_path / "persona.txt"
    persona.write_text("Answer briefly.", encoding="utf-8")
    model = MagicMock()
    runner = TurnRunner(model, db, PromptAssembler(db, persona_path=persona), model="fixture",
                        context_window=8192, max_tokens=200, temperature=0, filler_enabled=False)
    db.add_message("fiction", "user", TRIO)
    db.add_message("fiction", "assistant", "Go on.")
    tokens = []
    turn = runner.run("fiction", "Which night works for Lea and Ben?", on_token=tokens.append)
    assert turn.text == "Thursday works for Lea and Ben."
    assert turn.telemetry.practical_reply_kind == "group_options"
    assert "".join(tokens) == db.get_message_row(turn.assistant_message_id).content == turn.text
    model.chat_stream.assert_not_called()
