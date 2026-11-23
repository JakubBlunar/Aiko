"""Answer direct questions about Aiko's pursuits from the self-history projection."""

import re

from app.core.world.pursuit_activities import ALIASES, activity_mentions


_INTEREST = re.compile(
    r"(?:what(?: have you been|'ve you been| are you) (?:into|interested in)"
    r"(?: lately| recently)?|what have you been enjoying(?: lately| recently)?|"
    r"what (?:have you been choosing|do you keep coming back) to do(?: lately)?|"
    r"have your interests changed(?: lately)?)\??", re.I,
)
_NAMED = re.compile(r"are you still (.+?)\??", re.I)
_WHY = re.compile(r"(?:what do you (?:like|enjoy) about|why do you (?:like|enjoy)) "
                  r"(.+?)\??", re.I)
_ADDITIONAL = re.compile(r"\b(?:also|as well as|and \w+ing)\b", re.I)


def _reason(activity, record):
    reason = record.get("expressed_reason")
    return ("I remember saying " + reason["statement"].rstrip(".!") + "." if reason else
            f"I haven't pinned down what I like about {activity} yet.")


def pursuit_reply(text, history, provider):
    if provider is None:
        return ""
    text = (text or "").strip()
    why = _WHY.fullmatch(text)
    named = why or _NAMED.fullmatch(text)
    topic = None
    prior_reply = ""
    previous_named = None
    if named:
        referent = named[1].lower()
        if why and referent in {"it", "that"}:
            if len(history) < 2 or history[-1].role != "assistant" or history[-2].role != "user":
                return ""
            previous = history[-2].content.strip()
            previous_named = _NAMED.fullmatch(previous)
            if not (_INTEREST.fullmatch(previous) or
                    previous_named and previous_named[1].lower() in ALIASES):
                return ""
            prior_reply = history[-1].content.strip().strip('"“”')
            mentions = activity_mentions(prior_reply)
            if len(mentions) == 1:
                topic, = mentions
                if (_ADDITIONAL.search(prior_reply)
                        or previous_named and ALIASES[previous_named[1].lower()] != topic):
                    return ""
            elif len(mentions) != 2 or (not previous_named
                                        and "Lately I've been choosing " not in prior_reply):
                return ""
        else:
            topic = ALIASES.get(referent)
            if topic is None:
                return ""
    elif not _INTEREST.fullmatch(text):
        return ""

    facts = provider("aiko").get("choice_history", {})
    activities = facts.get("activities", {})
    if prior_reply and not topic:
        if previous_named:
            earlier = ALIASES[previous_named[1].lower()]
            account = activities.get(earlier, {}).get("recent_account", {})
            later = account.get("finding_activity")
            if account.get("statement") == prior_reply and later and later != earlier:
                reasons = (_reason(name, activities[name]) for name in (earlier, later))
                return " ".join(reasons)
        # A change mentions two subjects. Only resolve "it" when the previous
        # answer describes the recorded change and names its current activity.
        topic = next((name for name, row in activities.items()
                      if row.get("finding", {}).get("statement") == prior_reply), None)
        if not topic:
            return ""
    topic = topic or facts.get("activity")
    record = activities.get(topic)
    if not record and not (facts and named and topic):
        return ""
    record = record or {}
    if why:
        answer = _reason(topic, record)
    elif account := record.get("recent_account"):
        answer = account["statement"]
    else:
        answer = f"I don't have enough to say how often I've been {topic} lately."
    return answer
