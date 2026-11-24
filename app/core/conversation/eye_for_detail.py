"""Use independent, reasoned observations to frame a detail in a newly described scene."""
import re

from app.core.conversation.perspective_sources import evidence, remarks, terms
from app.core.conversation.practical_answer import PracticalAnswer


_ASK = re.compile(r"\b(?:which|what) (?:visual )?detail (?:would you (?:frame|notice|"
                  r"put in (?:the|an) (?:opening|first) shot|show first)|catches your eye|"
                  r"stands out to you)|\bwhat would you (?:frame|focus on|put in the "
                  r"opening shot|notice first)\b", re.I)
_OBSERVATION = re.compile(r"(?:^|[.!?]\s+)(?:i (?:noticed|kept looking at|was drawn to|"
                          r"asked about)|what caught my eye was)\s+(.+?)\s+"
                          r"(?:because|since|as it|because of)\s+(.+)", re.I)
_LENSES = (
    (re.compile(r"\b(?:traces?|marks? of use|who (?:used|had been)|how (?:people|someone) used|"
                r"left behind|signs? of (?:use|wear))\b", re.I),
     re.compile(r"\b(?:worn|wear|scuff\w*|scratched|patched|initials|threadbare|footprints?)\b",
                re.I)),
    (re.compile(r"\b(?:attention|gaze|eyes?|audience|look first)\b", re.I),
     re.compile(r"\b(?:bright\w*|spotlit|spotlight|curtain|glow\w*|contrast\w*)\b", re.I)),
)
_REASON_FILLER = {"caught", "look", "makes", "place", "people", "remind", "something",
                  "someone", "stood", "interesting", "detail"}


def _present(pattern, description):
    for match in pattern.finditer(description):
        clause = re.split(r"[.,;!?]|\b(?:and|but)\b", description[:match.start()], flags=re.I)[-1]
        if not re.search(r"\b(?:no|without|absent|missing|not|lacks?)\b", clause, re.I):
            return match
    return None


def _listed_details(scene):
    listing = re.search(r"\b(?:has|there (?:is|are)|there['’]s|i see|contains)\s+"
                        r"([^.!?]+)[.!]?\s*$", scene, re.I)
    if not listing:
        return []
    parts = re.split(r",\s*(?:and )?|\s+and (?=(?:a|an|the|one) )", listing[1], flags=re.I)
    if len(parts) < 2 or not all(re.fullmatch(r"(?:a|an|the|one) [\w '’-]{2,65}", part, re.I)
                                 for part in parts):
        return []
    return [re.sub(r"^(?:a|an|the|one) ", "the ", p, flags=re.I) for p in parts]


def _observations(rows, scene):
    observations = []
    for row in reversed(rows):
        if row["role"] == "assistant" and (match := _OBSERVATION.search(row["content"])):
            if re.search(r"\byou (?:said|asked|noticed|told me|pointed out)\b", match[2], re.I):
                continue
            observations.append((row, match[1].strip().casefold(), match[2]))
            if len(observations) == 24:
                break
    # A repeated line or two remarks from one sitting cannot establish a recurring lens.
    for i, (latest, object_name, reason) in enumerate(observations):
        for older, other_object, earlier_reason in observations[i + 1:]:
            if latest["created_at"][:10] == older["created_at"][:10] or object_name == other_object:
                continue
            for why, visible in _LENSES:
                if why.search(reason) and why.search(earlier_reason) and _present(visible, scene):
                    return [older, latest], visible
            common = (terms(reason) & terms(earlier_reason)) - _REASON_FILLER
            if len(common) >= 2 and common & terms(scene):
                return [older, latest], None
    return [], None


def check_eye_for_detail(text, previous, db, owner, key, current_id):
    request = _ASK.search(text)
    if not request:
        return PracticalAnswer()
    scene = text[:request.start()].strip() or text[request.end():].partition("?")[2].strip()
    if not scene:
        scene = previous
    if not scene:
        return PracticalAnswer()
    sources, visible = _observations(remarks(db, owner, key, current_id), scene)
    guidance = (
        "Answer with one shot or observation. Choose a detail physically present in the user's "
        "description and say what draws your eye to it. You may interpret the detail. Keep "
        "invented objects or offscreen events out of the description. Speak in first person "
        "about this scene. No claim about what you have always liked is needed.")
    if sources:
        guidance += ("\nYour earlier observations and your own reasons, quoted as historical "
                     "evidence. Apply the way of looking to this new scene.\n" + evidence(sources))
        if visible:
            word = _present(visible, scene)[0]
            guidance += f"\nUse the detail described by the word “{word}” in the current scene."
    details = _listed_details(scene)
    if details and re.fullmatch(r"(?:which|what) detail would you (?:frame|put in the opening "
                                r"shot|show first)\??", text[request.start():].strip(), re.I):
        selected = next((d for d in details if visible and _present(visible, d)), "")
        if selected:
            reason = ("The wear gives me a way to show how the place has been used."
                      if visible is _LENSES[0][1] else
                      "I'd hold there for a moment to give the eye somewhere to settle.")
            return PracticalAnswer(guidance, f"I'd open on {selected}. {reason}")
        if not sources:
            return PracticalAnswer(guidance, f"I'd start close on {details[-1]}, then widen "
                                   f"until {details[0]} comes into the frame.")
    return PracticalAnswer(guidance)
