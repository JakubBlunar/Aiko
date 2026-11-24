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
    return PracticalAnswer(guidance)
