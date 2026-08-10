"""K91 pass 3 — chain idle beats into one episode.

A beat used to be atomic: one activity per firing, one journal line, a
cooldown, repeat. Read back, a long absence was a stack of unrelated
postcards — at 11:44 she took her tea out to the garden, at 11:51 she was
indoors dusting a keyboard, tea abandoned. Nothing said that what she did
at noon had anything to do with what she did at 11:58.

This module is the pure planner for episodes: given the beat she starts
with and the beats her room can currently afford, it proposes a short
plausible chain and joins their clauses into the one sentence a person
would actually say. "I stretched in the garden and then went round with
the watering can" is a single memory, not two.

Chains are deliberately short and physically sensible. The successor
table encodes *continuation*, not variety: making tea leads to settling
down with it, a nap ends an episode because waking up is a new one.
"""
from __future__ import annotations

import random


# Beats that plausibly follow one another in the same stretch of time.
# Read as "having just done KEY, she might then ...". An empty tuple
# means the beat closes an episode: nothing naturally continues from
# falling asleep, and an open-vocab LLM beat can't be reasoned about.
SUCCESSORS: dict[str, tuple[str, ...]] = {
    "tea": ("read_book", "look_outside", "doodle", "move_cat"),
    "snack": ("read_book", "look_outside", "move_cat", "tidy_desk"),
    "read_book": ("tea", "look_outside", "nap"),
    "look_outside": ("tea", "doodle", "wander", "read_book"),
    "tidy_desk": ("tea", "doodle", "snack"),
    "doodle": ("tea", "look_outside", "nap"),
    "move_cat": ("read_book", "doodle", "nap"),
    "wander": ("look_outside", "read_book", "tea", "nap"),
    "outing": ("tea", "snack", "look_outside"),
    "nap": (),
    "llm": (),
}

# Chance of a three-beat episode once chaining is already happening; the
# rest are pairs. Long chains read as a montage rather than an afternoon.
_THIRD_BEAT_CHANCE = 0.3


def should_chain(
    *,
    seconds_since_last_beat: float | None,
    min_gap_seconds: float,
    ratio: float,
    rng: random.Random,
) -> bool:
    """Whether this firing should become an episode rather than one beat.

    The gate is how long she has been left to her own devices: a beat
    following hard on the last one is part of an already-busy day, while
    a long uninterrupted stretch is when a connected sequence is both
    plausible and worth telling. A first-ever beat counts as a long gap.
    """
    if ratio <= 0.0:
        return False
    if seconds_since_last_beat is not None and min_gap_seconds > 0:
        if seconds_since_last_beat < min_gap_seconds:
            return False
    return rng.random() < ratio


def pick_length(*, rng: random.Random, max_beats: int) -> int:
    """How many beats this episode should contain (at least 2)."""
    if max_beats <= 2:
        return max(1, max_beats)
    if rng.random() < _THIRD_BEAT_CHANCE:
        return 3
    return 2


def plan_chain(
    first_key: str,
    available: list[str],
    *,
    rng: random.Random,
    length: int,
) -> list[str]:
    """Extend ``first_key`` into a chain of at most ``length`` beats.

    ``first_key`` always leads, whether or not it appears in
    ``available`` -- the beat has already been chosen by the time this
    runs, and a forced or LLM-composed one legitimately has no entry in
    the candidate pool. Every *subsequent* key is drawn from
    ``available``, never repeats one, and the chain stops as soon as a
    beat has no eligible successor -- so a room that affords nothing to
    continue with simply yields a single beat.
    """
    chain = [first_key]
    if length <= 1:
        return chain
    pool = set(available)
    while len(chain) < length:
        options = [
            key
            for key in SUCCESSORS.get(chain[-1], ())
            if key in pool and key not in chain
        ]
        if not options:
            break
        chain.append(rng.choice(options))
    return chain


def join_clauses(clauses: list[str]) -> str:
    """Join beat clauses into the one sentence she'd actually say."""
    parts = [c.strip().rstrip(".") for c in clauses if c and c.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] + ", then " + parts[1]
    return ", then ".join(parts[:-1]) + ", and later " + parts[-1]


__all__ = [
    "SUCCESSORS",
    "should_chain",
    "pick_length",
    "plan_chain",
    "join_clauses",
]
