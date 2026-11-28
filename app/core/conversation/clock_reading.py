"""Ground schedule lookups and local clock-format questions in the stated readings."""
import re

from app.core.conversation.practical_answer import PracticalAnswer


_DIGITS = r"([01]?\d|2[0-3]):([0-5]\d)"
_VALUE = re.compile(rf"(?<![\w:]){_DIGITS}(?![\d:])"
                    r"(?:[ \t]*([ap])\.?m\b\.?)?", re.I)
_LOOKUP = re.compile(r"\b(?:what time\b(?!\s+signature)|when\b[^.!?]{0,60}\b(?:start|begin|"
                     r"finish|arrive|leave|open|close|end|depart)(?:s|ed)?\b)", re.I)
_FORMAT = re.compile(r"\b(?:(?:12|24)[ -]?hour|(?:in the |at )(?:morning|afternoon|evening|"
                     r"midnight|noon)|morning or (?:evening|night)|[ap]\.?m\.?\s*(?:or|/)"
                     r"\s*[ap]\.?m\.?|[ap]\.?m\.?\s*\?)", re.I)
_SIMPLE = re.compile(r"(?:^|[.!]\s+)(?:is (?:that|it) (?:am or pm|pm or am)|"
                     r"what (?:time )?is (?:that|it) in (?:12[ -]hour (?:notation|format|time)))"
                     r"\?\s*$", re.I)
_INLINE_PERIOD = re.compile(rf"\s*is ({_DIGITS}) (?:am or pm|pm or am)\?\s*", re.I)
_SOURCE_24H = re.compile(r"\b24[ -]hour (?:clock|notation|format|listing|timetable|"
                         r"schedule)\b", re.I)
LOOKUP_GUIDANCE = (
    "Read the listing for the event they asked about. For a simple lookup or reminder, "
    "your entire answer is the time as printed. If only another event has a time, say "
    "the requested time isn't listed. A clock reading by itself gives no date or plans "
    "to remember. Finish once you've answered."
)
FORMAT_GUIDANCE = (
    "Answer the requested clock interpretation from the source. Converting notation preserves "
    "the local time. In a 24-hour source, hours 00–11 are AM and 12–23 are PM. If the source "
    "leaves the period unspecified, say so. Keep the answer to the time they asked about."
)


def _equivalent(match, source):
    hour, minute = int(match[1]), int(match[2])
    if match[3]:
        if not 1 <= hour <= 12:
            return ""
        hour = hour % 12 + (12 if match[3].lower() == "p" else 0)
    elif 1 <= hour <= 12 and (not _SOURCE_24H.search(source)
                              or re.search(r"\b(?:not|never|isn['’]t|doesn['’]t)\b", source, re.I)):
        return ""
    period = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {period}"


def check_clock(text, recent_turns=()):
    recent_turns = tuple(recent_turns)
    lookup = _LOOKUP.search(text)
    convert = _FORMAT.search(text, lookup.start() if lookup else 0)
    if not (lookup or convert) or ("?" not in text and not re.search(
            r"\b(?:when|what time|convert|tell me)\b", text, re.I)):
        return PracticalAnswer()
    if not any(_VALUE.search(part) for part in (text, *recent_turns)):
        return PracticalAnswer()
    if re.search(r"\b(?:time ?zones?|my (?:local )?time|what time (?:is|would) "
                 r"(?:that|it) (?:be )?in (?!\d{2}[ -]hour))\b", text, re.I):
        return PracticalAnswer()
    if not convert:
        return PracticalAnswer(LOOKUP_GUIDANCE)
    inline = _INLINE_PERIOD.fullmatch(text)
    if inline:
        # AM and PM are offered alternatives in this question, not suffixes on the digits.
        value = inline[1]
        equivalent = _equivalent(_VALUE.fullmatch(value), "\n".join(recent_turns))
        if equivalent:
            return PracticalAnswer(FORMAT_GUIDANCE, f"That's {equivalent}.")
        if not any(recent_turns):
            return PracticalAnswer(FORMAT_GUIDANCE, f"{value} alone doesn't specify AM or PM.")
    simple = _SIMPLE.search(text)
    if not simple or text.count("?") != 1:
        return PracticalAnswer(FORMAT_GUIDANCE)
    # A deictic conversion asks about the reading itself. Only answer directly with
    # a single source reading; event selection and other requests stay with chat.
    source = text[:simple.start()].strip()
    if not source:
        source = "\n".join(recent_turns)
    elif not (re.match(r"(?:the|my|our|this|that|it|a|an)\b", source, re.I)
              or _VALUE.fullmatch(source)):
        return PracticalAnswer(FORMAT_GUIDANCE)
    elif re.search(r"[.!]\s+\w|\b(?:please|remind|convert|tell me|can you|also)\b", source, re.I):
        return PracticalAnswer(FORMAT_GUIDANCE)
    matches = list(_VALUE.finditer(source))
    if re.search(r"\b(?:timer|stopwatch|elapsed|duration|timestamp|score|ratio)\b", source, re.I):
        return PracticalAnswer()
    if len(matches) != 1:
        return PracticalAnswer(FORMAT_GUIDANCE)
    match = matches[0]
    equivalent = _equivalent(match, source)
    if equivalent:
        return PracticalAnswer(FORMAT_GUIDANCE, f"That's {equivalent}.")
    # A quoted bare value carries no period. If there's a daypart elsewhere in the
    # exchange, let the ordinary reader weigh it rather than flattening the context.
    if (not match[3] and re.search(r"\b(?:only|just|simply) (?:says|reads|shows|lists)\s*"
                                   r"['\"“]?$", source[:match.start()], re.I)
            and not source[match.end():].strip(" .'\"”")
            and not re.search(r"\b(?:morning|afternoon|evening|night|am|pm|noon|midnight)\b",
                              " ".join((*recent_turns, source)), re.I)):
        return PracticalAnswer(FORMAT_GUIDANCE, f"{match[0]} alone doesn't specify AM or PM.")
    return PracticalAnswer(FORMAT_GUIDANCE)
