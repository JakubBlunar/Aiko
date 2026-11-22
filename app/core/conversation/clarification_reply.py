"""Render a model's request for clarification as one grounded question."""

import re


CLARIFICATION_GUIDANCE = (
    "Decide whether the latest request can be answered from the conversation and any tool "
    "results. If the user has already answered a clarification, use their answer. For a clear "
    "request or a harmless default, give the useful answer directly. Don't add a question just "
    "to close a concrete answer. When recalling something, stick to the reported facts and "
    "keep unknown causes and outcomes open.\n"
    "If two plausible referents, an unclear goal, or a missing essential input block the "
    "answer, the entire reply must be one of these tags. The app turns it into a short "
    "question. Output the tag alone, with no guesses, examples, greeting, or explanation.\n"
    "[[clarify:reference]] — which person, thing, or event?\n"
    "[[clarify:goal]] — what does the user want to accomplish?\n"
    "[[clarify:time]] — what time? [[clarify:place]] — where?\n"
    "[[clarify:requirement]] — what requirement? [[clarify:detail]] — another essential detail.\n"
    "Two drafts and the user says 'review that one': [[clarify:reference]]\n"
    "User says 'help with my project' without asking for an outcome: [[clarify:goal]]\n"
    "A message needs an exact time the user hasn't provided: [[clarify:time]]\n"
    "User says 'use the second draft and suggest a heading': suggest one heading."
)

CURRENT_REPLY_GUIDANCE = (
    "Reply to the latest user message. A necessary clarification is a single [[clarify:…]] tag, "
    "with no lead-in or guessed options. Otherwise give the requested answer. When they ask "
    "what happened, give the remembered action and observed result without diagnosis or advice."
)

_START = re.compile(r"\s*\[\[clarify:", re.IGNORECASE)
REFERENCE_QUESTION = "Which one do you mean?"
_QUESTIONS = {
    "reference": REFERENCE_QUESTION,
    "goal": "What would you like to achieve?",
    "time": "What time should I use?",
    "place": "Which place do you mean?",
    "requirement": "Which requirement should I keep in mind?",
    "detail": "Could you tell me a little more?",
}


def starts_clarification(body: str) -> bool:
    return _START.match(body) is not None


def is_clarification_question(message: str) -> bool:
    return message.strip() in _QUESTIONS.values()


def clarification_question(body: str) -> str | None:
    """Accept a leading directive. All other model text stays outside the question."""
    match = _START.match(body)
    if match is None:
        return None
    kind, end, _ = body[match.end():].partition("]]")
    return _QUESTIONS.get(kind.strip().casefold() if end else "detail", _QUESTIONS["detail"])
