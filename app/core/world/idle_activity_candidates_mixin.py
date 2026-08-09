"""What her room affords right now — the K36 beat candidate builder.

Split out of :class:`app.core.world.idle_activity_worker.
IdleAwayActivityWorker` when K91 needed the seam: episodes have to know
the *whole* set of beats the room can currently support so a chain can
pick a plausible continuation, not just the one beat that got drawn.

Every candidate is grounded in a real row: no book on the shelf means no
reading beat, an empty tea pot means no pouring one. Two beats always
survive an empty room (``doodle`` and ``wander``) so she is never left
with nothing to have done.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.core.world import beat_detail
from app.core.world.idle_activity_plan import (
    EFFECT_ADVANCE_BOOK,
    EFFECT_POUR_TEA,
    OUTING_BEATS,
    ActivityPlan,
    ItemEffect,
    RoomSnapshot,
    match_location,
)


log = logging.getLogger("app.idle_activity_worker")


class ActivityCandidatesMixin:
    """Builds the ``key -> ActivityPlan`` map for the current room."""

    def _build_candidates(
        self, user_name: str, now: datetime,
    ) -> RoomSnapshot:
        try:
            items = self._world_store.list_items()
        except Exception:
            items = []
        try:
            locations = self._world_store.list_locations()
        except Exception:
            locations = []

        candidates: dict[str, ActivityPlan] = {}

        # H13 — resolve the cozy spots once so each beat can actually move
        # Aiko there (not just change her posture at the desk).
        loc_kitchen = match_location(locations, "kitchenette", "kitchen")
        loc_bookshelf = match_location(locations, "bookshelf", "shelf")
        loc_beanbag = match_location(locations, "beanbag")
        loc_window = match_location(locations, "window")
        loc_desk = match_location(locations, "desk")
        loc_bed = match_location(locations, "bed")

        self._add_snack(candidates, items, loc_kitchen, self._read_period())
        self._add_tea(candidates, items, loc_kitchen)
        self._add_book(candidates, items, loc_beanbag, loc_bookshelf)
        self._add_pet(candidates, items, locations)

        # Window — look outside.
        window = match_location(locations, "window")
        if window is not None:
            candidates["look_outside"] = ActivityPlan(
                key="look_outside",
                posture="leaning",
                activity="looking_outside",
                summary="sat by the window for a bit, watching the world go by",
                aiko_location_id=window.id,
            )

        # Desk — tidy / tinker (almost always present).
        if loc_desk is not None:
            candidates["tidy_desk"] = ActivityPlan(
                key="tidy_desk",
                posture="sitting",
                activity="tinkering",
                summary="tidied up my desk and tinkered with a little project",
                aiko_location_id=loc_desk.id,
            )

        # Nap — only when there's a bed to do it in.
        if loc_bed is not None:
            candidates["nap"] = ActivityPlan(
                key="nap",
                posture="lying",
                activity="napping",
                summary="curled up for a little nap to recharge",
                aiko_location_id=loc_bed.id,
            )

        # Doodle — always available, no inventory needed.
        candidates["doodle"] = ActivityPlan(
            key="doodle",
            posture="sitting",
            activity="doodling",
            summary="doodled in my notebook for a while",
            aiko_location_id=(
                loc_beanbag.id if loc_beanbag
                else (loc_desk.id if loc_desk else None)
            ),
        )

        # H22 — light outing. Only offered when its own daylight + cooldown
        # + daily-cap gates pass (or it's MCP-forced), so it stays rare. No
        # location move — she's back home by the time the beat is journalled.
        outing_forced = (
            self._forced_activity_key == "outing" and self._outings_enabled()
        )
        if outing_forced or self._outing_eligible(now):
            candidates["outing"] = ActivityPlan(
                key="outing",
                posture="sitting",
                activity="idle",
                summary=self._rng.choice(OUTING_BEATS),
            )

        # Fallback — let her thoughts wander. Always available.
        candidates["wander"] = ActivityPlan(
            key="wander",
            posture="curled_up",
            activity="thinking",
            summary=(
                "mostly let my thoughts wander — kept thinking about "
                + user_name
            ),
            aiko_location_id=(
                loc_window.id if loc_window
                else (loc_beanbag.id if loc_beanbag else None)
            ),
        )

        return RoomSnapshot(
            items=items, locations=locations, candidates=candidates,
        )

    # ── individual inventory-backed beats ────────────────────────────

    def _add_snack(
        self,
        candidates: dict[str, ActivityPlan],
        items: list[Any],
        loc_kitchen: Any,
        period: str,
    ) -> None:
        """Eat something the user (or a harvest) left in the kitchenette.

        K91 — what she reaches for and how she describes it both follow
        the hour: breakfast and dinner lean on what the garden gave her,
        a 2 a.m. raid leans on the biscuit tin.
        """
        food = beat_detail.pick_food(items, period=period)
        if food is None:
            return
        candidates["snack"] = ActivityPlan(
            key="snack",
            posture="sitting",
            activity="eating" if period in ("morning", "midday", "evening")
            else "snacking",
            summary=beat_detail.snack_summary(food, period=period),
            consume_item_id=food.id,
            aiko_location_id=loc_kitchen.id if loc_kitchen else None,
        )

    def _add_tea(
        self,
        candidates: dict[str, ActivityPlan],
        items: list[Any],
        loc_kitchen: Any,
    ) -> None:
        """Pour from the pot — but only while there's tea left in it.

        The pot is a gadget, so the food-based snack beat never sees it,
        and an empty pot yields no candidate rather than a cup she
        couldn't have poured.
        """
        pot = next(
            (
                i
                for i in items
                if getattr(i, "slug", "") == "tea_pot"
                or "tea pot" in (getattr(i, "name", "") or "").lower()
            ),
            None,
        )
        if pot is None:
            return
        tea_line = beat_detail.tea_summary(pot)
        if not tea_line:
            return
        candidates["tea"] = ActivityPlan(
            key="tea",
            posture="standing",
            activity="making_tea",
            summary=tea_line,
            aiko_location_id=loc_kitchen.id if loc_kitchen else None,
            item_effect=ItemEffect(item_id=pot.id, action=EFFECT_POUR_TEA),
        )

    def _add_book(
        self,
        candidates: dict[str, ActivityPlan],
        items: list[Any],
        loc_beanbag: Any,
        loc_bookshelf: Any,
    ) -> None:
        """Curl up with whatever she's part-way through."""
        book = next(
            (
                i
                for i in items
                if getattr(i, "kind", "") == "book"
                or "book" in (getattr(i, "name", "") or "").lower()
            ),
            None,
        )
        if book is None:
            return
        candidates["read_book"] = ActivityPlan(
            key="read_book",
            posture="curled_up",
            activity="reading",
            summary=beat_detail.read_book_summary(book),
            aiko_location_id=(
                loc_beanbag.id if loc_beanbag
                else (loc_bookshelf.id if loc_bookshelf else None)
            ),
            item_effect=ItemEffect(
                item_id=book.id, action=EFFECT_ADVANCE_BOOK,
            ),
        )

    def _add_pet(
        self,
        candidates: dict[str, ActivityPlan],
        items: list[Any],
        locations: list[Any],
    ) -> None:
        """Wander the cat to another spot for company."""
        pet = next(
            (
                i
                for i in items
                if getattr(i, "kind", "") in ("pet", "animal")
                or "cat" in (getattr(i, "name", "") or "").lower()
            ),
            None,
        )
        if pet is None or not locations:
            return
        other = [
            loc for loc in locations
            if loc.id != getattr(pet, "location_id", None)
        ]
        target = self._rng.choice(other) if other else None
        candidates["move_cat"] = ActivityPlan(
            key="move_cat",
            posture="sitting",
            activity="idle",
            summary=pet.name + " curled up next to me and kept me company",
            move_item_id=pet.id,
            move_to_location_id=target.id if target is not None else None,
        )


__all__ = ["ActivityCandidatesMixin"]
