"""K77: rare permission for candor about a grounded, low-stakes taste."""
from __future__ import annotations

import math
import logging
from datetime import datetime, timedelta


COOLDOWN = timedelta(days=14)


def eligible(
    *,
    trust: float,
    tenure_days: float,
    source_kind: str,
    definite: bool,
    now: datetime,
    last_offered: str | None,
) -> bool:
    """Fail closed on uncertainty; taste is not a calibrated factual judgment."""
    if source_kind != "taste" or not definite:
        return False
    if not math.isfinite(trust) or not math.isfinite(tenure_days):
        return False
    if trust < 0.65 or tenure_days < 30:
        return False
    if last_offered:
        try:
            if now - datetime.fromisoformat(last_offered) < COOLDOWN:
                return False
        except (ValueError, TypeError):
            return False
    return True


PERMISSION = (
    "Candor permission, only about the grounded taste above: you may briefly ask "
    "whether they want your candid take. Wait for their agreement before expanding. "
    "Keep it kind, subjective, and easy to decline; silence is not consent. "
    "Do not infer a flaw in them, diagnose a pattern, or extend this permission "
    "to health, safety, finances, or their relationships. This is optional, "
    "never a reason to withhold requested help."
)


def offer_for_stance(host: object, result: object) -> str:
    """Refine K29's existing T6 offer, without creating another topical cue."""
    if not getattr(host._settings.agent, "candor_gate_enabled", False):
        return ""
    try:
        from app.core.concepts.concept_view import concept_view_from
        from app.core.infra import timephrase

        if result.stance_origin != "concept" or result.stance_memory_id >= 0:
            return ""
        view = concept_view_from(host)
        if view is None or not view.enabled:
            return ""
        source = next(
            (row for row in view.for_consumer("stance")
             if row.concept_id == -result.stance_memory_id),
            None,
        )
        if (
            source is None or source.subject != "aiko"
            or not 0.75 <= float(source.confidence) <= 1.0
            or " ".join(source.label.split()) != result.stance_text
        ):
            return ""
        axes = host._relationship_axes_store.get(host._user_id)
        now = timephrase.utcnow()
        key = f"aiko.candor.{host._user_id}.last_offered"
        if not eligible(
            trust=axes.trust, tenure_days=host._relationship_tenure_days(),
            source_kind=source.kind,
            definite=result.trigger == "contradiction_definite",
            now=now, last_offered=host._chat_db.kv_get(key),
        ):
            return ""
        host._chat_db.kv_set(key, now.isoformat())
        logging.getLogger("app.candor_gate").info(
            "candor offered: concept_id=%d", source.concept_id,
        )
        return PERMISSION
    except Exception:
        logging.getLogger("app.candor_gate").debug("candor unavailable", exc_info=True)
        return ""
