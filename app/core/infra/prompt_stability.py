"""Coarsening helpers for numbers that appear in cached prompt tiers.

Nothing here is about accuracy. It is about a specific, invisible cost:
a running count printed into an early prompt tier changes the bytes of
that tier on *every turn*, and an early tier that changes every turn
cannot be cached.

That matters more than it used to. GPT-5.6+ only reads the cache at an
explicit breakpoint, so the reusable prefix is a contiguous run of blocks
that has to be byte-identical to last turn's -- one incrementing integer
anywhere inside it forfeits the whole discount for everything after it.
The prompt does not get worse, no test fails, and the bill goes up. See
[`docs/prompt-caching.md`](../../../docs/prompt-caching.md).

The counts these functions coarsen were never load-bearing at full
precision. "You've been talking for ~40 days, 1,487 turns" and "…, 1,450
turns" say the same thing to a reader; the second one is free on the
turns in between. Both call sites already hedged with a "~".

Keep the exact value anywhere it is *read* rather than narrated --
thresholds, milestones, telemetry. This is for prose.
"""
from __future__ import annotations


# Grid to round down onto, by magnitude. Coarse enough that a long
# conversation crosses a boundary rarely, fine enough that the narrated
# figure still tracks reality: at 1,500 turns the step is 100, so the
# line changes about once every hundred turns instead of every one.
_STEPS: tuple[tuple[int, int], ...] = (
    (10, 1),      # under 10, every turn matters and there are few of them
    (50, 5),
    (200, 10),
    (1000, 25),
    (5000, 100),
)
_COARSEST = 250


def coarse_count(value: int) -> int:
    """Round ``value`` DOWN onto a coarse ladder for narration.

    Down rather than to-nearest so the figure never claims more history
    than there is -- "1,500 turns" at turn 1,480 is a small lie, and the
    call sites say "~" precisely because they are approximating already.

    >>> coarse_count(7), coarse_count(47), coarse_count(1487)
    (7, 45, 1400)
    """
    n = int(value)
    if n <= 0:
        return 0
    for ceiling, step in _STEPS:
        if n < ceiling:
            return (n // step) * step
    return (n // _COARSEST) * _COARSEST


# Bands for "how long has this stretch been going", narrated rather than
# counted. A turn counter here was the single most expensive byte in T1:
# ``arc_block`` renders on most turns and its count moved on all of them,
# so it broke the prefix for every block after it. Phrasing also drops a
# false precision -- the arc's start turn is a detector's guess, so
# "(last ~7 turns)" was reporting a rounded number about an estimate.
_ELAPSED_BANDS: tuple[tuple[int, str], ...] = (
    (3, "just started"),
    (6, "the last few turns"),
    (12, "the last several turns"),
    (25, "a good stretch now"),
)
_ELAPSED_LONGEST = "most of this conversation"


def coarse_elapsed_turns(elapsed: int) -> str:
    """Narrate a turn span as a phrase that changes at most four times."""
    n = max(0, int(elapsed))
    for ceiling, phrase in _ELAPSED_BANDS:
        if n < ceiling:
            return phrase
    return _ELAPSED_LONGEST


__all__ = ["coarse_count", "coarse_elapsed_turns"]
