"""J5 — reconnection ritual helpers.

Pure functions backing the reconnection cue: a warm re-anchoring beat
surfaced on the *first* reply after a genuinely long absence (a day or
more), distinct from the post-turn gap family:

  * K14 absence_curiosity (30 min - 4 h)  — light welcome-back curiosity
  * K28 turning_over (>= 90 min)          — "I've been thinking about X"
  * K36 away_activities (>= 4 h)          — "while you were away I ..."
  * K57 lonely episode (~5 h, scaled)     — the *felt* missed-you beat

J5 sits well above all of those (default 24 h base, closeness-scaled) and
owns the immediate "good to see you, it's been a while" opener. The
closer the relationship, the *sooner* a gap reads as a real absence
(closeness lowers the threshold), mirroring the K57 lonely scaling.

Kept as pure functions (no I/O) so the threshold math and the duration
phrasing are exhaustively unit-testable without a controller.
"""
from __future__ import annotations


# Never treat a gap shorter than this as a reconnection, even for a very
# close relationship — keeps J5 clear of the K57 lonely band (~5 h).
_MIN_THRESHOLD_HOURS = 6.0

# How strongly closeness pulls the threshold down. At closeness +1 the
# threshold is 70% of base (notices sooner); at -1 it's 130% (slower).
_CLOSENESS_SCALE = 0.3


def reconnection_threshold_hours(
    closeness: float | None,
    *,
    base_hours: float,
) -> float:
    """Closeness-scaled gap threshold (hours) for a reconnection beat."""
    c = max(-1.0, min(1.0, float(closeness) if closeness is not None else 0.0))
    scaled = float(base_hours) * (1.0 - _CLOSENESS_SCALE * c)
    return max(_MIN_THRESHOLD_HOURS, scaled)


def should_reconnect(
    gap_seconds: float | None,
    *,
    closeness: float | None,
    base_hours: float,
) -> bool:
    """True when the gap clears the closeness-scaled threshold."""
    if gap_seconds is None:
        return False
    threshold_s = reconnection_threshold_hours(
        closeness, base_hours=base_hours,
    ) * 3600.0
    return float(gap_seconds) >= threshold_s


def humanize_gap(seconds: float | None) -> str:
    """Render a gap as a natural, imprecise duration phrase.

    Deliberately fuzzy ("a couple of days", "about a week") — a companion
    says "it's been a while", not "it's been 53.2 hours".
    """
    if seconds is None:
        return "a while"
    hours = max(0.0, float(seconds)) / 3600.0
    if hours < 20.0:
        return "several hours"
    days = hours / 24.0
    if days < 1.5:
        return "about a day"
    if days < 6.5:
        return f"{round(days)} days"
    weeks = days / 7.0
    if weeks < 1.5:
        return "about a week"
    if weeks < 4.0:
        return f"{round(weeks)} weeks"
    months = days / 30.0
    if months < 1.5:
        return "about a month"
    return f"{round(months)} months"
