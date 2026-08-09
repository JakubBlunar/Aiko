"""Shared vocabulary for K36 idle beats.

Split out of :mod:`app.core.world.idle_activity_worker` so the worker and
its candidate-building mixin can both depend on the plan types without
importing each other. Nothing here does I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# K91 — the effects a beat may have on the item it acted on. Deliberately
# a closed set: the transition math is H20's (``room_evolution``) or the
# store's (``water_plant``), so a beat can only ever move an item along a
# path the room already understands.
EFFECT_ADVANCE_BOOK = "advance_book"
EFFECT_POUR_TEA = "pour_tea"
EFFECT_WATER_PLANT = "water_plant"
VALID_EFFECTS: frozenset[str] = frozenset(
    {EFFECT_ADVANCE_BOOK, EFFECT_POUR_TEA, EFFECT_WATER_PLANT}
)

# Periods during which stepping out reads as natural (no 3 a.m. strolls).
OUTING_DAYLIGHT_PERIODS: frozenset[str] = frozenset(
    {"early_morning", "morning", "midday", "afternoon", "evening"}
)

# Varied past-tense outing flavours. She's back by the time the beat is
# journalled, so each is narrated as a completed short trip. v0 of H5 —
# no scene_id, no item relocation; the trace lives in the journal + the
# H17 seed path (a small detail she brought home).
OUTING_BEATS: tuple[str, ...] = (
    "popped out for a short walk — the air was lovely",
    "stepped out and grabbed a coffee from the place downstairs",
    "took a quick stroll around the block to stretch my legs",
    "nipped out for some fresh air and watched the street for a bit",
    "wandered out to the little shop on the corner and back",
)


@dataclass(frozen=True, slots=True)
class ItemEffect:
    """A state change a beat leaves behind on one item.

    Declarative on purpose: the plan names the item and the transition,
    and the worker resolves it against the live row at apply time. That
    keeps candidate building free of writes and means a stale candidate
    can never write stale state.
    """

    item_id: int
    action: str


@dataclass(frozen=True)
class ActivityPlan:
    """One chosen idle beat + the world mutation it implies."""

    key: str
    posture: str
    activity: str
    summary: str
    consume_item_id: int | None = None
    move_item_id: int | None = None
    move_to_location_id: int | None = None
    # H13 — where Aiko herself relocates to for this beat (None = stay put).
    aiko_location_id: int | None = None
    # H14 — set when the worker LLM already composed a final summary, so
    # run() skips the rephrase pass.
    precomposed: bool = False
    # K91 — the state this beat writes back, so what she says she did and
    # what her room shows stop drifting apart.
    item_effect: ItemEffect | None = None


@dataclass(frozen=True, slots=True)
class RoomSnapshot:
    """The room as one candidate-building pass saw it."""

    items: list[Any]
    locations: list[Any]
    candidates: dict[str, ActivityPlan]


def match_location(locations: list[Any], *keywords: str) -> Any | None:
    """First location whose slug/name contains any keyword (slug wins).

    Used by H13 to resolve a beat's cozy spot from whatever the room
    actually has, tolerant of renamed/removed locations.
    """
    if not locations:
        return None
    kws = [k.lower() for k in keywords if k]
    # Prefer an exact slug match, then substring on slug, then on name.
    for loc in locations:
        if (getattr(loc, "slug", "") or "").lower() in kws:
            return loc
    for loc in locations:
        slug = (getattr(loc, "slug", "") or "").lower()
        if any(k in slug for k in kws):
            return loc
    for loc in locations:
        name = (getattr(loc, "name", "") or "").lower()
        if any(k in name for k in kws):
            return loc
    return None


__all__ = [
    "EFFECT_ADVANCE_BOOK",
    "EFFECT_POUR_TEA",
    "EFFECT_WATER_PLANT",
    "VALID_EFFECTS",
    "OUTING_BEATS",
    "OUTING_DAYLIGHT_PERIODS",
    "ActivityPlan",
    "ItemEffect",
    "RoomSnapshot",
    "match_location",
]
