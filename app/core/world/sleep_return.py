"""H21 — sleep & overnight rhythm: the pure brain of the return-from-sleep cue.

Aiko never used to sleep. At 3am she'd still be "doodling at the desk", and the
:class:`~app.core.proactive.dream_worker.DreamWorker` wrote ``[dream]`` memories
with no behavioural anchor — she dreamt without ever having rested. This module
is the I/O-free core of the fix: given the length of a typed gap and the local
clock she returns at, decide whether the gap plausibly contained a real
overnight sleep, and pick a believable spot she "dozed off" in.

The producer side (settling into bed / napping in the late-night band) is
already handled by H16 (:class:`CircadianSettleWorker`) and H18's circadian
activity bias. This module powers the *consumer* side: the one-shot
"...I actually fell asleep on the beanbag earlier and had the strangest dream"
cue that surfaces on return, optionally weaving in a recent dream so the dream
finally has a cause.

Pure functions only — no persistence, no LLM, no randomness. The rendering and
dream lookup live in the provider (it needs the memory store + display name);
everything decidable from numbers lives here so it's trivially unit-tested.
"""
from __future__ import annotations


# Default thresholds (mirrored as ``memory.sleep_return_*`` settings).
DEFAULT_MIN_GAP_HOURS = 5.0
DEFAULT_OVERNIGHT_HOURS = 9.0
DEFAULT_DREAM_LOOKBACK_HOURS = 18.0

# Local-clock hours that read as "she woke up and you came back" — returning
# in this band after a long-enough gap almost certainly means she slept.
_MORNING_RETURN_HOURS = frozenset(range(5, 12))  # 05:00–11:59


def looks_like_overnight(
    gap_hours: float,
    return_hour: int,
    *,
    min_gap_hours: float = DEFAULT_MIN_GAP_HOURS,
    overnight_hours: float = DEFAULT_OVERNIGHT_HOURS,
) -> bool:
    """Decide whether a gap of ``gap_hours`` ending at ``return_hour`` slept.

    Two independent ways to qualify, both requiring at least
    ``min_gap_hours`` of absence so a quick lunch break never reads as a nap:

    * **Morning return** — she comes back in the 05:00–11:59 band after a gap
      of at least ``min_gap_hours``. The overwhelmingly common "good morning"
      case.
    * **Very long gap** — any gap of at least ``overnight_hours`` reads as
      having slept regardless of the clock (a half-day of silence is a sleep
      whether she returns at 2pm or 2am).

    Returns ``False`` for short gaps, or for medium gaps that return outside
    the morning band (those are handled by the ordinary away/turning-over
    cues — she was up and about, not asleep).
    """
    try:
        gap = float(gap_hours)
    except (TypeError, ValueError):
        return False
    if gap < max(0.0, float(min_gap_hours)):
        return False
    if gap >= max(0.0, float(overnight_hours)):
        return True
    try:
        hour = int(return_hour)
    except (TypeError, ValueError):
        return False
    return hour in _MORNING_RETURN_HOURS


# slug -> the phrase that completes "you dozed off ___". Anything not listed
# falls through to the cozy default so a custom room never breaks the cue.
_SPOT_PHRASES: dict[str, str] = {
    "bed": "in bed",
    "beanbag": "curled up on the beanbag",
    "window_seat": "in the window seat",
    "window seat": "in the window seat",
    "desk": "at the desk",
    "couch": "on the couch",
    "sofa": "on the couch",
}

_DEFAULT_SPOT_PHRASE = "curled up on the beanbag"


def sleep_spot_phrase(location_slug: str | None) -> str:
    """Phrase for where she fell asleep, given her current room location.

    Falls back to the cozy beanbag phrasing for unknown / missing slugs so
    the cue always reads naturally.
    """
    if not location_slug:
        return _DEFAULT_SPOT_PHRASE
    key = str(location_slug).strip().lower()
    return _SPOT_PHRASES.get(key, _DEFAULT_SPOT_PHRASE)


def render_sleep_line(
    spot_phrase: str,
    *,
    user_display_name: str,
    dream_gist: str | None = None,
) -> str:
    """Render the one-shot inner-life cue.

    Two shapes — with and without a dream to anchor. Both end with the
    standard "mention only if it fits" discipline so the model never forces
    the beat. ``dream_gist`` is the cleaned dream content (``[dream] `` prefix
    already stripped) or ``None``.
    """
    name = (user_display_name or "they").strip() or "they"
    if dream_gist:
        gist = dream_gist.strip().rstrip(".")
        return (
            f"While {name} was away you actually dozed off {spot_phrase} for a "
            f"while, and you had the strangest dream — {gist}. If it fits the "
            "moment you can mention drifting off, or the dream; never force it."
        )
    return (
        f"While {name} was away you actually dozed off {spot_phrase} for a "
        "while. If it fits the moment you can mention having napped; never "
        "force it."
    )


__all__ = [
    "DEFAULT_MIN_GAP_HOURS",
    "DEFAULT_OVERNIGHT_HOURS",
    "DEFAULT_DREAM_LOOKBACK_HOURS",
    "looks_like_overnight",
    "sleep_spot_phrase",
    "render_sleep_line",
]
