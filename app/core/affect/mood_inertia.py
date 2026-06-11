"""K45 — mood inertia: instant face, lagging heart.

``[[reaction:X]]`` can jump excited -> sad -> calm on consecutive turns;
the avatar and TTS follow the *instant* tag while :class:`AffectState`
smooths with alpha=0.35 — so the face teleports and the underlying
feeling lags a turn behind. Humans are the opposite: expressions are
fast but residue *lingers*.

This module is the pure half: derive the (valence, arousal) point a
reaction tag *implies*, measure how far it sits from the smoothed felt
state, and render the one-shot prompt cue. No I/O, no controller state —
the post-turn hook in ``post_turn_mixin`` owns the ring + cooldown and
the inner-life provider owns the one-shot slot.

The same implied targets ship to the frontend via the avatar manifest
(``reaction_affect_targets``) so ``ExpressionChannel`` can damp the
expression amplitude proportionally without a TS mirror table.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.affect.affect_state import _REACTION_IMPULSE, felt_phrase

# Normalisation constants: the impulse table tops out at |dv|=0.18 and
# |da|=0.20, so dividing by these stretches the per-turn nudge into the
# full-range "where this reaction points" target.
_VALENCE_IMPULSE_SCALE = 0.15
_AROUSAL_IMPULSE_SCALE = 0.20
# Arousal targets pivot around the resting baseline (AffectState's
# default arousal), with the positive direction saturating at 1.0.
_AROUSAL_BASELINE = 0.4
_AROUSAL_SWING = 0.6

# Reactions whose impulse magnitude is below this are too weak to imply
# a direction at all (neutral, thoughtful) — no mismatch possible.
_MIN_IMPULSE_MAGNITUDE = 0.06

# Mismatch weighting: valence disagreement reads as a bigger lie than
# arousal disagreement ("beaming while heavy-hearted" vs "beaming a
# little too energetically").
_VALENCE_WEIGHT = 1.0
_AROUSAL_WEIGHT = 0.5
# Max weighted distance: valence diff spans 2.0, arousal diff spans 1.0.
_DISTANCE_NORM = math.hypot(_VALENCE_WEIGHT * 2.0, _AROUSAL_WEIGHT * 1.0)

# A whiplash sequence (excited -> sad -> cheerful) makes the same
# mismatch feel worse; bump the effective score before banding.
_WHIPLASH_BONUS = 0.15

# Mild band sits at this fraction of the strong threshold.
_MILD_FRACTION = 0.66

DEFAULT_STRONG_THRESHOLD = 0.45


@dataclass(frozen=True, slots=True)
class InertiaResult:
    """Outcome of one post-turn inertia assessment."""

    mismatch: float        # effective score in [0, 1] (whiplash included)
    raw_mismatch: float    # distance-only score in [0, 1]
    whiplash: bool         # recent tags flip valence direction turn-to-turn
    band: str              # "none" | "mild" | "strong"


def reaction_affect_target(reaction: str) -> tuple[float, float] | None:
    """Implied (valence, arousal) point of a reaction tag.

    Returns ``None`` for unknown reactions and for tags whose impulse is
    too weak to imply a direction (``neutral``, ``thoughtful``) — those
    can never produce a mismatch.
    """
    impulse = _REACTION_IMPULSE.get((reaction or "").strip().lower())
    if impulse is None:
        return None
    dv, da = impulse
    if math.hypot(dv, da) < _MIN_IMPULSE_MAGNITUDE:
        return None
    target_valence = _clamp(dv / _VALENCE_IMPULSE_SCALE, -1.0, 1.0)
    target_arousal = _clamp(
        _AROUSAL_BASELINE + (da / _AROUSAL_IMPULSE_SCALE) * _AROUSAL_SWING,
        0.0,
        1.0,
    )
    return (target_valence, target_arousal)


def reaction_affect_targets() -> dict[str, tuple[float, float]]:
    """Every known reaction's implied target (for the avatar manifest)."""
    out: dict[str, tuple[float, float]] = {}
    for name in _REACTION_IMPULSE:
        target = reaction_affect_target(name)
        if target is not None:
            out[name] = target
    return out


