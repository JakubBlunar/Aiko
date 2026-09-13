"""Bounded concept selection for Live policy (and later shared T3 use).

Uses the same ``surface_score`` blend as conversational T3. Live callers
must pass ``habituation=1.0`` and must not call ``save_habituation`` or
write an L37 surfaced row -- reading a concept for policy is not a
conversational surfacing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.core.infra import timephrase
from app.core.concepts.concept_kinds import DEFAULT_SURFACE_WEIGHTS, get_kind
from app.core.concepts.concept_surfacing import (
    REASON_CORE,
    REASON_TOPIC,
    recency_boost,
    stability,
    surface_score,
)
from app.llm.token_utils import estimate_tokens

_CONCEPT_RENDER_OVERHEAD_TOKENS = 6


@dataclass(frozen=True, slots=True)
class PolicyConceptPick:
    concept_id: int
    kind: str
    label: str
    subject: str
    score: float
    reason: str
    cosine: float
    lane: str
    confidence: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "kind": self.kind,
            "label": self.label,
            "subject": self.subject,
            "score": self.score,
            "reason": self.reason,
            "cosine": self.cosine,
            "lane": self.lane,
            "confidence": self.confidence,
        }


def score_concept(
    concept: Any,
    *,
    cosine: float,
    habituation: float = 1.0,
    importance: float = 0.5,
    importance_strength: float = 0.0,
    now=None,
) -> float:
    """Pure ranking. Default habituation leaves the T3 clock untouched."""
    spec = get_kind(str(getattr(concept, "kind", "") or ""))
    weights = getattr(spec, "surface_weights", None) or DEFAULT_SURFACE_WEIGHTS
    confidence = float(getattr(concept, "confidence", 0.0) or 0.0)
    plasticity = float(getattr(concept, "plasticity", 0.5) or 0.0)
    stamp = getattr(concept, "last_reinforced_at", None)
    clock = now if now is not None else timephrase.utcnow()
    recency = recency_boost(
        stamp if isinstance(stamp, str) else None,
        clock,
        float(getattr(weights, "recency_halflife_days", 30.0) or 30.0),
    )
    return surface_score(
        cosine=float(cosine),
        confidence=confidence,
        recency=recency,
        stability=stability(confidence, plasticity),
        habituation=float(habituation),
        importance=float(importance),
        importance_strength=float(importance_strength),
        w=weights,
    )


def select_bounded_concepts(
    *,
    rails: Sequence[Any] = (),
    relevant: Sequence[tuple[Any, float]] = (),
    token_budget: int = 96,
    habituation: float = 1.0,
) -> tuple[PolicyConceptPick, ...]:
    """Merge rail + situation-relevant concepts under a token cap.

    Rails win on id collision so a core value is not replaced by a
    slightly closer embedding. Order is rails first, then relevant by
    score. ``habituation`` stays 1.0 for Live.
    """
    budget = max(0, int(token_budget))
    picked: dict[int, PolicyConceptPick] = {}
    order: list[int] = []

    def _take(concept: Any, *, cosine: float, reason: str, lane: str) -> None:
        cid = int(getattr(concept, "concept_id", 0) or 0)
        if cid <= 0 or cid in picked:
            return
        label = str(getattr(concept, "label", "") or "").strip()
        if not label:
            return
        pick = PolicyConceptPick(
            concept_id=cid,
            kind=str(getattr(concept, "kind", "") or ""),
            label=label,
            subject=str(getattr(concept, "subject", "") or ""),
            score=score_concept(
                concept, cosine=cosine, habituation=habituation,
            ),
            reason=reason,
            cosine=float(cosine),
            lane=lane,
            confidence=float(getattr(concept, "confidence", 0.0) or 0.0),
        )
        picked[cid] = pick
        order.append(cid)

    for concept in rails:
        _take(concept, cosine=1.0, reason=REASON_CORE, lane="rail")
    ranked_relevant = sorted(
        relevant,
        key=lambda pair: score_concept(
            pair[0], cosine=float(pair[1]), habituation=habituation,
        ),
        reverse=True,
    )
    for concept, cosine in ranked_relevant:
        _take(concept, cosine=float(cosine), reason=REASON_TOPIC, lane="situation")

    out: list[PolicyConceptPick] = []
    spent = 0
    for cid in order:
        pick = picked[cid]
        cost = estimate_tokens(pick.label) + _CONCEPT_RENDER_OVERHEAD_TOKENS
        if budget and spent + cost > budget:
            continue
        out.append(pick)
        spent += cost
    return tuple(out)
