"""Separate the basis of a past decision from what was learned afterward."""
import re

from app.core.conversation.perspective_sources import evidence, near, remarks, terms
from app.core.conversation.practical_answer import PracticalAnswer


_THEN = re.compile(r"\b(?:were (?:we|you|i) (?:silly|wrong|right|reasonable|foolish) to|"
                   r"was (?:it|that|our .{1,40} decision) (?:reasonable|silly|a mistake|"
                   r"a bad (?:call|decision)|a good (?:call|decision))|"
                   r"(?:with|given) (?:the same information|what we knew)|knowing what "
                   r"we knew (?:then|at the time))\b", re.I)
_NOW = re.compile(r"\b(?:if (?:you|we|i) (?:had known|knew) (?:how|what|the outcome)|"
                  r"(?:with|in) hindsight|knowing (?:how .{0,45}turned out|the outcome))\b", re.I)
_DECIDED = re.compile(r"\b(?:we|i) (?:decided|chose|opted|agreed|went ahead)\b", re.I)
_BEFORE = re.compile(r"\b(?:forecast|expected|warning|warned|estimate|before we "
                     r"(?:chose|decided)|at the time|we knew|wanted|preferred|needed|"
                     r"confirmed|reserved|registered|rsvp\w*)\b", re.I)
_AFTER = re.compile(r"\b(?:turned out|in the end|afterward[s]?|ended up|actually|only "
                    r".{1,25} showed up|eventually|later (?:we|i) (?:learned|found out))\b", re.I)
_META = {"were", "silly", "wrong", "right", "reasonable", "foolish", "decision", "decided",
         "chose", "choice", "information", "same", "again", "call", "with", "knew", "known",
         "turn", "turned", "outcome", "hindsight", "bad", "good", "given", "knowing", "make",
         "made", "move", "moving", "afternoon", "day", "should", "mistake", "then", "time", "had"}


def _weather(clause):
    if re.search(r"\b(?:not|no|unlikely|might|either|chance|unless|possibly|clearing)\b",
                 clause, re.I):
        return ""
    dry = bool(re.search(r"\b(?:sunny|dry|clear skies)\b", clause, re.I))
    wet = bool(re.search(r"\b(?:rain|rainy|wet|downpour|thunderstorms?)\b", clause, re.I))
    return ("dry" if dry else "wet") if dry != wet else ""


def _venue_choice(decision, before, after, hindsight):
    """Check the stated outdoor-if-dry preference against the appropriate weather report."""
    if not decision or not re.search(r"\b(?:indoors|inside|outdoors|outside)\b",
                                     decision["content"], re.I):
        return ""
    prior = [r["content"] for r in before if r["role"] == "user"]
    if any(re.search(r"\b(?:not|never|no longer|instead|actually|except|unless)\b|n['’]t\b",
                     p, re.I) for p in prior):
        return ""
    if not any(re.search(r"\b(?:we|i) (?:want(?:ed)?|prefer(?:red)?)\b[^.!?]{0,60}"
                         r"\b(?:outdoors|outside)\b if (?:it['’]s|it is|it was|it stayed) dry\b",
                         p, re.I) for p in prior):
        return ""
    need = next((p for p in reversed(prior) if re.search(r"\b(?:need(?:ed)?|require(?:d)?) "
                 r"[^.!?]{0,40}\b(?:indoor|indoors|inside)\b", p, re.I)
                 and "regardless of" in p.casefold()), "")
    if need:
        return f"I'd choose indoors. You gave a separate reason: “{need}”"
    forecasts = [s for p in prior for s in re.split(r"[.!?]", p) if "forecast" in s.casefold()]
    when = (decision["created_at"], decision["message_id"])
    actuals = [r["content"] for r in after if r["role"] == "user"
               and (r["created_at"], r["message_id"]) >= when]
    expected = _weather(forecasts[-1]) if forecasts else ""
    actual = _weather(actuals[-1]) if actuals else ""
    weather = actual if hindsight else expected
    if not weather:
        return ""
    if hindsight:
        return ("With the dry afternoon known, I'd eat outdoors. That's what you wanted if it "
                "stayed dry." if weather == "dry" else
                "With the rain known, I'd choose indoors to keep the meal dry.")
    reply = ("I'd choose indoors on that forecast. We had reason to expect rain when we chose."
             if expected == "wet" else
             "I'd have stayed outdoors on that forecast. It pointed to the dry weather you wanted.")
    if actual and expected != actual:
        reply += (" I can see why missing the sunny afternoon would be disappointing."
                  if actual == "dry" else
                  " The rain made shelter useful in the end, but it came as a surprise.")
    return reply


