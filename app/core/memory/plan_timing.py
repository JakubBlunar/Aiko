"""Elapsed planning windows describe relevance, never execution."""
from app.core.infra import timephrase


def historical_plan(memory, now=None):
    if getattr(memory, "temporal_type", None) != "future_plan":
        return False
    ref = now or timephrase.utcnow()
    event = timephrase.parse_iso(getattr(memory, "event_time", None))
    if event and event > ref:
        return False  # A later reschedule is not retired by an old expiry.
    meta = getattr(memory, "metadata", None) or {}
    receipt = meta.get("plan_retirement") or {}
    until = timephrase.parse_iso(getattr(memory, "relevance_until", None))
    return bool(isinstance(receipt, dict) and receipt.get("version") == 1
                and receipt.get("intended_for") == getattr(memory, "event_time", None)
                and until and until < ref)
