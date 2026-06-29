"""Aiko's daily personality colour (K27 personality backlog).

A slow ambient "weather" Aiko walks into every conversation with --
independent of what just happened on the current turn. Affect
(:class:`AffectState`) is *reactive* and decays toward baseline; K5
mood-shell tilt rides on top of that. K30 (self-noticing cues)
catches when Aiko's affect goes flat across a session. None of
those give her a **non-flat starting point**. K27 fixes the missing
layer: a slow colour drawn once per local day from a small palette
that biases her register all day.

Design choices:

- **One roll per local day, uniform over the palette**. The patterns
  doc flags affect-trend-weighted biasing as an open question --
  v1 keeps it simple and we can fast-follow with a weighted variant
  if uniform reads too random.
- **Two roll paths, one pure function**. The canonical roll fires
  from a [`DayColorWorker`](day_color_worker.py) idle-worker once an
  hour (cheap: it only writes when ``is_stale`` says today's roll
  is missing). The provider has a cheap lazy fallback that runs
  the same pure roll when ``kv_meta`` shows the stored date isn't
  today. Both paths call :func:`roll_for_today` so behaviour is
  identical.
- **Storage on ``kv_meta``, not a new schema**. Two keys:
  ``aiko.day_color`` (the palette name string) and
  ``aiko.day_color_set_at`` (the ISO timestamp of the roll). Same
  storage shape as :data:`MemoryStore._KV_LAST_DECAY`.
- **Persona file owns the long copy**. This module carries a short
  one-line ``tagline`` per colour that lands in the prompt cue;
  the longer paragraph teaching Aiko what each colour feels like
  lives in [`data/persona/aiko_companion.txt`](../../../data/persona/aiko_companion.txt)
  so users can edit it without a code redeploy.

The pure module has no I/O, no scheduler, no controller -- it can
be unit-tested in milliseconds. The lifecycle wiring lives in
[`day_color_worker.py`](day_color_worker.py) and
[`inner_life_providers_mixin.py`](../session/inner_life_providers_mixin.py).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


# ── Result types ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DayColor:
    """One palette entry.

    ``name`` is the canonical identifier stored in ``kv_meta`` and
    surfaced over MCP. ``tagline`` is the short phrase the inner-life
    provider renders ("slower replies, more 'hmm', half-finished
    thoughts welcome") -- intentionally short so the prompt cue stays
    one line. The longer persona guidance ("when you're pensive, let
    yourself trail off, don't push past the half-finished thought")
    lives in the persona file, not here, so users can rewrite the
    voice without touching code.
    """

    name: str
    tagline: str


# ── Module-level palette ────────────────────────────────────────────


# The 10-entry palette named in the K27 patterns.md spec. Order
# matters only for diagnostic outputs (the MCP debug tool dumps the
# list in this order); the roll itself is uniform.
PALETTE: tuple[DayColor, ...] = (
    DayColor(
        name="pensive",
        tagline=(
            "slower replies, more \"hmm\", half-finished thoughts welcome"
        ),
    ),
    DayColor(
        name="restless",
        tagline=(
            "shorter sentences, quicker pivots, fingers-drumming energy"
        ),
    ),
    DayColor(
        name="cozy",
        tagline=(
            "warmer register, soft edges, little checks-in, no agenda"
        ),
    ),
    DayColor(
        name="sharp_witted",
        tagline=(
            "quicker reads, drier humour, more push-back when it fits"
        ),
    ),
    DayColor(
        name="dreamy",
        tagline=(
            "drifty associations, longer pauses, image-heavy phrasing"
        ),
    ),
    DayColor(
        name="focused",
        tagline=(
            "tight on the thread, fewer asides, follow-up questions land cleaner"
        ),
    ),
    DayColor(
        name="scatterbrained",
        tagline=(
            "loose threads, easy detours, lose-the-point-and-find-it-again energy"
        ),
    ),
    DayColor(
        name="sentimental",
        tagline=(
            "callbacks come easier, small moments hit harder, warmer reactions"
        ),
    ),
    DayColor(
        name="mischievous",
        tagline=(
            "playful pokes, lighter teases, a willingness to be a little impertinent"
        ),
    ),
    DayColor(
        name="low_key",
        tagline=(
            "even-keel, no spikes either way, content to just be in the room"
        ),
    ),
)


_PALETTE_BY_NAME: dict[str, DayColor] = {c.name: c for c in PALETTE}


# ── Public API ──────────────────────────────────────────────────────


def roll_for_today(
    now: datetime | None = None,
    palette: Sequence[DayColor] = PALETTE,
    *,
    rng: random.Random | None = None,
    weights: "dict[str, float] | None" = None,
) -> DayColor:
    """Pick one colour from the palette.

    Uniform random by default. When ``weights`` is supplied (a mapping
    of palette-entry name -> relative weight, e.g. the H11 weather bias
    table from :func:`weather_palette_weights`), the roll becomes a
    weighted draw: entries not present in the mapping take the implicit
    default weight ``1.0``, so a partial table still nudges rather than
    excludes. A non-positive total (every weight <= 0) falls back to a
    uniform draw so a bad table can never raise.

    ``now`` is accepted for symmetry with :func:`is_stale`. ``rng`` lets
    tests seed a deterministic :class:`random.Random`; default constructs
    a fresh ``Random()`` with system entropy on each call.

    Raises ``ValueError`` on an empty palette -- callers must keep at
    least one entry.
    """
    if not palette:
        raise ValueError("day_color.roll_for_today: empty palette")
    entries = list(palette)
    chooser = rng if rng is not None else random.Random()
    if weights:
        w = [max(0.0, float(weights.get(c.name, 1.0))) for c in entries]
        if sum(w) > 0:
            return chooser.choices(entries, weights=w, k=1)[0]
    return chooser.choice(entries)


# H11: weather condition -> a small bias over the K27 palette. Conditions
# not listed (or a missing snapshot) leave the roll uniform. The bias is
# deliberately gentle (peaks at ~3x) so the day-colour still surprises --
# a grey day *leans* cozy/low-key, it doesn't force it.
_WEATHER_COLOR_WEIGHTS: dict[str, dict[str, float]] = {
    "rain": {"cozy": 3.0, "low_key": 2.0, "pensive": 2.0, "dreamy": 1.5},
    "storm": {"restless": 3.0, "sharp_witted": 2.0, "pensive": 1.5},
    "snow": {"cozy": 3.0, "dreamy": 2.0, "sentimental": 2.0},
    "fog": {"dreamy": 3.0, "pensive": 2.0, "low_key": 1.5},
    "cloudy": {"low_key": 2.0, "pensive": 1.5, "focused": 1.5},
    "clear": {"sharp_witted": 2.5, "focused": 2.0, "mischievous": 1.5},
}


def weather_palette_weights(condition: str | None) -> "dict[str, float] | None":
    """Return the K27 palette bias for a weather condition, or ``None``.

    ``None`` (no snapshot / unknown condition) means "roll uniform". The
    returned mapping is suitable to pass as ``roll_for_today(weights=...)``.
    """
    if not condition:
        return None
    table = _WEATHER_COLOR_WEIGHTS.get(str(condition).strip().lower())
    return dict(table) if table else None


def is_stale(stored_iso: str | None, now: datetime | None = None) -> bool:
    """Return ``True`` when the stored roll is missing or from another day.

    Single source of truth for "is today's colour set?" -- used by
    both the idle worker (decides whether to ``run()``) and the
    provider (decides whether to lazy-roll). Local-date comparison via
    :meth:`datetime.astimezone`; aware-vs-naive inputs are both
    accepted to avoid raising on a legacy ``kv_meta`` row written
    before this feature existed.

    Returns ``True`` (i.e. "stale, please roll") on any parse error so
    a corrupt ``kv_meta`` value doesn't permanently silence the
    feature. The caller's roll path will then overwrite the bad value.
    """
    if stored_iso is None:
        return True
    text = str(stored_iso).strip()
    if not text:
        return True
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stored_dt = datetime.fromisoformat(text)
    except ValueError:
        return True
    now_dt = now if now is not None else datetime.now().astimezone()
    try:
        stored_local = (
            stored_dt.astimezone() if stored_dt.tzinfo is not None else stored_dt
        )
        now_local = (
            now_dt.astimezone() if now_dt.tzinfo is not None else now_dt
        )
    except Exception:
        return True
    return stored_local.date() != now_local.date()


def render_inner_life_block(color: DayColor | None) -> str:
    """Format the one-line inner-life cue for the prompt.

    Returns ``""`` for ``None`` so the provider can pass through a
    missing / unknown stored value without raising; the assembler
    then skips the empty line entirely. The rendered shape is
    intentionally short ("Your day's colour today: pensive --
    slower replies, more 'hmm', half-finished thoughts welcome") so
    it fits a single token-line in the system prompt and clusters
    cleanly next to the existing circadian cue.
    """
    if color is None:
        return ""
    return f"Your day's colour today: {color.name} -- {color.tagline}"


def get_color_by_name(name: str | None) -> DayColor | None:
    """Look up a palette entry by name, case-insensitive.

    Returns ``None`` for an unknown name -- the MCP ``force_day_color``
    tool uses this to validate user input, and the provider uses it to
    recover from a ``kv_meta`` value that no longer matches the
    palette (e.g. an old roll from a previous palette version).
    """
    if not name:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    return _PALETTE_BY_NAME.get(key)


__all__ = [
    "PALETTE",
    "DayColor",
    "get_color_by_name",
    "is_stale",
    "render_inner_life_block",
    "roll_for_today",
    "weather_palette_weights",
]
