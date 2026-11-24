"""Follow a stated conditional agreement through confirmation and later changes."""
from itertools import islice
import re

from app.core.conversation.practical_answer import PracticalAnswer


_CLOCK = r"(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2})"
_SUBJECT = r"(?:the [\w-]+|[\w'-]+)"
_BRANCH = r"([\w'-]+?)(?: will|'ll) ([\w -]+)"
_PLAN = re.compile(
    rf"If ({_SUBJECT}) confirms (by|before) ({_CLOCK}),\s*{_BRANCH}[.;]\s*"
    rf"otherwise[,]?\s+{_BRANCH}\.", re.I,
)
_RESPONSIBLE = re.compile(r"(?:whose turn is it|who(?: is|'s) responsible|"
                          r"which plan (?:applies|are we following)|who is on duty)\?", re.I)
_RESULT = re.compile(r"(?:do we (?:have|already have)|did we get) (?:the|a|an) "
                     r"([\w-]+)(?: yet)?\?", re.I)
_PLAN_QUESTION = re.compile(r"\b(?:backup|fallback|which plan|on the hook|supposed to|"
                            r"should .{1,30} (?:bring|pick up|collect|bake))\b", re.I)
_GUIDANCE = (
    "Read the user's conditional agreement. Identify the trigger and deadline, then use "
    "what was actually reported. Distinguish a reply from confirmation, a suggested exception "
    "from a joint decision, and responsibility from a completed action. Answer the requested "
    "part. Ask about the timing only if the answer depends on it."
)


def _minutes(clock):
    clock = clock.lower().replace(" ", "")
    if clock in ("midnight", "noon"):
        return 0 if clock == "midnight" else 720
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", clock)
    h, m, period = int(match[1]), int(match[2] or 0), match[3]
    if m >= 60 or h >= 24 or (period and not 1 <= h <= 12):
        return None
    # An unqualified 1:00 through 11:59 could refer to either half of the day.
    if not period and 1 <= h < 12 and not clock.startswith("0"):
        return None
    if period:
        h = h % 12 + (12 if period == "pm" else 0)
    return h * 60 + m


def _action(subject, verb):
    if subject.casefold() == "i":
        return f"you'll {verb}"
    if subject.casefold() == "we":
        return f"you'll {verb} together"
    return f"{subject} will {verb}"


def check_contingency(text, recent_users=()):
    if not text:
        return PracticalAnswer()
    # A current agreement supersedes earlier scenarios. Otherwise keep every
    # intervening user update so an unrecognized correction prevents a shortcut.
    current = text.strip().replace("’", "'")
    question = re.split(r"(?<=[.!?])\s+|\n+", current)[-1]
    result = _RESULT.fullmatch(question)
    responsible_question = _RESPONSIBLE.fullmatch(question)
    if not result and not responsible_question and not _PLAN_QUESTION.search(question):
        return PracticalAnswer()
    parts = [current]
    for prior in islice(recent_users, 4):
        if re.search(r"\bif\b", parts[0], re.I):
            break
        parts.insert(0, prior.strip().replace("’", "'"))
    source = "\n".join(parts)
    if not re.search(r"\bif\b", source, re.I) or not re.search(
            r"\b(?:otherwise|if not|backup plan)\b", source, re.I):
        return PracticalAnswer()
    plan = _PLAN.match(source)
    guide = PracticalAnswer(_GUIDANCE)
    if not plan:
        return guide
    party, rule, deadline, yes, first, no, second = plan.groups()
    if not result and not responsible_question:
        return guide
    if result and not re.search(rf"\b{re.escape(result[1])}\b", first + " " + second, re.I):
        return PracticalAnswer()
    if (yes.lower() == "you" or no.lower() == "you"
            or any(re.search(r"\b(?:if|unless|except|before|after|when|or|maybe|but|"
                             r"my|our|mine|ours|me|us|you|your)\b", v, re.I)
                   for v in (first, second))):
        return guide
    cutoff = _minutes(deadline)
    evidence = source[plan.end():-len(question)].strip()
    responsible = None
    confirmed = accepted = complete = False
    offered = False
    party_name = re.escape(party)
    for line in filter(None, re.split(r"(?<=[.!?])\s+|\n+", evidence)):
        if _RESPONSIBLE.fullmatch(line) or _RESULT.fullmatch(line):
            continue
        fact = line.strip().rstrip(".")
        confirmation = re.fullmatch(rf"(?:{party_name} (?:first )?confirmed|{party_name}'s "
                                    rf"first confirmation (?:came|arrived))(?: at ({_CLOCK}))?",
                                    fact, re.I)
        if confirmation:
            if confirmed or accepted:
                return guide
            confirmed = True
            arrival = _minutes(confirmation[1]) if confirmation[1] else None
            if arrival is not None and cutoff is not None:
                responsible = arrival <= cutoff if rule.lower() == "by" else arrival < cutoff
            continue
        if re.fullmatch(rf"{party_name} (?:didn't|did not) confirm {rule} {deadline}",
                        fact, re.I):
            if confirmed or accepted:
                return guide
            confirmed, responsible = True, False
            continue
        if re.fullmatch(rf"{party_name} (?:texted|called) at {_CLOCK} to say they'd "
                        r"(?:let us know|confirm) later", fact, re.I):
            continue
        offer = rf"(?:take|accept) {party_name}'s late offer"
        if re.fullmatch(rf"I (?:suggested|proposed) (?:we )?{offer}", fact, re.I):
            offered = True
            continue
        group = rf"(?:we|{re.escape(no)} and I|I and {re.escape(no)})"
        if re.fullmatch(rf"{group} agreed to {offer}", fact, re.I):
            if yes.lower() != "i" or not re.match(r"(?:pick up|collect)\b", first, re.I):
                return guide
            accepted = responsible = True
            continue
        if result and re.fullmatch(
                rf"(?:{re.escape(no)}|{re.escape(yes)}) (?:brought the (?:finished )?"
                rf"{re.escape(result[1])}|[\w-]+ the {re.escape(result[1])} and brought it here)",
                fact, re.I):
            complete = True
            continue
        # A prior change of subject can leave a bare "Whose turn is it?"
        # referring to a different agreement entirely.
        return PracticalAnswer() if current == question else guide
    picked, fallback = _action(yes, first), _action(no, second)
    if result:
        if complete:
            reply = f"Yes, you said the {result[1]} was brought over."
        else:
            reply = f"You haven't mentioned receiving the {result[1]} yet."
            if responsible is not None:
                reply += " Under the agreement, " + (picked if responsible else fallback) + "."
    elif responsible is None:
        reply = (f"Did {party} confirm {rule} {deadline}? If so, {picked}. "
                 f"Otherwise, {fallback}.")
    elif accepted:
        reply = f"You agreed to the late offer, so {picked}."
    elif responsible:
        reply = f"The confirmation met the cutoff, so under your plan {picked}."
    else:
        reply = f"The cutoff passed without a timely confirmation, so under your plan {fallback}."
        if offered:
            reply += " Taking the late offer is still a suggestion."
    return PracticalAnswer(_GUIDANCE, reply)
