from contextlib import closing

import pytest

from app.core.conversation.clarification_reply import (
    COPY_QUESTION, answered_copy_question, choice_guidance, clarification_question,
    is_clarification_question,
)
from app.core.conversation.clock_reading import LOOKUP_GUIDANCE, check_clock
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from scripts.choice_chat_replay import DRAFT, MIX


def test_recovery_question_has_a_specific_clarification_and_can_be_answered():
    for question in (DRAFT, MIX):
        assert "[[clarify:restore_copy]]" in choice_guidance(question)
    assert clarification_question("[[clarify:restore_copy]]") == COPY_QUESTION
    assert is_clarification_question(COPY_QUESTION)


def test_general_choices_and_unrelated_turns():
    assert "restore_copy" not in choice_guidance("Should I use cotton or linen?")
    assert choice_guidance("The original canvas was red.") == ""
    assert choice_guidance("I backed up a different file yesterday.") == ""
    assert "different item" in choice_guidance("Correction: the backup was for another model.")


def test_recovery_answer_refers_to_the_preceding_question():
    assert answered_copy_question("Yes, I tested the restore.", COPY_QUESTION)
    assert answered_copy_question("No, just the original.", "Have you verified your backup?")
    assert not answered_copy_question("Yes, I can.", "Can you describe the original?")
    assert not answered_copy_question("The print is blue.", COPY_QUESTION)


def test_choice_preserves_the_users_cutoff_and_unit():
    guidance = choice_guidance("Should I print invitations? I print for 12 guests or fewer.")
    assert "12 guests or fewer" in guidance
    assert "How many guests" in guidance


def test_personal_pick_yields_to_a_concrete_recovery_question():
    assert "actual alternatives" in choice_guidance("Two poster designs. Which do you prefer?")
    question = ("I have two drafts. Which one would you keep if I permanently replace the "
                "original but want a way back?")
    assert "restore_copy" in choice_guidance(question)


def test_reconsideration_uses_history_and_leaves_unrelated_chat_alone():
    history = ("More people stayed for the second act.",)
    question = "Do you still think the new cast drew a bigger crowd?"
    assert "earlier explanation" in choice_guidance(question, iter(history))
    assert not choice_guidance(question)
    assert not choice_guidance("Do you still think about that evening?", history)
    assert not choice_guidance("I forgot to mention the cast party. Are you coming?", history)


@pytest.mark.parametrize("text", [
    "The ferry leaves at 21:47. What time does it leave?",
    "The notice says 00:32. Is that AM or PM?",
    "Convert 12:00 from a 24-hour clock",
    "The note says 6:15. Is that in the morning?",
])
def test_clock_guidance_depends_on_the_question_and_a_clock_value(text):
    assert check_clock(text).guidance


@pytest.mark.parametrize(("source", "answer"), [
    ("00:01", "12:01 AM"), ("13:09", "1:09 PM"), ("23:58", "11:58 PM"),
    ("24-hour listing says 12:00", "12:00 PM"), ("4:25 p.m.", "4:25 PM"),
    ("24-hour clock reads 06:31", "6:31 AM"),
])
def test_a_simple_conversion_preserves_the_local_clock_reading(source, answer):
    assert answer in check_clock("What time is that in 12-hour notation?", [source]).reply


def test_period_requires_evidence_and_more_complex_requests_stay_in_chat():
    assert check_clock("Is 19:06 AM or PM?").reply == "That's 7:06 PM."
    assert check_clock("Is 8:23 AM or PM?").reply == "8:23 alone doesn't specify AM or PM."
    question = "The slip just says '6:15'. Is that AM or PM?"
    assert check_clock(question).reply == "6:15 alone doesn't specify AM or PM."
    assert not check_clock(question, ["It's an evening show."]).reply
    for text in ("Doors at 18:20, music at 19:10. Is that AM or PM?",
                 "Please remind me at 18:20. Is that AM or PM?",
                 "The train leaves at 18:20. What time is that in New York?",
                 "The 12-hour display says 18:20 PM. Is that AM or PM?"):
        assert not check_clock(text).reply


@pytest.mark.parametrize(("text", "prior"), [
    ("What time does the play start?", ""),
    ("That's a beautiful poster. Who drew it?", "The play begins at 19:45."),
    ("At 18:15 I wrote a scene about the morning.", ""),
    ("What time does it start?", "The score was 28:94."),
    ("At 16:20 I am drawing. Do you like the sketch?", ""),
    ("The stopwatch reads 18:15. Is that AM or PM?", ""),
    ("The train leaves at 18:20. What time is that in New York?", ""),
])
def test_clock_guidance_stays_out_of_unrelated_turns(text, prior):
    assert not check_clock(text, [prior]).guidance


def test_followup_uses_the_clock_guidance_in_ordinary_prompt_assembly(tmp_path):
    path = tmp_path / "persona.txt"
    path.write_text("You are a friendly companion.", encoding="utf-8")
    db = ChatDatabase(tmp_path / "chat.db")
    with closing(db._get_conn()):
        db.add_message("fiction", "user", "The doors open at 17:10 and the music starts at 19:40.")
        db.add_message("fiction", "assistant", "I love a good opening number.")
        assembler = PromptAssembler(db, persona_path=path)
        for question, expected in (("What time does the music start?", True),
                                   ("Do you like the poster?", False)):
            mid = db.add_message("fiction", "user", question)
            messages, _ = assembler.assemble_with_budget(
                "fiction", question, current_user_message_id=mid,
                context_window=8192, response_budget=200)
            assert (LOOKUP_GUIDANCE in messages[0]["content"]) == expected
            assert messages[-1]["content"] == question
        question = "The last tram departs at 23:08. What time is that in 12-hour notation?"
        mid = db.add_message("fiction", "user", question)
        _, telemetry = assembler.assemble_with_budget(
            "fiction", question, current_user_message_id=mid,
            context_window=8192, response_budget=200)
        assert telemetry.practical_reply_kind == "clock_reading"
        assert telemetry.decision_reply == "That's 11:08 PM."
