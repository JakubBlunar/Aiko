"""K-time4: session-elapsed & mid-session gap awareness (pure core).

The cross-session gap family (J5 reconnection, K14 absence_curiosity, K28
turning_over, …) all answer "{user} came *back* after being away". None of
them know anything about the **current conversation's own clock**:

  * how long *this* continuous sitting has run ("we've been at this a
    while now" / "it's gotten late and we've been talking an hour"), and
  * a notable *mid-session* pause that's too short for a reconnection beat
    but too long to ignore ("you stepped away for 20 min and came back").

This module is the pure, deterministic core (no I/O, injected ``now``):

- :func:`continuous_burst` collapses a newest-first list of message
  timestamps into the duration of the current uninterrupted sitting — it
  walks backward only while each step's gap stays under
  ``break_seconds`` (a longer pause means a new sitting), so a session
  that started days ago but has a fresh burst reads as minutes, not days;
- :func:`classify` bands the elapsed duration (``long`` / ``very_long``)
  and decides whether the latest pause is a notable mid-session gap;
- :func:`render_block` builds the prompt cue with the explicit "observe,
  don't police" tonal guard.

The mid-session pause band tops out at the absence_curiosity floor
(30 min) by config, so K-time4 never double-fires with the gap-return
family that owns everything above it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


@dataclass(frozen=True)
class SessionClockSignal:
    """The two derived sub-signals for one assembly."""

    elapsed_seconds: float
    elapsed_band: str | None  # "very_long" | "long" | None
    burst_start_iso: str  # one-shot anchor: identifies the current sitting
    gap_seconds: float
    gap_notable: bool


def continuous_burst(
    times_desc: Sequence[datetime],
    now: datetime,
    *,
    break_seconds: float,
) -> tuple[float, datetime]:
    """Return ``(elapsed_seconds, burst_start)`` for the current sitting.

    ``times_desc`` is the recent message timestamps newest-first (all
    timezone-aware). Walks backward from the newest message while each
    consecutive gap stays ``<= break_seconds``; the first larger gap ends
    the sitting. ``elapsed`` is ``now - burst_start`` (never negative).
    Empty input → ``(0.0, now)``.
    """
    if not times_desc:
        return 0.0, now
    burst_start = times_desc[0]
    for older in times_desc[1:]:
        if (burst_start - older).total_seconds() > break_seconds:
            break
        burst_start = older
    elapsed = max(0.0, (now - burst_start).total_seconds())
    return elapsed, burst_start


def _classify_elapsed(
    elapsed_seconds: float, *, long_seconds: float, very_long_seconds: float,
) -> str | None:
    if elapsed_seconds >= very_long_seconds:
        return "very_long"
    if elapsed_seconds >= long_seconds:
        return "long"
    return None


def classify(
    times_desc: Sequence[datetime],
    now: datetime,
    *,
    long_seconds: float,
    very_long_seconds: float,
    break_seconds: float,
    gap_min_seconds: float,
    gap_max_seconds: float,
) -> SessionClockSignal:
    """Compute the elapsed band + notable-pause flag for this assembly."""
    elapsed, burst_start = continuous_burst(
        times_desc, now, break_seconds=break_seconds,
    )
    band = _classify_elapsed(
        elapsed, long_seconds=long_seconds, very_long_seconds=very_long_seconds,
    )
    # The mid-session pause is the delta before the latest message (the
    # gap the user just took before this turn). Needs two messages.
    gap_seconds = 0.0
    if len(times_desc) >= 2:
        gap_seconds = max(
            0.0, (times_desc[0] - times_desc[1]).total_seconds()
        )
    gap_notable = bool(
        gap_min_seconds <= gap_seconds < gap_max_seconds
        and gap_max_seconds > gap_min_seconds
    )
    return SessionClockSignal(
        elapsed_seconds=elapsed,
        elapsed_band=band,
        burst_start_iso=burst_start.isoformat(),
        gap_seconds=gap_seconds,
        gap_notable=gap_notable,
    )


def humanize_elapsed(seconds: float) -> str:
    """Fuzzy duration phrase for the current sitting ("about an hour")."""
    minutes = max(0.0, float(seconds)) / 60.0
    if minutes < 90.0:
        return "about an hour"
    if minutes < 150.0:
        return "an hour and a half or so"
    if minutes < 210.0:
        return "a couple of hours"
    hours = round(seconds / 3600.0)
    return f"{hours} hours" if hours > 2 else "a couple of hours"


def humanize_pause(seconds: float) -> str:
    """Fuzzy pause phrase rounded to the nearest 5 min ("about 20 minutes")."""
    minutes = max(1.0, float(seconds) / 60.0)
    rounded = max(5, int(round(minutes / 5.0) * 5))
    return f"about {rounded} minutes"


def render_block(signal: SessionClockSignal, user_display_name: str) -> str:
    """Render the (one or two line) cue, or ``""`` when nothing surfaces."""
    name = (user_display_name or "").strip() or "they"
    lines: list[str] = []
    if signal.elapsed_band is not None:
        dur = humanize_elapsed(signal.elapsed_seconds)
        lines.append(
            f"You and {name} have been talking for {dur} now — notice it "
            "naturally only if it fits (a soft 'we've been at this a while' "
            "or, if it's late, a gentle nudge toward rest). Observe, never "
            "police how long they've been here."
        )
    if signal.gap_notable:
        pause = humanize_pause(signal.gap_seconds)
        lines.append(
            f"{name} was away {pause} and just came back — a light, "
            "warm acknowledgement is fine if it fits; never make them "
            "explain where they went."
        )
    return "\n".join(lines)


__all__ = [
    "SessionClockSignal",
    "continuous_burst",
    "classify",
    "humanize_elapsed",
    "humanize_pause",
    "render_block",
]
