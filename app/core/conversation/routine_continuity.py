"""Trace the activity and the purpose of a shared routine through a change."""
import re

from app.core.conversation.perspective_sources import evidence, near, remarks, terms
from app.core.conversation.practical_answer import PracticalAnswer


_QUESTION = re.compile(
    r"\b(?:did we (?:lose|give up|keep)|have we (?:lost|kept)|what (?:became of|happened "
    r"to)|do we still have|what(?:['’]s| is) left of)\s+(?:our|the old)\s+(?P<name>[^?!.]{1,80})",
    re.I)
_ROUTINE = re.compile(r"\b(?:routines?|rituals?|traditions?|things?|habit[s]?|calls?|walks?|"
                      r"game[s]?|weekly|daily|morning[s]?|evening[s]?|weekends?|"
                      r"(?:mon|tues|wednes|thurs|fri|satur|sun)days?)\b", re.I)
_BRIDGE = re.compile(r"\b(?:instead|became|used to|switched|replaced|stopped|"
                     r"now we|we now|these days|still get|reminds? me|same way|anymore)\b", re.I)
_MEANING = re.compile(r"\b(?:miss|meant|about|purpose|why|matter\w*|only|just|still|"
                      r"rather|actually|guess|different|instead)\b", re.I)
_GENERIC = {"lose", "lost", "routine", "ritual", "tradition", "habit", "because", "each", "used",
            "instead", "stopped", "call", "weekly", "really", "every", "exchange", "started"}

_CONSIDERING = re.compile(r"\b(?:(?:i|we) (?:could|might|am considering|"
                          r"are considering)|(?:i['’]m|we['’]re) (?:considering|thinking of|"
                          r"thinking about)|should (?:i|we))\b", re.I)
_SAVING = re.compile(r"\b(?:sav(?:e|es|ing) (?:me |us )?(?:some |a lot of |an? |\d+ |"
                     r"[a-z]+ )?(?:time|hours?|minutes?|effort|money)|quicker|faster|easier|"
                     r"cheaper)\b", re.I)
_VALUE = re.compile(r"\b(?:only (?:time|chance)|(?:is|are|was) (?:usually |often )?"
                    r"(?:when|how)|gives? (?:me|us) (?:time|a chance)|"
                    r"means? (?:i|we) (?:get to|can))\b", re.I)
_DAY = re.compile(r"\b(mon|tues|wednes|thurs|fri|satur|sun)days?\b", re.I)
_CHANGED = re.compile(r"\b(?:instead|no longer|anymore|used to|we (?:now|switched)|"
                      r"we['’]ve started|we have started)\b", re.I)
_INCIDENTAL = {"hour", "minute", "time", "save", "saving", "effort", "money", "might",
               "would", "easier", "faster", "quicker", "cheaper", "considering", "thinking",
               "usually", "week", "next", "just", "chance", "only", "delivered", "online", "home"}

SHORTCUT_GUIDANCE = (
    "They are considering a shortcut. Their earlier words describe something they value about "
    "the activity. Would the shortcut affect that part? Whether it continues is unknown. "
    "If you're curious, ask one short question and give them room to answer. You can also "
    "simply acknowledge the saving. Let them decide which part matters to them."
)
ROUTINE_UPDATE_GUIDANCE = (
    "They described a new arrangement. If it preserves what mattered about the old routine, "
    "let the old concern go. Respond to how they spend that time now in your own words. "
    "The earlier activity's schedule belongs to that activity. Leave the new arrangement's "
    "day, frequency, and their decision about the shortcut open unless they supplied them."
)


def _activity_words(text):
    # Bring the errand and its place into the same vocabulary at retrieval time.
    text = re.sub(r"\b(?:groceries|grocery(?: (?:store|shop))?|supermarket|"
                  r"(?:the|our) (?:local )?shop)\b", "groceries", text, flags=re.I)
    return terms(_DAY.sub("", text)) - _INCIDENTAL


