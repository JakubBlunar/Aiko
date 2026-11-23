"""Explicit feedback about Aiko's response before a performance or evaluation."""

import re
from dataclasses import dataclass
from datetime import timedelta

from app.core.conversation.implicit_need import unquoted_text
from app.core.infra import timephrase


_EVENT = r"(?:interview|audition|presentation|exam|recital|performance)"
_BEFORE = rf"before (?:my|the|that) {_EVENT}"
_NERVOUS = r"(?:nervous|anxious|scared|getting (?:in my head|the jitters))"
_MY_NERVES = (
    rf"(?:i(?:'m| am) (?:really |so |a little )?{_NERVOUS}|"
    r"my stomach is in knots|i have (?:butterflies|the jitters))"
)
_WHEN = r"(?:in (?:\d+|a|an|a few|[a-z]+) (?:minutes?|hours?|days?)|tomorrow|next week|soon)"
_UPCOMING = (
    rf"(?:my {_EVENT} (?:(?:starts|begins)(?: {_WHEN})?|is (?:{_WHEN}|about to start))\b|"
    rf"i(?:'m| am) (?:about to (?:start|give|take)|waiting for) (?:my|the|an?) {_EVENT}\b)"
)
_ANXIOUS_ABOUT = re.compile(
    rf"\b{_MY_NERVES} (?:before my {_EVENT}\b|(?:about|for) my (?:upcoming {_EVENT}\b|"
    rf"{_EVENT} (?:tomorrow|later|next|this afternoon|in \w+ minutes)\b))",
)
_JOIN = r"(?:\s*[,;.!—-]\s*(?:and |but )?| (?:and|but) )"
_EVENT_AND_FEELING = re.compile(
    rf"\b(?:{_UPCOMING}{_JOIN}{_MY_NERVES}|{_MY_NERVES}{_JOIN}{_UPCOMING})\b",
)
_OTHER_FOCUS = re.compile(
    r"\b(?:instead|if|yesterday|last night|how (?:it|that) went|already over|"
    r"pretend|imagine|hypothetically|as an example|for a (?:story|script|scene)|"
    rf"(?:nervous|anxious|scared|butterflies|knots|jitters) (?:about|for|over) "
    rf"(?!my (?:upcoming )?{_EVENT}\b)|"
    r"(?:no|skip the) jokes?|(?:be|keep it) serious|please|don't|do not|can you|could you|"
    r"tell me|help me|i (?:need|want|would like) you to)\b",
)


def pre_event_nerves(text: str) -> bool:
    """Require the user's own nerves and an upcoming occasion; yield to specific requests."""
    text = unquoted_text(text).lower()
    return bool(
        (_ANXIOUS_ABOUT.search(text) or _EVENT_AND_FEELING.search(text))
        and not _OTHER_FOCUS.search(text)
    )


_ACTS = {
    "humor": r"(?:you (?:joked with me|made me laugh|cracked a joke)|your (?:joke|humor))",
    "company": r"(?:you (?:stayed with me|kept me company|sat with me)|your (?:quiet )?company)",
}
_ACT = "|".join(f"(?P<{name}>{phrase})" for name, phrase in _ACTS.items())
_PRAISE = re.compile(
    rf"(?:actually,? )?(?:i (?:liked|appreciated|loved) (?:that |how )?|"
    rf"(?:what|the part that) helped was (?:that )?|it helped (?:that|when) )"
    rf"(?:{_ACT})",
)
_REPORT = re.compile(rf"(?:{_ACT})")
_EFFECT = re.compile(r" (?:really )?helped(?: me)?")
_REJECT = re.compile(rf"(?:actually,? )?i (?:didn't|did not) like (?:that |how )?(?:{_ACT})")
_SCOPE = re.compile(rf" {_BEFORE}")
_TRAILING = re.compile(r"(?:[.!] i was (?:getting in my head|nervous|anxious))?[.!]?")
_WITHDRAW = re.compile(
    r"(?:actually,? )?(?:that (?:didn't|did not) help|"
    r"what helped was something else|i take that back|"
    r"what (?:actually |really )?helped was that you [a-z' ]+)[.!]?",
)
_ACK = re.compile(
    r"(?:i'm glad (?:you told me|it helped)|thanks for letting me know|thanks for telling me|"
    r"got it|i'll (?:remember that|keep that in mind)|i hear you(?: about (?:the joke|"
    r"having me there)|[.!] it was (?:the joke|having me there) that mattered)?)[.!]?", re.I,
)

