"""Motivation for a guess, separate from evidence that could establish it.

A hypothesis can go beyond its inspiration. Naming that inspiration does not
add support, credence or concept evidence. It makes the leap and its usefulness
inspectable, while leaving alternatives and an ordinary way to find out.
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.core.infra import timephrase


VERSION = 1
FIELDS = ("useful_because", "alternative", "check_question", "raise_when")
_UNOBSERVABLE = re.compile(
    r"\b(?:subconsciously|unconsciously|secretly monitors)\b|"
    r"\b(?:keeps?|contains?|owns?)\b[^.!?]{0,100}\b(?:hidden|secret)\b|"
    r"\b(?:specific|exact)\b.{0,35}\bthreshold\b", re.I,
)
_PRECISION = re.compile(
    r"\b(?:exactly\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+(?:seconds?|minutes?|hours?|times?)\b", re.I,
)


def decode_quality(raw: str | None) -> dict[str, Any]:
    import json

    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def usable_hypothesis(row: Any) -> bool:
    """Legacy inventions need review; actual answers retain their standing.

    Outcome-led introspection already has its own grounded admission contract.
    Neither exception turns inspiration references into confirming evidence.
    """
    if int(getattr(row, "support_count", 0) or 0) > 0:
        return True
    if getattr(row, "origin", "") == "belief_outcome":
        return bool(getattr(row, "origin_refs", None))
    q = getattr(row, "quality", None)
    return (isinstance(q, dict) and q.get("version") == VERSION
            and isinstance(q.get("inspiration"), dict)
            and all(isinstance(q.get(k), str) and q[k].strip() for k in FIELDS))


def validate_proposal(candidate: dict[str, Any], offered: dict) -> tuple[dict, str]:
    """Validate authored fields and the actual offered inspiration receipt."""
    from app.core.concepts.concept_kinds import CONCEPT_KINDS

    if not isinstance(candidate.get("kind"), str) or candidate["kind"] not in CONCEPT_KINDS:
        return {}, "kind"
    if (not isinstance(candidate.get("subject"), str)
            or candidate["subject"] not in {"user", "aiko", "relationship", "world"}):
        return {}, "subject"
    limits = {"statement": 300, "rationale": 400, **dict.fromkeys(FIELDS, 300)}
    for key, cap in limits.items():
        value = candidate.get(key)
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= cap:
            return {}, "incomplete"
        if timephrase.has_relative_deictic(value):
            return {}, "relative_time"
    if len(candidate["statement"].split()) < 4:
        return {}, "fragment"
    if _UNOBSERVABLE.search(candidate["statement"]):
        return {}, "unobservable"
    seed = candidate.get("inspiration")
    if (not isinstance(seed, dict) or seed.get("type") not in ("memory", "topic")
            or type(seed.get("id")) is not int):
        return {}, "inspiration_required"
    receipt = offered.get((seed["type"], seed["id"]))
    if receipt is None:
        return {}, "unoffered_inspiration"
    quote = seed.get("quote")
    if not isinstance(quote, str) or not 8 <= len(quote) <= 200 or quote not in receipt["text"]:
        return {}, "inspiration_quote"
    for match in _PRECISION.finditer(candidate["statement"]):
        if match.group().lower() not in receipt["text"].lower():
            return {}, "invented_precision"
    if "?" not in candidate["check_question"]:
        return {}, "not_checkable"
    try:
        credence = float(candidate.get("credence", 0.4))
    except (ValueError, TypeError):
        return {}, "credence"
    if not math.isfinite(credence):
        return {}, "credence"
    return {"version": VERSION, "inspiration": {**receipt, "quote": quote},
            **{key: candidate[key].strip() for key in FIELDS}}, ""


def motivation_lines(quality: dict[str, Any]) -> str:
    if quality.get("version") != VERSION:
        return ""
    return "\n".join(f"{title}: {quality[key]}" for key, title in (
        ("useful_because", "Why finding out could help"),
        ("alternative", "Another possibility"),
        ("raise_when", "A fitting context"),
        ("check_question", "Possible way to find out, adapt naturally"),
    ) if isinstance(quality.get(key), str) and quality[key])


def cue_target_valid(payload: dict, *, concepts: Any, hypotheses: Any, label: str = "") -> bool:
    """All delivery routes must check the current owner, including Live claims."""
    if "target_id" not in payload:
        return True  # A curated move, not a worker-owned hypothesis reference.
    try:
        invented = payload.get("target_type") == "hypothesis"
        store = hypotheses if invented else concepts
        row = store.get(int(payload.get("target_id") or 0)) if store is not None else None
        if row is None:
            return False
        current = row.statement if invented else row.label
        if current != str(payload.get("label") or label):
            return False
        if invented:
            return (row.is_live and row.linked_concept_id is None and row.asked_count <= 0
                    and usable_hypothesis(row))
        return row.status == "candidate"
    except (AttributeError, TypeError, ValueError):
        return False
