"""K83: a rare, overridable personal-disclosure boundary, never a task refusal."""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta


COOLDOWN = timedelta(days=30)
_REQUEST = re.compile(
    r"(?:please )?(?:(?:can|could|would) you )?tell me about your "
    r"(?P<topic>family|childhood|feelings|insecurities|private thoughts|personal memories)"
    r"(?: please)?[?.!]?",
    re.IGNORECASE,
)


def disclosure_topic(text: str) -> str:
    match = _REQUEST.fullmatch(" ".join(text.split()))
    return match.group("topic").lower() if match else ""


def eligible(
    text: str,
    *,
    source: object,
    plasticity: float,
    now: datetime,
    last_offered: str | None,
) -> bool:
    """Only a matching, established self-boundary can authorize a decline."""
    topic = disclosure_topic(text)
    if not topic:
        return False
    privacy_pattern = (
        r"(?:(?:i|aiko|she) )?(?:prefer(?:s)? to keep (?:my|her) " + re.escape(topic)
        + r" private|(?:would rather not|prefer(?:s)? not to) (?:discuss|share) (?:my|her) "
        + re.escape(topic) + r")[.!]?"
    )
    if (
        source.kind != "boundary" or source.subject != "aiko" or source.status != "active"
        or source.concept_id <= 0 or source.distinct_source_count < 2
        or not 0.85 <= float(source.confidence) <= 1.0
        or not math.isfinite(plasticity) or not 0 <= plasticity < 0.7
        or re.fullmatch(privacy_pattern, " ".join(source.label.split()), re.I) is None
    ):
        return False
    try:
        if now - datetime.fromisoformat(source.created_at) < timedelta(days=14):
            return False
        if last_offered and now - datetime.fromisoformat(last_offered) < COOLDOWN:
            return False
    except (ValueError, TypeError):
        return False
    return True


PERMISSION = (
    "Personal-disclosure permission for this turn only: you may briefly decline "
    "this specific disclosure, grounded only in the boundary above. Name your "
    "preference gently and offer another topic or a less personal alternative. "
    "Never invent a human past, trauma, or a reason the boundary does not support. "
    "This does not authorize refusing practical help, support, or any other request. "
    "If they ask again, answer within your actual knowledge; no second refusal, "
    "bargaining, guilt, punishment, or demand for reassurance."
)


def _key(host: object) -> str:
    return f"aiko.decline.{host._user_id}.offer"


def take_override(host: object) -> str:
    """Expire the one-turn permission before any following boundary detection."""
    try:
        state = json.loads(host._chat_db.kv_get(_key(host)) or "{}")
        if not state.get("offered_at") or state.get("override_sent"):
            return ""
        state["override_sent"] = True
        host._chat_db.kv_set(_key(host), json.dumps(state))
        return (
            "Any earlier personal-disclosure decline permission has expired. "
            "Answer the current request normally within your actual knowledge. "
            "If they ask again, respect that override without another refusal, "
            "bargaining, invented biography, guilt, or demands for reassurance."
        )
    except (AttributeError, TypeError, ValueError):
        return ""


def offer_for_boundary(host: object, text: str, source: object) -> str:
    if not getattr(host._settings.agent, "decline_enabled", False):
        return ""
    if not disclosure_topic(text) or source is None:
        return ""
    try:
        from app.core.concepts.concept_kinds import get_kind
        from app.core.concepts.concept_lifecycle import RelationshipSignal, effective_plasticity
        from app.core.infra import timephrase

        if host._arc_store.get_or_default(host._user_id).arc != "casual_check_in":
            return ""
        axes = host._relationship_axes_store.get(host._user_id)
        days = host._relationship_tenure_days()
        full = float(getattr(host._memory_settings, "concept_plasticity_duration_days_full", 180))
        if (
            not all(math.isfinite(value) for value in (axes.trust, days, full, source.plasticity))
            or full <= 0 or not 0 <= source.plasticity <= 1
        ):
            return ""
        plasticity = effective_plasticity(
            source.plasticity,
            signal=RelationshipSignal(trust01=axes.trust, duration01=days / full),
            mod=get_kind("boundary").plasticity_modulation,
        )
        now = timephrase.utcnow()
        state = json.loads(host._chat_db.kv_get(_key(host)) or "{}")
        if not eligible(
            text, source=source, plasticity=plasticity, now=now,
            last_offered=state.get("offered_at"),
        ):
            return ""
        host._chat_db.kv_set(_key(host), json.dumps({
            "offered_at": now.isoformat(), "override_sent": False,
            "concept_id": source.concept_id,
        }))
        logging.getLogger("app.decline_gate").info(
            "decline offered: concept_id=%d", source.concept_id,
        )
        return f"Your stored personal boundary: {source.label[:240]}\n{PERMISSION}"
    except Exception:
        logging.getLogger("app.decline_gate").debug("decline unavailable", exc_info=True)
        return ""
