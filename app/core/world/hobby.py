"""H19 — Aiko's current hobby / ongoing personal project (pure helpers).

A *hobby* is a multi-day thread Aiko returns to in her idle time: working
through a book series, teaching herself guitar, an astronomy phase, filling
a sketchbook. Unlike a one-off away-beat (H13/H14) it has **continuity of
intent** — it progresses across days, forms small opinions she can voice,
and makes the gaps between sessions feel used.

This module owns the catalogue + the deterministic progress / milestone /
rotation math so it stays trivially testable. The mutable state lives in a
single ``kv_meta`` JSON blob managed by
:class:`app.core.proactive.hobby_worker.HobbyWorker`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class HobbyTemplate:
    """One hobby Aiko can pick up. Pure data — no state."""

    key: str
    label: str           # "working through a sci-fi series"
    kind: str            # reading | making | learning | tending | collecting
    unit: str            # progress unit: "chapter", "session", "sketch"
    progress_verb: str   # past-tense advance: "read another chapter of ..."
    takeaway_hint: str   # what the worker LLM riffs on for a milestone seed


# The catalogue is intentionally small + evocative; the worker rotates
# through it so Aiko isn't stuck on one thread forever. Open-vocab labels
# (free text) are fine downstream — these are just the seeds.
HOBBY_CATALOGUE: tuple[HobbyTemplate, ...] = (
    HobbyTemplate(
        "scifi_series", "working through a sci-fi series", "reading",
        "chapter", "read another chapter of the series",
        "a twist or character in the series",
    ),
    HobbyTemplate(
        "guitar", "teaching yourself guitar", "learning",
        "session", "practiced for a bit",
        "how the chord changes are starting to click (or not)",
    ),
    HobbyTemplate(
        "astronomy", "in an astronomy phase", "learning",
        "night", "read up on another corner of the night sky",
        "something you learned about stars or planets",
    ),
    HobbyTemplate(
        "sketchbook", "filling a sketchbook", "making",
        "sketch", "added another sketch",
        "what you drew and whether it came out right",
    ),
    HobbyTemplate(
        "baking", "working through a baking book", "making",
        "recipe", "tried another recipe",
        "how the bake turned out",
    ),
    HobbyTemplate(
        "houseplants", "nursing the windowsill plants", "tending",
        "check", "fussed over the plants",
        "a tiny new leaf or one that's struggling",
    ),
    HobbyTemplate(
        "language", "picking up a new language", "learning",
        "lesson", "did another lesson",
        "a word that delighted or completely confused you",
    ),
    HobbyTemplate(
        "vinyl", "digging through a stack of old records", "collecting",
        "record", "listened through another record",
        "an album that surprised you",
    ),
)


def template_for(key: str) -> HobbyTemplate | None:
    """Return the catalogue entry for ``key`` (or ``None``)."""
    return next((h for h in HOBBY_CATALOGUE if h.key == key), None)


def pick_hobby(
    rng: random.Random, *, exclude: tuple[str, ...] = (),
) -> HobbyTemplate:
    """Pick a hobby, avoiding ``exclude`` keys when possible."""
    excl = set(exclude)
    pool = [h for h in HOBBY_CATALOGUE if h.key not in excl]
    if not pool:
        pool = list(HOBBY_CATALOGUE)
    return rng.choice(pool)


def render_hobby_line(label: str, progress: int, unit: str) -> str:
    """Render the standing "what she's been up to" phrase.

    ``"working through a sci-fi series (5 chapters in)"``. Progress 0 reads
    as "just started".
    """
    label = (label or "").strip() or "a little project"
    if progress <= 0:
        return f"{label} (just started)"
    unit = (unit or "step").strip() or "step"
    plural = unit if progress == 1 else unit + "s"
    return f"{label} ({progress} {plural} in)"


def should_rotate(
    *, progress: int, advances: int, max_advances: int,
) -> bool:
    """Whether the current hobby has run long enough to rotate out.

    ``max_advances <= 0`` disables rotation (she stays on it forever).
    """
    if max_advances <= 0:
        return False
    return advances >= max_advances


def is_milestone(*, advances: int, every: int) -> bool:
    """Whether this advance count is a milestone (worth a takeaway seed).

    ``every <= 0`` disables milestones. The first advance is never a
    milestone (``advances`` starts at 1); milestones land on multiples of
    ``every``.
    """
    if every <= 0:
        return False
    return advances > 0 and advances % every == 0


__all__ = [
    "HobbyTemplate",
    "HOBBY_CATALOGUE",
    "template_for",
    "pick_hobby",
    "render_hobby_line",
    "should_rotate",
    "is_milestone",
]
