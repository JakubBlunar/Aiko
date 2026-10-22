"""Synthetic expression contracts; no saved personal conversation."""
import pytest

from app.core.concepts.hypothesis_expression import question_span


@pytest.mark.parametrize("reply", [
    "Walking helps you think. I'll keep you company.",
    "Walking helps you think. Which film shall we watch?",
    'Someone asked, "Does walking help you think?"',
    "Imagine walking helps you think?",
    "I said walking helps you think. Is that right?",
])
def test_topical_or_quoted_text_is_not_a_target_question(reply):
    assert question_span("The user uses walking as thinking time", reply) == ""


@pytest.mark.parametrize("reply, expected", [
    ("Hello. Does walking help you think? We can go later.",
     "Does walking help you think?"),
    ("Tell me whether walking helps you think.", "Tell me whether walking helps you think."),
    ("Walking helps you think. Is that right?", "Walking helps you think. Is that right?"),
])
def test_actual_request_is_preserved_verbatim(reply, expected):
    assert question_span("The user uses walking as thinking time", reply) == expected
