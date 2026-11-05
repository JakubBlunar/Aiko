"""Eligibility for retiring an interpretation that never established itself.

Retirement is not disproof or deletion. Source memories and the concept's
history survive, and genuinely new support can bring it back through admission.
"""
from __future__ import annotations


def candidate_retirement_reason(*, age_days: float, ttl_days: float, sources: int,
                                min_sources: int, confidence: float, retire_floor: float,
                                quiet_wall_days: float | None) -> str | None:
    """Preserve the thin-source route; add a conservative weak-support route.

    Age uses the lifecycle's engaged-time clock (calendar fallback when disabled).
    The additional quiet window is calendar time since last reinforcement, like
    dormant expiry. Both windows use the configured candidate TTL. Recent support
    protects a weak candidate even when its original evidence is old.
    """
    if (ttl_days <= 0 or age_days < ttl_days or quiet_wall_days is None
            or quiet_wall_days < ttl_days):
        return None
    if sources < min_sources:
        return "insufficient_sources"
    if confidence < retire_floor:
        return "weak_without_recent_support"
    return None
