"""Assess what an empty record supports within the period it actually covered."""
from itertools import islice
import re

from app.core.conversation.practical_answer import PracticalAnswer


_GUIDE = (
    "Assess the missing trace. Would the event necessarily have left that trace, and for "
    "which part of the period was recording active? State what the empty record supports "
    "during that interval. Treat a person's account as a report and check whether it falls "
    "inside the gap or conflicts with the record. Ask how recording worked only if unknown."
)
_LOG = r"(?:guest ?book|visitor log|entry register|attendance log)"
_CAMERA = r"(?:[\w-]+ ){0,3}camera"
_EMPTY = re.compile(
    rf"(?:The (?P<log>{_LOG})(?: at (?:my|the) [\w-]+)? was (?:blank|empty)"
    rf"(?: when I closed up)?|The (?P<cam>{_CAMERA}) (?:saved|recorded) no clips"
    rf"(?: this morning| all day)?)", re.I,
)
_QUESTION = re.compile(
    r"Did (?:nobody come|(?:anyone|anybody) (?:come|visit)|no visitors come|"
    r"(?P<birds>(?:no|any) birds visit (?:the )?feeder))"
    r"(?: (?P<period>during (?:the )?(?:staffed hours|break)|while it was "
    r"(?:powered|unplugged)|all day|this morning))?\?", re.I,
)
_ABSENT = re.compile(r"\b(?:blank|empty|no (?:entries|clips|visits|recordings?|signatures)|"
                     r"didn't (?:record|hear|see)|never (?:rang|went off))\b", re.I)
_INFER = re.compile(r"\b(?:did (?:nobody|anybody|anyone|no|any)|does (?:that|this) mean|"
                    r"can I (?:conclude|assume)|was (?:anyone|nobody))\b", re.I)
_STAFF = r"(?:The attendant|I) (?:recorded|logged) every visitor (?:on entry|at the door)"
_BREAK = r"(?:They|I|The attendant) took a [\w-]+(?: minute)? break with the door open"
_WITNESS = re.compile(r"([A-Z][\w'-]*) (?:says|said) (she|he|they) visited during "
                      r"(the break|staffed hours)")


def _assess_record(facts, question, camera):
    coverage = ""
    gap = False
    witness = None
    for fact in facts:
        if camera:
            if re.fullmatch(r"(?:It|The camera) (?:was unplugged|was off) for "
                            r"(?:an?|one|\d+|[a-z]+) (?:hours?|minutes?)", fact, re.I):
                gap = True
                continue
            active = re.fullmatch(r"(?:It|The camera) recorded every visit "
                                  r"(while powered|all morning|all day)", fact, re.I)
            if not active:
                return ""
            mode = "partial" if active[1].lower() == "while powered" else "complete"
        elif re.fullmatch(r"(?:Signing(?: it| in)? was optional|Visitors could choose "
                        r"whether to sign)", fact, re.I):
            mode = "optional"
        elif re.fullmatch(_STAFF + r" (?:all day|throughout the exhibition)", fact, re.I):
            mode = "complete"
        elif re.fullmatch(_STAFF + r" while on duty", fact, re.I):
            mode = "partial"
        elif re.fullmatch(_BREAK, fact, re.I):
            gap = True
            continue
        elif m := _WITNESS.fullmatch(fact):
            if witness:
                return ""
            witness = m
            continue
        else:
            return ""
        if coverage and coverage != mode:
            return ""
        coverage = mode
    period = (question["period"] or "").lower()
    asks_staffed = "staffed" in period or period == "while it was powered"
    asks_break = "break" in period or "unplugged" in period
    if coverage == "complete" and gap:
        return ""  # A conflicting coverage account needs the full exchange.
    if witness:
        name, pronoun, interval = witness.groups()
        during_break = interval == "the break"
        if asks_break and not during_break and coverage == "partial" and gap:
            return (f"{name}'s account concerns the staffed hours. The book can't tell you "
                    "whether anyone visited during the break.")
        if coverage == "complete" or (coverage == "partial" and not during_break):
            return (f"{name} reports a visit, although every arrival during that period was "
                    "supposed to appear in the book. The two accounts conflict.")
        if not (asks_staffed and during_break or asks_break and not during_break):
            return (f"{name} says {pronoun} visited during {interval}. "
                    "A blank book can fit that account.") if coverage or gap else ""
    if coverage == "optional":
        return "People could visit without signing. The blank book tells you nobody signed it."
    if coverage == "complete":
        if camera:
            return ("The camera recorded every visit. The absence of clips is strong evidence "
                    "that no birds visited during that period.")
        return ("Every arrival was recorded. The blank book is strong evidence that "
                "nobody visited during the recorded hours.")
    if coverage == "partial":
        if camera:
            known = "The empty recording supports no visits while the camera was powered."
            if asks_staffed:
                return known
            if gap or asks_break:
                return known + " Birds could have visited while it was unplugged."
            return known + " Was it powered the whole time?"
        if asks_staffed:
            return "The blank book is strong evidence that nobody came during the staffed hours."
        if asks_break or gap:
            return ("The blank book supports no visits while the attendant was on duty. "
                    "Someone could have come during the break.")
        return "It supports no visits during staffed hours. Was the door staffed the whole time?"
    if camera:
        return "Would the camera have captured every visit throughout that period?"
    return ("Was every visitor recorded, or could people come in without signing? "
            "An empty book alone leaves attendance open.")


def check_missing_clue(text, recent_users=()):
    if not text:
        return PracticalAnswer()
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip().replace("’", "'"))
    ask = parts.pop()
    if not _INFER.search(ask):
        return PracticalAnswer()
    question = _QUESTION.fullmatch(ask)
    # Only borrow a previous observation for a bounded, related question.
    borrowed = False
    if question:
        for prior in islice(recent_users, 3):
            if any(_ABSENT.search(s) for s in parts):
                break
            borrowed = True
            parts[:0] = re.split(r"(?<=[.!?])\s+|\n+", prior.strip().replace("’", "'"))
    if not any(_ABSENT.search(s) for s in parts):
        return PracticalAnswer()
    if not any(re.search(rf"\b(?:{_LOG}|camera|sensor|bell|log|recorder|register)\b", s, re.I)
               for s in parts):
        return PracticalAnswer()
    guide = PracticalAnswer(_GUIDE)
    if not question:
        return guide
    facts = [s.rstrip(".") for s in parts]
    records = [m for f in facts if (m := _EMPTY.fullmatch(f))]
    if len(records) != 1:
        return guide
    record = records[0]
    if bool(record["cam"]) != bool(question["birds"]):
        return PracticalAnswer()
    scope = (question["period"] or "").lower()
    if record["log"] and "while it was" in scope or record["cam"] and "staffed" in scope:
        return PracticalAnswer()
    facts.remove(record[0])
    if record["cam"] and "feeder" not in record["cam"].lower():
        return guide
    if scope == "all day" and "this morning" in record[0].lower():
        return guide
    reply = _assess_record(facts, question, bool(record["cam"]))
    return PracticalAnswer(_GUIDE, reply) if reply or not borrowed else PracticalAnswer()
