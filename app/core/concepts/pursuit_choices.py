"""Connect independently chosen sketches to the existing pursuit and its memory edges."""

import re
from datetime import timedelta

from app.core.infra import timephrase


SKETCH_PURSUIT = "I keep coming back to sketching in my own time"
_SKETCH = re.compile(r"\b(?:sketch(?:es|ing|ed|book)?|drawing(?:s)?|draw|drew)\b"
                     r"(?! (?:water|the curtains|a bath|a conclusion)\b)", re.I)
_ACTION = "home_new_sketch"


def mentions_sketch(text):
    return bool(_SKETCH.search(text))


def sketch_note(note):
    meta = getattr(note, "metadata", None) or {}
    if meta.get("keys"):
        return any(key in {_ACTION, "home_revise_sketch"} for key in meta["keys"])
    return mentions_sketch(str(meta.get("topic") or note.content))


def independent_run(note):
    meta = getattr(note, "metadata", None) or {}
    receipt = meta.get("sketch_choice")
    if (meta.get("source") == "away_beat" and meta.get("keys") == [_ACTION]
            and isinstance(receipt, dict) and isinstance(receipt.get("run_id"), str)
            and isinstance(receipt.get("alternatives"), list) and receipt["alternatives"]
            and all(type(receipt.get(k)) is int and receipt[k] > 0
                    for k in ("choice_event_id", "completion_event_id"))
            and receipt["completion_event_id"] > receipt["choice_event_id"]):
        return receipt["run_id"]
    return None


def supported_pursuit(concepts, memories, owner):
    """Only expose the supported observation; older labels may also speculate about motives."""
    pursuits = [c for c in concepts.list_by(kind="pursuit", subject="aiko", status="active")
                if c.user_id in (None, owner) and c.label == SKETCH_PURSUIT]
    edges = concepts.evidence_for(c.concept_id for c in pursuits)
    for concept in pursuits:
        support = {}
        for edge in edges.get(concept.concept_id, ()):
            if edge.src_type == "memory":
                note = memories.get(int(edge.src_id))
                if note and note.kind == "pursuit_note" and (run := independent_run(note)):
                    support[run] = note.id
        if len(support) >= 3:
            return {"concept_id": concept.concept_id, "statement": SKETCH_PURSUIT,
                    "memory_ids": list(support.values())}
    return None


_REASON = re.compile(
    r"I (?:like|love|enjoy) (?:sketching|drawing) because ([^.!?\n]{3,140})[.!]?", re.I,
)


def expressed_reason(db, owner):
    now = timephrase.utcnow()
    rows = db.messages_in_range((now - timedelta(days=30)).isoformat(), now.isoformat(),
                                user_id=owner, limit=400)
    for row in rows:
        if row["role"] != "assistant":
            continue
        text = row["content"].strip()
        if re.match(r"I (?:no longer|don't|do not) (?:like|love|enjoy) (?:sketching|drawing)\b",
                    text, re.I):
            return None
        if _REASON.fullmatch(text):
            return {"statement": text, "message_id": row["message_id"],
                    "created_at": row["created_at"]}
    return None
