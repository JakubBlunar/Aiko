"""Turn-local feasibility guidance and arithmetic for two stated boundaries."""
from decimal import Decimal
import re

from app.core.conversation.practical_answer import PracticalAnswer


_ASK = re.compile(
    r"\b(?:smallest|minimum|least|fit it|fit (?:this|that|the)|enough|possible|feasible|"
    r"can I|could I|would (?:that|it)|do I need|how much (?:earlier|later|more)|"
    r"will (?:it|that) (?:work|fit))\b", re.I,
)
_DURATION = re.compile(
    r"\b(?:(\d+(?:\.\d+)?)\s*[- ]?\s*(minutes?|mins?|hours?|hrs?)\b|"
    r"(?:an?\s+)?(hour)[- ]long\b)", re.I,
)
_CLOCK = re.compile(
    r"(?<![\d:])(?:(\d{1,2}):(\d{2})\s*(am|pm)?\b|(\d{1,2})\s*(am|pm)\b|"
    r"(?<=at )(\d{1,2})\b(?![.:]\d|\s*(?:minutes?|hours?|cm|mm)\b))", re.I,
)
_RESOURCE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(mm|cm|metres?|meters?|inches|litres?|liters?|"
    r"sheets?|pages?|slots?|ports?)\b", re.I,
)
_FOLLOW_UP = re.compile(
    r"\b(?:my arrival|the start|the finish|my departure|the deadline|how much earlier|"
    r"how much later|same (?:plan|setup)|what if I (?:arrive|leave))\b", re.I,
)
_GUIDANCE = (
    "Check the whole plan against the user's stated limits. Answer whether it fits as stated. "
    "If it already fits, say so. For a shortfall, give its size and the smallest change the "
    "user allows that really closes it. Moving arrival helps an on-demand start; it cannot "
    "advance a fixed performance. Keep transit and setup time if supplied. Preserve continuous "
    "materials and equipment limits. If everything relevant is fixed, explain the conflict. "
    "If flexibility is unstated, label any alternative conditional. Never invent a replacement "
    "ride, spare material or permission. Answer briefly in your own words."
)


# The direct answer covers a small, closed set of relations. An unmatched
# clause (travel time, a second activity, a conditional permission...) leaves
# the complete request in ordinary chat.
_TIME_CLAUSES = {
    "arrival": r"I (?:can |will )?(?:arrive|get there) at @",
    "start": r"the [\w -]+? (?:starts|begins) at @(?: sharp)?",
    "available": r"I can start at @",
    "deadline": (r"(?:(?:I )?(?:have to|must|need to) (?:leave at|finish by)"
                 r"|the last bus leaves at) @"),
    "on_demand": r"(?:the [\w -]+? )?starts (?:whenever|when|as soon as) I arrive",
    "duration": r"(?:the [\w -]+? )?(?:runs for|takes|lasts) #",
    "earlier": r"I can (?:arrive earlier|change my arrival)",
    "later": r"I can (?:leave later|change (?:when I leave|my departure))",
    "fixed": r"(?:neither time|neither one) can (?:move|change)|both times are fixed",
}
_TIME_CLAUSES = {key: re.compile(value, re.I) for key, value in _TIME_CLAUSES.items()}
_DIRECT_ASK = re.compile(
    r"(?:what(?:'s| is) the (?:smallest|minimum) change(?: that works)?|"
    r"would that let me (?:see|finish) (?:it all|the whole thing)|can I fit (?:it|this) all in|"
    r"do I need to change anything|how much earlier would I need to be|"
    r"can I (?:see|finish) the whole [\w -]+|will (?:it|that|this) fit|"
    r"(?:is there|do I have) enough (?:time|room|material))\?", re.I,
)


def _clock_label(value, period):
    hour, minute = divmod(value, 60)
    if period:
        return f"{hour % 12 or 12}:{minute:02} {'pm' if hour >= 12 else 'am'}"
    return f"{hour}:{minute:02}"


def _schedule_reply(statements, question):
    # Treat one duration in the question ("whole hour-long show") as the
    # required duration too. A bare arrival never establishes an on-demand start.
    durations = list(_DURATION.finditer(statements + " " + question))
    if len(durations) != 1:
        return ""
    duration = durations[0]
    unit = (duration[2] or duration[3]).lower()
    minutes = Decimal(duration[1] or 1) * (60 if unit.startswith("h") else 1)
    if not 0 < minutes < 1440 or minutes != int(minutes):
        return ""
    needed = int(minutes)
    roles = {}
    periods = set()
    clauses = re.split(r"[.!;\n]+|\s+and\s+", _DURATION.sub("#", statements), flags=re.I)
    for clause in filter(None, (c.strip() for c in clauses)):
        shape = _CLOCK.sub("@", clause)
        role = next((key for key, pattern in _TIME_CLAUSES.items() if pattern.fullmatch(shape)), "")
        if not role or role in roles:
            return ""
        match = _CLOCK.search(clause)
        if match:
            stamp = _clock_minutes(match)
            if stamp is None:
                return ""
            roles[role], period = stamp
            periods.add(bool(period))
        else:
            roles[role] = True
    if len(periods) != 1 or "deadline" not in roles:
        return ""
    if "fixed" in roles and ("earlier" in roles or "later" in roles):
        return ""
    arrival, start = roles.get("arrival"), roles.get("start")
    if "available" in roles:
        if start is not None or arrival is not None or "on_demand" in roles:
            return ""
        arrival = roles["available"]
    if ("on_demand" in roles and start is not None
            or start is None and arrival is None
            or arrival is not None and start is not None and arrival > start):
        return ""
    begin = start if start is not None else arrival
    end = roles["deadline"]
    if not 0 <= end - begin < 720 or begin + needed >= 1440:
        return ""
    shortfall = needed - (end - begin)
    finish = _clock_label(begin + needed, True in periods)
    if shortfall <= 0:
        if start is None and "on_demand" not in roles and "available" not in roles:
            return ""
        spare = -shortfall
        margin = f" with {spare} minutes to spare" if spare else " exactly"
        return f"It fits{margin}. Starting at {_clock_label(begin, True in periods)}, " \
               f"you'd finish at {finish}."
    why = f"You have {end - begin} minutes and need {needed}, so you're {shortfall} minutes short."
    options = []
    if "earlier" in roles and "on_demand" in roles and end >= needed:
        options.append(f"arrive {shortfall} minutes earlier, at "
                       f"{_clock_label(end - needed, True in periods)}")
    if "later" in roles:
        options.append(f"leave {shortfall} minutes later, at {finish}")
    if options:
        return why + " You could " + " or ".join(options) + "."
    if "fixed" in roles:
        return why + " The full activity won't fit within those times."
    if start is not None:
        return why + f" The fixed start puts the finish at {finish}. Arriving earlier won't help."
    if "on_demand" not in roles:
        return why + " An earlier arrival would help only if you could start then."
    return why + " The full activity won't fit within those times."


