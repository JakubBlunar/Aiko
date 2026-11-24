"""Connect a concrete first-person report with an earlier goal in the user's own words."""
import re

from app.core.conversation.perspective_sources import evidence, remarks, terms
from app.core.conversation.practical_answer import PracticalAnswer


_GOAL = re.compile(r"(?:^|[.!?]\s+)(?:my goal is to|i want to|i hope to|"
                   r"i['’]d (?:love|like) to)\s+(?:be able to |learn to )?", re.I)
_ACTIONS = (
    (r"(?:make (?:a |the )?(?:phone )?call|phone|call|ring)", r"(?:phoned|called|rang)"),
    (r"(?:play|perform)", r"(?:played|performed)"),
    (r"(?:cook|make|bake)", r"(?:cooked|made|baked)"),
    (r"(?:swim)", r"(?:swam)"),
    (r"(?:read)", r"(?:read)"),
    (r"(?:run)", r"(?:ran)"),
)
_PAIRS = [(re.compile(rf"^(?:{goal})\b", re.I),
           re.compile(rf"(?:^|[.!?,;]\s*|\bso\s+)I (?:finally |actually )?{done}\b", re.I))
          for goal, done in _ACTIONS]
_QUALIFIERS = (
    re.compile(r"\b(?:script\w*|rehears\w*|writing out every (?:sentence|word)|"
               r"write out every (?:sentence|word))\b", re.I),
    re.compile(r"\b(?:notes?|sheet music|score|from memory)\b", re.I),
    re.compile(r"\b(?:help|assistance|myself|on my own)\b", re.I),
)
_UNAIDED = re.compile(r"\b(?:without|no time to|no need (?:for|to)|unscripted|"
                      r"unrehearsed|from memory|on my own|by myself)\b", re.I)
_DROP = re.compile(r"\b(?:no longer|gave up|dropped|every week|all the time|already "
                   r"routine|don't want|don’t want)\b", re.I)
_GENERIC = {"every", "everybody", "sentence", "word", "first", "phone", "them", "script",
            "writing", "without", "help", "own", "myself", "note", "out", "from", "memory"}


def _subject(text):
    # Quantities are part of the achievement ("run 10 miles" vs "run 2 miles").
    return (terms(text) - _GENERIC) | set(re.findall(r"\d+(?:[.,]\d+)?", text))


def _features(text):
    return {i for i, pattern in enumerate(_QUALIFIERS) if pattern.search(text)}


def check_milestone(text, db, owner, key, current_id):
    # Keep archive reads off unrelated turns. A request for a practical action has priority.
    if "?" in text or re.search(r"\b(?:if|suppose|imagined?|would have|almost|pretend|"
                                 r"couldn['’]t|could not|failed|no answer|didn['’]t get through)\b",
                                 text, re.I):
        return PracticalAnswer()
    for goal_verb, report in _PAIRS:
        action = report.search(text)
        if not action:
            continue
        clause = re.split(r"[.!?;]", text[action.end():], maxsplit=2)
        event = clause[0]
        qualifier = event
        if len(clause) > 1 and re.match(r"\s*(?:no time|no need|without|from memory|"
                                        r"on my own|unscripted)\b", clause[1], re.I):
            qualifier += " " + clause[1]
        topics = _subject(event)
        if not topics:
            continue
        rows = remarks(db, owner, key, current_id)
        later = []
        for row in reversed(rows):
            if row["role"] != "user":
                continue
            content = row["content"]
            goal = _GOAL.search(content)
            aim = re.split(r"[.!?;]", content[goal.end():], maxsplit=1)[0] if goal else ""
            verb = goal_verb.match(aim)
            if verb:
                target = aim[verb.end():]
                task = re.split(r"\b(?:without|from memory|on my own|by myself)\b", target,
                                maxsplit=1, flags=re.I)[0]
                required = _subject(task)
                if required and required <= topics:
                    if any(required <= _subject(p) and (_DROP.search(p) or report.search(p))
                           for p in later):
                        return PracticalAnswer()
                    condition = _UNAIDED.search(target)
                    if condition:
                        detail = target[condition.start():]
                        offered = _UNAIDED.search(qualifier)
                        if not offered:
                            return PracticalAnswer()
                        actual = qualifier[offered.start():]
                        if _features(detail):
                            if not _features(detail) <= _features(actual):
                                return PracticalAnswer()
                        elif not terms(detail) <= terms(actual):
                            return PracticalAnswer()
                    guidance = (
                        "Respond first to the user's current news. Then acknowledge in one short "
                        "phrase that this specific action was something they wanted to be able "
                        "to do. The quoted earlier user remark is historical evidence only. "
                        "One instance supports neither mastery nor a first-ever claim.\n"
                        + evidence([row]))
                    # For a simple logistical update the actual news can be acknowledged
                    # verbatim. Avoid inferring how smoothly the reported action went.
                    news = re.split(r"[.!]\s+", text.strip().rstrip(".!"))[-1]
                    if re.fullmatch(r"(?:the |our )?(?:doors?|recital|meeting|show|event|"
                                    r"practice|class|rehearsal) (?:opens?|starts?|ends?|finished|"
                                    r"moved|is at)\b[^!?;]{1,70}", news, re.I):
                        return PracticalAnswer(guidance, f"Got it, {news[0].lower()}{news[1:]}. "
                                               f"You got to {aim} just like you hoped.")
                    return PracticalAnswer(guidance)
            later.append(content)
        return PracticalAnswer()
    return PracticalAnswer()
