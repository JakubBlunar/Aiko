"""Answer direct questions about Aiko's pursuits from the self-history projection."""

import re


_INTEREST = re.compile(
    r"(?:what(?: have you been|'ve you been| are you) (?:into|interested in)"
    r"(?: lately| recently)?|what have you been enjoying(?: lately| recently)?|"
    r"what (?:have you been choosing|do you keep coming back) to do(?: lately)?|"
    r"have your interests changed(?: lately)?|are you still (?:sketching|drawing))\??", re.I,
)
_WHY = re.compile(r"(?:what do you (?:like|enjoy) about|why do you (?:like|enjoy)) "
                  r"(?P<topic>sketching|drawing|it|that)\??", re.I)


def pursuit_reply(text, history, provider):
    if provider is None:
        return ""
    text = (text or "").strip()
    why = _WHY.fullmatch(text)
    if not why and not _INTEREST.fullmatch(text):
        return ""
    if why and why["topic"].lower() in {"it", "that"} and not (
        len(history) >= 2 and history[-1].role == "assistant" and history[-2].role == "user"
        and _INTEREST.fullmatch(history[-2].content.strip())
        and re.search(r"\b(?:sketch\w*|drawing)\b", history[-1].content, re.I)
        and not re.search(r"\b(?:also|as well as|and (?:doing|playing|reading|writing|painting))\b",
                          history[-1].content, re.I)
    ):
        return ""
    facts = provider("aiko").get("choice_history", {})
    finding = facts.get("finding") or {}
    pursuit = facts.get("pursuit") or {}
    if not (finding or pursuit):
        return ""
    if why:
        reason = facts.get("expressed_reason")
        if reason:
            # Attribute the old statement. A recalled answer cannot become a
            # new independent source for the explicit-reason parser.
            answer = "I remember saying " + reason["statement"].rstrip(".!") + "."
        else:
            answer = "I haven't pinned down what I like about sketching yet."
    elif finding.get("kind") == "changed_choice_pattern":
        answer = "I used to reach for a book more often. Lately I keep picking up my sketchbook."
    elif pursuit:
        answer = pursuit["statement"] + "."
    elif finding.get("kind") == "repeated_choice":
        answer = "I've picked up my sketchbook a few times when I could have done something else."
    else:
        answer = "I've spent some time sketching."
    return ("The user asked for a fact about your own experience. Reply with exactly the text "
            "inside the quotation marks. This is the whole reply, including its final period. "
            "Output no continuation or question.\n" + f'"{answer}"')
