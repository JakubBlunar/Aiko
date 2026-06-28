"""H18 — weighted, anti-repetition idle-activity selection.

The K36 :class:`IdleAwayActivityWorker` used to pick its next beat with a
flat ``random.choice`` over whatever candidates the room afforded. That
made her idle life feel like a shuffle with no memory: the same verb
could land three times in a row, and the time of day / her mood never
tilted what she'd plausibly be doing.

This module is the pure brain of the new selector. Given the candidate
activity *keys*, the recent journal history, the current circadian
period, an optional affect ``valence`` and the daily personality
``day_color`` name, it produces a weight per key and draws one. No I/O,
no randomness in :func:`compute_weights` (so it's trivially testable);
:func:`weighted_pick` is the thin rng wrapper the worker calls.

Design notes:

* **Anti-repetition dominates.** A key that just fired is heavily
  down-weighted; the penalty fades with distance so variety recovers
  naturally over a few beats.
* **Circadian + mood are gentle tilts**, never hard gates — every key
  keeps a non-zero floor so the room never gets stuck refusing an
  activity outright.
"""
from __future__ import annotations

import random


# Floor every multiplier lands above, so no activity becomes impossible.
_WEIGHT_FLOOR = 0.05

# How many of the most-recent journal keys the recency penalty considers.
_DEFAULT_RECENCY_WINDOW = 6

# ── Circadian tilts ─────────────────────────────────────────────────────
# period -> {activity_key: multiplier}. Missing keys default to 1.0.
_CIRCADIAN_BIAS: dict[str, dict[str, float]] = {
    "late_night": {
        "nap": 1.8, "wander": 1.3, "read_book": 1.2,
        "tidy_desk": 0.6, "doodle": 0.7, "move_cat": 0.7, "snack": 0.8,
    },
    "early_morning": {
        "nap": 1.4, "look_outside": 1.3, "snack": 1.2,
        "doodle": 0.8, "move_cat": 0.8,
    },
    "morning": {
        "tidy_desk": 1.4, "doodle": 1.2, "snack": 1.2, "look_outside": 1.1,
        "nap": 0.4,
    },
    "midday": {
        "snack": 1.4, "tidy_desk": 1.1, "move_cat": 1.1,
        "nap": 0.5,
    },
    "afternoon": {
        "read_book": 1.3, "doodle": 1.2, "look_outside": 1.1,
        "nap": 0.6,
    },
    "evening": {
        "read_book": 1.3, "wander": 1.2, "look_outside": 1.2, "snack": 1.1,
        "tidy_desk": 0.8,
    },
    "night": {
        "read_book": 1.3, "wander": 1.2, "nap": 1.4,
        "tidy_desk": 0.6, "move_cat": 0.7,
    },
}

# ── Day-color tilts (K27 palette names) ─────────────────────────────────
_DAY_COLOR_BIAS: dict[str, dict[str, float]] = {
    "pensive": {"wander": 1.3, "look_outside": 1.3, "read_book": 1.1, "doodle": 0.8},
    "restless": {"tidy_desk": 1.3, "move_cat": 1.3, "look_outside": 1.2, "read_book": 0.7, "nap": 0.6},
    "cozy": {"read_book": 1.4, "snack": 1.3, "wander": 1.1, "tidy_desk": 0.8},
    "sharp_witted": {"doodle": 1.4, "tidy_desk": 1.3, "wander": 0.8},
    "dreamy": {"look_outside": 1.4, "wander": 1.3, "tidy_desk": 0.7},
    "focused": {"tidy_desk": 1.4, "doodle": 1.3, "wander": 0.7, "nap": 0.6},
    "scatterbrained": {"move_cat": 1.3, "doodle": 1.3, "wander": 1.1, "tidy_desk": 0.6},
    "sentimental": {"read_book": 1.3, "look_outside": 1.3, "wander": 1.1},
    "mischievous": {"move_cat": 1.4, "doodle": 1.3, "tidy_desk": 0.8},
    "low_key": {"wander": 1.3, "read_book": 1.2, "nap": 1.1, "tidy_desk": 0.7},
}

# Activities that read as "quiet / cozy" vs "active", for the affect tilt.
_COZY_KEYS = frozenset({"wander", "read_book", "look_outside", "nap"})
_ACTIVE_KEYS = frozenset({"tidy_desk", "doodle", "move_cat"})


def _recency_multiplier(
    key: str, recent_keys: list[str], window: int,
) -> float:
    """Down-weight ``key`` for each recent occurrence; harsher when newer.

    ``recent_keys`` is oldest→newest. Distance 0 is the most recent beat.
    """
    if not recent_keys or window <= 0:
        return 1.0
    tail = recent_keys[-window:]
    mult = 1.0
    n = len(tail)
    for idx, k in enumerate(tail):
        if k != key:
            continue
        # distance from newest: 0 for last element.
        distance = (n - 1) - idx
        factor = min(0.85, 0.2 + 0.12 * distance)
        mult *= factor
    return mult


def _affect_multiplier(key: str, valence: float | None) -> float:
    if valence is None:
        return 1.0
    if valence <= -0.2:
        if key in _COZY_KEYS:
            return 1.3
        if key in _ACTIVE_KEYS:
            return 0.8
    elif valence >= 0.3:
        if key in _ACTIVE_KEYS:
            return 1.3
        if key == "wander":
            return 0.85
    return 1.0


def compute_weights(
    keys: list[str],
    *,
    recent_keys: list[str] | None = None,
    period: str = "",
    valence: float | None = None,
    day_color: str | None = None,
    recency_window: int = _DEFAULT_RECENCY_WINDOW,
) -> dict[str, float]:
    """Return a positive weight per candidate key (pure, deterministic)."""
    recent_keys = recent_keys or []
    circ = _CIRCADIAN_BIAS.get((period or "").strip(), {})
    color = _DAY_COLOR_BIAS.get((day_color or "").strip().lower(), {})
    weights: dict[str, float] = {}
    for key in keys:
        w = 1.0
        w *= _recency_multiplier(key, recent_keys, recency_window)
        w *= circ.get(key, 1.0)
        w *= color.get(key, 1.0)
        w *= _affect_multiplier(key, valence)
        weights[key] = max(_WEIGHT_FLOOR, w)
    return weights


def weighted_pick(
    keys: list[str],
    *,
    rng: random.Random,
    recent_keys: list[str] | None = None,
    period: str = "",
    valence: float | None = None,
    day_color: str | None = None,
    recency_window: int = _DEFAULT_RECENCY_WINDOW,
) -> str | None:
    """Draw one key proportional to its computed weight."""
    if not keys:
        return None
    weights = compute_weights(
        keys,
        recent_keys=recent_keys,
        period=period,
        valence=valence,
        day_color=day_color,
        recency_window=recency_window,
    )
    ordered = list(keys)
    population = [weights[k] for k in ordered]
    total = sum(population)
    if total <= 0:
        return rng.choice(ordered)
    return rng.choices(ordered, weights=population, k=1)[0]


__all__ = ["compute_weights", "weighted_pick"]