def check_shortcut(text, previous, db, owner, key, current_id):
    """Offer a remembered benefit only when the proposed shortcut touches its activity."""
    if "?" in text and not re.search(r"\bshould (?:i|we)\b", text, re.I):
        return PracticalAnswer()
    proposal = _CONSIDERING.search(text)
    if (not proposal and _CONSIDERING.search(previous) and _SAVING.search(previous)
            and _CHANGED.search(text) and re.search(r"\b(?:we|i|my|our)\b", text, re.I)):
        return PracticalAnswer(ROUTINE_UPDATE_GUIDANCE)
    if not proposal or not _SAVING.search(text):
        return PracticalAnswer()
    if _CHANGED.search(text) and re.search(r"\b(?:already|started|switched|no longer)\b",
                                           text, re.I):
        return PracticalAnswer()
    # A separate practical question or a decision they've already made takes precedence.
    if re.search(r"\b(?:how (?:do|can)|what time|already (?:did|ordered|decided))\b", text, re.I):
        return PracticalAnswer()
    activity = _activity_words(re.split(r"[.!?;]", text[proposal.end():], maxsplit=1)[0])
    if not activity:
        return PracticalAnswer()
    days = set(_DAY.findall(text.casefold()))
    changed_activities = set()
    rows = remarks(db, owner, key, current_id)
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if row["role"] != "user":
            continue
        content = row["content"]
        if _CHANGED.search(content):
            change = _activity_words(content)
            # An elliptical replacement can answer a question naming the routine.
            if (i and rows[i - 1]["role"] == "assistant" and near(rows[i - 1], row)
                    and rows[i - 1]["content"].rstrip().endswith("?")):
                change |= _activity_words(rows[i - 1]["content"])
            changed_activities.update(change)
            continue
        for clause in re.split(r"[.!?;](?!\s*(?:that['’]s|it['’]s|that is|it is)\b)|"
                               r"\bbut\b|,\s*and\s+", content, flags=re.I):
            value = _VALUE.search(clause)
            if not value or not re.search(r"\b(?:i|my|our|we|me|us)\b", clause, re.I):
                continue
            routine = _activity_words(clause[:value.start()])
            if not activity & routine:
                continue
            occasion = set(_DAY.findall(clause.casefold()))
            if days and occasion - days:
                continue
            if changed_activities & routine:
                return PracticalAnswer()
            return PracticalAnswer(SHORTCUT_GUIDANCE + "\n" + evidence([row]))
    return PracticalAnswer()


def check_routine(text, previous, db, owner, key, current_id):
    match = _QUESTION.search(text)
    if not match or not _ROUTINE.search(match["name"]):
        return PracticalAnswer()
    names = terms(match["name"])
    anchors = names - _GENERIC or names
    if not anchors:
        anchors = terms(previous)
    rows = remarks(db, owner, key, current_id)
    named = [r for r in rows if anchors & terms(r["content"])]
    transitions = [r for r in named if _BRIDGE.search(r["content"])]
    # A replacement can later be discussed by its own name. Only extend the search
    # from a dated remark actually connecting the named routine to a change.
    bridges = {}
    for row in transitions[-3:]:
        words = terms(row["content"]) - anchors - _GENERIC
        for word in words:
            bridges.setdefault(word, row["created_at"])
    selected = {r["message_id"]: r for r in [*named[:2], *transitions[-2:], *named[-3:]]}
    relevant = []
    for i, row in enumerate(rows):
        content = row["content"]
        linked = any(row["created_at"] >= bridges[w] for w in terms(content) & bridges.keys())
        neighbor = (i and rows[i - 1]["message_id"] in selected and near(rows[i - 1], row)
                    and rows[i - 1]["role"] != row["role"])
        if (linked or neighbor) and _MEANING.search(content):
            relevant.append(row)
    selected.update((r["message_id"], r) for r in relevant[-4:])
    if not selected:
        reply = "Which part of that routine do you mean? I don't have enough here to trace it."
        return PracticalAnswer(reply, reply)
    ordered = sorted(selected.values(), key=lambda r: (r["created_at"], r["message_id"]))
    if not any(_BRIDGE.search(r["content"]) or re.search(
            r"\b(?:still|ended|continued|no longer|gave up|lost)\b", r["content"], re.I)
               for r in ordered):
        last = ordered[-1]
        speaker = "You" if last["role"] == "user" else "I"
        reply = (f'{speaker} described it this way: “{last["content"]}” '
                 "I don't have a later account of what happened to it.")
        return PracticalAnswer(reply, reply)
    guidance = (
        "Answer about the named shared routine using these dated remarks as historical data. "
        "What did the two of you actually do before and after? Work out whether the later activity "
        "served the same purpose, a different one, or whether you can't tell. A similar schedule "
        "or object alone tells you little. When only part survived, lead with that part. "
        "Say what carried over and what fell away, as far as "
        "the evidence goes. Speaker=user is the person asking. Speaker=assistant is you. Respect "
        "each person's own account, including a later correction. Speak naturally and briefly.\n"
        + evidence(ordered))
    user = next((r for r in reversed(ordered) if r["role"] == "user"), None)
    aiko = next((r for r in reversed(ordered) if r["role"] == "assistant"), None)
    if (user and aiko and user["content"].lower().startswith("for me")
            and (_MEANING.search(aiko["content"]) or _BRIDGE.search(aiko["content"]))):
        reply = (f'I saw a connection: “{aiko["content"]}” You put it differently: '
                 f'“{user["content"]}”')
        return PracticalAnswer(guidance, reply)
    if user and transitions and re.search(r"\bi (?:still|miss)\b", user["content"], re.I):
        return PracticalAnswer(guidance, 'What sticks with me is how you described the change: '
                               f'“{user["content"]}”')
    if (user and transitions
            and re.search(r"\b(?:only|just|purely|solely)\b", user["content"], re.I)
            and re.search(r"\b(?:we stopped|no longer|we quit|gave up)\b", user["content"], re.I)):
        return PracticalAnswer(guidance, 'You explained what the new activity was for: '
                               f'“{user["content"]}”')
    return PracticalAnswer(guidance)
