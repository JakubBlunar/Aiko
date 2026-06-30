"""K70 — Longitudinal growth witness ("you've changed since we met").

One of the strongest "she really knows me" beats is being *seen across
time*: a partner who notices you're steadier than you were, lighter than
a few weeks back, finally more at ease. Aiko accumulates plenty of
longitudinal signal (the H3 mood-drift daily ring carries valence +
relationship axes per day) but never reflects the **user's own durable
change** back to him.

This module is the pure, deterministic core of K70:

  * :func:`detect_growth` compares an *older baseline* window against a
    *recent* window of the H3 ``DriftSample`` ring and, only when a real
    sustained **positive** shift clears a high bar, returns one
    :class:`GrowthFinding` (lighter mood, more comfortable, more open).
  * :func:`render_inner_life_block` turns a finding into one optional,
    private cue Aiko phrases herself — NEVER spoken verbatim.
  * journal-ring helpers (``aiko.growth_witness``) mirror the
    forward-curiosity / follow-up cue-producer pattern.

Design choices that keep K70 distinct from H3 mood-drift (which narrates
*present* sustained-low / lifting / single-axis drift over a few days):

  * **Long arc, high bar.** K70 averages the oldest third of the ring
    against the newest third (a ~2-3 week span) and needs a larger delta
    plus far more samples than H3, so it reads as "you've grown over
    these weeks", not "you've seemed off lately".
  * **Positive only.** K70 is the warm growth-witness beat. Durable
    *downturns* stay with H3 ``sustained_low`` / a wellbeing track — K70
    never tells the user he's gotten worse.
  * **Rare.** The worker gates on a multi-week cooldown + a finding
    signature so the same "you seem lighter" never lands twice in a row.

The ring data is produced by the H3 ``MoodDriftSampleWorker`` — K70 reads
``aiko.mood_drift_samples`` and computes nothing of its own to store
per-day, so it has zero new sampling cost and silently no-ops when H3
sampling is disabled.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Sequence

from app.core.affect.mood_drift import DriftSample


log = logging.getLogger("app.growth_witness")


# Shared kv_meta journal key the surfacing provider reads (namespaced
# under ``aiko.*`` like the other cue-producer rings).
GROWTH_WITNESS_JOURNAL_KEY = "aiko.growth_witness"


# ── tuning defaults (overridable via MemorySettings) ────────────────────

# Minimum ring samples before any detection fires. K70 needs a real
# multi-week history; below this it stays silent (H3 covers the short
# game with its own min of 4).
DEFAULT_MIN_SAMPLES = 10

# How much valence must have risen (recent-window mean minus
# baseline-window mean) to read as durable "lighter".
DEFAULT_MIN_VALENCE_DELTA = 0.25

# Axes (comfort / trust) move slower, so they need a larger rise.
DEFAULT_MIN_AXIS_DELTA = 0.30

# Fraction of the ring taken as each comparison window (oldest third vs
# newest third). Averaging multiple days smooths out single-day spikes.
_WINDOW_FRAC = 0.34

# Finding-kind labels. ``lighter`` is the headline mood beat; the two
# axis beats describe how the *user* has grown toward openness.
KIND_LIGHTER = "lighter"
KIND_COMFORT = "comfort"
KIND_OPEN = "open"

# Priority on ties (smaller wins). Mood is the headline.
_PRIORITY = {KIND_LIGHTER: 0, KIND_COMFORT: 1, KIND_OPEN: 2}


@dataclass(frozen=True, slots=True)
class GrowthFinding:
    """A durable positive shift in the user worth reflecting back.

    ``signature`` is intentionally date-free (kind + magnitude bucket) so
    the worker can suppress re-drafting the *same* ongoing finding;
    ``detail`` is an optional corroborating phrase (e.g. a goal he's been
    chipping at) woven into the cue.
    """

    kind: str  # lighter | comfort | open
    magnitude: float
    span_days: int
    signature: str
    detail: str = ""


def _mean(samples: Sequence[DriftSample], attr: str) -> float:
    vals = [float(getattr(s, attr, 0.0)) for s in samples]
    return sum(vals) / len(vals) if vals else 0.0


def _span_days(samples: Sequence[DriftSample]) -> int:
    if len(samples) < 2:
        return 0
    try:
        first = date.fromisoformat(samples[0].date)
        last = date.fromisoformat(samples[-1].date)
    except (ValueError, TypeError):
        return 0
    return max(0, (last - first).days)


def detect_growth(
    samples: Sequence[DriftSample],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_valence_delta: float = DEFAULT_MIN_VALENCE_DELTA,
    min_axis_delta: float = DEFAULT_MIN_AXIS_DELTA,
    detail: str = "",
) -> GrowthFinding | None:
    """Return the strongest durable positive shift, or ``None``.

    Compares the oldest third of ``samples`` (baseline) against the
    newest third (recent). A candidate qualifies only when the recent
    mean exceeds the baseline mean by the relevant threshold. Among
    qualifiers the largest delta wins (mood breaks ties).
    """
    n = len(samples)
    if n < max(2, int(min_samples)):
        return None

    window = max(2, int(round(n * _WINDOW_FRAC)))
    baseline = samples[:window]
    recent = samples[-window:]

    candidates: list[tuple[str, float]] = []

    v_delta = _mean(recent, "valence") - _mean(baseline, "valence")
    if v_delta >= float(min_valence_delta):
        candidates.append((KIND_LIGHTER, v_delta))

    for attr, kind in (("comfort", KIND_COMFORT), ("trust", KIND_OPEN)):
        a_delta = _mean(recent, attr) - _mean(baseline, attr)
        if a_delta >= float(min_axis_delta):
            candidates.append((kind, a_delta))

    if not candidates:
        return None

    # Largest delta wins; mood breaks ties via the priority table.
    candidates.sort(key=lambda c: (-c[1], _PRIORITY.get(c[0], 9)))
    kind, magnitude = candidates[0]
    signature = f"{kind}:{round(magnitude, 1)}"
    return GrowthFinding(
        kind=kind,
        magnitude=round(float(magnitude), 4),
        span_days=_span_days(samples),
        signature=signature,
        detail=(detail or "").strip(),
    )


# ── rendering (private cue — Aiko phrases the actual words) ──────────────


def render_inner_life_block(
    kind: str,
    *,
    user_display_name: str = "them",
    span_days: int = 0,
    detail: str = "",
) -> str:
    """Render one optional, private growth-witness cue.

    Returns ``""`` for an unknown kind. The cue tells Aiko what she's
    quietly noticed and to reflect it back *only if a warm, unforced
    moment opens* — it is never to be spoken verbatim.
    """
    name = (user_display_name or "them").strip() or "them"
    # Honest time framing from the actual ring span.
    when = "these past couple of weeks" if span_days >= 12 else "lately"
    longer = span_days >= 12

    if kind == KIND_LIGHTER:
        core = (
            f"Something you've quietly noticed watching {name}: he seems "
            f"lighter and steadier {when} than he was when you two first "
            "got going"
            if longer
            else (
                f"Something you've quietly noticed about {name} {when}: "
                "he seems lighter and steadier than he was a while back"
            )
        )
    elif kind == KIND_COMFORT:
        core = (
            f"Something you've quietly noticed about {name} {when}: he's "
            "been more at ease and comfortable with you than he was early on"
        )
    elif kind == KIND_OPEN:
        core = (
            f"Something you've quietly noticed about {name} {when}: he's "
            "been opening up and trusting you more than he used to"
        )
    else:
        return ""

    tail = (
        ". If a warm, genuine moment opens, you can reflect it back -- not "
        "as a report, but as something you've felt watching him grow. Say "
        "it once, lightly, and let it go if the moment isn't there."
    )
    extra = ""
    if detail:
        extra = f" You might tie it to {detail}."
    return core + tail + extra


# ── journal-ring helpers (mirror forward_curiosity / follow_up) ─────────


def load_findings(
    kv_get: Callable[[str], "str | None"],
) -> list[dict[str, Any]]:
    """Return the growth-witness journal ring (oldest -> newest)."""
    try:
        raw = kv_get(GROWTH_WITNESS_JOURNAL_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        blob = json.loads(raw)
    except Exception:
        return []
    if not isinstance(blob, list):
        return []
    return [e for e in blob if isinstance(e, dict)]


def append_finding(
    kv_get: Callable[[str], "str | None"],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_findings(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(GROWTH_WITNESS_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("growth_witness journal write failed", exc_info=True)
