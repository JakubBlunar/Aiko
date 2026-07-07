"""L18 composite surfacing scorer.

Concept surfacing used to be single-signal: the always-on core lane ranks by
*confidence* alone and the turn-relevant fill by *cosine* alone. Behaviour
concepts (boundary) want a richer blend -- a line the user was just reminded of
should outrank an equally-relevant but stale one -- so this module scores a
turn-relevant concept candidate by a normalized mix of three signals:

- **context**: cosine of the concept label to the live turn embedding.
- **confidence**: the concept's stored confidence.
- **recency**: a half-life decay boost from ``last_reinforced_at``.

The per-kind weights live on :class:`app.core.concepts.concept_kinds.ConceptKind`
(``surface_weights``); the default is context-only, which reproduces the
pre-L18 cosine ranking exactly, so every existing kind is unchanged until it
opts in. This module is intentionally pure (no store / clock dependency beyond
the ``now`` passed in) so it is trivially testable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.concepts.concept_kinds import DEFAULT_SURFACE_WEIGHTS, SurfaceWeights


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 -> aware ``datetime`` (``None`` on junk)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def recency_boost(
    last_reinforced_at: str | None,
    now: datetime,
    halflife_days: float,
) -> float:
    """A ``(0, 1]`` freshness weight from ``last_reinforced_at``.

    ``1.0`` right after reinforcement, decaying by half every
    ``halflife_days``. Returns ``1.0`` (neutral, no penalty) when the timestamp
    is missing or unparseable, or when ``halflife_days <= 0`` -- a missing
    signal should never *suppress* a concept, only a present-but-old one should
    rank lower relative to a fresh sibling.
    """
    if halflife_days <= 0.0:
        return 1.0
    ts = _parse_iso(last_reinforced_at)
    if ts is None:
        return 1.0
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - ts).total_seconds() / 86_400.0)
    return float(0.5 ** (age_days / float(halflife_days)))


def composite_score(
    *,
    cosine: float,
    confidence: float,
    recency: float,
    w: SurfaceWeights = DEFAULT_SURFACE_WEIGHTS,
) -> float:
    """Blend the three surfacing signals into a single ``[0, 1]`` score.

    Weights are sum-normalized so the result stays in the same range as the
    cosine used by the other candidate sources in ``ContextBudgetSelector``.
    With the default weights (context-only) this returns exactly ``cosine``, so
    the scorer is a no-op for any kind that hasn't opted into the blend.
    """
    total = float(w.context) + float(w.confidence) + float(w.recency)
    if total <= 0.0:
        return float(cosine)
    blended = (
        float(w.context) * float(cosine)
        + float(w.confidence) * float(confidence)
        + float(w.recency) * float(recency)
    )
    return blended / total


__all__ = ["composite_score", "recency_boost"]
