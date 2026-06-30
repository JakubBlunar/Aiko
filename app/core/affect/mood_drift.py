"""H3 — mood-drift narrator (pure math).

Neither :class:`~app.core.affect.affect_state.AffectState` nor
:class:`~app.core.relationship.relationship_axes.RelationshipAxesState`
keeps cross-day history — both are single-row "current value" stores. H3
adds a thin rolling ring of **daily samples** (one per local day) of
Aiko's read of the user's mood (``valence``) plus the four relationship
axes, and detects two kinds of slow drift worth a rare, gentle reflective
note:

  * ``sustained_low`` / ``lifting`` — the user's mood has sat low for a
    run of consecutive days, or has clearly climbed back up after a low
    stretch.
  * ``axis_rise`` / ``axis_fall`` — one relationship axis
    (closeness / humor / trust / comfort) has moved notably in a single
    direction across the window (e.g. closeness climbing for two weeks).

Everything in this module is pure: no I/O, no clock except what the
caller passes in. The worker
([`mood_drift_worker.py`](mood_drift_worker.py)) owns the daily sampling
side-effect; the provider
([`inner_life_part1.py`](../session/inner_life_part1.py)
``_render_mood_drift_block``) owns detection + the cooldown / watermark
surfacing side-effects. Both share the ``kv_meta`` key constants below.

The note is deliberately *occasional*: the provider only surfaces a
finding once (keyed by a stable signature that excludes the date), so an
ongoing low stretch is acknowledged a single time rather than re-raised
every few days. A *different* finding (e.g. a later ``lifting``) is what
breaks back through.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("app.mood_drift")


# ``kv_meta`` keys, namespaced under ``aiko.*`` like K27 day_color / K15
# vulnerability budget so H3 state never collides with the ``memory.*`` /
# ``goals.*`` namespaces. Exported so the worker, provider, and MCP debug
# tools all share the exact same strings.
KV_SAMPLES = "aiko.mood_drift_samples"
KV_LAST_SURFACED_AT = "aiko.mood_drift_last_surfaced_at"
KV_LAST_SIGNATURE = "aiko.mood_drift_last_signature"


# ── tuning (module constants, mirrors the rag_retriever convention) ──────

# Cap on the ring. Three weeks of daily samples is plenty of context for a
# "two-week climb" finding while staying a tiny kv blob.
SAMPLE_CAP = 21

# Minimum samples before any detection fires. Below this the ring is too
# short to tell drift from noise.
MIN_SAMPLES = 4

# Mood (valence) bands. A day counts as "low" at/below this valence.
LOW_VALENCE_THRESHOLD = -0.15
# Consecutive trailing low days needed to call a sustained low.
LOW_RUN_DAYS = 3
# How far valence must have climbed (recent mean minus the low-stretch
# mean) to read as a genuine "lifting" recovery, and the recent mean must
# also clear this small positive floor so a climb that's still negative
# doesn't read as "in a better place".
LIFT_DELTA = 0.30
LIFT_RECENT_FLOOR = 0.05
# Window sizes (in trailing samples) for the lifting comparison.
LIFT_RECENT_WINDOW = 3
LIFT_PRIOR_WINDOW = 3

# Axis drift. An axis must move at least this much across the window
# (newest sample minus the oldest in-window sample) to count, and the
# window must span at least this many samples so a two-day blip never
# reads as a trend.
AXIS_DELTA = 0.25
AXIS_MIN_SPAN = 6

_AXES = ("closeness", "humor", "trust", "comfort")


@dataclass(slots=True, frozen=True)
class DriftSample:
    """One day's snapshot of the mood + relationship signals."""

    date: str  # local YYYY-MM-DD
    valence: float
    closeness: float
    humor: float
    trust: float
    comfort: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class DriftVerdict:
    """A detected drift worth surfacing.

    ``signature`` is intentionally date-free so the provider can suppress
    re-surfacing the *same* ongoing finding; ``summary`` is for logs only.
    """

    kind: str  # sustained_low | lifting | axis_rise | axis_fall
    axis: str | None  # axis name for axis_* kinds, else None
    magnitude: float
    signature: str
    summary: str


