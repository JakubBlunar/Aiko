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
from datetime import datetime, timezone

from app.core.concepts.concept_kinds import DEFAULT_SURFACE_WEIGHTS, SurfaceWeights

log = logging.getLogger("app.concept_surfacing")


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
    table = weights if weights is not None else _SALIENCE_EVENT_WEIGHTS
    best = 0.0
    for ev_type, created_at in events:
        base = float(table.get(str(ev_type), 0.0))
        if base <= 0.0:
            continue
        best = max(best, base * recency_boost(created_at, now, halflife_days))
    return _c01(best)


def salience(*, change: float = 0.0, affect: float = 0.0) -> float:
    """Blend the recent-change charge and emotional affect into a single
    ``[0, 1]`` intrusion signal via a soft-OR (``a + b - a*b``) so either alone
    lifts a concept and the two compound without ever exceeding ``1``."""
    a = _c01(change)
    b = _c01(affect)
    return a + b - a * b


def surface_score(
    *,
    cosine: float,
    confidence: float,
    recency: float = 0.0,
    stability: float = 0.0,
    salience: float = 0.0,
    activation: float = 0.0,
    habituation: float = 1.0,
    w: SurfaceWeights = DEFAULT_SURFACE_WEIGHTS,
) -> float:
    """Blend every surfacing signal into a single ``[0, 1]`` score.

    The five *ranking* signals (context, confidence, recency, stability,
    salience) are sum-normalized so the base stays comparable to the cosine used
    by the other candidate sources in ``ContextBudgetSelector``. On top of that
    base, ``activation`` is an **additive** spreading-activation boost (scaled by
    ``w.activation``, outside the normalization so a primed concept can rise
    above its raw relevance), and ``habituation`` is a **multiplier** that damps
    a just-surfaced concept. The result is clamped to ``[0, 1]``.

    With the default weights (context-only) and no activation/habituation this
    returns exactly ``cosine`` (clamped), so the scorer is a no-op for any kind
    that hasn't opted into the blend.
    """
    total = (
        float(w.context)
        + float(w.confidence)
        + float(w.recency)
        + float(w.stability)
        + float(w.salience)
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
        ) / total
    boosted = base + float(w.activation) * float(activation)
    return _c01(boosted * float(habituation))


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
    "HABITUATION_KV_KEY",
    "composite_score",
    "event_charge",
    "habituation_factor",
    "load_habituation",
    "recency_boost",
    "salience",
    "save_habituation",
    "stability",
    "surface_score",
    "turns_since_surfaced",
]
