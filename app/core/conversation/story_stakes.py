"""Select a conversation-local response to what the storyteller cared about."""
from itertools import islice
import re

from app.core.conversation.practical_answer import PracticalAnswer


_STAKE = re.compile(
    r"\b(?:I (?:really |just )?(?:want(?:ed)?|hope[ds]?|promised|care[ds]? about|"
    r"was (?:hoping|counting on|looking forward to|worried|nervous))|"
    r"it (?:matters|mattered|means a lot) to me)\b", re.I,
)
_OUTCOME = re.compile(
    r"\b(?:finished|ended up|went back|picked|chose|chosen|made|got|landed|"
    r"turned (?:up|out)|accepted|declined|ran out|showed up|left out|"
    r"worked|won|lost|cancelled|canceled|didn't|did not|finally)\b", re.I,
)
_COMMON = set(
    "the and for with that then said they them there their this from just have had has "
    "but her she his him you were was very some back went really wanted hope hoping "
    "promised like after before would didn't made ended finished got finally first last".split()
)
STAKE_GUIDANCE = (
    "The storyteller has just given the outcome. Use a single warm line. Connect this "
    "specific event to what the user explicitly wanted. The same person in a different "
    "story is insufficient. Stay within the facts: others' feelings, things happening off "
    "stage, and a next step actually taking place are all unknown."
)


def _anchors(text):
    return set(re.findall(r"[^\W\d_]{3,}", text.casefold())) - _COMMON


_PORTION = re.compile(r"\bI promised (my \w+|[A-Z][\w'-]*) the last "
                      r"(bowl|slice|piece|portion|serving|cup) of ([\w-]+)\b")
_HELPING = re.compile(r"([A-Z][\w'-]*) (?:went back for seconds|had another helping) and "
                      r"(?:finished|emptied) the (pot|dish|pan|bowl|plate)"
                      r"(?: of ([\w-]+))?[.!]?")
_SELECTION = re.compile(r"My ([\w-]+) made the (set list|lineup|programme|program)[.!]?", re.I)


def _reading(ending, sources, relevant):
    # These two relations can be phrased from the narrated observations alone.
    # Extra conditions, corrections or a different item stay in ordinary chat.
    if any(re.search(r"\b(?:unless|actually|instead|if|but|used to|no longer|changed my mind|"
                     r"set aside|saved some|already saved)\b", s, re.I) for s in sources):
        return ""
    if any(re.search(r'["“”]|\b(?:said|wrote|quoted)\s*[:,]', s, re.I) for s in relevant):
        return ""
    selection = _SELECTION.fullmatch(ending)
    if selection and len(relevant) == 1:
        item, listing = selection.groups()
        subject = "your " + item.lower()
        obj = re.escape(item)
        hope = re.match(rf"I (?:really )?(?:hoped|was hoping) they'd "
                        rf"(leave my {obj} out|(?:play|include|feature) my {obj})"
                        rf"(?: at the [\w-]+)?[.!]", relevant[0], re.I)
        if hope and "leave" in hope[1].lower():
            return f"So {subject} made the {listing}, even though you'd hoped they'd leave it out."
        if hope:
            return f"There's {subject} on the {listing}. One step closer to what you hoped for."
    helping = _HELPING.fullmatch(ending)
    if not helping:
        return ""
    person = helping[1]
    approval = any(
        re.search(rf"\b{re.escape(person)}'s [\w-]+\.", s)
        and re.search(r"\bI (?:really )?wanted (?:him|her|them) to like it[.!]", s, re.I)
        for s in relevant)
    promises = [m for s in relevant for m in _PORTION.finditer(s)]
    if len(promises) > 1:
        return ""
    if not promises:
        return f"Sounds like {person} liked it. Those seconds are a pretty good sign." \
            if approval else ""
    promise = promises[0]
    if helping[3] and helping[3].casefold() != promise[3].casefold():
        return ""
    if helping[2] == "pot" and promise[2] in ("slice", "piece"):
        return ""
    recipient = promise[1].replace("my ", "your ", 1)
    portion = promise[2]
    # A later statement from the named recipient can settle the promise. A
    # pronoun, an ambiguous update or a new serving arrangement needs the model.
    for s in sources:
        if promise.string == s:
            break
        if re.fullmatch(rf"{re.escape(promise[1])} (?:(?:called to say|said|texted that) "
                        r"(?:she|he|they) (?:already ate and )?)?"
                        r"(?:didn't want|did not want|didn't need) any[.!]?", s, re.I):
            return f"Good thing {recipient} had already passed on that last {portion}."
        if s and (s in relevant or _anchors(s) & _anchors(promise.string)
                  or re.search(r"\b(?:some|any|she|her|he|him|they|them|put it|saved it)\b",
                               s, re.I)):
            return ""
    if approval:
        return (f"Seconds sound like a good sign from {person}. "
                f"Did you get to set that last {portion} aside for {recipient}?")
    return (f"Ah, and you'd promised that last {portion} to {recipient}. "
            "I hope you got to save some.")


def check_stakes(text, recent_users=()):
    if not text or "?" in text or not _OUTCOME.search(text):
        return PracticalAnswer()
    if re.search(r"(?:^|[.!]\s+)(?:please|write|tell me|explain|translate|summarize|"
                 r"help|imagine|what if)\b", text, re.I):
        return PracticalAnswer()
    sentences = re.split(r"(?<=[.!])\s+|\n+", text.strip())
    ending = sentences[-1]
    if not _OUTCOME.search(ending):
        return PracticalAnswer()
    anchors = _anchors(ending)
    setup = " ".join(sentences[:-1])
    sources = [setup, *islice(recent_users, 5)]
    relevant = [s for s in sources if _STAKE.search(s) and anchors & _anchors(s)]
    if not relevant:
        return PracticalAnswer()
    notes = [STAKE_GUIDANCE]
    if any(re.search(r"\bI promised\b", s, re.I) for s in relevant):
        notes.append("A promise was made. Check whether the intended recipient later released "
                     "it. If so, treat it as settled. Otherwise, if the outcome uses up what "
                     "was promised, acknowledge the tension. Whether any was saved is unknown.")
    if len(relevant) > 1:
        notes.append("Two earlier stakes may matter at once. In one line acknowledge both "
                     "the welcome part of this outcome and its possible cost.")
    if re.search(r"\b(?:list|shortlist|lineup|programme|program|picked|chosen|selected)\b",
                 ending, re.I):
        notes.append("Selection is the event. Compare inclusion with the direction of the "
                     "user's wish. The performance itself is still unreported.")
    return PracticalAnswer(" ".join(notes), _reading(ending, sources, relevant))