def detect_whiplash(recent_reactions: list[str]) -> bool:
    """True when consecutive recent tags flip valence direction.

    ``recent_reactions`` is oldest-first and already includes the fresh
    tag. Directionless tags (neutral / unknown) break the chain — a
    pause through neutral is not whiplash.
    """
    signs: list[int] = []
    for reaction in recent_reactions:
        target = reaction_affect_target(reaction)
        if target is None:
            signs.append(0)
            continue
        signs.append(1 if target[0] > 0 else (-1 if target[0] < 0 else 0))
    for prev, cur in zip(signs, signs[1:]):
        if prev != 0 and cur != 0 and prev != cur:
            return True
    return False


def assess(
    reaction: str,
    valence: float,
    arousal: float,
    recent_reactions: list[str] | None = None,
    *,
    strong_threshold: float = DEFAULT_STRONG_THRESHOLD,
) -> InertiaResult:
    """Score how far ``reaction`` outruns the smoothed felt state.

    ``valence`` / ``arousal`` should be the *pre-impulse* smoothed state
    (what Aiko actually still feels), so the fresh tag's own nudge
    doesn't shrink its own mismatch.
    """
    target = reaction_affect_target(reaction)
    if target is None:
        return InertiaResult(
            mismatch=0.0, raw_mismatch=0.0, whiplash=False, band="none",
        )
    tv, ta = target
    v = _clamp(_safe_float(valence, 0.0), -1.0, 1.0)
    a = _clamp(_safe_float(arousal, _AROUSAL_BASELINE), 0.0, 1.0)
    distance = math.hypot(
        _VALENCE_WEIGHT * (tv - v),
        _AROUSAL_WEIGHT * (ta - a),
    )
    raw = _clamp(distance / _DISTANCE_NORM, 0.0, 1.0)
    whiplash = detect_whiplash(list(recent_reactions or []))
    effective = _clamp(raw + (_WHIPLASH_BONUS if whiplash else 0.0), 0.0, 1.0)

    strong = max(0.1, float(strong_threshold))
    mild = strong * _MILD_FRACTION
    if effective >= strong:
        band = "strong"
    elif effective >= mild:
        band = "mild"
    else:
        band = "none"
    return InertiaResult(
        mismatch=round(effective, 4),
        raw_mismatch=round(raw, 4),
        whiplash=whiplash,
        band=band,
    )


def render_cue(
    result: InertiaResult,
    reaction: str,
    valence: float,
    arousal: float,
) -> str:
    """One-shot prompt cue for a strong mismatch.

    K44 contract: felt-language only, no numeric coordinates. The cue
    names the gap once and tells Aiko to let the residue show — it never
    suppresses the reaction itself.
    """
    if result.band != "strong":
        return ""
    label = (reaction or "").strip().lower().replace("_", " ") or "that"
    underneath = felt_phrase(valence, arousal)
    target = reaction_affect_target(reaction)
    jumping_brighter = bool(target and target[0] >= _safe_float(valence, 0.0))
    if jumping_brighter:
        direction = (
            "don't snap fully bright in one beat — let the warmth come "
            "back gradually"
        )
    else:
        direction = (
            "don't plunge all the way down in one beat — the previous "
            "feeling is still echoing"
        )
    whiplash_part = (
        " Your reactions have been swinging hard between turns; "
        "let them settle."
        if result.whiplash
        else ""
    )
    return (
        f"Heads-up: your face just jumped to {label}, but underneath "
        f"you're still {underneath} — let the words catch up; "
        f"{direction}.{whiplash_part}"
    )


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _safe_float(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if math.isnan(out):
        return default
    return out


__all__ = [
    "InertiaResult",
    "assess",
    "detect_whiplash",
    "reaction_affect_target",
    "reaction_affect_targets",
    "render_cue",
    "DEFAULT_STRONG_THRESHOLD",
]
