"""One optional craft exercise, backed by the endings of saved original works."""
import hashlib
import re

from app.core.conversation.practical_answer import PracticalAnswer


AIM = "end_on_an_image"
_REVIEW = re.compile(r"\b(?:what (?:have you been|are you) (?:getting better at|practi[cs]ing|"
                     r"working on in your writing)|how has your writing changed|"
                     r"have you (?:improved|gotten better) (?:at|in) writing)\b", re.I)
_WRITE = re.compile(r"\b(?:write|tell|compose) (?:me |us )?(?:a |an |another )?"
                    r"(?:little |short |new |fresh )*(?:poem|scene|story)\b", re.I)


def _works(items):
    for item in items:
        state = item.state or {}
        rows = state.get("revisions")
        if state.get("creation_schema") != 1 or not isinstance(rows, list):
            continue
        for number, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            text = row.get("text")
            if (row.get("author") == "aiko" and row.get("mode") == "creative_fiction"
                    and row.get("revision") == number and isinstance(text, str) and text
                    and isinstance(row.get("at"), str) and isinstance(row.get("title"), str)
                    and row.get("text_sha256") == hashlib.sha256(text.encode()).hexdigest()):
                yield item.id, row


def reference(item_id, row):
    return f"{item_id}/{row['revision']}/{row['text_sha256'][:12]}"


def recent_revision_contrast(items):
    """Offer a short changed passage from consecutive verified drafts of one work."""
    latest = None
    prior = None
    for item_id, row in _works(items):
        if prior and prior[0] == item_id and row["revision"] == prior[1]["revision"] + 1:
            old = prior[1]
            if row["text"] != old["text"] and (latest is None or row["at"] > latest[1]["at"]):
                latest = (old, row, item_id)
        prior = (item_id, row)
    if not latest:
        return None
    old, new, item_id = latest
    passages = []
    for version, other in ((old, new), (new, old)):
        sentences = re.split(r"(?<=[.!?])\s+|\n+", version["text"].strip())
        passage = next((s for s in sentences if 8 <= len(s) <= 220 and s not in other["text"]), "")
        if not passage:
            return None
        passages.append({"ref": reference(item_id, version), "at": version["at"],
                         "passage": passage})
    return {"title": new["title"][:100], "before": passages[0], "after": passages[1]}


def latest_aim(items):
    return max(((item_id, row) for item_id, row in _works(items)
                if row.get("practice_aim") == AIM), key=lambda pair: pair[1]["at"], default=None)


def available_aim(items):
    aim = latest_aim(items)
    if not aim:
        return ""
    item_id, row = aim
    return (f"An earlier aim you chose was to end on an image. Source "
            f"{reference(item_id, row)} from {row['title']!r}. You may use it or set it aside.")


def _ending(row):
    text = row["text"].strip()
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    return sentences[-1][-150:]


def _review(works, aim):
    item_id, original = aim
    ref = reference(item_id, original)
    parts = [f"In {original['title']} I chose to practise letting the last image carry the "
             f"ending. That version ended “{_ending(original)}”"]
    attempts = [(i, r) for i, r in works if r.get("practice_ref") == ref
                and r["at"] >= original["at"] and isinstance(r.get("image_passage"), str)
                and r["image_passage"] and r["image_passage"] in r["text"]]
    revisions = [r for i, r in attempts if i == item_id and r["revision"] > original["revision"]]
    revision = max(revisions, key=lambda r: r["revision"], default=None)
    if revision:
        parts.append(f"My revision ended “{_ending(revision)}”")
    later = max((r for i, r in attempts if i != item_id and r["revision"] == 1
                 and r["at"] > (revision or original)["at"]), key=lambda r: r["at"], default=None)
    if later:
        if later["text"].rstrip().endswith(later["image_passage"]):
            parts.append(f"I tried it again in {later['title']} and left the image I chose as "
                         f"the last line: “{later['image_passage']}”")
        else:
            parts.append(f"In {later['title']} I kept writing after “{later['image_passage']}” "
                         f"and ended “{_ending(later)}” instead.")
    else:
        parts.append("I don't have a separate later piece showing that I used it again.")
    return " ".join(parts)


def practice_answer(text, provider):
    review = _REVIEW.search(text)
    if not provider or not (review or _WRITE.search(text)):
        return PracticalAnswer()
    items = provider()
    aim = latest_aim(items)
    if review:
        reply = (_review(list(_works(items)), aim) if aim else
                 "I don't have a saved writing exercise I can point to yet.")
        return PracticalAnswer("Answer from saved writing practice:\n" + reply, reply)
    if not aim or re.search(r"\b(?:explain\w*|explicit|direct|moral|spell out)\b", text, re.I):
        return PracticalAnswer()
    return PracticalAnswer("For this new writing, you previously chose to practise ending on "
                           "a concrete image. Try it if it suits this piece. The user's "
                           "requested style and ending take priority. Give the writing itself.")
