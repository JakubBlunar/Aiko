"""Render a model's request for clarification as one grounded question."""

import re

from app.core.conversation.perspective_sources import terms


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


_INVITES_VIEW = re.compile(
    r"(?:^|[.!?,;:]\s*)(?:what do you (?:think|reckon)(?: about that)?|"
    r"what['’]s your (?:take|view)|how do you feel about (?:it|that)|"
    r"do you agree|thoughts)\s*\?\s*$", re.I)
_IDEA = re.compile(r"\b(?:should|ought to|every|always|never|would be better|"
                   r"i (?:love|like|prefer|wish)|what if|my idea)\b", re.I)
_SUGGESTION = re.compile(
    r"^\s*(?:(?:oh|well|maybe|then)[, ]+)?(?:what (?:if|about)|how about|we could|"
    r"it could|(?P<detail>an?\b[^.!?]{1,80}\b(?:you|we|that|with|without)))\b", re.I)
EXAMPLE_GUIDANCE = (
    "Consider the idea at the scope the speaker gave it. If a particular imagined situation "
    "helps you find your own view, try it there. One concrete case can show what you like "
    "or what gives you pause. A personal pleasure can simply be enjoyed. If they offer a "
    "variation, think about what made your earlier case matter and whether the variation "
    "changes that. A change of subject gets a fresh response. Speak in your own words."
)


def _invited_idea(text):
    ask = _INVITES_VIEW.search(text)
    return bool(ask and _IDEA.search(text[:ask.start()]))


def ordinary_reply_guidance(text, previous_user="", previous_assistant=""):
    """Offer a way to weigh an invited idea or answer a clear switch to practical help."""
    if re.match(r"\s*(?:but |ok(?:ay)?[, ]+)?(?:seriously|jok(?:es|ing) aside|"
                r"on a serious note)[, .:!-]+(?:how (?:do|can|should)|"
                r"can you help|what should)\b", text, re.I):
        return ("They've closed the bit and asked for practical help. Use earlier turns only "
                "to identify what they mean. Treat the earlier invented details as fiction. "
                "Open with the first useful step, and finish when the steps are clear.")
    if _invited_idea(text):
        return EXAMPLE_GUIDANCE
    if previous_assistant and _invited_idea(previous_user):
        suggestion = _SUGGESTION.match(text)
        # A proposed detail can introduce a new word for the solution. For other
        # proposals, look for continuity before asking the model to revisit a view.
        if suggestion and (suggestion["detail"] or
                           terms(text) & (terms(previous_assistant) | terms(previous_user))):
            return ARGUMENT_GUIDANCE
    return CURRENT_REPLY_GUIDANCE


_ADVICE = re.compile(r"\b(?:should I|what (?:would|do) you (?:suggest|recommend)|"
                     r"what should I do|which (?:one|option) (?:would|should))\b", re.I)
_PERSONAL_PICK = re.compile(r"\bwhich (?:one )?(?:(?:do|would) you (?:personally )?"
                            r"(?:keep|prefer|like|pick)|is your favou?rite)\b", re.I)
_CREATIVE_MATERIAL = re.compile(r"\b(?:openings?|lines?|drafts?|titles?|names?|designs?|"
                                r"sketch(?:es)?|endings?)\b", re.I)
_REVISIT = re.compile(r"\bdo you still (?:think|believe|expect)\b(?!\s+(?:about|of)\b)", re.I)
_ADDED_DETAIL = re.compile(r"\b(?:forgot to mention|left out one detail)\b", re.I)
_COMPARATIVE_ASK = re.compile(r"\bwhich\b[^?!.]*\b(?:more|less|better|worse)\b[^?!.]*\?", re.I)
_COMPARISON = re.compile(r"\b(?:more|less|fewer|higher|lower|better|worse|bigger|smaller|"
                         r"faster|slower|cause[ds]?|because)\b", re.I)
IMPRESSION_GUIDANCE = (
    "Read the actual alternatives to yourself with the other person's vote set aside. "
    "Start with the particular word, sound or image you responded to, then say which one "
    "you'd keep. Either choice is fine. If you can't point to anything they actually shared, "
    "the whole reply can be 'Can I read them both?' One pick tells you about this piece."
)
REVISION_GUIDANCE = (
    "Test your earlier explanation against the latest detail. A busier stall that alone "
    "gave a discount leaves you unsure whether the product or the discount drew people. "
    "Both stalls giving the same discount leaves the original comparison intact. One "
    "person's favorite tells you about that person. Give the observed result and how sure "
    "you are about its cause. Leave unreported audience motives open."
)
ARGUMENT_GUIDANCE = (
    "Weigh the other person's reason against the value behind your own last choice. A reason "
    "can reveal a benefit you overlooked even when the facts stay the same. Decide for yourself "
    "whether it changes your preference, suggests an exception, or leaves your reason stronger. "
    "Say what matters to you in this particular choice. If they simply urge agreement, there "
    "is no new reason to credit. Keep this conversational and leave your longer-term taste open."
)


def argument_guidance(text, previous_user, previous_assistant):
    if (not re.search(r"\bi(?:['’]d (?:pick|choose|prefer|keep|go)| (?:like|prefer|favor|"
                      r"care|value|would choose))\b", previous_assistant, re.I)
            or not re.search(r"\b(?:because|since|so that|matters|means|gives|lets|keeps)\b",
                             previous_assistant, re.I)):
        return ""
    if not re.match(r"\s*(?:(?:but|actually|well)[, ]+)?(?:i (?:like|prefer|care|value)|"
                    r"what about|the (?:advantage|reason)|come on|just admit|"
                    r"have you considered)\b", text, re.I):
        return ""
    shared = terms(text) & (terms(previous_assistant) | terms(previous_user))
    if (shared - {"because", "since", "give", "keep", "let", "matter", "mean", "care", "value"}
            and not re.search(r"\b(?:anyway|unrelated|change of subject)\b", text, re.I)):
        return ARGUMENT_GUIDANCE
    return ""


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
    advice = _ADVICE.search(user_text)
    if (advice and re.search(r"\b(?:overwrit\w*|replac\w*|permanent\w*|irreversib\w*)\b",
                             user_text, re.I)
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
    if advice and threshold:
        return (f"The user's exact cutoff is '{threshold[0]}'. Check whether the actual count "
                f"is already known. If it is missing, ask only: How many {threshold[1]} are there? "
                "Then wait for the answer. If it is known, apply the user's exact cutoff.")
    single_question = user_text.count("?") == 1 and user_text.rstrip().endswith("?")
    if (single_question and _PERSONAL_PICK.search(user_text)
            and _CREATIVE_MATERIAL.search(user_text)):
        return IMPRESSION_GUIDANCE
    new_comparison = _ADDED_DETAIL.search(user_text) and _COMPARATIVE_ASK.search(user_text)
    revisit = _REVISIT.search(user_text) and _COMPARISON.search(user_text)
    if single_question and not advice and (revisit or new_comparison) and any(recent_turns):
        return REVISION_GUIDANCE
    if not advice:
        return ""
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
