"""Intersect explicitly offered choices for the people included in a group decision."""
from dataclasses import dataclass, field
from itertools import islice
import re

from app.core.conversation.practical_answer import PracticalAnswer


_WEEK = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
_DAYS = {form.lower(): day for day in _WEEK for form in (day, day[:3])}
_GUIDE = (
    "Check each option against every person included in the question. Separate confirmed "
    "availability from an offered change. Pairwise overlap alone does not give the whole "
    "group an option. Report actual common choices or say whose confirmation is needed."
)
_NAME = r"[A-Z][a-zA-Z'-]{0,30}"
_LIST = re.compile(rf"({_NAME})(?: (can rehearse|can do|can meet|can watch|is available|"
                   rf"am available)(?: on)? |:\s*)(.+)")
_CHANGE = re.compile(
    rf"({_NAME}) (might (?:be able to )?(?:switch to|do|watch)|can switch to|"
    r"confirmed(?: (?:she|he|they|I) can (?:do|watch|meet))?|can also (?:do|watch)|"
    r"can no longer (?:do|watch)|can't (?:do|watch)) (.+)"
)
_ASK = re.compile(
    r"(?:we all overlap with someone, so )?which (nights?|days?|films?|movies?) "
    r"(?:works? for (?:just )?([^?.!]+)|should (?:we|I) (?:book|pick|choose))\?", re.I,
)


@dataclass
class Choices:
    name: str
    confirmed: set[str] = field(default_factory=set)
    possible: set[str] = field(default_factory=set)


def _options(raw, films):
    if films and '"' in raw:
        # Delimiters inside a quoted film title belong to the title.
        title = r'"([\w][\w ,:!?&’\'-]{0,79})"'
        if not re.fullmatch(rf"{title}(?:(?:,\s*(?:(?:or|and)\s+)?| (?:or|and) ){title})*", raw):
            return []
        return re.findall(title, raw)
    if films and re.search(r"(?<!,)\s+and\s+", raw, re.I):
        return []
    words = re.split(r",\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+", raw)
    if not films:
        return [_DAYS[w.lower()] for w in words] if all(w.lower() in _DAYS for w in words) else []
    # Bare titles only. A qualifier or another action belongs with the model.
    title = r"[A-Z][\w'-]*(?: (?:[A-Z][\w'-]*|of|the|in|on|at|to)){0,5}"
    if not all(re.fullmatch(title, w) and w.lower() not in _DAYS for w in words):
        return []
    return words


def _record(statement, people, labels, films):
    change = _CHANGE.fullmatch(statement)
    row = change or _LIST.fullmatch(statement)
    if not row:
        return False
    who, verb, raw = row.groups()
    if who.lower() in {"they", "she", "he", "we", "you", "everyone", "someone"}:
        return False
    if verb and verb.startswith("confirmed ") and (who == "I") != verb.startswith("confirmed I "):
        return False
    if (films and verb in {"can rehearse", "can meet"} or not films and verb == "can watch"):
        return False
    items = _options(raw, films)
    if not items:
        return False
    key = who.casefold()
    if (change and key not in people) or (not change and key in people):
        return False
    person = people.setdefault(key, Choices("you" if key == "i" else who))
    options = {i.casefold() for i in items}
    labels.update((i.casefold(), i) for i in items)
    if change and verb.startswith(("might", "can switch")):
        person.possible.update(options)
    elif change and verb.startswith(("can no longer", "can't")):
        person.possible.difference_update(options)
        person.confirmed.difference_update(options)
    else:
        person.confirmed.update(options)
        person.possible.difference_update(options)
    return True


