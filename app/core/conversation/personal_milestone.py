"""Connect a concrete first-person report with an earlier goal in the user's own words."""
import json
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

_DOWNPLAY = re.compile(r"\bi (?:barely did anything|hardly (?:did|had to do) anything|"
                       r"did(?:n['’]t| not) (?:have to )?do (?:much|anything)|"
                       r"had (?:almost )?nothing to do)\b", re.I)
_PREPARATION = re.compile(r"\b(?:in (?:case|advance)|ahead of|beforehand|so (?:that |"
                          r"\w+ (?:could|would|wouldn['’]t))|because|to (?:avoid|prevent|"
                          r"help|make sure))\b", re.I)
# A previous intention or somebody else's work cannot support personal credit.
_DID_WORK = re.compile(r"^i(?:['’]ve| have| had)? (?:already )?(?:put (?!off\b)|set up|"
                       r"made|wrote|left|sent|bought|brought|built|laid|packed|checked|"
                       r"prepared|arranged|booked|labelled|labeled|marked|printed|"
                       r"tested|installed|cleared|cleaned|organised|organized|moved)\b", re.I)
_REVISION = re.compile(r"\b(?:actually|turns out|instead|never|didn['’]t|did not|but|"
                       r"except|although|ended up|after all)\b", re.I)
_BACKGROUND = {"barely", "hardly", "almost", "anything", "nothing", "everyone", "everybody",
               "people", "tonight", "yesterday", "today", "much", "evening", "work", "found",
               "room", "went", "smoothly", "community", "last", "night"}


def check_preparation(text, previous_user, db, owner, key, current_id):
    """Offer original context when someone discounts their work after a quiet outcome."""
    if "?" in text:
        return PracticalAnswer()
    if not _DOWNPLAY.search(text):
        if _DOWNPLAY.search(previous_user) and re.match(
                r"\s*(?:actually|turns out|it turns out|in fact)\b", text, re.I):
            return PracticalAnswer(
                "Their latest observation can replace any earlier guess. They reported what "
                "happened. Reporting it gives them no special role in making it happen. A "
                "brief reaction to their discovery is enough. Stick to the people and actions "
                "in their latest message.")
        return PracticalAnswer()

    subjects = terms(_DOWNPLAY.sub("", text)) - _BACKGROUND
    if not subjects and re.search(r"\b(?:back from|finished|wrapped up)\b", previous_user, re.I):
        subjects = terms(previous_user) - _BACKGROUND - {"finished", "wrapped"}
    # Across sessions a generic quiet outcome is not enough to identify the occasion.
    # The ordinary chat history remains available even without a named archival match.
    if subjects:
        for row in reversed(remarks(db, owner, key, current_id)):
            if row["role"] != "user" or not subjects & terms(row["content"]):
                continue
            source = row["content"].strip()
            if _REVISION.search(source):
                break
            purpose = _PREPARATION.search(source)
            if purpose and _DID_WORK.match(source):
                # Quote the action on its own. Its intended effect is especially easy
                # to misread as an observed result when placed beside a later update.
                action = source[:purpose.start()].rstrip(" ,")
                return PracticalAnswer(
                    "This excerpt is something they already did before the occasion. If you "
                    "bring it up, recognize the effort they put in then. How it turned out "
                    "for other people is unknown. One warm sentence is plenty.\n"
                    "Their words from the earlier conversation: " + json.dumps(
                        action, ensure_ascii=False))
            if purpose:
                break
    return PracticalAnswer()


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