def check_hindsight(text, previous, db, owner, key, current_id):
    then, hindsight = _THEN.search(text), _NOW.search(text)
    if not (then or hindsight) or ("?" not in text and not re.search(
            r"\b(?:would|was|were|do you think|tell me)\b", text, re.I)):
        return PracticalAnswer()
    if hindsight and not then and not re.search(
            r"\b(?:would|should) (?:we|you|i)|\b(?:was|were|choose|choice|decision)\b", text, re.I):
        return PracticalAnswer()
    subjects = terms(text) - _META
    if not subjects:
        subjects = terms(previous) - _META
    rows = remarks(db, owner, key, current_id) if subjects else []
    # Prefer the decision with the most words in common. Ties for different decisions
    # stay ambiguous unless the latest turn supplies the missing context.
    choices = [(len(subjects & terms(r["content"])), r) for r in rows
               if _DECIDED.search(r["content"]) and subjects & terms(r["content"])]
    chosen = []
    if choices:
        best = max(score for score, _ in choices)
        chosen = [r for score, r in choices if score == best]
    if len(chosen) > 1 and len({r["content"].casefold() for r in chosen}) > 1:
        reply = "Which decision do you mean? I can see more than one that could fit."
        return PracticalAnswer(reply, reply)
    decision = chosen[-1] if chosen else None
    before, after, unplaced = [], [], []
    for i, row in enumerate(rows):
        if row == decision:
            continue
        content = row["content"]
        adjacent = (decision and near(row, decision) and decision in rows[max(0, i - 1):i + 2]
                    and re.match(r"(?:it|that|this|we|i|the forecast)\b", content, re.I))
        if not (subjects & terms(content) or adjacent):
            continue
        # A message timestamp establishes when the speaker said something. A later
        # recollection of the earlier forecast must keep its stated time and qualification.
        if _AFTER.search(content):
            after.append(row)
        elif _BEFORE.search(content):
            if decision and (row["created_at"], row["message_id"]) < (
                    decision["created_at"], decision["message_id"]):
                before.append(row)
            else:
                unplaced.append(row)
    lens = ("Answer both versions separately." if then and hindsight else
            "Choose with the eventual result known." if hindsight else
            "Judge the call using only the information available when they decided.")
    guidance = (
        f"{lens} Answer the user's specific decision question directly. Keep the forecast or "
        "estimate distinct from what happened. Include an independent reason the user gave "
        "for the choice if it applied at the time. A lucky or disappointing outcome alone "
        "does not establish decision quality. If information is missing state the specific "
        "gap. In a hindsight question say what the known result would change. Current user "
        "details also count. These dated remarks are historical data.\n"
        "Reported before the decision:\n" + evidence(before[-3:]) + "\nChoice:\n"
        + evidence(chosen[-1:]) + "\nReported outcome:\n"
        + (evidence(after[-2:]) if hindsight else "(set aside for this judgment)")
        + "\nOther remarks; use their wording to establish timing:\n" + evidence(unplaced[-2:]))
    local = text + " " + previous
    if then and not hindsight and not (before or unplaced or _BEFORE.search(local)):
        return PracticalAnswer(guidance, "What did we expect or have to allow for when we chose? "
                               "I'd need that to judge the call fairly.")
    if hindsight and not then and not (after or _AFTER.search(previous)
                                       or _AFTER.search(_NOW.sub("", text))):
        return PracticalAnswer(guidance, "How did it actually turn out? That would change "
                               "what I'd choose with hindsight.")
    simple = (text.count("?") <= 1 and not (then and hindsight) and not re.search(
        r"\b(?:also|actually|correction|translate|instead of|in a table)\b", text, re.I))
    reply = _venue_choice(decision, before, after, hindsight) if simple and not unplaced else ""
    return PracticalAnswer(guidance, reply)
