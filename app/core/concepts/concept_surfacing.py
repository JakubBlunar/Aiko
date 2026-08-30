"""L18 + L23 concept surfacing scorer (the "cognitive surfacing" blend).

Concept surfacing used to be single-signal: the always-on core lane ranks by
*confidence* alone and the turn-relevant fill by *cosine* alone. This module
scores a concept candidate by a per-kind blend that models how a mind actually
brings a thought forward:

- **context**: cosine of the concept label to the live turn embedding.
- **confidence**: the concept's stored confidence.
- **recency**: a half-life decay boost from ``last_reinforced_at``.
- **stability** (L23): ``confidence * plasticity-adjusted`` -- a settled, sticky
  core belief is worth asserting on how *held* it is, not just relevance.
- **salience** (L23): an emotional / recent-change charge so a freshly changed
  or affect-loaded concept can intrude.
- **activation** (L23): an additive spreading-activation boost (a concept
  associated with the turn's hot topics is primed even at low direct cosine).
- **habituation** (L23 / L27): a repetition-suppression multiplier so a concept
  surfaced last turn steps aside and recovers over a few turns.
- **standing** (L38): a slowly learned prior from how reliably this concept's
  past surfacings led to an engaged next turn.
- **importance** (L32): a multiplier for how much the belief *matters* --
  the second strength axis, orthogonal to how likely it is to be true, so an
  uncertain-but-weighty concept can outrank a certain-but-trivial one. Derived
  per turn in ``concept_importance``; neutral (``0.5``) leaves a score alone.

The per-kind weights live on :class:`app.core.concepts.concept_kinds.SurfaceWeights`
(``ConceptKind.surface_weights``); the default is context-only, which reproduces
the pre-L18 cosine ranking exactly, so every existing kind is unchanged until it
opts in. The scoring helpers are intentionally pure (no store / clock dependency
beyond the ``now`` / turn index passed in) so they are trivially testable; the
habituation *state* helpers at the bottom are the one stateful seam, a thin
``kv_meta`` map keyed by concept id.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from app.core.concepts.concept_importance import (
    IMPORTANCE_NEUTRAL,
    importance_factor,
)
from app.core.concepts.concept_kinds import DEFAULT_SURFACE_WEIGHTS, SurfaceWeights

log = logging.getLogger("app.concept_surfacing")

STANDING_KV_KEY = "concept.earned_standing"
STANDING_NEUTRAL = 0.5


def _c01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


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


def stability(confidence: float, plasticity: float) -> float:
    """L23 stability term: ``confidence`` scaled by how *sticky* the concept is.

    A settled belief that is also low-plasticity (a core trait, a hard-won
    value) is worth asserting on how firmly it is held, not just on cosine to
    the turn. A fluid concept (high plasticity) contributes its confidence at
    half weight, so tastes don't masquerade as identity. Returns ``[0, 1]``.
    """
    return _c01(confidence) * (1.0 - 0.5 * _c01(plasticity))


def habituation_factor(
    turns_since: int | None, *, window: int, floor: float
) -> float:
    """L23/L27 repetition suppression: a ``[floor, 1]`` multiplier from how many
    turns ago a concept was last surfaced.

    Returns ``1.0`` (no penalty) when the concept was never surfaced
    (``turns_since is None``), when ``window <= 0``, when ``turns_since <= 0`` (a
    second look on the *same* turn), or once ``turns_since >= window`` (fully
    recovered). Suppression is strongest (``floor``) at ``turns_since == 1`` --
    surfaced on the immediately previous turn -- and ramps linearly back to
    ``1.0`` across the ``window``, so a concept steps aside for a few turns and
    returns instead of nagging every turn. ``floor`` is clamped to ``[0, 1]``.
    """
    if turns_since is None or window <= 0 or turns_since <= 0:
        return 1.0
    if turns_since >= window:
        return 1.0
    fl = _c01(floor)
    frac = (turns_since - 1) / (window - 1) if window > 1 else 1.0
    return fl + (1.0 - fl) * frac


# Per-event base "charge" for L23 salience: a recently *contradicted* concept
# intrudes hardest (a belief just clashed), a loosened boundary
# (``plasticity_shift``) and a revival next, a fresh promotion mildly. Other
# lifecycle events (discovered / dormant / retired) don't charge an *active*
# concept's surfacing. Each is decayed by recency in :func:`event_charge`.
_SALIENCE_EVENT_WEIGHTS: dict[str, float] = {
    "contradicted": 1.0,
    "plasticity_shift": 0.6,
    "revived": 0.5,
    "promoted": 0.4,
}


def event_charge_detail(
    events,
    now: datetime,
    *,
    halflife_days: float,
    weights: "dict[str, float] | None" = None,
) -> tuple[float, str | None]:
    """:func:`event_charge`, plus *which* event produced the charge.

    L35 needs the driver's name to turn a salience win into a reason a
    human can read -- "unresolved contradiction" and "recently revived"
    are the same number to the scorer and completely different stories.
    Returns ``(0.0, None)`` when nothing charged.
    """
    table = weights if weights is not None else _SALIENCE_EVENT_WEIGHTS
    best = 0.0
    driver: str | None = None
    for ev_type, created_at in events:
        base = float(table.get(str(ev_type), 0.0))
        if base <= 0.0:
            continue
        charge = base * recency_boost(created_at, now, halflife_days)
        if charge > best:
            best, driver = charge, str(ev_type)
    return _c01(best), driver


def event_charge(
    events,
    now: datetime,
    *,
    halflife_days: float,
    weights: "dict[str, float] | None" = None,
) -> float:
    """The strongest recent lifecycle-event charge for a concept, in ``[0, 1]``.

    ``events`` is an iterable of ``(event_type, created_at_iso)`` for one
    concept; each event's base weight (see ``_SALIENCE_EVENT_WEIGHTS``) is
    decayed by :func:`recency_boost` and the max is taken -- one sharp recent
    change is enough to make a concept salient, and it fades over the half-life.
    """
    return event_charge_detail(
        events, now, halflife_days=halflife_days, weights=weights
    )[0]


def salience(*, change: float = 0.0, affect: float = 0.0) -> float:
    """Blend the recent-change charge and emotional affect into a single
    ``[0, 1]`` intrusion signal via a soft-OR (``a + b - a*b``) so either alone
    lifts a concept and the two compound without ever exceeding ``1``."""
    a = _c01(change)
    b = _c01(affect)
    return a + b - a * b


def engagement_baseline(stats_by_id: dict[int, object]) -> float:
    """Relationship-local engaged rate across an outcome snapshot.

    L37's labels are not balanced classes: in the first real ledger sample
    only about 29% of settled rows were ``engaged``. Treating a raw rate of
    0.5 as the population norm would therefore push almost every item
    below neutral. The empirical baseline is the honest prior. Empty or
    malformed snapshots fall back to ``0.5`` so standing remains a no-op.

    For **cluster**-scoped consumers only (K81 taste affinity, L42 neglect).
    Per-item standing uses :func:`landing_baseline`, because the engaged
    label is a property of the turn rather than of any one item on it.
    """
    return _pooled(stats_by_id, "engaged", "settled")


def landing_baseline(stats_by_id: dict[int, object]) -> float:
    """Relationship-local echo rate across a concept outcome snapshot.

    The same shrinkage prior as :func:`engagement_baseline`, over the
    signal that is actually attributable to the item.
    """
    return _pooled(stats_by_id, "echoed", "judged")


def _pooled(stats_by_id: dict[int, object], hit: str, total: str) -> float:
    hits = 0
    seen = 0
    for stats in (stats_by_id or {}).values():
        try:
            row_total = max(0, int(getattr(stats, total, 0) or 0))
            row_hits = max(0, int(getattr(stats, hit, 0) or 0))
        except (TypeError, ValueError):
            continue
        seen += row_total
        hits += min(row_total, row_hits)
    if seen <= 0:
        return STANDING_NEUTRAL
    return _c01(hits / seen)


def earned_standing(
    *,
    landed: int,
    judged: int,
    baseline: float,
    min_judged: int = 4,
    prior_strength: float = 10.0,
    floor: float = 0.35,
    ceiling: float = 1.0,
    protect_downward: bool = False,
) -> float:
    """Return a shrunk, baseline-calibrated L38 standing score.

    ``confidence`` answers whether a concept is true; standing answers whether
    it is useful to bring forward. They must remain separate. The observed
    landing rate is shrunk toward the relationship-local ``baseline`` and then
    mapped so that baseline performance is neutral (``0.5``), a zero posterior
    reaches only ``floor``, and a perfect posterior reaches ``ceiling``.

    ``landed`` is the count of surfacings the reply **actually drew on** (the
    L37 echo verdict), out of the ``judged`` surfacings an echo test was run
    on. It used to be the count of *turns labelled engaged* that this item
    happened to be present for, which cannot work: the label belongs to the
    turn and the median turn surfaces 67 items, so every item on a good turn
    was credited equally and the resulting per-item rate was statistically
    indistinguishable from shuffling the labels at random (split-half
    reliability 0.05; the echo verdict on the same rows scores 0.60).

    The trade is real and worth naming. Engagement is the user's verdict and
    echo is only Aiko's, so rewarding echo does risk favouring what she
    already reaches for. But a reliable measure of a near-enough quantity
    beats a measure of the right quantity that carries no information, and
    the alternative was to retire standing entirely. Combining the two --
    crediting only echoes on engaged turns -- measures *worse* than echo
    alone (0.12 vs 0.48): the AND inherits the label's noise and thins the
    positive class to 5%.

    Thin samples return neutral. ``protect_downward`` is for values/boundaries:
    an uncomfortable truth or behaviour guard may earn more standing, but can
    never be suppressed because it went unquoted.
    """
    neutral = STANDING_NEUTRAL
    try:
        total = max(0, int(judged))
        hits = min(total, max(0, int(landed)))
        warmup = max(0, int(min_judged))
        raw_strength = float(prior_strength)
        raw_prior = float(baseline)
        raw_low = float(floor)
        raw_high = float(ceiling)
    except (TypeError, ValueError):
        return neutral
    if not all(
        math.isfinite(value)
        for value in (raw_strength, raw_prior, raw_low, raw_high)
    ):
        return neutral
    strength = max(0.0, raw_strength)
    prior = _c01(raw_prior)
    low = _c01(raw_low)
    high = _c01(raw_high)
    if low > high:
        low, high = high, low
    neutral = min(high, max(low, neutral))
    if total < warmup or total <= 0:
        return neutral
    denominator = total + strength
    posterior = (
        (hits + strength * prior) / denominator
        if denominator > 0.0 else prior
    )
    posterior = _c01(posterior)
    if posterior >= prior:
        span = 1.0 - prior
        mapped = (
            neutral
            if span <= 1e-9
            else neutral + (high - neutral) * (posterior - prior) / span
        )
    else:
        span = prior
        mapped = (
            neutral
            if span <= 1e-9
            else neutral - (neutral - low) * (prior - posterior) / span
        )
    score = min(high, max(low, mapped))
    if protect_downward:
        score = max(neutral, score)
    return float(score)


def apply_evidence_cluster_boost(
    pairs: Sequence[tuple[Any, float]],
    extras: Sequence[tuple[Any, float]],
) -> list[tuple[Any, float]]:
    """Merge label-cosine neighbours with evidence-grounded hits.

    H14: when the live turn's topic cluster is in an affective concept's
    cluster evidence, treat that as a context hit even if the *label*
    cosine is low. A polarity-flipped "X drains him" would otherwise lose
    to a warmer wording of the same neighborhood. For each concept id,
    keep the max cosine. Concepts only in ``extras`` are appended.
    """
    best: dict[int, tuple[Any, float]] = {}
    order: list[int] = []

    def _take(concept: Any, cosine: float) -> None:
        try:
            cid = int(getattr(concept, "concept_id", 0) or 0)
        except (TypeError, ValueError):
            return
        if cid <= 0:
            return
        prev = best.get(cid)
        if prev is None:
            best[cid] = (concept, float(cosine))
            order.append(cid)
            return
        if float(cosine) > prev[1]:
            best[cid] = (concept, float(cosine))

    for concept, cosine in pairs:
        _take(concept, cosine)
    for concept, cosine in extras:
        _take(concept, cosine)
    return [best[cid] for cid in order if cid in best]


def surface_score(
    *,
    cosine: float,
    confidence: float,
    recency: float = 0.0,
    stability: float = 0.0,
    salience: float = 0.0,
    standing: float | None = None,
    activation: float = 0.0,
    habituation: float = 1.0,
    importance: float = IMPORTANCE_NEUTRAL,
    importance_strength: float = 0.0,
    w: SurfaceWeights = DEFAULT_SURFACE_WEIGHTS,
) -> float:
    """Blend every surfacing signal into a single ``[0, 1]`` score.

    The six *ranking* signals (context, confidence, recency, stability,
    salience, standing) are sum-normalized so the base stays comparable to the
    cosine used by the other candidate sources in ``ContextBudgetSelector``.
    Standing is usefulness, never truth, so it remains beside confidence rather
    than mutating it. On top of that base, ``activation`` is an **additive**
    spreading-activation boost (scaled by ``w.activation``, outside the
    normalization so a primed concept can rise above its raw relevance), and
    two **multipliers** apply: ``habituation`` damps a just-surfaced concept,
    and ``importance`` (L32) tilts by how much the belief *matters* as opposed
    to how likely it is to be true. The result is clamped to ``[0, 1]``.

    Importance is a modulator rather than a seventh normalized term because
    the two axes answer different questions: diluting cosine with a stake
    would confuse "on topic" with "at stake", and would need a per-kind weight
    tuned across every kind. As a multiplier it needs one global
    ``importance_strength`` knob, and at the neutral importance ``0.5`` -- or
    at ``importance_strength=0.0`` -- the factor is exactly ``1.0``.

    With the default weights (context-only) and no activation/habituation/
    importance this returns exactly ``cosine`` (clamped), so the scorer is a
    no-op for any kind that hasn't opted into the blend.
    """
    total = (
        float(w.context)
        + float(w.confidence)
        + float(w.recency)
        + float(w.stability)
        + float(w.salience)
        + (float(w.standing) if standing is not None else 0.0)
    )
    if total <= 0.0:
        base = float(cosine)
    else:
        base = (
            float(w.context) * float(cosine)
            + float(w.confidence) * float(confidence)
            + float(w.recency) * float(recency)
            + float(w.stability) * float(stability)
            + float(w.salience) * float(salience)
            + (
                float(w.standing) * float(standing)
                if standing is not None else 0.0
            )
        ) / total
    boosted = base + float(w.activation) * float(activation)
    weight = importance_factor(importance, strength=importance_strength)
    return _c01(boosted * float(habituation) * weight)


# ── L35 surface reasons (why *this* concept is in the prompt) ─────────────
# The scorer above collapses seven signals into one number, which makes the
# ranking legible but the *choice* opaque: a concept in the prompt could be
# there because it matched the topic, because it was just contradicted, or
# because a neighbour primed it, and the trace couldn't tell you which.
# ``surface_reason`` names the winner. Kept as plain string constants like
# the rest of ``app/core/concepts`` (see ``concept_kinds.SUBJECTS``).
#
# Debug-only by design: the reason is stamped on the L26 trace, never handed
# to Aiko. Letting her read "I surfaced this because we clashed on it" is the
# fastest route to a companion who narrates her own machinery.

REASON_CORE = "core_belief"
REASON_TOPIC = "topic_match"
REASON_CONFIDENT = "high_confidence"
REASON_RECENT = "recently_reinforced"
REASON_SETTLED = "settled_belief"
REASON_ASSOCIATION = "association"
REASON_CONTRADICTION = "unresolved_contradiction"
REASON_REVIVED = "recently_revived"
REASON_LOOSENED = "loosening_boundary"
REASON_PROMOTED = "newly_promoted"
REASON_CHANGE = "recent_change"
# There is deliberately no ``earned_standing`` reason. L38 standing tilts
# the *ranking* and nothing else: it answers "is this worth bringing
# forward", which is a property of the surfacing machinery rather than a
# reason a reader would recognise, and L41 has no framing for it because
# "I mention this because it usually lands well" is not a thing anyone
# says. It was a candidate here for a while and won zero of 11,321 live
# surfacings -- at a weight of 0.10 against a context weight of 0.45-0.65
# it would have needed a standing above 1.0, past its own ceiling -- so
# the label promised a capability that did not exist and could not have
# been used if it had. See ``surface_score`` for where standing does act.

#: Human phrasing for the debug view, keyed by reason token.
SURFACE_REASON_LABELS: dict[str, str] = {
    REASON_CORE: "always-on core belief",
    REASON_TOPIC: "matches what we're talking about",
    REASON_CONFIDENT: "high confidence",
    REASON_RECENT: "reinforced recently",
    REASON_SETTLED: "settled, firmly held",
    REASON_ASSOCIATION: "primed by an associated topic",
    REASON_CONTRADICTION: "unresolved contradiction",
    REASON_REVIVED: "recently revived",
    REASON_LOOSENED: "boundary loosening",
    REASON_PROMOTED: "newly promoted",
    REASON_CHANGE: "changed recently",
}

#: Lifecycle events that count as *she changed her mind*, and may therefore
#: name a surfacing ahead of the weighted contest.
#:
#: ``promoted`` is deliberately absent. Promotion is the ordinary
#: candidate-to-active transition every concept undergoes exactly once, not
#: a revision of anything: on the live graph 514 active concepts had a
#: recent promotion against 80 plasticity shifts and 2 contradictions, so
#: admitting it would frame two thirds of everything she believes as
#: "lately you've come around to feeling that" and make the voice
#: meaningless. It can still win the ordinary contest and label itself
#: ``newly_promoted``; it just cannot jump the queue.
_CHANGE_NAMING_EVENTS = frozenset({
    "contradicted", "plasticity_shift", "revived",
})

#: How much decayed charge it takes for such an event to name the
#: surfacing. The charge is the event's base weight halved every
#: ``salience_halflife_days`` (21 by default), so a single floor buys each
#: driver a window in proportion to how big a change it was: roughly four
#: weeks for a contradiction, twelve days for a loosened belief, a week for
#: a revival. After that the belief goes back to being narrated as ordinary
#: relevance, which by then it is. Gates the *label*, never the score.
CHANGE_REASON_FLOOR = 0.4

#: Which salience driver maps to which reason. A salience win is only ever
#: as specific as the event behind it; anything unrecognised degrades to the
#: generic "something changed".
_EVENT_REASONS: dict[str, str] = {
    "contradicted": REASON_CONTRADICTION,
    "revived": REASON_REVIVED,
    "plasticity_shift": REASON_LOOSENED,
    "promoted": REASON_PROMOTED,
}


def surface_reason(
    *,
    lane: str,
    cosine: float = 0.0,
    confidence: float = 0.0,
    recency: float = 0.0,
    stability: float = 0.0,
    salience: float = 0.0,
    standing: float | None = None,
    activation: float = 0.0,
    recency_known: bool = True,
    change_event: str | None = None,
    change_charge: float = 0.0,
    w: SurfaceWeights = DEFAULT_SURFACE_WEIGHTS,
) -> str:
    """Name the signal that won this concept its place in the prompt.

    Two lanes answer themselves. A **core** concept is pinned on
    confidence before any scoring happens, and an **activation**-lane
    concept had no cosine to the turn at all -- it is in the prompt purely
    because a neighbour primed it. Neither ran a contest, so neither needs
    one decided.

    Everything else is the dominant term of :func:`surface_score`, decided
    on each signal's *weighted contribution* rather than its raw value: a
    cosine of 0.9 against a zero context weight didn't win anything.
    Contributions are normalized exactly the way the score is, so
    ``activation`` (additive, outside the normalization) is compared on
    the same footing as the six ranking terms.

    The two *multipliers* -- habituation and L32 importance -- are
    deliberately not candidates. They scale every term equally rather than
    competing with them, so neither can be "the signal that won"; calling
    importance a reason would claim a contest it never entered. Both are
    reported as their own fields on the L26 trace instead, where the lift
    they applied is visible without being misattributed.

    **L38 standing is not a candidate either**, though it is a genuine
    ranking term. It answers "is this worth bringing forward", which is a
    fact about the surfacing machinery rather than a reason a reader would
    recognise; it is reported on the trace as its own field. ``standing``
    stays in the signature because it belongs in ``total`` -- leaving it
    out would inflate every other term's share.

    ``recency_known=False`` drops recency from contention. Its neutral
    value is ``1.0`` -- the *highest* it goes -- so a concept that has
    simply never been reinforced would otherwise win on a missing signal.
    That default is deliberate in the score (a missing timestamp must not
    penalise) but it is not a reason for anything.

    A salience win is refined by ``change_event`` (from
    :func:`event_charge_detail`) into the specific story behind the charge.
    Ties resolve toward the more specific signal, which is why the
    candidate order below is deliberate rather than alphabetical.

    **A fresh change short-circuits the contest.** A recent contradiction
    or revival is a categorical fact about the belief, not another
    continuous signal to weigh against cosine, and the weighted contest
    could never surface one: nine of thirteen kinds set ``salience`` to
    zero (so a change was not even a candidate), and for the three that do
    weight it, out-sharing ``context`` needs a salience above ``0.75`` at a
    cosine of only ``0.3`` and is arithmetically impossible past ``0.4``.
    Across 11,321 live surfacings the entire changed family -- and
    ``unresolved_contradiction`` with it -- came back zero, against a graph
    holding 207 contradictions and 72 revivals. So a
    :data:`_CHANGE_NAMING_EVENTS` driver whose ``change_charge`` clears
    :data:`CHANGE_REASON_FLOOR` names the reason outright.

    This is the one place the answer is "the most informative true thing
    about why this belief is worth raising now" rather than strictly "the
    largest weighted term". It is deliberate: the reason drives L41's
    lead-in phrasing, where "you haven't fully settled it, but" earns its
    place over "this came up" even on a turn cosine also matched. It does
    not touch :func:`surface_score`, so *which* concepts surface, and in
    what order, is unchanged.
    """
    if lane == "core":
        return REASON_CORE
    if lane == "activation":
        return REASON_ASSOCIATION
    if (
        str(change_event or "") in _CHANGE_NAMING_EVENTS
        and float(change_charge) >= CHANGE_REASON_FLOOR
    ):
        return _salience_reason(change_event)
    total = (
        float(w.context)
        + float(w.confidence)
        + float(w.recency)
        + float(w.stability)
        + float(w.salience)
        + (float(w.standing) if standing is not None else 0.0)
    )
    if total <= 0.0:
        # Degenerate weights: the score is raw cosine, so nothing else can
        # have won -- except a pure activation pickup with no cosine at all.
        return REASON_TOPIC if cosine > 0.0 else REASON_ASSOCIATION
    # Most specific first, so an exact tie tells the more interesting story.
    ranked = [
        (float(w.salience) * _c01(salience) / total, _salience_reason(change_event)),
        (float(w.activation) * _c01(activation), REASON_ASSOCIATION),
        (float(w.stability) * _c01(stability) / total, REASON_SETTLED),
        (float(w.context) * _c01(cosine) / total, REASON_TOPIC),
        (float(w.confidence) * _c01(confidence) / total, REASON_CONFIDENT),
    ]
    if recency_known:
        ranked.insert(2, (
            float(w.recency) * _c01(recency) / total, REASON_RECENT
        ))
    best_share, best_reason = max(ranked, key=lambda pair: pair[0])
    if best_share <= 0.0:
        # Every signal is zero (or zero-weighted); the lane is the only
        # thing left that's true.
        return REASON_TOPIC
    return best_reason


def _salience_reason(change_event: str | None) -> str:
    return _EVENT_REASONS.get(str(change_event or ""), REASON_CHANGE)


def composite_score(
    *,
    cosine: float,
    confidence: float,
    recency: float,
    w: SurfaceWeights = DEFAULT_SURFACE_WEIGHTS,
) -> float:
    """Back-compat thin wrapper over :func:`surface_score` (context + confidence
    + recency only). Retained for the L18 call sites / tests; new code should
    call :func:`surface_score` with the full signal set."""
    return surface_score(
        cosine=cosine, confidence=confidence, recency=recency, w=w
    )


# ── habituation state (the one stateful seam) ─────────────────────────────
# A thin ``kv_meta`` map ``{concept_id: last_surfaced_turn}`` under the
# ``concept.*`` namespace (mirrors ``cluster_affect`` load/save). The turn index
# is the monotonic user-turn counter (``relationship.total_turns``). Surfacing
# reads it to damp just-surfaced concepts and writes it (chosen concepts only)
# at the end of ``build_relevant_context`` -- the sole write on the read path,
# analogous to ``rag.mark_surfaced``.

HABITUATION_KV_KEY = "concept.surfacing_habituation"


def load_standing(
    kv_get, key: str = STANDING_KV_KEY,
) -> dict[int, float]:
    """Load the persisted ``{concept_id: standing}`` map (empty on junk).

    Missing entries are interpreted as neutral by the scorer, so a failed read
    cannot suppress a concept or break prompt assembly.
    """
    try:
        raw = kv_get(key)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(blob, dict):
        return {}
    out: dict[int, float] = {}
    for cid, value in blob.items():
        try:
            parsed_id = int(cid)
            raw_value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(raw_value):
            continue
        parsed_value = _c01(raw_value)
        if parsed_id > 0:
            out[parsed_id] = parsed_value
    return out


def save_standing(
    kv_set,
    state: dict[int, float],
    *,
    key: str = STANDING_KV_KEY,
    cap: int = 1000,
) -> None:
    """Persist a bounded standing map; best-effort and never raises."""
    if not isinstance(state, dict):
        return
    cleaned: list[tuple[int, float]] = []
    for cid, value in state.items():
        try:
            parsed_id = int(cid)
            raw_value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(raw_value):
            continue
        parsed_value = _c01(raw_value)
        if parsed_id > 0:
            cleaned.append((parsed_id, parsed_value))
    cleaned.sort(key=lambda item: item[0])
    if cap > 0:
        cleaned = cleaned[: int(cap)]
    payload = {str(cid): round(value, 6) for cid, value in cleaned}
    try:
        kv_set(key, json.dumps(payload, separators=(",", ":")))
    except Exception:
        log.debug("save_standing failed", exc_info=True)


def load_habituation(kv_get, key: str = HABITUATION_KV_KEY) -> dict[int, int]:
    """Return the persisted ``{concept_id: last_surfaced_turn}`` map (empty on
    missing / junk). Never raises."""
    try:
        raw = kv_get(key)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(blob, dict):
        return {}
    out: dict[int, int] = {}
    for cid, turn in blob.items():
        try:
            out[int(cid)] = int(turn)
        except (TypeError, ValueError):
            continue
    return out


def turns_since_surfaced(
    state: dict[int, int], concept_id: int, current_turn: int
) -> int | None:
    """Turns elapsed since ``concept_id`` was last surfaced, or ``None`` if it
    has no record (never surfaced -> no habituation penalty)."""
    last = state.get(int(concept_id))
    if last is None:
        return None
    return max(0, int(current_turn) - int(last))


def save_habituation(
    kv_set,
    state: dict[int, int],
    *,
    key: str = HABITUATION_KV_KEY,
    cap: int = 300,
) -> None:
    """Persist the habituation map, pruning to the ``cap`` most-recently-surfaced
    concepts (highest turn index wins) so the map can't grow without bound.
    Never raises."""
    if not isinstance(state, dict):
        return
    items = sorted(state.items(), key=lambda kv: int(kv[1]), reverse=True)
    if cap > 0:
        items = items[:cap]
    payload = {str(int(cid)): int(turn) for cid, turn in items}
    try:
        kv_set(key, json.dumps(payload))
    except Exception:
        log.debug("save_habituation failed", exc_info=True)


__all__ = [
    "CHANGE_REASON_FLOOR",
    "HABITUATION_KV_KEY",
    "STANDING_KV_KEY",
    "STANDING_NEUTRAL",
    "SURFACE_REASON_LABELS",
    "apply_evidence_cluster_boost",
    "composite_score",
    "earned_standing",
    "engagement_baseline",
    "landing_baseline",
    "event_charge",
    "event_charge_detail",
    "habituation_factor",
    "load_habituation",
    "load_standing",
    "recency_boost",
    "salience",
    "save_habituation",
    "save_standing",
    "stability",
    "surface_reason",
    "surface_score",
    "turns_since_surfaced",
]
