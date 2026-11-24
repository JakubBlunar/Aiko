"""Offer a fresh contribution to an explicitly named collaborative story."""
import re

from app.core.conversation.perspective_sources import evidence, near, remarks, terms
from app.core.conversation.practical_answer import PracticalAnswer


_ASK = re.compile(r"\b(?:what (?:would|could) you (?:add|bring)|how would you (?:continue|"
                  r"develop)|(?:write|suggest|pitch|imagine) (?:us |me )?(?:(?:a|the|our) )?"
                  r"(?:new|next) (?:scene|chapter|verse))\b", re.I)
_WORK = re.compile(r"\b(?:story|stories|scene|fiction|tale|chapter|poem|verse|comic|novel)\b", re.I)
_WISH = re.compile(r"\b(?:i(?:['’]d (?:like|love)| (?:like|love|want|prefer|hope|imagine|"
                   r"picture|insist|changed my mind))|let['’]s|must|has to|stays?|"
                   r"should|never|nobody)\b", re.I)
_FILLER = {"add", "bring", "write", "suggest", "pitch", "imagine", "develop", "continue",
           "next", "scene", "chapter", "story", "fiction", "tale", "poem", "verse", "comic",
           "novel", "please", "short", "something", "writing", "project", "make"}
_TITLE = re.compile(r"\b(?:our|the|my) ([\w'-]+(?: [\w'-]+){0,2}?) "
                    r"(?:story|tale|poem|comic|novel)\b", re.I)
_NO_ENTRY = re.compile(r"\b(?:no one|nobody) (?:ever )?(?:enters|goes inside|gets in|"
                       r"can enter|is allowed inside)\b[.!?]?(?:\s*$|[.!?]\s)", re.I)
_INSIDE = re.compile(r"\b(?:must|has to|have to|insist (?:on|that))\b[^.!?]{0,80}"
                     r"\b(?:meet(?:ing)?|gather(?:ing)?)\b[^.!?]{0,24}\binside\b", re.I)
_REVISION = re.compile(r"\b(?:changed my mind|i['’]ve reconsidered|drop my earlier|"
                       r"no longer want|replace my earlier)\b", re.I)


def check_shared_creation(text, previous, db, owner, key, current_id):
    if not _ASK.search(text):
        return PracticalAnswer()
    if not (_WORK.search(text) or _WORK.search(previous) and re.search(
            r"\b(?:it|that|next|this)\b", text, re.I)):
        return PracticalAnswer()
    name = _TITLE.search(text)
    subject = terms(name[1] if name else text) - _FILLER
    if not subject:
        subject = terms(previous) - _FILLER
    if not subject:
        return PracticalAnswer()
    rows = remarks(db, owner, key, current_id)
    wishes = {}
    previous_rows = {}
    for row in rows:
        prior = previous_rows.get(row["session_id"])
        previous_rows[row["session_id"]] = row
        content = row["content"]
        other_name = _TITLE.search(content)
        if name and other_name and not (subject & terms(other_name[1])):
            continue
        if not (_WISH.search(content) or _REVISION.search(content)) or _ASK.search(content):
            continue
        named = subject & terms(content)
        brief = (prior and near(prior, row) and _WORK.search(prior["content"])
                 and subject & terms(prior["content"]))
        referent = re.search(r"\b(?:there|its|that place|the doors|that character|it)\b",
                             content, re.I)
        if (named and _WORK.search(content)) or (brief and (named or referent)):
            speaker_wishes = wishes.setdefault(row["role"], [])
            if _REVISION.search(content):
                speaker_wishes.clear()
            speaker_wishes.append(row)
    if not wishes:
        return PracticalAnswer()
    selected = [r for speaker_wishes in wishes.values() for r in speaker_wishes[-3:]]
    guidance = (
        "Write a single proposed scene in the requested form. These dated remarks are source "
        "material for the fiction. Preserve the user's conditions. Your own earlier interest "
        "supplies the event to stage. A current revision takes priority. If conditions conflict "
        "ask which to use.\nThe user's side of the creative brief:\n"
        + evidence(wishes.get("user", [])[-3:]) + "\nYour side of the brief (you are Aiko):\n"
        + evidence(wishes.get("assistant", [])[-3:]))
    closed = (any(_NO_ENTRY.search(r["content"]) for r in selected) and not re.search(
        r"\b(?:actually|instead|unlock\w*|opens?|enters?|entry|changed my mind)\b", text, re.I))
    if closed:
        guidance += ("\nStage the scene outside. Keep the viewpoint there so the characters "
                     "can see or hear everything that happens without entering.")
    if wishes.get("assistant"):
        interest = wishes["assistant"][-1]["content"]
        guidance += ("\nBuild the action around the interest in your most recent source above. "
                     "Show it happening through a concrete action or object.")
        if re.search(r"\bstrangers?\b[^.!?]{0,50}\bmeet\b", interest, re.I):
            guidance += " Bring two strangers together and show their first exchange."
    opening = ("Give the requested creative text directly." if re.search(
        r"\bwrite (?:us |me )?(?:(?:a|the|our) )?(?:new|next)\b", text, re.I)
        else "Start with 'What if' and give the proposed scene itself in two or three sentences.")
    guidance += "\n" + opening + " Leave out commentary, attribution and alternative suggestions."
    # Give the writer a playable exterior scene. Restating a forbidden door action
    # repeatedly made it the main action of the generated story.
    if closed:
        place = name[1] if name else "place"
        interest = wishes.get("assistant", [])
        last = interest[-1]["content"] if interest else ""
        if re.search(r"\bstrangers?\b[^.!?]{0,50}\bmeet\b", last, re.I):
            guidance += (f"\nYour scene begins with two strangers waiting outside the {place}. "
                         "Keep the camera on their faces. Write the first exchange between them: "
                         "one offers something small and the other answers. End there.")
        elif not last or re.search(r"\b(?:traces|left behind|long gone)\b", last, re.I):
            guidance += (f"\nSet the entire moment on the pavement outside the {place}. "
                         "Invent a small object on that pavement. Show what a passerby can tell "
                         "about whoever left it there, and end on that detail.")
    if len(wishes) == 2 and not (_WISH.search(text) or _REVISION.search(text)
                                 or re.search(r"[.!;]\s+\S", text)):
        barred = {r["role"] for r in selected if _NO_ENTRY.search(r["content"])
                  and re.search(r"\b(?:never open|permanently locked|always locked)\b",
                                r["content"], re.I)}
        inside = {r["role"] for r in selected if _INSIDE.search(r["content"])}
        if any(speaker != other for speaker in barred for other in inside):
            reply = ("One of our ideas keeps everyone outside, and the other needs the meeting "
                     "to happen inside. Which would you like to keep for this scene?")
            return PracticalAnswer(guidance, reply)
    return PracticalAnswer(guidance)
