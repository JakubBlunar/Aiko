"""Turn-local guidance for questions about the order of narrated events."""
import re

from app.core.conversation.story_reply import refers_to_story
from app.core.conversation.practical_answer import PracticalAnswer


_TIME = re.compile(
    r"\b(?:before|after|earlier|later|beforehand|afterwards?|by then|first|"
    r"back (?:up|then)|by the time|had already|on (?:the|my|our) way)\b", re.I,
)
_EVENT = re.compile(
    r"\b(?:handed|gave|lent|returned|took|got|had|left|moved|put|tucked|"
    r"arrived|went|found|sent|received|changed|started|stopped|finished|was|were)\b", re.I,
)
_STATE_QUESTION = re.compile(
    r"\b(?:who (?:last |still )?(?:has|had|holds|held|got|ended up)|where (?:was|were|is|are|did)|"
    r"what (?:happened|came) (?:first|next|before|after)|which [^?.!]{1,35} "
    r"(?:first|did (?:I|we|they) have))\b", re.I,
)
TIMELINE_GUIDANCE = (
    "Put the reported events in time order. Answer for the moment asked about. "
    "Give just the holder for 'who' or the stated location for 'where', in one short "
    "sentence. A return establishes who got the object, never where they put it afterward. "
    "Skip scene-setting and extra details."
)


def timeline_guidance(text, previous=""):
    questions = list(_STATE_QUESTION.finditer(text or ""))
    if not questions:
        return ""
    question = questions[-1]
    account = text[:question.start()]
    earlier = (_TIME.search(previous) and _EVENT.search(previous)
               and (refers_to_story(text, previous) or re.fullmatch(
                r"\s*(?:who (?:has|had) it|where (?:was|is) it)(?: (?:now|then))?\?\s*",
                text, re.I)))
    if not earlier and not (_TIME.search(account) and _EVENT.search(account)):
        return ""
    if re.search(r"\b(?:forget|forgot|can't remember|don't know) (?:which|the order)\b",
                 account + " " + previous, re.I):
        return TIMELINE_GUIDANCE + (" The narrator says the order is unknown. If the answer "
                                    "depends on it, explain that gap directly.")
    if re.search(r"\b(?:meant|intended|planned) to\b", account + " " + previous, re.I):
        return TIMELINE_GUIDANCE + (" If a handoff was only intended, phrase the answer as "
                                    "the last known holder. Its later completion is unknown.")
    if re.search(r"\bon (?:my|our|the) way to\b", text[question.start():], re.I):
        return TIMELINE_GUIDANCE + " The journey to an event precedes what happened there."
    return TIMELINE_GUIDANCE


_HOLDER = re.compile(r"who (?:has|holds|last had) (?:the |my )?([\w -]+?)(?: now)?\?", re.I)
_JOURNEY = re.compile(r"where was (?:the |my )?([\w -]+) on my way to ([\w -]+)\?", re.I)
_STAMP = re.compile(r"(before leaving for|before|at|after) ([\w -]+?) (I |[A-Z][\w'-]* )(.+)")
_ACTION = re.compile(r"(?:(?:planned|intended|meant) to (?:hand|give|lend)|handed|gave|lent) "
                     r"([A-Z][\w'-]*) (?:the|my) ([\w -]+)")
_STOW = re.compile(r"(?:put|tucked|placed) (?:the|my) ([\w -]+?) (in(?:side)?|on) (my [\w -]+)")


def _possession(text, previous):
    # Compare event times within one narrated occasion. Every assertion must
    # parse before a computed answer can replace ordinary conversation.
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    question = parts.pop()
    holder, journey = _HOLDER.fullmatch(question), _JOURNEY.fullmatch(question)
    if not (holder or journey):
        return ""
    if previous and (not parts or re.search(r"\b(?:it|back)\b", " ".join(parts), re.I)):
        parts = re.split(r"(?<=[.!?])\s+|\n+", previous.strip()) + parts
    events = {}
    occasion = item = ""
    for part in parts:
        clause = part.strip().rstrip(".")
        if clause.casefold() in ("oh, i should back up", "i should back up"):
            continue
        if re.search(r"\b(?:if|unless|except|and|but|maybe|or|until|instead|only if)\b",
                     clause, re.I):
            return ""
        stamp = _STAMP.fullmatch(clause[:1].lower() + clause[1:])
        if not stamp:
            return ""
        when, scene, actor, verb = stamp.groups()
        rank = {"before leaving for": 0, "before": 1, "at": 2, "after": 3}[when]
        if (rank in events or (rank < 2 and events.keys() & {0, 1})
                or (occasion and scene != occasion)):
            return ""
        occasion = scene
        move, stow = _ACTION.fullmatch(verb), _STOW.fullmatch(verb)
        if actor == "I " and move:
            name, obj = move.groups()
            event = ("plan" if " to " in verb else "give", name)
        elif actor == "I " and stow:
            obj, prep, place = stow.groups()
            event = ("stow", prep + " " + place.replace("my ", "your ", 1))
        elif re.fullmatch(r"(?:handed|gave) it back(?: to me)?|returned it to me", verb):
            obj = ""
            event = ("return", actor.strip())
        else:
            return ""
        if obj and item and item != obj:
            return ""
        item = obj or item
        events[rank] = event
    asked = (holder or journey)[1].lower()
    if not item or (asked != "it" and not (item == asked or item.endswith(" " + asked))):
        return ""
    if journey:
        if journey[2].lower() == occasion and 0 in events and events[0][0] == "stow":
            return f"Last you mentioned, it was {events[0][1]} on the way there."
        return ""
    owner = ""
    planned = False
    for kind, value in (events[k] for k in sorted(events)):
        if kind == "return" and owner != value:
            return ""
        if kind == "plan":
            planned = True
        else:
            owner = value if kind == "give" else "you"
            planned = False
    if not owner:
        return ""
    if planned:
        return f"Last you mentioned, it was with {owner}. The handoff was still a plan."
    return "You have it back." if owner == "you" and kind == "return" else f"It's with {owner}."


def check_timeline(text, previous=""):
    guide = timeline_guidance(text, previous)
    return PracticalAnswer(guide, _possession(text, previous)) if guide else PracticalAnswer()
