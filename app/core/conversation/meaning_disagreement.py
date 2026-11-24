"""Guide ordinary replies about two speakers' interpretations of a disputed label."""
import re

from app.core.conversation.practical_answer import PracticalAnswer


_ASK = re.compile(
    r"\b(?:which of us|who(?:'s| is| was) (?:right|wrong)|(?:do|did|are) we "
    r"(?:actually |really )?(?:disagree|disagreeing|mean|talking past)|"
    r"are (?:we |they )?both right|is (?:one|either) of us wrong|"
    r"(?:is that|is this) (?:a |an? actual |a real )?contradiction)\b", re.I,
)
_ACCOUNT = re.compile(r"\b(?:I (?:said|called|told|meant|say|count)|"
                      r"(?:we|both of us) (?:called|mean|meant|agree)|by .{1,28} I mean)\b", re.I)
_OTHER = re.compile(r"\b(?!I\b|i\b|we\b|We\b)(?:[A-Z][\w'-]*|he|she|they) "
                    r"(?:(?:just|also|actually) )?"
                    r"(?:says?|said|calls?|called|means?|meant|counts?)\b"
                    r"|\b(?:we both|both of us) (?:mean|meant|count)\b")
_BARE_ASK = re.compile(r"(?:which of us (?:has (?:the|that) (?:date|time) wrong|is (?:right|wrong))"
                       r"|who(?:'s| is| was) (?:right|wrong)|(?:do|did) we (?:actually |really )?"
                       r"disagree|are we (?:talking past each other|both right)"
                       r"|is (?:one|either) of us wrong|is (?:that|this) a contradiction)\??", re.I)
_GUIDE = (
    "Compare what each speaker counted as the disputed milestone. If each supplied a different "
    "criterion, name both and attach the right observation or date to each. If they meant the "
    "same milestone, incompatible claims are a genuine factual dispute; the accounts alone "
    "cannot pick the winner. When a speaker gave only a label or a date, leave their meaning "
    "open and ask what they meant. Answer the actual question in a sentence or two."
)
_DAY = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_HOUR = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_TIME = rf"(?:{_DAY}|{_HOUR})"
_NAME = r"[A-Z][a-zA-Z'-]+"
_CRITERION = r"[\w '-]{1,120}"
# Match complete accounts. Additional evidence or a correction needs the full
# chat context and must never be discarded to produce a short verdict.
_REPORT = re.compile(
    rf"I (?:called|said|told {_NAME}) (?P<topic>[\w -]+?) (?:was )?(?P<label>[\w-]+) "
    rf"(?:at|on) (?P<first>{_TIME})(?:(?:, when |\. That's when )(?P<why>{_CRITERION}))?\. "
    rf"(?P<speaker>{_NAME}) (?:says|(?:just )?said|called) "
    rf"(?:(?:it|(?P=topic)) (?:was )?(?P=label) (?:at|on) )?(?P<second>{_TIME})"
    rf"(?:, when (?P<other>{_CRITERION}))?\.(?: We both mean (?P<shared>{_CRITERION})\.)?"
)


def _compare(report):
    match = _REPORT.fullmatch(" ".join(report.split()))
    if not match or re.search(r"\b(?:never|not|maybe|might|except|unless|probably)\b",
                              report, re.I):
        return ""
    a, b, name = match["first"], match["second"], match["speaker"]
    why, other, shared = match["why"], match["other"], match["shared"]
    days = bool(re.fullmatch(_DAY, a))
    if days != bool(re.fullmatch(_DAY, b)):
        return ""
    same = why and other and why.casefold() == other.casefold()
    if same and not shared and re.search(r"\b(?:I|me|my|mine|we|our|you|your)\b", why, re.I):
        return ""
    if shared or same:
        if a == b:
            return f"You and {name} give the same answer, {a}, for the same milestone."
        return (f"You both mean “{shared or why}”. You said {a} and {name} said {b}. "
                "Those accounts conflict, and I'd need another source to tell which is right.")
    if why and other:
        unit = "dates" if days else "times"
        return (f"You said {a} for “{why}”. {name} said {b} for “{other}”. "
                f"Those {unit} can both fit if you're counting different steps.")
    if why:
        prep = "on" if days else "at"
        return f"You were counting “{why}” {prep} {a}. What did {name} mean by {b}?"
    return ""


def check_meanings(text, previous=""):
    if not text:
        return PracticalAnswer()
    # A standalone follow-up can use the immediately preceding user report.
    # A new question with its own subject must supply its own accounts.
    text = text.strip().replace("’", "'")
    question = re.split(r"(?<=[.!?])\s+|\n+", text)[-1]
    if not _ASK.search(question):
        return PracticalAnswer()
    report = text[:-len(question)].strip()
    if not report:
        if not _BARE_ASK.fullmatch(question):
            return PracticalAnswer()
        report = previous
    if not (_ACCOUNT.search(report) and _OTHER.search(report)):
        return PracticalAnswer()
    if not re.search(rf"\b(?:{_TIME}|mean|means|meant|definition|milestone)\b", report, re.I):
        return PracticalAnswer()
    return PracticalAnswer(_GUIDE, _compare(report) if _BARE_ASK.fullmatch(question) else "")
