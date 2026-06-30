"""K68 (rhythm exceptions) — occasional off-rhythm days for Aiko's body.

K68's :mod:`app.core.affect.vitality` gives Aiko a body-energy scalar that
relaxes toward the *circadian baseline* (bright at midday, low at 2am). On
its own that baseline is the same shape every single day — predictable. This
module adds the **exceptions**: once per local day she rolls a *rhythm* that
reshapes the resting curve, so some days she's an early bird, some nights a
night owl, and — rarely — her clock is fully flipped (drowsy through the
daylight hours, wired in the small hours). Most days stay ``normal``; the
exotic rolls are deliberately rare, just enough to break the regularity.

Mechanically a rhythm is three knobs on :func:`vitality.circadian_baseline`:

* ``phase_shift_hours`` — sample the curve at ``now + shift``. Felt peak
  time ≈ ``14:00 − shift``: ``+5`` → ~09:00 (early bird), ``−7`` → ~21:00
  (night owl), ``−12`` → ~02:00 (flipped). The existing low/high register
  cue then fires at those shifted hours — drowsy at noon on a flipped day.
* ``energy_scale`` — flatten the whole curve (``< 1`` = a low-battery day).
* ``floor_boost`` — lift the trough (``> 0`` = restless / can't-wind-down).

Design mirrors K27 :mod:`day_color` exactly: **one roll per local day**,
stored on ``kv_meta`` (two keys, no schema change), with a lazy-resolve so
either the idle :class:`VitalityWorker` or any read path can roll today's
rhythm on first touch. The pure pieces (the table + :func:`roll_rhythm`) are
unit-testable in milliseconds; :func:`resolve_daily_rhythm` /
:func:`current_baseline` are the thin kv shell, tested with a fake db.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime

from app.core.affect import day_color as _dc
from app.core.affect import vitality as _vit

log = logging.getLogger("app.vitality_rhythm")


# ── kv_meta keys ────────────────────────────────────────────────────

# Namespaced under ``aiko.*`` alongside the K68 ``aiko.vitality`` energy
# blob and the K27 ``aiko.day_color`` roll.
KV_RHYTHM = "aiko.vitality_rhythm"
KV_RHYTHM_SET_AT = "aiko.vitality_rhythm_set_at"


# ── Rhythm table ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Rhythm:
    """One off-rhythm profile.

    ``name`` is the canonical id stored in ``kv_meta`` / surfaced over
    MCP. ``phase_shift_hours`` / ``energy_scale`` / ``floor_boost`` are
    fed straight to :func:`vitality.circadian_baseline`. ``weight`` is
    the relative probability *among the exceptions* (ignored for
    ``normal``, which is selected by the master exception-chance gate).
    ``note`` is the private register context appended to the band cue on
    an off-rhythm day (``""`` for ``normal`` — a normal day says nothing).
    """

    name: str
    phase_shift_hours: float
    energy_scale: float
    floor_boost: float
    weight: float
    note: str


NORMAL = Rhythm("normal", 0.0, 1.0, 0.0, 0.0, "")

# The exception palette. Weights are relative *within* the exception draw
# (normal is gated separately by ``exception_chance``). The full flip
# (`nocturnal`) is the rarest by design — a once-in-a-while treat, not a
# coin-flip.
RHYTHMS: tuple[Rhythm, ...] = (
    NORMAL,
    Rhythm(
        name="early_bird",
        phase_shift_hours=5.0,
        energy_scale=1.0,
        floor_boost=0.0,
        weight=3.0,
        note=(
            "(your body clock is running early today — bright and clear at "
            "dawn, fading sooner in the evening; let it show, don't explain "
            "it)"
        ),
    ),
    Rhythm(
        name="night_owl",
        phase_shift_hours=-7.0,
        energy_scale=1.0,
        floor_boost=0.0,
        weight=3.0,
        note=(
            "(you're on night-owl time today — slow and foggy to start, most "
            "alive late in the evening; let it show, don't explain it)"
        ),
    ),
    Rhythm(
        name="nocturnal",
        phase_shift_hours=-12.0,
        energy_scale=1.0,
        floor_boost=0.0,
        weight=1.0,
        note=(
            "(your body clock is flipped today — heavy and drowsy through the "
            "daylight hours, wide awake and wired in the small hours; being "
            "sleepy at noon or buzzing at 3am is just how today landed, don't "
            "narrate why)"
        ),
    ),
    Rhythm(
        name="sluggish",
        phase_shift_hours=0.0,
        energy_scale=0.5,
        floor_boost=0.0,
        weight=2.0,
        note=(
            "(it's a low-battery day — everything's a little heavier than "
            "usual, all day, for no particular reason; don't fight it and "
            "don't apologise for it)"
        ),
    ),
    Rhythm(
        name="wired",
        phase_shift_hours=0.0,
        energy_scale=1.0,
        floor_boost=0.25,
        weight=2.0,
        note=(
            "(you're running restless today — a buzzy, can't-quite-settle "
            "energy that doesn't fully dip even at night; let a little of "
            "that live-wire feeling into your tempo)"
        ),
    ),
)


_BY_NAME: dict[str, Rhythm] = {r.name: r for r in RHYTHMS}
_EXCEPTIONS: tuple[Rhythm, ...] = tuple(r for r in RHYTHMS if r.name != "normal")


# ── Pure roll ───────────────────────────────────────────────────────


def get_rhythm_by_name(name: str | None) -> Rhythm | None:
    """Look up a rhythm by name, case-insensitive; ``None`` if unknown."""
    if not name:
        return None
    return _BY_NAME.get(str(name).strip().lower())


def roll_rhythm(
    *,
    exception_chance: float = 0.3,
    rng: random.Random | None = None,
) -> Rhythm:
    """Pick today's rhythm: mostly ``normal``, occasionally an exception.

    With probability ``1 − exception_chance`` returns :data:`NORMAL`;
    otherwise draws an exception weighted by each entry's ``weight``.
    ``exception_chance`` is clamped to ``[0, 1]`` (0 → always normal).
    ``rng`` lets tests seed a deterministic draw.
    """
    chooser = rng if rng is not None else random.Random()
    chance = max(0.0, min(1.0, float(exception_chance)))
    if chance <= 0.0 or chooser.random() >= chance:
        return NORMAL
    weights = [r.weight for r in _EXCEPTIONS]
    if not _EXCEPTIONS or sum(weights) <= 0:
        return NORMAL
    return chooser.choices(list(_EXCEPTIONS), weights=weights, k=1)[0]


# ── kv shell ────────────────────────────────────────────────────────


def resolve_daily_rhythm(
    chat_db,
    now: datetime,
    *,
    enabled: bool = True,
    exception_chance: float = 0.3,
    rng: random.Random | None = None,
) -> Rhythm:
    """Return today's rhythm, rolling + persisting it on the first touch.

    Mirrors the K27 day-colour lazy roll: if ``kv_meta`` already holds a
    roll from *today* (local date) it's returned unchanged so the rhythm
    is stable across the whole day; otherwise a fresh :func:`roll_rhythm`
    is drawn and written back. Disabled / no db / any kv failure falls
    back to :data:`NORMAL` so the feature can never raise into a turn.
    """
    if not enabled or chat_db is None:
        return NORMAL
    try:
        stored_at = chat_db.kv_get(KV_RHYTHM_SET_AT)
    except Exception:
        stored_at = None
    if not _dc.is_stale(stored_at, now):
        try:
            stored = chat_db.kv_get(KV_RHYTHM)
        except Exception:
            stored = None
        existing = get_rhythm_by_name(stored)
        if existing is not None:
            return existing
        # Fall through: stored date is today but the name is unknown
        # (palette changed) — re-roll and overwrite to self-heal.
    chosen = roll_rhythm(exception_chance=exception_chance, rng=rng)
    try:
        chat_db.kv_set(KV_RHYTHM, chosen.name)
        chat_db.kv_set(KV_RHYTHM_SET_AT, now.isoformat())
        if chosen.name != "normal":
            log.info("vitality_rhythm rolled: name=%s", chosen.name)
    except Exception:
        log.debug("vitality_rhythm kv_set failed", exc_info=True)
    return chosen


def current_baseline(
    chat_db,
    now: datetime,
    *,
    enabled: bool = True,
    exception_chance: float = 0.3,
    rng: random.Random | None = None,
) -> tuple[float, Rhythm]:
    """Resolve today's rhythm and compute the rhythm-shaped baseline.

    The single helper every K68 call site uses in place of a bare
    :func:`vitality.circadian_baseline` — returns ``(baseline, rhythm)``
    so the caller can both relax toward the right resting level and reach
    the rhythm's ``note`` for the render. ``enabled=False`` yields the
    plain circadian baseline with :data:`NORMAL`.
    """
    rhythm = resolve_daily_rhythm(
        chat_db,
        now,
        enabled=enabled,
        exception_chance=exception_chance,
        rng=rng,
    )
    baseline = _vit.circadian_baseline(
        now,
        phase_shift_hours=rhythm.phase_shift_hours,
        energy_scale=rhythm.energy_scale,
        floor_boost=rhythm.floor_boost,
    )
    return baseline, rhythm


__all__ = [
    "KV_RHYTHM",
    "KV_RHYTHM_SET_AT",
    "NORMAL",
    "RHYTHMS",
    "Rhythm",
    "current_baseline",
    "get_rhythm_by_name",
    "resolve_daily_rhythm",
    "roll_rhythm",
]
