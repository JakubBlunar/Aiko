"""Pure lifecycle math for the L3 concept engine.

Kept free of any store / settings imports so it can be unit-tested in
isolation and imported by the kind registry without a cycle. The L3
worker (:mod:`app.core.concepts.concept_lifecycle_worker`) orchestrates;
these are the stateless pieces:

- :func:`confidence_target` -- the saturating "confidence this evidence
  deserves" (a logistic of *distinct* source count; diminishing returns
  so repetition alone can't run confidence away -- the anti-bias core).
- :func:`next_confidence` -- one incremental step: decay the stored
  confidence over the *engaged* time elapsed (damped by plasticity),
  then, only if the concept was reinforced since we last looked, snap it
  up to the evidence-deserved target.
- :func:`set_evidence_gate` -- the ``set``-evidence promotion predicate
  (distinct sources + calendar-age stability + confidence), attached to
  the ``identity`` kind's ``promotion_gate`` slot.
"""
from __future__ import annotations

import math

# Confidence saturates below 1.0 so a concept is never "certain"; matches
# the memory-confidence ceiling convention.
CONFIDENCE_CAP = 0.97
# Logistic shape for target(distinct_sources): tuned so 1 source ~0.39,
# 2 ~0.61, 3 ~0.79, 4 ~0.90, then the cap bites. Diminishing returns.
_LOGISTIC_K = 0.9
_LOGISTIC_S0 = 1.5


def confidence_target(
    distinct_source_count: int,
    *,
    k: float = _LOGISTIC_K,
    s0: float = _LOGISTIC_S0,
    cap: float = CONFIDENCE_CAP,
) -> float:
    """Saturating confidence the current evidence deserves.

    A logistic of the *distinct* source count (diversity, not raw
    repetition), capped at ``cap``. Zero sources floors at the logistic's
    low tail rather than 0 so a freshly-seeded candidate isn't stuck.
    """
    n = max(0, int(distinct_source_count))
    raw = 1.0 / (1.0 + math.exp(-k * (n - s0)))
    return min(cap, max(0.0, raw))


def effective_halflife(halflife_days: float, plasticity: float) -> float:
    """Plasticity-damped half-life: low-plasticity concepts decay slower
    (are "stickier"). ``plasticity`` in [0, 1] maps to a [2x .. 1x]
    multiplier on the base half-life."""
    p = min(1.0, max(0.0, float(plasticity)))
    return max(1e-6, float(halflife_days) * (2.0 - p))


def next_confidence(
    current: float,
    *,
    engaged_days: float,
    halflife_days: float,
    plasticity: float,
    target: float,
    reinforced: bool,
    cap: float = CONFIDENCE_CAP,
) -> float:
    """One incremental confidence step (decay, then accrual-on-reinforce).

    ``engaged_days`` is active-conversation time since this concept was
    last evaluated (already clamped by the caller). Decay is
    ``confidence * 0.5 ** (engaged_days / effective_halflife)``; if the
    concept gained fresh evidence since we last looked, confidence then
    snaps *up* to ``target`` (never below what the evidence deserves).
    """
    hl = effective_halflife(halflife_days, plasticity)
    decayed = float(current) * (0.5 ** (max(0.0, float(engaged_days)) / hl))
    if reinforced:
        decayed = max(decayed, float(target))
    return min(cap, max(0.0, decayed))


def apply_contradiction_penalty(
    current: float,
    *,
    penalty: float,
    plasticity: float,
    floor: float = 0.0,
    cap: float = CONFIDENCE_CAP,
) -> float:
    """One downward confidence step from confirmed counter-evidence (L9).

    Sibling to :func:`next_confidence`, but for *disproof* rather than
    decay: a memory that contradicts the belief knocks its confidence
    down by a plasticity-damped ``penalty``. Sticky (low-plasticity)
    beliefs resist -- the effective drop scales with plasticity so even a
    firm identity belief still moves, just slower. Plasticity in [0, 1]
    maps to a [0.5x .. 1x] multiplier on ``penalty``. Clamped to
    ``[floor, cap]``; the lifecycle worker keys the ``contradicted``
    transition off the resulting confidence, not off this function.
    """
    p = min(1.0, max(0.0, float(plasticity)))
    effective = max(0.0, float(penalty)) * (0.5 + 0.5 * p)
    stepped = float(current) - effective
    return min(cap, max(float(floor), stepped))


def set_evidence_gate(
    *,
    distinct_source_count: int,
    age_days: float,
    confidence: float,
    min_sources: int,
    min_age_days: float,
    min_confidence: float,
) -> bool:
    """Promotion predicate for ``set``-evidence kinds (e.g. identity).

    A candidate promotes to ``active`` only once it draws on enough
    *distinct* sources, has been around long enough to be stable (not a
    flash in the pan), and its confidence clears the bar. Age is
    calendar-based on purpose: it only ever *delays* promotion, so
    intermittent uptime is harmless.
    """
    return (
        int(distinct_source_count) >= int(min_sources)
        and float(age_days) >= float(min_age_days)
        and float(confidence) >= float(min_confidence)
    )


__all__ = [
    "CONFIDENCE_CAP",
    "confidence_target",
    "effective_halflife",
    "next_confidence",
    "apply_contradiction_penalty",
    "set_evidence_gate",
]
