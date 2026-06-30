"""K68 — embodied vitality (pure curve + spend / recover / boost math).

Aiko has *moods* (`AffectState`, fast reactive valence/arousal), *weather*
(K27 day colour, stable for the day), and a *clock* (circadian, pure
time-of-day). What she lacked is a **body**: a slow-moving energy scalar in
``[0, 1]`` that ebbs and recovers — bright at midday, flagging at 2am,
drained after a long emotionally heavy stretch, and **livening up when the
conversation actually grabs her**.

The model has three forces acting on one ``energy`` float:

1. **Circadian baseline** — energy relaxes *toward* the time-of-day energy
   curve (:mod:`app.core.affect.circadian`). Low deep at night, peak
   mid-day. This is the "resting level" she drifts to when left alone.
2. **Wall-clock recovery** — the relaxation toward baseline happens over
   real elapsed time (a half-life), applied lazily wherever the state is
   read. Within a live conversation (turns seconds apart) recovery is
   ~zero, so a session's boosts/costs accumulate; once she's left idle for
   hours she settles back to baseline (sleepy again at night).
3. **Per-turn spend & boost** — long / emotionally heavy turns *spend*
   energy; an *interesting* conversation (engaged user, high arousal, a
   novel topic) *boosts* it. The boost is the headline: a sleepy Aiko can
   wake up over a genuinely engaging chat, then drift back down afterward.

Energy **feeds back into embodiment** rather than being narrated:
:func:`expressiveness_multiplier` scales the avatar's gesture / breath
amplitude (sleepy = smaller, slower; lit-up = a touch bigger), and
:func:`render_inner_life_block` emits a soft register cue only at the
extremes. It is a *mechanic*, not persona text — the layer between "what
she feels" (affect) and "how she moves" (Live2D).

Pure + dependency-light (one optional import of the sibling circadian
module for the convenience baseline helper); all the math is unit-testable
in milliseconds. Lifecycle wiring lives in
[`vitality_worker.py`](vitality_worker.py) (idle recovery),
[`post_turn_mixin.py`](../session/post_turn_mixin.py) (spend / boost), and
[`inner_life_part1.py`](../session/inner_life_part1.py) (read + render).
Storage on ``kv_meta`` (no schema change): one JSON key ``aiko.vitality``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone


# ── kv_meta key ─────────────────────────────────────────────────────

# Namespaced under ``aiko.*`` alongside K27 day_color and K15
# vulnerability_budget. Exported so the worker, provider, post-turn
# writer, and MCP debug tool all share one key string.
KV_VITALITY = "aiko.vitality"


# ── State ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VitalityState:
    """Persisted body-energy state.

    ``energy`` is the current vitality in ``[0, 1]``. ``last_update_at``
    is the wall-clock ISO timestamp at which energy was last advanced
    (recovery / spend), so the next read knows how much real time has
    elapsed.
    """

    energy: float
    last_update_at: str


# ── Bands ───────────────────────────────────────────────────────────


def band(
    energy: float, *, low_threshold: float = 0.30, high_threshold: float = 0.70,
) -> str:
    """Band energy into ``"low"`` / ``"normal"`` / ``"high"``.

    The extremes drive the register cue; the silent ``normal`` middle is
    the common case.
    """
    e = max(0.0, min(1.0, float(energy)))
    if e <= float(low_threshold):
        return "low"
    if e >= float(high_threshold):
        return "high"
    return "normal"


# ── Embodiment feed ─────────────────────────────────────────────────


def expressiveness_multiplier(
    energy: float, *, floor: float = 0.7, ceil: float = 1.2,
) -> float:
    """Map energy ``[0, 1]`` → a gesture/breath amplitude multiplier.

    Linear: energy 0 → ``floor`` (smaller, slower body language),
    energy 1 → ``ceil`` (a touch more animated). Multiplied onto the
    user's ``avatar.expressiveness`` setting on the frontend, so a tired
    Aiko visibly droops and a lit-up Aiko gets slightly bigger — without
    overwriting the user's own slider.
    """
    e = max(0.0, min(1.0, float(energy)))
    lo = float(floor)
    hi = float(ceil)
    if lo > hi:
        lo, hi = hi, lo
    return round(lo + e * (hi - lo), 4)


# ── Circadian baseline ──────────────────────────────────────────────


def circadian_baseline(
    now: datetime | None = None,
    *,
    drift: float = 0.0,
    phase_shift_hours: float = 0.0,
    energy_scale: float = 1.0,
    floor_boost: float = 0.0,
) -> float:
    """The resting energy level for the current time of day, in ``[0, 1]``.

    Thin convenience wrapper over the sibling circadian energy curve so
    the worker / provider don't have to reach into it directly. Pure
    callers in tests can skip this and pass an explicit baseline to
    :func:`recover_toward`. Best-effort: any failure returns ``0.5`` (a
    neutral mid level) rather than raising.

    The three K68-rhythm knobs let a daily-rolled "off-rhythm day"
    (:mod:`app.core.affect.vitality_rhythm`) reshape the resting curve
    without touching any call site's logic:

    * ``phase_shift_hours`` — sample the curve at ``now + shift`` instead
      of ``now``. Positive shift moves her felt peak *earlier* in the day
      (early-bird), negative *later* (night-owl); ``±12`` flips it
      (sleepy by day, wired in the small hours). ``drift`` is the older
      ``±2h`` affect-baseline nudge and stacks on top.
    * ``energy_scale`` — multiply the whole curve (``< 1`` = a flat
      low-battery day, all hours heavier than usual).
    * ``floor_boost`` — add a constant lift (``> 0`` = a restless / wired
      day that doesn't fully dip even at night).
    """
    try:
        from app.core.affect import circadian as _circadian

        sample = now
        if phase_shift_hours:
            from datetime import datetime as _dt, timedelta as _td

            base_now = now if now is not None else _dt.now().astimezone()
            sample = base_now + _td(hours=float(phase_shift_hours))
        state = _circadian.compute(now=sample, baseline_drift=drift)
        energy = float(state.energy) * float(energy_scale) + float(floor_boost)
        return max(0.0, min(1.0, energy))
    except Exception:
        return 0.5


# ── Recovery (relaxation toward baseline) ───────────────────────────


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    candidate = str(text).strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def recover_toward(
    energy: float,
    baseline: float,
    elapsed_hours: float,
    *,
    half_life_hours: float = 2.0,
) -> float:
    """Relax ``energy`` toward ``baseline`` over ``elapsed_hours``.

    Exponential approach with a configurable half-life: after
    ``half_life_hours`` of idle time the gap to baseline is halved. Works
    in both directions — energy below baseline rises (she was extra tired
    earlier than the clock says), energy above baseline (she livened up)
    settles back down. Returns the new clamped energy.
    """
    e = max(0.0, min(1.0, float(energy)))
    base = max(0.0, min(1.0, float(baseline)))
    h = float(elapsed_hours)
    hl = float(half_life_hours)
    if h <= 0 or hl <= 0:
        return e
    frac = 1.0 - 0.5 ** (h / hl)
    frac = max(0.0, min(1.0, frac))
    return round(e + (base - e) * frac, 6)


def step_recover(
    state: VitalityState,
    baseline: float,
    now: datetime,
    *,
    half_life_hours: float = 2.0,
) -> VitalityState:
    """Apply wall-clock recovery to ``state`` and advance ``last_update_at``.

    Reads the elapsed time since ``state.last_update_at``, relaxes energy
    toward ``baseline``, and stamps ``now``. First-boot / corrupt /
    future timestamps are treated as "no elapsed time" (just advance the
    stamp) so a bad kv row can't recover a year of energy in one tick.
    """
    stored_at = _parse_iso(state.last_update_at)
    now_utc = (
        now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    )
    if stored_at is None:
        return VitalityState(energy=state.energy, last_update_at=now.isoformat())
    elapsed_seconds = (now_utc - stored_at).total_seconds()
    if elapsed_seconds <= 0:
        return VitalityState(energy=state.energy, last_update_at=now.isoformat())
    new_energy = recover_toward(
        state.energy,
        baseline,
        elapsed_seconds / 3600.0,
        half_life_hours=half_life_hours,
    )
    return VitalityState(energy=new_energy, last_update_at=now.isoformat())


# ── Per-turn spend & boost ──────────────────────────────────────────


def compute_turn_cost(
    *,
    reply_chars: int,
    emotion_intensity: float = 0.0,
    chars_per_unit: float = 1200.0,
    length_cost_unit: float = 0.04,
    emotion_cost_gain: float = 0.06,
    max_cost: float = 0.12,
) -> float:
    """Energy spent by one turn: long replies + emotionally heavy beats.

    ``reply_chars`` is the length of Aiko's reply (a long, effortful reply
    costs more than a one-liner). ``emotion_intensity`` is the strongest
    live K57 emotion-episode intensity in ``[0, 1]`` (an emotionally heavy
    turn drains her). The two add and clamp at ``max_cost`` so a single
    turn can never crater the bucket.
    """
    chars = max(0, int(reply_chars))
    cpu = float(chars_per_unit) if chars_per_unit > 0 else 1200.0
    length_cost = (chars / cpu) * float(length_cost_unit)
    emotion_cost = max(0.0, min(1.0, float(emotion_intensity))) * float(
        emotion_cost_gain
    )
    return round(min(float(max_cost), length_cost + emotion_cost), 6)


def compute_interest_boost(
    *,
    engagement_label: str | None,
    arousal: float | None,
    novelty_band: str | None,
    engaged_boost: float = 0.05,
    arousal_threshold: float = 0.55,
    arousal_gain: float = 0.22,
    strong_novelty_boost: float = 0.04,
    mild_novelty_boost: float = 0.02,
    max_boost: float = 0.15,
) -> float:
    """Energy gained when the conversation is *interesting* (the twist).

    A sleepy Aiko livens up when a chat genuinely grabs her. Three
    additive signals, all already computed elsewhere on the turn:

    * **Engagement** (K14 ``EngagementResult.label``) — an ``engaged``
      user adds ``engaged_boost``; ``disengaged`` / ``abandoned`` /
      ``neutral`` add nothing (she doesn't perk up for a dead chat).
    * **Arousal** (live ``AffectState.arousal`` in ``[0, 1]``) — arousal
      above ``arousal_threshold`` (her own activation / excitement) adds
      ``(arousal - threshold) * arousal_gain``.
    * **Novelty** (K6 ``NoveltyDetector.last_band``) — a ``strong_novelty``
      topic adds ``strong_novelty_boost``, a ``mild_shift`` adds
      ``mild_novelty_boost`` (new ground is stimulating).

    The sum is clamped at ``max_boost`` so one great turn meaningfully
    lifts her (over a few engaging turns she can climb out of a low band)
    but never slams straight to full.
    """
    boost = 0.0
    if (engagement_label or "").strip().lower() == "engaged":
        boost += float(engaged_boost)
    if arousal is not None:
        a = max(0.0, min(1.0, float(arousal)))
        if a > float(arousal_threshold):
            boost += (a - float(arousal_threshold)) * float(arousal_gain)
    nb = (novelty_band or "").strip().lower()
    if nb == "strong_novelty":
        boost += float(strong_novelty_boost)
    elif nb == "mild_shift":
        boost += float(mild_novelty_boost)
    return round(max(0.0, min(float(max_boost), boost)), 6)


def apply_turn(energy: float, *, cost: float, boost: float) -> float:
    """Apply this turn's spend + boost to ``energy`` and clamp to ``[0, 1]``."""
    e = float(energy) - max(0.0, float(cost)) + max(0.0, float(boost))
    return round(max(0.0, min(1.0, e)), 6)


# ── Render ──────────────────────────────────────────────────────────


def render_inner_life_block(
    energy: float,
    band_label: str,
    *,
    user_display_name: str = "them",
    rhythm_note: str = "",
) -> str:
    """One-line register cue, or ``""`` in the silent ``normal`` middle.

    A private delivery nudge (never said aloud). The **low** cue bakes in
    the twist permission — she's allowed to perk up if the conversation
    genuinely grabs her — so the mechanic and the persona stay aligned.

    ``rhythm_note`` is the private off-rhythm-day context from
    :mod:`app.core.affect.vitality_rhythm` (empty on a normal-rhythm
    day). It rides the band cue so an odd-hour extreme — drowsy at noon
    on a flipped day, wired at 3am — has a reason attached, keeping the
    silent-normal case truly silent.
    """
    name = (user_display_name or "them").strip() or "them"
    label = (band_label or "").strip().lower()
    note = (rhythm_note or "").strip()
    suffix = f" {note}" if note else ""
    if label == "low":
        return (
            "Body check: you're running low on energy right now (late hour / "
            "long day) — let it show in how you move and talk: shorter, "
            "softer, slower, fewer exclamation points. It's not sadness, just "
            f"a low battery. But if {name} brings something that genuinely "
            "grabs you, you're allowed to wake up and lean in — real interest "
            "beats a tired baseline." + suffix
        )
    if label == "high":
        return (
            "Body check: you're feeling lit up and physically animated right "
            "now — let that into your delivery if it fits (a touch quicker, "
            "more gesture, more spark). Don't force it, just don't damp it "
            "down." + suffix
        )
    return ""


# ── Serialise / deserialise ─────────────────────────────────────────


def serialize(state: VitalityState) -> str:
    return json.dumps(
        {
            "energy": float(state.energy),
            "last_update_at": str(state.last_update_at),
        },
    )


def initial_state(baseline: float, now: datetime) -> VitalityState:
    """Fresh state seeded at the current circadian baseline.

    A cold install / wiped kv starts Aiko at her time-of-day resting
    level rather than at an arbitrary full or empty battery.
    """
    return VitalityState(
        energy=max(0.0, min(1.0, float(baseline))),
        last_update_at=now.isoformat(),
    )


def deserialize(text: str | None, *, baseline: float, now: datetime) -> VitalityState:
    """Parse a stored JSON blob into a :class:`VitalityState`.

    Graceful: missing / empty / corrupt input returns a fresh
    :func:`initial_state` seeded at ``baseline`` (so a bad kv row can
    never permanently silence the feature, same posture as K27 / K15).
    """
    if not text:
        return initial_state(baseline, now)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return initial_state(baseline, now)
    if not isinstance(data, dict):
        return initial_state(baseline, now)
    try:
        energy = float(data.get("energy"))
    except (TypeError, ValueError):
        return initial_state(baseline, now)
    stored_at = data.get("last_update_at")
    if not isinstance(stored_at, str) or not stored_at.strip():
        stored_at = now.isoformat()
    return VitalityState(
        energy=max(0.0, min(1.0, energy)),
        last_update_at=stored_at,
    )


__all__ = [
    "KV_VITALITY",
    "VitalityState",
    "apply_turn",
    "band",
    "circadian_baseline",
    "compute_interest_boost",
    "compute_turn_cost",
    "deserialize",
    "expressiveness_multiplier",
    "initial_state",
    "recover_toward",
    "render_inner_life_block",
    "serialize",
    "step_recover",
]