def _and(items):
    if len(items) < 3:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _compare(people, labels, scope, films):
    group = list(people.values())
    if scope and re.search(r"\bus\b", scope, re.I) and "i" not in people:
        return "I have the others' options. What works for you?"
    count = re.fullmatch(r"(?:all |the )?(two|three|four|five|six) of (us|them)", scope or "", re.I)
    if count:
        size = ("two", "three", "four", "five", "six").index(count[1].lower()) + 2
        if size != len(group) or (count[2].lower() == "us") != ("i" in people):
            return ""
    elif scope and scope.casefold() in {"both of them", "both of us"}:
        if len(group) != 2 or ("us" in scope.casefold()) != ("i" in people):
            return ""
    elif scope and scope.casefold() not in {"everyone", "us all", "all of us", "the whole group"}:
        names = re.split(r",\s*(?:and\s+)?|\s+and\s+", scope)
        if not all(re.fullmatch(_NAME, n) or n.lower() == "me" for n in names):
            return ""
        keys = ["i" if n.lower() == "me" else n.casefold() for n in names]
        missing = [("you" if k == "i" else n) for k, n in zip(keys, names, strict=True)
                   if k not in people]
        if missing:
            return f"I'd need the options for {_and(missing)} to answer for that group."
        group = [people[k] for k in dict.fromkeys(keys)]
    confirmed = set.intersection(*(p.confirmed for p in group))
    possible = set.intersection(*(p.confirmed | p.possible for p in group)) - confirmed
    order = list(labels) if films else [day.lower() for day in _WEEK]
    result = []
    if confirmed:
        choices = [labels[o] for o in order if o in confirmed]
        verb = "works" if len(choices) == 1 else "work"
        result.append(f"{_and(choices)} {verb} for {_and([p.name for p in group])}.")
    else:
        what = "film" if films else "day"
        result.append(f"There's no confirmed {what} in common for {_and([p.name for p in group])}.")
    for o in order:
        if o in possible:
            needed = [p.name for p in group if o not in p.confirmed]
            result.append(f"{labels[o]} could work with confirmation from {_and(needed)}.")
    return " ".join(result)


def _clauses(text):
    return [c.strip().rstrip(".!") for c in re.split(r"(?<=[.!?])\s+|[;\n]+", text.strip())
            if c.strip()]


def check_group_options(text, recent_users=()):
    if not text:
        return PracticalAnswer()
    clauses = _clauses(text.replace("’", "'"))
    if not clauses:
        return PracticalAnswer()
    question = clauses.pop()
    ask = _ASK.fullmatch(question)
    if ask:
        films = ask[1].lower().startswith(("film", "movie"))
        scope = ask[2]
    elif re.fullmatch(r"When can we all (?:meet|rehearse)\?", question, re.I):
        films, scope = False, "all of us"
    elif re.fullmatch(r"What can we all watch\?", question, re.I):
        films, scope = True, "all of us"
    else:
        return PracticalAnswer()
    # Several people can be introduced in one turn and the rest in another.
    # Keep contiguous offers and updates together, including unparsed clauses.
    borrowed = False
    full_report = sum(bool(_LIST.fullmatch(c)) for c in clauses) >= 2
    for depth, prior in enumerate(islice(recent_users, 5), 1):
        earlier = _clauses(prior.replace("’", "'"))
        if earlier and _ASK.fullmatch(earlier[-1]):
            earlier.pop()
        if not earlier:
            continue
        if not any(_LIST.fullmatch(c) or _CHANGE.fullmatch(c) for c in earlier):
            break
        offers = [row for c in earlier if (row := _LIST.fullmatch(c))]
        if offers and all(_options(row[3], not films) for row in offers):
            break  # A preceding film choice cannot add members to a rehearsal.
        if full_report and len(offers) >= 2:
            return PracticalAnswer(_GUIDE)  # Two competing rosters.
        borrowed = True
        clauses[:0] = earlier
        full_report = full_report or len(offers) >= 2
        if depth == 5 and not full_report:
            return PracticalAnswer(_GUIDE)
    if sum(bool(_LIST.fullmatch(c)) for c in clauses) < 2:
        return PracticalAnswer()
    people, labels = {}, {}
    for statement in clauses:
        if not _record(statement, people, labels, films):
            return PracticalAnswer() if borrowed else PracticalAnswer(_GUIDE)
    return PracticalAnswer(_GUIDE, _compare(people, labels, scope, films))
