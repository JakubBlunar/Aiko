"""H5 — named places Aiko can be, besides the single seeded apartment.

Her apartment (with the garden as a location inside it) is the builtin
home scene and stays locked: the user can still put objects in it, but
cannot delete the scene or its seeded spots. Extra scenes are
user-authored — typically *his* room — so she can visit and behave as if
she is actually there.

Items stay where they were placed. Carried items (``location_id IS NULL``)
travel with her. ``move_to`` only walks spots inside the current scene;
``go_to_scene`` is the travel verb.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


APARTMENT_SLUG = "apartment"
APARTMENT_NAME = "Aiko's apartment"
APARTMENT_DESCRIPTION = (
    "Her cozy virtual apartment — books, gadgets, glowing screens, "
    "and the garden just outside."
)

ORIGIN_BUILTIN = "builtin"
ORIGIN_CUSTOM = "custom"


def slugify_scene(text: str) -> str:
    cleaned = (text or "").strip().lower()
    out: list[str] = []
    last_underscore = False
    for ch in cleaned:
        if ch.isalnum():
            out.append(ch)
            last_underscore = False
        elif not last_underscore and out:
            out.append("_")
            last_underscore = True
    while out and out[-1] == "_":
        out.pop()
    return "".join(out) or "scene"

# Seeded spots in the home scene. The user can still add items here;
# they cannot rename or delete these rows.
BUILTIN_LOCATION_SLUGS: frozenset[str] = frozenset(
    {
        "bed",
        "desk",
        "bookshelf",
        "kitchenette",
        "window_seat",
        "beanbag",
        "mirror_corner",
        "garden",
    }
)


@dataclass(slots=True)
class Scene:
    id: int
    slug: str
    name: str
    description: str
    origin: str
    created_at: str
    updated_at: str

    @property
    def locked(self) -> bool:
        return self.origin == ORIGIN_BUILTIN

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "origin": self.origin,
            "locked": self.locked,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
