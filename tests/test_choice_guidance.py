from app.core.conversation.clarification_reply import (
    COPY_QUESTION, answered_copy_question, choice_guidance, clarification_question,
    is_clarification_question,
)
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
