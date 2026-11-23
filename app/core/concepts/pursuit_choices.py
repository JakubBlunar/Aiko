"""Connect independent activity choices to the existing pursuit and its memory edges."""

import re
from datetime import timedelta

from app.core.infra import timephrase
from app.core.world.pursuit_activities import (
    ACTIVITY_BY_KEY, ACTIVITY_WORDS, ALIASES, LABEL_ACTIVITIES,
)

_SKETCH = re.compile(r"\b(?:sketch(?:es|ing|ed|book)?|drawing(?:s)?|draw|drew)\b"
                     r"(?! (?:water|the curtains|a bath|a conclusion)\b)", re.I)
_ACTION = "home_new_sketch"


def mentions_sketch(text):
    return bool(_SKETCH.search(text))


def is_choice_note(note):
    meta = getattr(note, "metadata", None) or {}
    if meta.get("keys"):
        return any(key in ACTIVITY_BY_KEY for key in meta["keys"])
    return meta.get("topic") in ACTIVITY_BY_KEY or mentions_sketch(
        str(meta.get("topic") or note.content)
    )


def note_activity(note):
    keys = (getattr(note, "metadata", None) or {}).get("keys")
    return ACTIVITY_BY_KEY.get(keys[0]) if isinstance(keys, list) and len(keys) == 1 else None


def independent_run(note):
    from app.core.world.idle_life_context import family

    meta = getattr(note, "metadata", None) or {}
    if not note_activity(note) or meta.get("source") != "away_beat":
        return None
    key, = meta["keys"]
    if "activity_choice" in meta:
        receipt = meta["activity_choice"]
        if not isinstance(receipt, dict) or receipt.get("key") != key:
            return None
    else:
        receipt = meta.get("sketch_choice") if key == _ACTION else None
    if (isinstance(receipt, dict) and isinstance(receipt.get("run_id"), str)
            and isinstance(receipt.get("alternatives"), list)
            and any(isinstance(opt, str) and opt and family(opt) != family(key)
                    for opt in receipt["alternatives"])
            and all(type(receipt.get(k)) is int and receipt[k] > 0
                    for k in ("choice_event_id", "completion_event_id"))
            and receipt["completion_event_id"] > receipt["choice_event_id"]):
        return receipt["run_id"]
    return None


def supported_pursuits(concepts, memories, owner):
    """Only expose the supported observation; older labels may also speculate about motives."""
    pursuits = [c for c in concepts.list_by(kind="pursuit", subject="aiko", status="active")
                if c.user_id in (None, owner) and c.label in LABEL_ACTIVITIES]
    edges = concepts.evidence_for(c.concept_id for c in pursuits)
    supported = {}
    for concept in sorted(pursuits, key=lambda c: (c.last_reinforced_at or c.created_at,
                                                  c.concept_id)):
        activity = LABEL_ACTIVITIES[concept.label]
        support = {}
        for edge in edges.get(concept.concept_id, ()):
            if edge.src_type == "memory":
                note = memories.get(int(edge.src_id))
                if (note and note.kind == "pursuit_note" and note_activity(note) == activity
                        and (run := independent_run(note))):
                    support[run] = note.id
        if len(support) >= 3:
            supported[activity] = {"concept_id": concept.concept_id, "statement": concept.label,
                                   "memory_ids": list(support.values())}
    return supported


def recent_pursuit_account(activity, activities, split):
    """Keep a lasting pursuit and the evidence for a present-tense answer distinct."""
    record = activities[activity]
    finding = record.get("finding") or {}
    kind = finding.get("kind")
    if kind == "changed_choice_pattern":
        return {"statement": finding["statement"], "finding_activity": activity}

    # A shift is filed under the later choice. The same paired evidence also
    # answers a question about the earlier activity, within those opportunities.
    shifts = (row["finding"] for row in activities.values()
              if (row.get("finding") or {}).get("previous_activity") == activity)
    change = max(shifts, key=lambda item: item["later"][-1]["recorded_at"], default=None)
    if change:
        later = change["activity"]
        return {"statement": f"I was {activity} earlier. Lately, when I had a free choice "
                             f"between {activity} and {later}, I picked {later}.",
                "finding_activity": later}
    if kind == "repeated_choice" and split and finding["later"][-1]["recorded_at"] < split:
        return {"statement": f"I remember choosing {activity} a week or two ago. "
                             "I'm not sure how often I've chosen it since.",
                "finding_activity": activity}
    pursuit = record.get("pursuit")
    if pursuit and kind == "repeated_choice":
        return {"statement": pursuit["statement"] + ".", "finding_activity": activity}
    if finding:
        return {"statement": finding["statement"], "finding_activity": activity}
    return {"statement": f"{activity.capitalize()} is something I've come back to before. "
                         "I can't say how often I've been doing it lately."}


_REASON = re.compile(r"I (?:like|love|enjoy) (.+?) because ([^.!?\r\n\[\]<>*]{3,140})[.!]?", re.I)
_RETRACT = re.compile(r"I (?:no longer|don['’]t|do not) (?:like|love|enjoy) ("
                      + ACTIVITY_WORDS.pattern + ")", re.I)


def expressed_reasons(db, owner, activities):
    now = timephrase.utcnow()
    rows = db.messages_in_range((now - timedelta(days=30)).isoformat(), now.isoformat(),
                                user_id=owner, limit=400)
    reasons, remaining = {}, set(activities)
    for row in rows:
        if row["role"] != "assistant":
            continue
        text = row["content"].strip()
        reason = _REASON.fullmatch(text)
        match = reason or _RETRACT.match(text)
        activity = ALIASES.get(match[1].lower()) if match else None
        if activity in remaining:
            remaining.remove(activity)
            if reason:
                reasons[activity] = {"statement": text, "message_id": row["message_id"],
                                     "created_at": row["created_at"]}
            if not remaining:
                break
    return reasons
