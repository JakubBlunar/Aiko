"""K76 — Affective memory salience (flashbulb encoding).

Human memory isn't flat: moments that hit you *emotionally* burn in
harder and fade slower (a flashbulb memory). Aiko's memories carry a
salience + tiered decay, but salience at write-time ignored **how she
felt when the memory formed** — a fact learned during a K8 rupture or a
K57 strong emotion episode was encoded with the same weight as small
talk.

This pure module is the whole mechanic: at memory-write time, read the
live ``AffectState`` arousal + any active K57 episode intensity, fold
them into an emotional **charge** in ``[0, 1]``, and apply a bounded
salience boost proportional to that charge. Neutral affect → ~0 charge →
no boost, so low-arousal small talk is untouched with no kind allow-list
needed. A higher initial salience both surfaces the memory more (RAG
scoring) and gives it more headroom before decay/prune drops it — exactly
like a person's emotionally charged memory resisting forgetting.

No worker, no LLM, no schema change — just an optional hook on
``MemoryStore.add`` (the boost) plus a ``metadata.affect_at_encoding``
stamp for observability.
"""
from __future__ import annotations

from dataclasses import dataclass


# Tuning defaults (overridable via MemorySettings).
DEFAULT_MAX_BOOST = 0.35
DEFAULT_AROUSAL_WEIGHT = 0.6
DEFAULT_EPISODE_WEIGHT = 0.7
# AffectState arousal is in [0, 1] with a resting baseline of ~0.4, so
# only activation *above* baseline counts as flashbulb-worthy.
DEFAULT_AROUSAL_NEUTRAL = 0.4
# Below this charge nothing is stamped / boosted (treat as neutral).
DEFAULT_MIN_CHARGE = 0.05


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def compute_charge(
    arousal: float,
    episode_intensity: float,
    *,
    arousal_weight: float = DEFAULT_AROUSAL_WEIGHT,
    episode_weight: float = DEFAULT_EPISODE_WEIGHT,
    arousal_neutral: float = DEFAULT_AROUSAL_NEUTRAL,
) -> float:
    """Fold live arousal + active episode intensity into a [0, 1] charge.

    Arousal contributes only its activation *above* ``arousal_neutral``
    (rescaled to [0, 1]); episode intensity contributes directly. The
    weighted sum is clamped to [0, 1].
    """
    span = 1.0 - float(arousal_neutral)
    arousal_component = 0.0
    if span > 0.0:
        arousal_component = (float(arousal) - float(arousal_neutral)) / span
    arousal_component = _clamp01(arousal_component)
    episode_component = _clamp01(episode_intensity)
    charge = (
        float(arousal_weight) * arousal_component
        + float(episode_weight) * episode_component
    )
    return _clamp01(charge)


@dataclass(frozen=True, slots=True)
class FlashbulbResult:
    salience: float
    charge: float
    boost: float


def apply_flashbulb(
    base_salience: float,
    *,
    arousal: float,
    episode_intensity: float,
    max_boost: float = DEFAULT_MAX_BOOST,
    arousal_weight: float = DEFAULT_AROUSAL_WEIGHT,
    episode_weight: float = DEFAULT_EPISODE_WEIGHT,
    arousal_neutral: float = DEFAULT_AROUSAL_NEUTRAL,
) -> FlashbulbResult:
    """Return the emotionally-boosted salience + the charge that drove it.

    ``salience = clamp(base + max_boost * charge)``. A neutral affect
    (charge 0) returns ``base`` unchanged.
    """
    charge = compute_charge(
        arousal,
        episode_intensity,
        arousal_weight=arousal_weight,
        episode_weight=episode_weight,
        arousal_neutral=arousal_neutral,
    )
    boost = max(0.0, float(max_boost)) * charge
    salience = _clamp01(float(base_salience) + boost)
    return FlashbulbResult(salience=salience, charge=charge, boost=boost)
