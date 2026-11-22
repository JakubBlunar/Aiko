from types import SimpleNamespace

import pytest

from app.core.conversation.occasion_recall import occasion_frequency
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner


FIRST = "On Monday the bike chain slipped at the crossing."
QUESTION = "How many times did my bike chain slip?"
REPAIR = "I ordered a spare link for that chain problem."


def test_ordinary_turn_rechecks_original_reports_after_edits(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    first = db.add_message("robin:ride", "user", FIRST)
    db.add_message("robin:ride", "assistant", "I think the bike chain slipped again.")
    db.add_message("other:ride", "user", FIRST)
    db.add_message("robin:noremember", "user", FIRST)
    db.add_message("robin:garden", "user", "The seedlings have sprouted.")
    later = db.add_message("robin:repairs", "user", REPAIR)
    persona = tmp_path / "persona.txt"
    persona.write_text("You are Aiko. Talk to {user_name}.", encoding="utf-8")
    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)

    class Host(InnerLifePart1Mixin):
        user_display_name = "Robin"
        _chat_db = db
        _memory_settings = SimpleNamespace(context_budget_enabled=True)

    assembler.set_relevant_context_provider(Host().build_relevant_context)
    runner = TurnRunner(SimpleNamespace(), db, assembler, model="offline", context_window=8192,
                        max_tokens=200, temperature=0, filler_enabled=False)

    def ask():
        result = runner.run(session_key="robin:chat", user_text=QUESTION)
        return result.text, result.telemetry.concepts_surfaced["original_exchange_message_ids"]

    reply, ids = ask()
    assert "one incident" in reply and REPAIR in reply and ids == [first, later]
    assert db.update_message_content(later, "The bike chain slipped again on Friday.")
    assert "two separate incidents" in ask()[0]
    assert db.update_message_content(later, "The bike chain slipped.")
    reply, ids = ask()
    assert "can't establish" in reply and not ids


@pytest.mark.parametrize("first,later", [
    (FIRST, "If the bike chain slipped again, I would replace it."),
    (FIRST, "The other bike chain slipped again."),
    (FIRST, "I bought a second chain."),
    (FIRST, "The bike chain slipped again but that was a dream."),
    ("The bike chain slipped twice.", REPAIR),
    ("The bike chain slipped and the brake jammed.", REPAIR),
    (FIRST, "I ordered a spare link for that chain problem. A different one broke."),
])
def test_multiple_readings_stay_open(tmp_path, first, later):
    db = ChatDatabase(tmp_path / "chat.db")
    db.add_message("robin:ride", "user", first)
    db.add_message("robin:repairs", "user", later)
    assert "can't establish" in occasion_frequency(db, QUESTION, "robin:chat")[0]


def test_third_account_and_private_session(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    for topic, text in (("ride", FIRST), ("parts", REPAIR), ("other", "My watch chain broke.")):
        db.add_message("robin:" + topic, "user", text)
    assert "can't establish" in occasion_frequency(db, QUESTION, "robin:chat")[0]
    assert occasion_frequency(db, QUESTION, "robin:noremember") is None
    assert occasion_frequency(db, "Can you help with my chain?", "robin:chat") is None
