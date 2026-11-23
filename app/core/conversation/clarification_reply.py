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

_ADVICE = re.compile(r"\b(?:should I|what (?:would|do) you (?:suggest|recommend)|"
                     r"what should I do|which (?:one|option) (?:would|should))\b", re.I)
COPY_QUESTION = "Do you have a verified copy you can restore this version from?"
COPY_ANSWER_GUIDANCE = (
    "The user has answered the recovery question. Answer their original choice in one sentence. "
    "If this item's copy is verified, they can try the change. If this is the only original, "
    "recommend duplicating the complete original before changing it. Use only capabilities "
    "the user has described."
)


def answered_copy_question(user_text, assistant_text):
    if not re.match(r"^(?:yes|no|yep|nope)\b", user_text.strip(), re.I):
        return False
    return bool(re.search(r"\b(?:do you have|do you already have|have you (?:saved|verified))"
                          r"[^.!?]{0,100}\b(?:copy|backup)\b[^.!?]*\?\s*$",
                          assistant_text, re.I))


def choice_guidance(user_text: str, recent_turns=()) -> str:
    """Spend a question on a missing fact only when its answer changes the advice."""
    if re.search(r"\b(?:correction|actually)\b", user_text, re.I) and re.search(
        r"\b(?:backup|backed up|restorable copy)\b", user_text, re.I,
    ) and re.search(r"\b(?:wrong|different|another)\b", user_text, re.I):
        return ("Reconsider the earlier recommendation using the corrected facts. A copy of a "
                "different item cannot restore this one. If the user wants a way back, advise "
                "preserving this version before an irreversible change.")
    if not _ADVICE.search(user_text):
        return ""
    if (re.search(r"\b(?:overwrit\w*|replac\w*|permanent\w*|irreversib\w*)\b", user_text, re.I)
            and re.search(r"\b(?:way back|undo|revers\w*|restor\w*|original)\b", user_text, re.I)):
        recovery = re.compile(r"\b(?:cop(?:y|ies)|backup|backed up|duplicat\w*|archive\w*|"
                              r"snapshot|only|preview\w*|version history|saved|undo)\b", re.I)
        if not any(recovery.search(turn) for turn in (user_text, *recent_turns)):
            return (
                "The user wants a way back from replacing their work. The decision hinges on "
                "one fact: do they already have a restorable copy? Check the supplied context. "
                "If it does not answer this for the current item, ask exactly one question: "
                f"{COPY_QUESTION} You can render that question with [[clarify:restore_copy]]. "
                "Wait for their answer before suggesting how to do the conversion."
            )
        return (
            "Decide whether the user has already told you if they can restore this specific "
            "original after the change. If this is only a preview, answer directly. If a copy "
            "is verified, use that fact in the recommendation. If they only have the original, "
            "advise preserving it first. If they have not answered, the entire response is "
            "[[clarify:restore_copy]]. Ask before giving instructions."
        )
    threshold = re.search(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
                          r"twenty|fifty|a hundred) ([a-z]+s) or (?:fewer|more)\b", user_text, re.I)
    if threshold:
        return (f"The user's exact cutoff is '{threshold[0]}'. Check whether the actual count "
                f"is already known. If it is missing, ask only: How many {threshold[1]} are there? "
                "Then wait for the answer. If it is known, apply the user's exact cutoff.")
    return (
        "For a choice, use facts the user has already supplied. If one unknown fact changes "
        "which option you recommend, your whole reply should be one direct question asking "
        "for that fact. Respect the user's stated criterion. If the answer is known or both "
        "answers give the same recommendation, give that recommendation now."
    )


_START = re.compile(r"\s*\[\[clarify:", re.IGNORECASE)
REFERENCE_QUESTION = "Which one do you mean?"
_QUESTIONS = {
    "restore_copy": COPY_QUESTION,
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
