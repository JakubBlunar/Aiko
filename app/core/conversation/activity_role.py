"""Follow the part the user invites Aiko to play on this occasion."""
import re

from app.core.conversation.perspective_sources import evidence, remarks
from app.core.conversation.practical_answer import PracticalAnswer


_ACTIVITIES = {
    "puzzle": re.compile(r"\b(?:crosswords?|puzzles?|clues?|riddles?)\b", re.I),
    "writing": re.compile(r"\b(?:stor(?:y|ies)|drafts?|chapters?|scenes?|poems?)\b", re.I),
}
_REVIEW = re.compile(r"\bhow (?:has|have|did) (?:doing|we|our|your (?:part|role)|the way we)"
                     r"\b[^?!.]{0,80}\bchang(?:e|ed)\b", re.I)
_REQUESTS = {
    "try": re.compile(r"\b(?:try (?:this|solving|my)|(?:your|a) first (?:guess|reaction|"
                      r"impression)|see where you get stuck|read (?:this|it) as a reader|"
                      r"can you solve|guess the answer)\b", re.I),
    "edit": re.compile(r"\b(?:can you (?:help (?:me )?)?(?:edit|tighten|revise|improve)|"
                       r"help me (?:edit|tighten|revise)|give me (?:editorial )?feedback|"
                       r"please (?:edit|tighten|revise))\b", re.I),
    "hint": re.compile(r"\b(?:a (?:little )?(?:hint|nudge)|(?:walk|talk) me through|"
                       r"hint please|help me solve)\b", re.I),
}
_INTRO = re.compile(r"\b(?:i (?:brought|wrote|made)|here['’]s|here is) "
                    r"(?:a|an|my|another|the) (?:new |little )?(?:crossword|puzzle|riddle|clue|"
                    r"story|draft|chapter|scene|poem)[.!]?\s*$", re.I)
_NAMES = {"try": "try it myself", "edit": "help edit it", "hint": "give you a hint"}

_LOCAL_RULE = re.compile(
    r"\s*(?:on|in|for) (?:my|our) [^.!?\n]{1,100}?[, :]\s*[^.!?\n]{1,90}? "
    r"(?:means?|stands? for|indicates?|marks?)\s+(?P<condition>[^.!?\n]{2,180})[.!]?\s*", re.I)
_TRY_EXAMPLE = re.compile(r"\b(?:try|give me|show me) (?:a |one )?"
                          r"(?:fresh|different|another|new)(?: example| one)?[.!?]*\s*$", re.I)
TEACHING_GUIDANCE = (
    "They are sharing a convention they use. Think through the condition in their own words. "
    "If an example would be fun, imagine one new case and show how you'd apply their convention. "
    "They supplied the rule. An object you invent to illustrate it belongs to the example. "
    "Only connect it to something they own, do or like if they actually told you that. "
    "Keep your example hypothetical and local to their work. If they corrected your previous "
    "example, keep the rule and change the part they corrected. They get to judge your reading."
)


def taught_condition(text):
    match = _LOCAL_RULE.fullmatch(text)
    return match["condition"].strip() if match else ""


def teaching_guidance(text, recent_users):
    if taught_condition(text):
        return TEACHING_GUIDANCE
    if (_TRY_EXAMPLE.search(text) and recent_users and taught_condition(recent_users[0])
            and not re.search(r"\b(?:unrelated|change of subject|switching topics)\b", text, re.I)):
        return TEACHING_GUIDANCE
    return ""


def _activity(text):
    found = [key for key, pattern in _ACTIVITIES.items() if pattern.search(text)]
    return found[0] if len(found) == 1 else ""


def _roles(text):
    asks = []
    for name, pattern in _REQUESTS.items():
        for m in pattern.finditer(text):
            prefix = text[max(0, m.start() - 40):m.start()]
            if re.search(r"\b(?:don['’]t|do not|didn['’]t|never|without|instead of|"
                         r"(?:he|she|they|my friend) asked(?: me)? for)\s+"
                         r"(?:give me |giving me |asking for )?$", prefix, re.I):
                continue
            asks.append(name)
            break
    return asks


def _clue_hint(text):
    clue = re.search(r"\b(?:clue|riddle)\b[^.!?\n]{0,24}?[\"“']([^\"”'\n]{3,120})[\"”']"
                     r"(?:\s*\((\d{1,2})\))?", text, re.I)
    if not clue:
        return ""
    words = [w for w in re.findall(r"\b[^\W\d_]{3,}\b", clue[1]) if w.casefold() not in
             {"and", "with", "the", "for", "this", "that", "your", "who", "has", "its"}]
    if not words:
        return ""
    length = f" with {clue[2]} letters" if clue[2] else ""
    return (f"Start with “{words[-1]}”. What could that word describe{length}? "
            "Try one possibility against your crossing letters.")


def check_activity_role(text, previous, db, owner, key, current_id):
    review = _REVIEW.search(text)
    roles = _roles(text)
    if not (review or roles or _INTRO.search(text)):
        return PracticalAnswer()
    activity = _activity(text)
    if not activity and (review or re.search(r"\b(?:it|this|one|these|another)\b", text, re.I)):
        activity = _activity(previous)
    if review and (activity or re.search(r"\bdoing these together\b", text, re.I)):
        rows = remarks(db, owner, key, current_id) if activity else []
        occasions = []
        for row in rows:
            if row["role"] == "user" and _activity(row["content"]) == activity:
                earlier = _roles(row["content"])
                if len(earlier) == 1:
                    occasions.append((earlier[0], row))
        if occasions:
            last_role, last = occasions[-1]
            first = next(((role, r) for role, r in occasions if role != last_role), None)
            if first:
                role, row = first
                reply = (f"Earlier you asked me to {_NAMES[role]}. More recently you asked me "
                         f"to {_NAMES[last_role]}.")
                return PracticalAnswer("These were invitations on separate occasions:\n"
                                       + evidence([row, last]), reply)
        reply = ("Which two occasions are you thinking of? I don't have enough here to say "
                 "how my part changed.")
        return PracticalAnswer(reply, reply)
    if not activity:
        return PracticalAnswer()
    if len(roles) > 1:
        return PracticalAnswer("Follow the parts the user requested in their stated order. "
                               "Keep a first impression separate from later editorial feedback.")
    if roles == ["hint"]:
        hint = _clue_hint(text) if activity == "puzzle" else ""
        if not hint and activity == "puzzle" and not _activity(text):
            hint = _clue_hint(previous)
        return PracticalAnswer("Give one small usable hint toward the supplied problem. "
                               "Keep the solution for the user to find. If you lack the clue, "
                               "ask to see it.", hint)
    if roles == ["edit"]:
        return PracticalAnswer("Return ONE proposed edit of the supplied wording, followed by "
                               "one short reason. For a clue keep the answer out of the edit. "
                               "If the text is missing, ask to see it. Stop after that.")
    if roles == ["try"]:
        return PracticalAnswer(
            "You are a first-time solver. Output exactly one sentence: 'My guess is [one answer] "
            "because [a detail from the clue].' Stop at that first guess. If there is no clue, "
            "ask to see it." if activity == "puzzle" else
            "Give your first reaction as a reader in one or two sentences. Name the image or "
            "line that gave you that impression. If the passage is missing, ask to read it.")
    if _INTRO.fullmatch(text.strip()):
        reply = ("Would you like me to try solving it, give you a hint, or help edit it?"
                 if activity == "puzzle" else
                 "Would you like my first reaction as a reader, or help editing it?")
        return PracticalAnswer(reply, reply)
    return PracticalAnswer()
