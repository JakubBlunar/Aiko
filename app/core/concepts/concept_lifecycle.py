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
  then, only if the concept was reinforced since we last looked, move it
  *up toward* the evidence-deserved target by a plasticity-damped step
  (:func:`accrual_alpha`) -- so plasticity governs movement symmetrically
  in both directions (L16).
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


def accrual_alpha(plasticity: float) -> float:
    """Plasticity-damped accrual step: the fraction of the gap to the
    evidence target a *reinforced* concept closes in one evaluation.

    The upward sibling of :func:`effective_halflife`'s decay damping, so
    plasticity governs confidence movement symmetrically in *both*
    directions (L16). ``plasticity`` in [0, 1] maps to ``0.5 + 0.5*p``:
    ``p=1`` => ``1.0`` (a full snap straight to target -- the pre-L16
    behaviour, so high-plasticity kinds are unchanged), ``p=0`` => ``0.5``
    (a half-step -- a sticky core trait needs several reinforced evals to
    build up, i.e. "needs more evidence to promote").
    """
    p = min(1.0, max(0.0, float(plasticity)))
    return 0.5 + 0.5 * p


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
    moves *up toward* ``target`` by a plasticity-damped step
    (:func:`accrual_alpha`) -- a full snap for high-plasticity kinds, a
    partial approach for sticky (low-plasticity) ones. ``max`` guards the
    up-move so reinforcement can only raise confidence (never drag a
    concept already above its evidence target back down).
    """
    hl = effective_halflife(halflife_days, plasticity)
    decayed = float(current) * (0.5 ** (max(0.0, float(engaged_days)) / hl))
    if reinforced:
        up = decayed + (float(target) - decayed) * accrual_alpha(plasticity)
        decayed = max(decayed, up)
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


# Minimum bars a *value* concept must clear on top of whatever the caller
# passes. Values are the stickiest, hardest-won concepts (L10) -- a stated
# principle should be slow to assert -- so this floors the ``set`` gate at a
# stricter point than identity even when the global promote settings are
# relaxed. The L21 young-graph tightening still layers on top (the caller may
# hand in higher values, which win via ``max``).
_VALUE_MIN_SOURCES = 3
_VALUE_MIN_AGE_DAYS = 1.0
_VALUE_MIN_CONFIDENCE = 0.72


def value_evidence_gate(
    *,
    distinct_source_count: int,
    age_days: float,
    confidence: float,
    min_sources: int,
    min_age_days: float,
    min_confidence: float,
) -> bool:
    """Promotion predicate for ``value`` concepts (L10).

    Same shape as :func:`set_evidence_gate`, but with stricter built-in
    floors so a value ("the principle under the choices") only promotes once
    it is genuinely well-evidenced and settled -- more distinct sources, a
    non-instant age, and a higher confidence than identity. The caller's
    thresholds still apply when they are *higher* (e.g. the L21 young-graph
    bar), via ``max``.
    """
    return set_evidence_gate(
        distinct_source_count=distinct_source_count,
        age_days=age_days,
        confidence=confidence,
        min_sources=max(int(min_sources), _VALUE_MIN_SOURCES),
        min_age_days=max(float(min_age_days), _VALUE_MIN_AGE_DAYS),
        min_confidence=max(float(min_confidence), _VALUE_MIN_CONFIDENCE),
    )


# Built-in bars for an *affective* concept (L13). Affect is the *fluid* end
# of the plasticity spectrum -- a topic's emotional weather shifts faster
# than an identity trait -- so the floors are gentler than value's: it only
# needs a modest age (enough that a one-off mood doesn't stick) and a
# moderate confidence, while still requiring at least two distinct sources so
# a single beat never becomes a durable "this topic always feels X".
_AFFECTIVE_MIN_SOURCES = 2
_AFFECTIVE_MIN_AGE_DAYS = 0.5
_AFFECTIVE_MIN_CONFIDENCE = 0.6


def affective_evidence_gate(
    *,
    distinct_source_count: int,
    age_days: float,
    confidence: float,
    min_sources: int,
    min_age_days: float,
    min_confidence: float,
) -> bool:
    """Promotion predicate for ``affective`` concepts (L13).

    Same shape as :func:`set_evidence_gate` with fluid-end floors (a lower
    age + confidence bar than :func:`value_evidence_gate`, since a topic's
    affect is meant to move). The caller's thresholds still apply when they
    are *higher* (e.g. the L21 young-graph bar), via ``max``.
    """
    return set_evidence_gate(
        distinct_source_count=distinct_source_count,
        age_days=age_days,
        confidence=confidence,
        min_sources=max(int(min_sources), _AFFECTIVE_MIN_SOURCES),
        min_age_days=max(float(min_age_days), _AFFECTIVE_MIN_AGE_DAYS),
        min_confidence=max(float(min_confidence), _AFFECTIVE_MIN_CONFIDENCE),
    )


# Built-in bars for a *ritual* concept (L7). A relationship ritual ("Friday
# debugging evenings") is only real once it has *recurred* -- so it needs
# several distinct shared moments as evidence and a non-instant age (a couple
# of moments in one sitting isn't a ritual yet), but a moderate confidence
# bar, since these are warm impressions the two share, not hard-won values.
_RITUAL_MIN_SOURCES = 3
_RITUAL_MIN_AGE_DAYS = 1.0
_RITUAL_MIN_CONFIDENCE = 0.65


def ritual_evidence_gate(
    *,
    distinct_source_count: int,
    age_days: float,
    confidence: float,
    min_sources: int,
    min_age_days: float,
    min_confidence: float,
) -> bool:
    """Promotion predicate for ``ritual`` concepts (L7).

    Same shape as :func:`set_evidence_gate` with recurrence-flavoured floors:
    at least three distinct shared moments (a pattern, not a one-off), a
    non-instant calendar age so a burst in a single session can't promote,
    and a moderate confidence bar. The caller's thresholds still apply when
    they are *higher* (e.g. the L21 young-graph bar), via ``max``.
    """
    return set_evidence_gate(
        distinct_source_count=distinct_source_count,
        age_days=age_days,
        confidence=confidence,
        min_sources=max(int(min_sources), _RITUAL_MIN_SOURCES),
        min_age_days=max(float(min_age_days), _RITUAL_MIN_AGE_DAYS),
        min_confidence=max(float(min_confidence), _RITUAL_MIN_CONFIDENCE),
    )


# Built-in bars for a *narrative* concept (L8). A narrative is an ordered
# causal chain of episodic memories collapsed into one named arc -- so unlike
# the set kinds, ``distinct_source_count`` here is the *chain length*. A real
# arc needs at least three steps (a beginning, a middle, and a resolution --
# two memories is an anecdote, not a story), a non-instant age so a burst in one
# session can't promote, and a moderate confidence bar. The *closed*-ness of
# the arc is judged upstream by the proposer (it only emits a NEW arc the LLM
# marked as resolved); this gate enforces the structural floor.
_NARRATIVE_MIN_SOURCES = 3
_NARRATIVE_MIN_AGE_DAYS = 1.0
_NARRATIVE_MIN_CONFIDENCE = 0.6


def narrative_evidence_gate(
    *,
    distinct_source_count: int,
    age_days: float,
    confidence: float,
    min_sources: int,
    min_age_days: float,
    min_confidence: float,
) -> bool:
    """Promotion predicate for ``narrative`` concepts (L8).

    Same shape as :func:`set_evidence_gate` but read over an ordered chain:
    ``distinct_source_count`` is the number of distinct memories in the arc, so
    the ``>= 3`` floor means "a chain, not a pair". Age + confidence floors as
    above. The caller's thresholds still apply when they are *higher* (e.g. the
    L21 young-graph bar), via ``max``.
    """
    return set_evidence_gate(
        distinct_source_count=distinct_source_count,
        age_days=age_days,
        confidence=confidence,
        min_sources=max(int(min_sources), _NARRATIVE_MIN_SOURCES),
        min_age_days=max(float(min_age_days), _NARRATIVE_MIN_AGE_DAYS),
        min_confidence=max(float(min_confidence), _NARRATIVE_MIN_CONFIDENCE),
    )


# Built-in bars for an *aspiration* concept (L14). Aspiration is the
# open-ended sibling of narrative -- an ordered chain that shows a *direction*
# rather than a resolved arc -- so its structural floors mirror narrative's,
# with one difference: a trajectory must be *sustained* to be believable, so
# the age floor is higher (a direction seen only over a day or two is noise,
# not a trajectory). ``distinct_source_count`` is again the chain length. The
# "consistent direction" judgement itself lives upstream (the proposer only
# emits a NEW aspiration the LLM marked ``directional``) plus a minimum
# evidence *span* enforced by the worker; this gate is the structural floor.
_ASPIRATION_MIN_SOURCES = 3
_ASPIRATION_MIN_AGE_DAYS = 3.0
_ASPIRATION_MIN_CONFIDENCE = 0.6


def aspiration_evidence_gate(
    *,
    distinct_source_count: int,
    age_days: float,
    confidence: float,
    min_sources: int,
    min_age_days: float,
    min_confidence: float,
) -> bool:
    """Promotion predicate for ``aspiration`` concepts (L14).

    Same shape as :func:`narrative_evidence_gate` (``distinct_source_count``
    is the ordered chain length), but with a higher age floor so a direction
    has to persist before it promotes. The caller's thresholds still win when
    they are higher, via ``max``.
    """
    return set_evidence_gate(
        distinct_source_count=distinct_source_count,
        age_days=age_days,
        confidence=confidence,
        min_sources=max(int(min_sources), _ASPIRATION_MIN_SOURCES),
        min_age_days=max(float(min_age_days), _ASPIRATION_MIN_AGE_DAYS),
        min_confidence=max(float(min_confidence), _ASPIRATION_MIN_CONFIDENCE),
    )


__all__ = [
    "CONFIDENCE_CAP",
    "confidence_target",
    "effective_halflife",
    "accrual_alpha",
    "next_confidence",
    "apply_contradiction_penalty",
    "set_evidence_gate",
    "value_evidence_gate",
    "affective_evidence_gate",
    "ritual_evidence_gate",
    "narrative_evidence_gate",
    "aspiration_evidence_gate",
]
