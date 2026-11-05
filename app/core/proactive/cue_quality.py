"""Admission for generated exploratory cues, without overriding curated moves."""
from __future__ import annotations


def seed_usable(payload: dict) -> bool:
    # The seed worker has always stamped source=llm. Manually queued topics
    # remain curated inputs; they need not impersonate a generated record.
    if payload.get("source") != "llm":
        return True
    quality = payload.get("quality")
    return (isinstance(quality, dict) and quality.get("version") == 1
            and isinstance(quality.get("useful_because"), str)
            and bool(quality["useful_because"].strip()))


def connection_usable(payload: dict) -> bool:
    if "connection" not in payload:
        return True  # An explicitly queued move, not the wander worker's output.
    value = payload.get("useful_because")
    return isinstance(value, str) and bool(value.strip())


def inventory_demand(producer, admissible, *, needs_llm=False):
    """Let a full legacy shelf receive a normal cleanup/replenishment run.

    Admission is read-only. Without this, unusable stock could suppress its
    own replacement until TTL even though delivery correctly refuses it.
    """
    signal = producer.demand(needs_llm=needs_llm)
    if signal is None or signal.pressure > 0:
        return signal
    if any(not admissible(row.payload) for row in producer.stock_rows(limit=200)):
        from app.core.proactive.idle_worker import WorkSignal

        return WorkSignal(pressure=0.2, reason="unreviewed_stock", needs_llm=needs_llm)
    return signal