GUIDANCE = {
    "humor": "Open with a short, gentle joke drawn from one detail they mentioned. "
             "Give the detail a silly job or imagine it taking a turn at the event. "
             "Make the situation the punchline and finish with one line of company.",
    "company": "Offer quiet company. Write one short sentence in the first person about "
               "staying with them while they wait, then stop. Omit instructions, including "
               "breathing exercises, questions, pep talks and jokes.",
}


@dataclass(frozen=True)
class _Appraisal:
    act: str | None
    scoped: bool = False
    rejected: bool = False


def _appraisal(text: str) -> _Appraisal | None:
    if len(text) > 300 or "\n" in text:
        return None
    text = " ".join(text.lower().replace("’", "'").split())
    match = _PRAISE.match(text) or _REJECT.match(text) or _REPORT.match(text)
    if not match:
        return _Appraisal(None) if _WITHDRAW.fullmatch(text) else None
    tail = text[match.end():]
    scope = _SCOPE.match(tail)
    if scope:
        tail = tail[scope.end():]
    if match.re is _REPORT:
        effect = _EFFECT.match(tail)
        if not effect:
            return None
        tail = tail[effect.end():]
        if not scope and (scope := _SCOPE.match(tail)):
            tail = tail[scope.end():]
    if not _TRAILING.fullmatch(tail):
        return None
    return _Appraisal(match.lastgroup, bool(scope), match.re is _REJECT)


def acknowledgment(text: str) -> str | None:
    appraisal = _appraisal(text)
    if appraisal is None or appraisal.act is None:
        return None
    response = "the joke" if appraisal.act == "humor" else "having me there"
    example = (f"I hear you about {response}." if appraisal.rejected
               else f"I hear you. It was {response} that mattered.")
    return (
        f'Acknowledge this feedback simply, along these lines: "{example}" '
        "End there. Keep their mood, reasons and plans open."
    )


@dataclass
class _Exchange:
    row: dict
    sources: tuple[int, ...]
    reply_id: int | None = None


def received_response(rows: list[dict]) -> tuple[str, tuple[int, ...]] | None:
    """Fold retained original dialogue in time order, binding bare feedback to its exchange."""
    latest = None
    # Interleaved sessions may each have an unfinished exchange.
    anchors = {}
    for row in reversed(rows):
        session = row["session_id"]
        anchor = anchors.get(session)
        if row["role"] != "user":
            if (anchor and row["role"] == "assistant" and anchor.reply_id is None
                    and (not anchor.sources or _ACK.fullmatch(row["content"].replace("’", "'")))):
                anchor.reply_id = row["message_id"]
            else:
                anchors.pop(session, None)
            continue
        anchors.pop(session, None)
        text = row["content"]
        feedback = _appraisal(text)
        if feedback is None:
            if pre_event_nerves(text):
                anchors[session] = _Exchange(row, ())
            continue
        if feedback.scoped:
            sources = (row["message_id"],)
        elif anchor:
            # A bare appraisal following a situation needs a response to appraise.
            if not anchor.sources and (anchor.reply_id is None or feedback.act is None):
                continue
            first = timephrase.parse_iso(anchor.row["created_at"])
            last = timephrase.parse_iso(row["created_at"])
            if (first is None or last is None
                    or not timedelta() <= last - first <= timedelta(minutes=10)):
                continue
            evidence = anchor.sources or (anchor.row["message_id"], anchor.reply_id)
            sources = (*evidence, row["message_id"])
        else:
            continue
        if feedback.rejected:
            if latest and latest[0] == feedback.act:
                latest = None
        else:
            latest = (feedback.act, sources) if feedback.act else None
        anchors[session] = _Exchange(row, sources)
    return latest