def _size_reply(statements):
    # One-piece length and maximum intake width are capacities. Joining a
    # second piece or cutting the original would change the requested outcome.
    amount = r"(\d+(?:\.\d+)?)\s*(mm|cm|inches|meters?|metres?)"
    material = re.fullmatch(
        rf"I (?:have|can use) {amount} of [\w -]+\. I need (?:one|a) (?:unbroken|continuous) "
        rf"(?:length|piece|strip) of {amount}(?:\. (I can (?:get|buy|use) a longer piece))?\.?",
        statements, re.I)
    equipment = re.fullmatch(
        rf"(?:The only ([\w-]+) available|My ([\w-]+)) (?:accepts|fits) [\w -]+ up to {amount} "
        rf"wide\. (?:My|The) [\w -]+ is {amount} wide and must stay intact"
        rf"(?:\. I can use (?:a different|another) ([\w-]+))?\.?", statements, re.I)
    if material:
        have, unit, need, other_unit, permitted = material.groups()
        target = f"one continuous piece of at least {need} {unit}"
    elif equipment:
        only, owned, have, unit, need, other_unit, permitted = equipment.groups()
        item = only or owned
        if permitted and permitted.casefold() != item.casefold():
            return ""
        target = f"a {item} that accepts at least {need} {unit} of width"
    else:
        return ""
    if unit.casefold() != other_unit.casefold() or min(Decimal(need), Decimal(have)) <= 0:
        return ""
    gap = Decimal(need) - Decimal(have)
    if gap <= 0:
        return f"The {have} {unit} you have covers the required {need} {unit}."
    return (f"You're {gap:f} {unit} short. Use {target}." if permitted
            else f"You're {gap:f} {unit} short. You'd need {target}.")


def _clock_minutes(match):
    hour = int(match[1] or match[4] or match[6])
    minute = int(match[2] or 0)
    period = (match[3] or match[5] or "").lower()
    if minute >= 60 or hour >= 24 or (period and not 1 <= hour <= 12):
        return None
    if period:
        hour = hour % 12 + (12 if period == "pm" else 0)
    return hour * 60 + minute, period


def _time_span(text, durations):
    if len(durations) != 1:
        return ""
    duration = durations[0]
    unit = (duration[2] or duration[3]).lower()
    needed = Decimal(duration[1] or 1) * (60 if unit.startswith("h") else 1)
    times = [_clock_minutes(m) for m in _CLOCK.finditer(text)]
    if not times or any(t is None for t in times):
        return ""
    # Mixed absolute and unqualified clocks can be on different half-days.
    if len({bool(period) for _, period in times}) != 1:
        return ""
    unique = list(dict.fromkeys(value for value, _ in times))
    if len(unique) != 2 or unique[1] <= unique[0]:
        return ""
    span = unique[1] - unique[0]
    return (f"The two clock readings are {span} minutes apart. The stated duration is "
            f"{needed:f} minutes. Duration minus that interval is {needed - span:f} minutes. "
            "Identify what each clock refers to before using the interval.")


def check_feasibility(text, recent_user_turns=()):
    if not text or not _ASK.search(text):
        return PracticalAnswer()
    source = text
    if _FOLLOW_UP.search(text):
        previous = next(iter(recent_user_turns), "")
        if previous and not _DURATION.search(text) and not _RESOURCE.search(text):
            source = previous + "\n" + text
    durations = list(_DURATION.finditer(source))
    amounts = _RESOURCE.findall(source)
    if durations and _CLOCK.search(source):
        facts = _time_span(source, durations)
    elif len(amounts) >= 2:
        facts = ""
        if len(amounts) == 2 and amounts[0][1].casefold() == amounts[1][1].casefold():
            gap = abs(Decimal(amounts[0][0]) - Decimal(amounts[1][0]))
            facts = f"The two stated amounts differ by {gap:f} {amounts[0][1]}."
    else:
        return PracticalAnswer()
    guidance = "Feasibility of the current plan. " + (facts + " " if facts else "") + _GUIDANCE
    parts = re.split(r"(?<=[.!?])\s+|\n+", source.strip())
    question = parts[-1]
    reply = ""
    if _DIRECT_ASK.fullmatch(question):
        statements = " ".join(parts[:-1])
        reply = (_schedule_reply(statements, question) if durations
                 else _size_reply(statements))
    return PracticalAnswer(guidance, reply)
