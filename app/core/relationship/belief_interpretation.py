"""The bounded contract for tentative, situation-specific interpretations.

Sources establish what was said, not that Aiko's inference is true. Concepts
and earlier beliefs are context for novelty, never corroborating evidence.
Stored quotes are verbatim receipts; only authored prose rejects deictics.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.infra import timephrase
from app.llm.json_answers import parse_json_array_answer


VERSION = 2
SCOPE_HOURS = {"exchange": 2, "situation": 24, "ongoing": 168}


def system_prompt(name: str) -> str:
    return (
        f"Form Aiko's tentative, situation-specific interpretations of {name}'s "
        "messages. You may notice a NEW possibility or useful idea beyond established "
        "concepts; a concept is not a prerequisite. Distinguish what was said from "
        "what you infer. Never invent hidden motives, diagnoses or certainty. "
        "Affection can coexist with frustration or tiredness.\n"
        'Return ONE JSON object {"beliefs": [...]} with zero to TWO items. '
        "Zero is the default: do not fill a quota or rediscover familiar affection. "
        "Each item has:\n"
        "kind: mood or opinion; topic: a clear 2-80 character subject with named "
        "people/objects (never dangling 'caring for' or 'relationship with her');\n"
        "interpretation: a complete, qualified sentence (up to 600 characters), "
        "not an adjective fragment; reason: how the cited words suggest the reading;\n"
        "situation: what exchange or circumstances it applies to; uncertainty: "
        "a meaningful alternative or what remains unknown; revise_when: what new "
        "information would change it; response_hint: optional gentle conversational "
        "implication, never a directive to persuade, repeatedly ask or announce insight;\n"
        "scope: exchange (2 hours), situation (24 hours), or ongoing (7 days; only "
        "for an explicitly continuing circumstance); confidence: 0-1, your own "
        "uncertainty, NOT proof; novelty: what this adds to existing readings and "
        "concept baselines; existing_id: an offered belief id ONLY for the SAME "
        "meaning, situation and qualifiers, otherwise null. For an existing_id, "
        "copy its interpretation, situation, uncertainty and revise_when exactly; "
        "only supply fresh evidence/reason/novelty. A changed qualifier is a revised "
        "reading, not corroboration;\n"
        'evidence: [{"message_id": <user message id>, "quote": "exact substring"}] '
        "with one to three short user quotes. At least one must be marked NEW. "
        "Older turns can resolve context but cannot be observed again. Assistant "
        "words resolve references only, never establish the user's state.\n"
        "Compare FULL meanings, not positive tone or topic labels. Use existing_id "
        "for paraphrases and genuinely fresh support of the same reading. Preserve "
        "material differences (wanting affection, feeling secure, considering "
        "someone's comfort). If nothing new is supported, return an empty array. "
        "Known concepts and earlier beliefs are not evidence. A new idea may be "
        "tentative; it must remain connected to actual words and explicit unknowns. "
        "Only interpret circumstances still applicable at the anchored date."
    )


@dataclass(slots=True)
class Interpretation:
    kind: str
    topic: str
    interpretation: str
    reason: str
    situation: str
    uncertainty: str
    revise_when: str
    scope: str
    novelty: str
    evidence: list[dict[str, Any]]
    confidence: float
    response_hint: str = ""
    existing_id: int | None = None


def parse_interpretations(raw: str, dropped: list[str]) -> list[Interpretation] | None:
    items = parse_json_array_answer(raw, key="beliefs", item_hint_keys=("interpretation",))
    if items is None:
        return None
    out = []
    limits = {
        "topic": 80, "interpretation": 600, "reason": 400, "situation": 300,
        "uncertainty": 300, "revise_when": 300, "novelty": 300,
    }
    for item in items[:2]:
        if not isinstance(item, dict):
            dropped.append("object_required")
            continue
        fields = {key: item.get(key) for key in limits}
        if any(not isinstance(v, str) or not 2 <= len(v.strip()) <= limits[k]
               for k, v in fields.items()):
            dropped.append("incomplete_interpretation")
            continue
        fields = {k: v.strip() for k, v in fields.items()}
        if any(timephrase.has_relative_deictic(v) for v in fields.values()):
            dropped.append("relative_time")
            continue
        if len(fields["interpretation"].split()) < 4:
            dropped.append("fragment")
            continue
        if re.search(r"\b(?:for|with|about|of|to|his|her|their)\s*$", fields["topic"], re.I):
            dropped.append("unresolved_subject")
            continue
        kind, scope = item.get("kind"), item.get("scope")
        evidence = item.get("evidence")
        if kind not in ("mood", "opinion") or scope not in SCOPE_HOURS:
            dropped.append("kind_or_scope")
            continue
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 3 or any(
            not isinstance(e, dict) or type(e.get("message_id")) is not int
            or not isinstance(e.get("quote"), str) or not 8 <= len(e["quote"]) <= 240
            for e in evidence
        ):
            dropped.append("evidence_required")
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (ValueError, TypeError):
            dropped.append("confidence")
            continue
        if not math.isfinite(confidence):
            dropped.append("confidence")
            continue
        existing_id = item.get("existing_id")
        if existing_id is not None and type(existing_id) is not int:
            dropped.append("existing_id")
            continue
        hint = item.get("response_hint", "")
        if not isinstance(hint, str) or len(hint) > 300 or timephrase.has_relative_deictic(hint):
            dropped.append("response_hint")
            continue
        out.append(Interpretation(
            **fields, kind=kind, scope=scope, evidence=evidence,
            confidence=max(0.0, min(1.0, confidence)),
            response_hint=hint.strip(), existing_id=existing_id,
        ))
    return out


def is_interpretation(metadata: dict[str, Any]) -> bool:
    return metadata.get("interpretation_version") == VERSION


def expiry(metadata: dict[str, Any], observed_at: str, kind: str) -> datetime | None:
    """Applicability is bounded even after manual confirmation."""
    try:
        stamp = metadata.get("expires_at") if is_interpretation(metadata) else None
        if stamp:
            return timephrase.to_aware(datetime.fromisoformat(str(stamp)))
        base = timephrase.to_aware(datetime.fromisoformat(observed_at))
        return base + timedelta(hours=24 if kind == "mood" else 24 * 90)
    except (ValueError, TypeError):
        return None


def is_current(belief: Any, now: datetime | None = None) -> bool:
    end = expiry(belief.metadata, belief.observed_at, belief.kind)
    return end is not None and end > timephrase.to_aware(now or timephrase.utcnow())


def supporting_ids(metadata: dict[str, Any], source_id: int | None = None) -> set[int]:
    evidence = metadata.get("evidence", [])
    ids = {e["message_id"] for e in evidence if isinstance(e, dict)
           and type(e.get("message_id")) is int} if isinstance(evidence, list) else set()
    if source_id is not None:
        ids.add(source_id)
    return ids


def applicable(belief: Any, *, session_key: str, user_text: str) -> bool:
    """Bounded live relevance; no model call or embedding on the turn path."""
    if not is_current(belief) or not user_text.strip():
        return False
    meta = belief.metadata
    if is_interpretation(meta) and meta.get("session_key") != session_key:
        return False
    from app.core.proactive.knowledge_gap_notice_worker import topic_relevant

    # The literal supporting words also provide retrieval language for a reading
    # whose canonical subject is expressed differently ("quiet company" / "stay").
    surfaces = [belief.topic, meta.get("situation", "")]
    surfaces += [e.get("quote", "") for e in meta.get("evidence", []) if isinstance(e, dict)]
    return any(topic_relevant(s, user_text) for s in surfaces if isinstance(s, str) and s)
