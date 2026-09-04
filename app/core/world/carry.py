"""Pocketable-carrying vocabulary shared by the store, tools, and idle life.

``location_id IS NULL`` means Aiko is holding the item. Only a small set
of kinds may land there, and even those are capped — the room is not a
pocket. Seeds are the exception: they live in inventory until planted
and do not count against the cap.
"""
from __future__ import annotations

PORTABLE_KINDS: frozenset[str] = frozenset(
    {"food", "book", "toy", "keepsake", "seed"}
)
CARRY_CAP = 2

# Slug -> default home location slug, matching the rich-room seed. Used
# when an item has no ``home_location_id`` yet (fresh add, v41 backfill,
# or a carried stray).
SEED_HOME_SLUGS: dict[str, str] = {
    "dual_monitors": "desk",
    "retro_keyboard": "desk",
    "warm_lamp": "desk",
    "scifi_paperback": "bookshelf",
    "photo_of_user": "bookshelf",
    "plush_blanket": "bed",
    "cat_pillow": "bed",
    "cookie_jar": "kitchenette",
    "tea_pot": "kitchenette",
    "fairy_lights": "beanbag",
    "watering_can": "garden",
    "lavender_pot": "garden",
    "basil_seedling": "garden",
    "tomato_seedling": "garden",
    "seed_packet_sunflower": "garden",
}


def is_portable(kind: str) -> bool:
    return (kind or "").strip().lower() in PORTABLE_KINDS


def counts_toward_cap(kind: str) -> bool:
    """Non-seed pocketables occupy a carry slot."""
    k = (kind or "").strip().lower()
    return k in PORTABLE_KINDS and k != "seed"


__all__ = [
    "CARRY_CAP",
    "PORTABLE_KINDS",
    "SEED_HOME_SLUGS",
    "counts_toward_cap",
    "is_portable",
]
