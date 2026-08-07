"""L30a - ranking the beliefs Aiko is still working out (derived, not stored).

Every surfacing path before this one reads ``status="active"`` only, so a
``candidate`` concept is *structurally hidden* rather than merely hedged.
But that is exactly the material a mind treats as a **hypothesis**: "I
think he might be into X, but I'm not sure yet." This module scores which
of those open questions is worth holding up on a given turn.

**Why not a confidence threshold.** The L30 sketch proposed selecting rows
under a ``hypothesis_max_confidence`` of roughly 0.6. Measured against a
real graph that selects almost nothing useful: only 2 of 388 active
concepts sat below 0.6, and the candidate pool's *median* confidence was
0.82. The proposer's confidence answers "is this a well-formed belief?",
not "have we established it?", so thresholding it surfaces the worst-
written candidates rather than the genuinely open questions.

**Why age is excluded.** The far bigger trap. A ``candidate`` is usually
not a doubt -- it is a belief that has not sat still long enough.
``concept_promote_min_age_days`` holds proposals for two engaged days (three
for ``aspiration``), and on the same measured graph **238 of 261 candidates
already cleared every evidence and confidence bar** and were waiting only on
that clock. Counting age as uncertainty would fill the lane with beliefs
Aiko is not actually unsure about, which is the "blurt a half-formed model"
failure L21 warns against. So :func:`unsettledness` reads evidence breadth
and conviction *only*: a belief is an open question when it is thinly
grounded, not when it is merely young.

Ranking multiplies three things -- turn relevance (cosine), how much the
belief would matter if it held (L32 importance), and how far it is from
settling. All three have to be non-trivial for a hypothesis to earn the one
slot the lane gets, which is what keeps it from becoming an interrogation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.concepts.concept_importance import (
    IMPORTANCE_NEUTRAL,
    importance_factor,
)

log = logging.getLogger("app.concept_hypothesis")


#: Evidence breadth at which a belief counts as fully grounded, for the
#: purpose of *this* score only. Matches the strictest common per-kind
#: promotion bar (``identity`` / ``value`` / ``aspiration`` all require
#: three distinct sources), so "settled" here means what the lifecycle
#: engine means by it rather than inventing a second standard.
SETTLED_SOURCES = 3

#: Conviction at which a belief counts as fully held, mirroring
#: ``concept_promote_young_min_confidence``.
SETTLED_CONFIDENCE = 0.72

#: How the two shortfalls split. Evidence leads because breadth of
#: grounding is the thing a *question* can actually fix -- asking the user
#: adds a source. Conviction is downstream of that.
_EVIDENCE_WEIGHT = 0.6
_CONFIDENCE_WEIGHT = 0.4

#: Confirmations at which an *invented* hypothesis counts as grounded.
#: The graduation bar rather than ``SETTLED_SOURCES``, because that is
#: genuinely all the evidence a guess can ever reach: a hypothesis has no
#: clusters and no edges, only the answers it collected. Holding it to a
#: three-source standard would leave every invention pinned near maximum
#: unsettledness right up to the moment it graduated.
_SETTLED_CONFIRMATIONS = 2


def _c01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _grounding(row: object) -> tuple[float, float] | None:
    """``(evidence_ratio, conviction)`` for an *invented* row, or ``None``.

    The one place :func:`unsettledness` knows two row shapes exist. A
    hypothesis carries ``support_count`` / ``credence`` where a concept
    carries ``distinct_source_count`` / ``confidence``, and the pair is
    detected by ``credence`` because that name exists on nothing else --
    the two numbers answer different questions and never share a column
    (see :mod:`app.core.concepts.hypothesis_store`).
    """
    if not hasattr(row, "credence"):
        return None
    try:
        support = int(getattr(row, "support_count", 0) or 0)
        credence = float(getattr(row, "credence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 1.0, 1.0
    return _c01(support / float(_SETTLED_CONFIRMATIONS)), credence


def unsettledness(
    concept: object,
    *,
    settled_sources: int = SETTLED_SOURCES,
    settled_confidence: float = SETTLED_CONFIDENCE,
) -> float:
    """How far a belief is from settling, in ``[0, 1]``.

    ``0.0`` is a belief that is fully grounded and fully held (whatever
    its ``status`` -- it may still be a candidate waiting out the age
    floor). ``1.0`` is one with no sources and no conviction.

    Accepts both row shapes: a concept's ``distinct_source_count`` /
    ``confidence``, or an invented hypothesis's ``support_count`` /
    ``credence`` against the graduation bar. **Age is deliberately
    absent** from both; see the module docstring for the measurement
    behind that. Anything unreadable degrades to the settled end, so a
    malformed row stays quiet rather than presenting itself as a
    fascinating open question.
    """
    invented = _grounding(concept)
    if invented is not None:
        evidence_ratio, conviction = invented
        return _c01(
            1.0
            - (
                _EVIDENCE_WEIGHT * evidence_ratio
                + _CONFIDENCE_WEIGHT
                * _c01(conviction / max(1e-6, float(settled_confidence)))
            )
        )
    try:
        sources = int(getattr(concept, "distinct_source_count", 0) or 0)
    except (TypeError, ValueError):
        sources = int(settled_sources)
    try:
        conviction = float(getattr(concept, "confidence", 1.0) or 0.0)
    except (TypeError, ValueError):
        conviction = float(settled_confidence)
    source_bar = max(1, int(settled_sources))
    conf_bar = max(1e-6, float(settled_confidence))
    evidence_ratio = _c01(sources / source_bar)
    conviction_ratio = _c01(conviction / conf_bar)
    settled = (
        _EVIDENCE_WEIGHT * evidence_ratio
        + _CONFIDENCE_WEIGHT * conviction_ratio
    )
    return _c01(1.0 - settled)


def hypothesis_score(
    *,
    cosine: float,
    unsettled: float,
    importance: float = IMPORTANCE_NEUTRAL,
    importance_strength: float = 0.0,
    habituation: float = 1.0,
) -> float:
    """Rank one open question for this turn.

    ``cosine * unsettled * importance_factor * habituation``. A product,
    not a weighted sum: each term is a veto. An off-topic hypothesis
    should not surface however important, a settled belief should not
    surface however on-topic, and a trivial one should not displace the
    confident lane just for being uncertain. A sum would let any single
    strong term carry a candidate into the prompt, which is precisely how
    a hypothesis lane turns into noise.

    ``habituation`` is the L23 factor, folded in here rather than applied
    by the caller so the "recently shown" discount lands before the cap
    rather than after it.
    """
    weight = importance_factor(importance, strength=importance_strength)
    return _c01(
        _c01(cosine) * _c01(unsettled) * float(weight) * float(habituation)
    )


@dataclass(frozen=True, slots=True)
class HypothesisDetail:
    """One scored open question with its inputs kept separable, so the
    concept trace can explain the pick instead of showing one number."""

    concept_id: int
    score: float
    cosine: float
    unsettled: float
    importance: float
    habituation: float

    def as_trace(self) -> dict[str, float | int]:
        return {
            "lane": "hypothesis",
            "score": round(self.score, 4),
            "cosine": round(self.cosine, 4),
            "unsettled": round(self.unsettled, 4),
            "importance": round(self.importance, 4),
            "habituation": round(self.habituation, 4),
        }


__all__ = [
    "SETTLED_CONFIDENCE",
    "SETTLED_SOURCES",
    "HypothesisDetail",
    "hypothesis_score",
    "unsettledness",
]
