"""Time-of-day modeling: a deterministic circadian state for Aiko.

Pure-function module — no persistence, no LLM. Given the current local time
(plus an optional pair of slowly-drifting affect baselines), produces a
:class:`CircadianState` describing what kind of moment-of-day Aiko is in:
period name, energy curve, drowsy flag, and a small sociability bias.

The curve is a simple piecewise sine fit to a normal evening-person
schedule, with a small "drift" knob fed by the user's affect baselines so
Aiko can become a slightly different morning/night person over weeks. We
don't try to be biologically accurate here — the goal is to give the
prompt a believable cue ("late evening, energy low") and a knob the
prosody mapper can lean on (drowsy → slower, breathier).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


CircadianPeriod = Literal[
    "late_night",
    "early_morning",
    "morning",
    "midday",
    "afternoon",
    "evening",
    "night",
]


@dataclass(slots=True, frozen=True)
class CircadianState:
    """Snapshot of Aiko's time-of-day state at the current local clock.

    All fields are derived deterministically from the input time + baseline
    knobs; no I/O. Read by :class:`PromptAssembler` (ambient block) and the
    Phase 5b prosody mapper.
    """

    period: CircadianPeriod
    energy: float          # 0..1; smooth daily curve
    drowsy: bool           # late_night & energy < 0.25
    sociability_bias: float  # -0.3..+0.3
    hour: int
    minute: int

    def ambient_line(self) -> str:
        """Render the small one-line cue we paste into the system prompt."""
        period_phrase = _PERIOD_PHRASES.get(self.period, self.period)
        time_part = _format_clock(self.hour, self.minute)
        if self.drowsy:
            return (
                f"It's {time_part} ({period_phrase}); your energy is low "
                f"({self.energy:.2f}) and you feel a bit drowsy."
            )
        return (
            f"It's {time_part} ({period_phrase}); your energy is "
            f"{self.energy:.2f}."
        )


_PERIOD_PHRASES: dict[CircadianPeriod, str] = {
    "late_night": "late night",
    "early_morning": "early morning",
    "morning": "morning",
    "midday": "midday",
    "afternoon": "afternoon",
    "evening": "evening",
    "night": "night",
}


def _format_clock(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def _classify_period(hour: int) -> CircadianPeriod:
    """Map a 24h hour to one of our seven named periods."""
    if hour < 5:
        return "late_night"
    if hour < 8:
        return "early_morning"
    if hour < 12:
        return "morning"
    if hour < 14:
        return "midday"
    if hour < 18:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "night"


def _energy_curve(hour: int, minute: int, drift: float = 0.0) -> float:
    """Continuous 0..1 energy curve over a 24h day.

    Modeled as a sum of two sinusoids:
      - main wake/sleep cycle (period 24h): peak around 14:00, trough ~03:00
      - mid-morning bump and afternoon slump (period 12h)

    ``drift`` shifts the peak earlier (drift < 0) or later (drift > 0) by up
    to ~2 hours, so an "evening person" baseline can produce a slightly
    later peak. Values outside [-1, +1] are clamped.
    """
    drift = max(-1.0, min(1.0, float(drift)))
    fractional_hour = hour + (minute / 60.0)
    peak_hour = 14.0 + (drift * 2.0)  # afternoon peak, drifts +/- 2h

    # Main 24h wake/sleep cycle, normalized to 0..1 (cosine peaks at peak_hour).
    main = 0.5 + 0.5 * math.cos(
        2 * math.pi * (fractional_hour - peak_hour) / 24.0,
    )
    # Secondary 12h ripple — very gentle (amplitude 0.08) — gives the
    # familiar mid-morning bump and afternoon dip without dominating.
    ripple = 0.08 * math.sin(
        2 * math.pi * (fractional_hour - 7.0) / 12.0,
    )
    energy = main + ripple
    if energy < 0.0:
        return 0.0
    if energy > 1.0:
        return 1.0
    return float(energy)


def compute(
    now: datetime | None = None,
    *,
    baseline_drift: float = 0.0,
    baseline_sociability: float = 0.0,
) -> CircadianState:
    """Build a :class:`CircadianState` for the given moment.

    ``now`` defaults to the current local time. ``baseline_drift`` is in
    [-1, +1] (negative = morning person, positive = night owl) and shifts
    the energy peak. ``baseline_sociability`` is in [-1, +1] and feeds into
    ``sociability_bias`` (capped at ±0.3 in the output so the prompt never
    swings wildly).
    """
    if now is None:
        try:
            now = datetime.now().astimezone()
        except Exception:
            now = datetime.now()
    hour = int(now.hour)
    minute = int(now.minute)
    period = _classify_period(hour)
    energy = _energy_curve(hour, minute, drift=baseline_drift)
    drowsy = period in ("late_night", "night") and energy < 0.25
    # Compose sociability_bias: the daytime peak naturally adds, the night
    # trough subtracts, plus the persistent baseline.
    daytime_kick = (energy - 0.5) * 0.3
    raw_bias = daytime_kick + (0.3 * float(baseline_sociability))
    sociability_bias = max(-0.3, min(0.3, raw_bias))
    return CircadianState(
        period=period,
        energy=round(energy, 3),
        drowsy=drowsy,
        sociability_bias=round(sociability_bias, 3),
        hour=hour,
        minute=minute,
    )