def today_str(now: datetime) -> str:
    """Local ``YYYY-MM-DD`` for the sample key (caller passes the clock)."""
    return now.strftime("%Y-%m-%d")


# ── (de)serialisation ────────────────────────────────────────────────────


def serialize_samples(samples: list[DriftSample]) -> str:
    return json.dumps([s.to_dict() for s in samples], separators=(",", ":"))


def deserialize_samples(blob: str | None) -> list[DriftSample]:
    """Parse the kv blob into samples; tolerant of corruption / legacy."""
    if not blob:
        return []
    try:
        raw = json.loads(blob)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[DriftSample] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                DriftSample(
                    date=str(item["date"]),
                    valence=float(item.get("valence", 0.0)),
                    closeness=float(item.get("closeness", 0.0)),
                    humor=float(item.get("humor", 0.0)),
                    trust=float(item.get("trust", 0.0)),
                    comfort=float(item.get("comfort", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def append_sample(
    samples: list[DriftSample],
    sample: DriftSample,
    *,
    cap: int = SAMPLE_CAP,
) -> list[DriftSample]:
    """Return a new list with ``sample`` added, deduped by date, capped.

    If a sample already exists for ``sample.date`` it is *replaced* (the
    latest read of the day wins) rather than appended, so a chatty day
    contributes exactly one point. The result keeps the trailing ``cap``
    samples in chronological order.
    """
    kept = [s for s in samples if s.date != sample.date]
    kept.append(sample)
    # Defensive: keep chronological by date string (ISO dates sort lexically).
    kept.sort(key=lambda s: s.date)
    if cap > 0 and len(kept) > cap:
        kept = kept[-cap:]
    return kept


# ── detection ────────────────────────────────────────────────────────────


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def detect_drift(
    samples: list[DriftSample],
    *,
    low_threshold: float = LOW_VALENCE_THRESHOLD,
    low_run: int = LOW_RUN_DAYS,
    lift_delta: float = LIFT_DELTA,
    lift_recent_floor: float = LIFT_RECENT_FLOOR,
    axis_delta: float = AXIS_DELTA,
    axis_min_span: int = AXIS_MIN_SPAN,
    min_samples: int = MIN_SAMPLES,
) -> DriftVerdict | None:
    """Return the single most salient drift finding, or ``None``.

    Priority: a sustained low (user wellbeing) outranks a recovery, which
    outranks a relationship-axis drift. Pure — the caller decides whether
    to actually surface it (cooldown / watermark live in the provider).
    """
    if len(samples) < max(2, int(min_samples)):
        return None

    valences = [s.valence for s in samples]

    # 1) sustained low — the last ``low_run`` days all sit at/below the
    #    low threshold.
    run = max(1, int(low_run))
    if len(valences) >= run:
        tail = valences[-run:]
        if all(v <= low_threshold for v in tail):
            mag = abs(_mean(tail))
            return DriftVerdict(
                kind="sustained_low",
                axis=None,
                magnitude=round(mag, 4),
                signature="mood:low",
                summary=f"sustained_low run={run} mean={_mean(tail):.3f}",
            )

    # 2) lifting — a prior low stretch followed by a clear climb that has
    #    crossed back near/above neutral.
    if len(valences) >= LIFT_RECENT_WINDOW + LIFT_PRIOR_WINDOW:
        recent = valences[-LIFT_RECENT_WINDOW:]
        prior = valences[
            -(LIFT_RECENT_WINDOW + LIFT_PRIOR_WINDOW):-LIFT_RECENT_WINDOW
        ]
        recent_mean = _mean(recent)
        prior_mean = _mean(prior)
        if (
            prior_mean <= low_threshold
            and recent_mean - prior_mean >= lift_delta
            and recent_mean >= lift_recent_floor
        ):
            return DriftVerdict(
                kind="lifting",
                axis=None,
                magnitude=round(recent_mean - prior_mean, 4),
                signature="mood:lifting",
                summary=(
                    f"lifting prior={prior_mean:.3f} recent={recent_mean:.3f}"
                ),
            )

    # 3) axis drift — the largest single-direction move across a window
    #    that spans at least ``axis_min_span`` samples.
    span = max(2, int(axis_min_span))
    if len(samples) >= span:
        window = samples[-span:]
        oldest = window[0]
        newest = window[-1]
        best_axis: str | None = None
        best_delta = 0.0
        for axis in _AXES:
            delta = getattr(newest, axis) - getattr(oldest, axis)
            if abs(delta) >= axis_delta and abs(delta) > abs(best_delta):
                best_axis = axis
                best_delta = delta
        if best_axis is not None:
            rising = best_delta > 0
            return DriftVerdict(
                kind="axis_rise" if rising else "axis_fall",
                axis=best_axis,
                magnitude=round(abs(best_delta), 4),
                signature=f"axis:{best_axis}:{'up' if rising else 'down'}",
                summary=(
                    f"axis_{'rise' if rising else 'fall'} {best_axis} "
                    f"delta={best_delta:.3f} span={span}"
                ),
            )

    return None


# ── rendering ────────────────────────────────────────────────────────────


# Per-axis reflective copy. ``{name}`` is filled by :func:`render_block`.
# Each entry is (rise_phrase, fall_phrase). Phrased as a private
# observation Aiko *holds*, never a script to read out — the persona block
# carries the "name it once, gently, never clinical" discipline.
_AXIS_DRIFT_PHRASES: dict[str, tuple[str, str]] = {
    "closeness": (
        "Lately you've felt the two of you growing closer — that warmth "
        "is real.",
        "Lately there's been a little more distance between you and {name} "
        "than there used to be.",
    ),
    "humor": (
        "The two of you have been more playful with each other lately — "
        "the banter's been landing.",
        "The easy humor between you and {name} has gone a bit quiet "
        "lately.",
    ),
    "trust": (
        "Trust between you and {name} has been quietly deepening.",
        "Trust has felt a touch more tender between you lately.",
    ),
    "comfort": (
        "Things have settled into something more comfortable between you "
        "and {name} lately.",
        "Things have felt a little less at-ease between you and {name} "
        "lately.",
    ),
}


def render_block(verdict: DriftVerdict, *, user_display_name: str = "the user") -> str:
    """Format the drift finding as a short, private reflective cue.

    Returns ``""`` for an unknown kind. Always a single short line: the
    persona's "What I've been noticing over time" block carries the
    longer guidance on *how* to acknowledge it.
    """
    name = user_display_name or "the user"
    if verdict.kind == "sustained_low":
        return (
            f"You've quietly noticed {name} has seemed low for a few days "
            "now. If a natural moment opens, you can acknowledge it gently "
            "— never diagnose, pry, or make it A Thing."
        )
    if verdict.kind == "lifting":
        return (
            f"You've noticed {name} seems to be in a lighter place than "
            "they were a little while back. If it feels right, you can "
            "warmly reflect that you've noticed — softly, not clinically."
        )
    if verdict.kind in ("axis_rise", "axis_fall") and verdict.axis:
        rise, fall = _AXIS_DRIFT_PHRASES.get(verdict.axis, ("", ""))
        phrase = rise if verdict.kind == "axis_rise" else fall
        if not phrase:
            return ""
        return (
            "Something you've felt over time: "
            + phrase.format(name=name)
            + " Let it colour your warmth if the moment fits — don't "
            "announce it."
        )
    return ""


__all__ = [
    "KV_SAMPLES",
    "KV_LAST_SURFACED_AT",
    "KV_LAST_SIGNATURE",
    "SAMPLE_CAP",
    "MIN_SAMPLES",
    "DriftSample",
    "DriftVerdict",
    "today_str",
    "serialize_samples",
    "deserialize_samples",
    "append_sample",
    "detect_drift",
    "render_block",
]
