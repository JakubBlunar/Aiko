"""H20 — a room that evolves (pure transition helpers).

The seeded room (``world_store._SEED_ITEMS``) is static: the tea pot is
forever "half full of jasmine", the cookies decrement but never refill, the
sci-fi paperback is eternally "dog-eared at the climax". H20 lets a slow
background pass quietly drift these so the space accrues a history — the pot
empties and she brews a fresh flavour, she *finishes* the book and starts a
new one (a great H17 seed), the cookie jar gets refilled.

This module owns the deterministic transition math on item ``state`` dicts
so it's trivially testable; :class:`app.core.world.room_evolution_worker.
RoomEvolutionWorker` applies the results to the live ``WorldStore``.
"""
from __future__ import annotations

import random


# Slugs of the seeded items H20 evolves. Kept here so the worker and the
# tests share one source of truth.
TEA_POT_SLUG = "tea_pot"
COOKIE_JAR_SLUG = "cookie_jar"
BOOK_SLUG = "scifi_paperback"


TEA_FLAVORS: tuple[str, ...] = (
    "jasmine", "genmaicha", "earl grey", "peppermint", "oolong",
    "chamomile", "matcha", "hojicha", "lapsang souchong", "rooibos",
)

COOKIE_FLAVORS: tuple[str, ...] = (
    "chocolate chip", "oatmeal raisin", "double chocolate", "ginger snap",
    "shortbread", "peanut butter", "white chocolate macadamia",
)

# (title, one-line blurb). The blurb rides in the item state + description
# so the World tab + inspect tool show what she's currently reading.
BOOK_TITLES: tuple[tuple[str, str], ...] = (
    ("The Quantum Garden", "a slow-burn sci-fi about a derelict generation ship"),
    ("Salt and Static", "a near-future story about a radio operator at the world's edge"),
    ("The Cartographer's Lament", "a fantasy about a mapmaker who can't find her way home"),
    ("Tin Hearts", "a cosy mystery set in a clockmaker's village"),
    ("Nightfall in Aria", "a space-opera with a reluctant, sarcastic pilot"),
    ("The Glasshouse Letters", "an epistolary novel about two botanists and a war"),
    ("Eleven Doors", "a twisty thriller where every chapter is a different room"),
)


_TEA_DESC = {
    "full": "a small ceramic pot, full of fresh {flavor} tea",
    "half": "a small ceramic pot, half full of {flavor} tea",
    "empty": "a small ceramic pot, empty and waiting to be refilled",
}


def _norm_tea(state: dict | None) -> tuple[str, str]:
    state = state or {}
    fullness = str(state.get("fullness") or "full").lower()
    if fullness not in ("full", "half", "empty"):
        fullness = "full"
    flavor = str(state.get("flavor") or "jasmine")
    return fullness, flavor


def next_tea(
    state: dict | None, rng: random.Random,
) -> tuple[dict, str, str | None]:
    """Step the tea pot one level. full → half → empty → (brew fresh) → full.

    Returns ``(new_state, new_description, event_label)`` where
    ``event_label`` is non-None only on the "brewed a fresh pot" wrap (a
    candidate H17 seed). ``new_description`` keeps the visible row in sync.
    """
    fullness, flavor = _norm_tea(state)
    if fullness == "full":
        new = {"fullness": "half", "flavor": flavor}
        return new, _TEA_DESC["half"].format(flavor=flavor), None
    if fullness == "half":
        new = {"fullness": "empty", "flavor": flavor}
        return new, _TEA_DESC["empty"], None
    # empty → brew a fresh pot with a new flavour
    pool = [f for f in TEA_FLAVORS if f != flavor] or list(TEA_FLAVORS)
    new_flavor = rng.choice(pool)
    new = {"fullness": "full", "flavor": new_flavor}
    return (
        new,
        _TEA_DESC["full"].format(flavor=new_flavor),
        f"brewed a fresh pot of {new_flavor} tea",
    )


def fresh_cookie_batch(
    prev_flavor: str | None, rng: random.Random,
) -> tuple[str, dict]:
    """Pick a fresh cookie flavour (avoiding the previous one).

    Returns ``(description, state)`` for the refilled jar.
    """
    pool = [f for f in COOKIE_FLAVORS if f != prev_flavor] or list(COOKIE_FLAVORS)
    flavor = rng.choice(pool)
    desc = f"warm, {flavor} cookies in a glass jar"
    return desc, {"flavor": flavor, "freshness": "fresh"}


def _norm_book(state: dict | None) -> tuple[str, str, int, int]:
    state = state or {}
    title = str(state.get("title") or "sci-fi paperback")
    blurb = str(state.get("blurb") or "")
    try:
        progress = int(state.get("progress", 0))
    except (TypeError, ValueError):
        progress = 0
    try:
        total = int(state.get("total", 12))
    except (TypeError, ValueError):
        total = 12
    return title, blurb, max(0, progress), max(1, total)


def advance_book(
    state: dict | None, rng: random.Random,
) -> tuple[dict, str, str | None, str | None]:
    """Read one more chapter. On finishing, start a fresh book.

    Returns ``(new_state, new_name, new_description, finished_title)``:
    - mid-book: ``new_state`` carries the bumped progress, ``new_name`` is
      the current title, ``finished_title`` is ``None``.
    - on finish: ``new_state`` is a fresh book at progress 0, ``new_name`` /
      ``new_description`` describe it, and ``finished_title`` is the book she
      just completed (the H17 seed material).
    """
    title, blurb, progress, total = _norm_book(state)
    progress += 1
    if progress < total:
        new = {
            "title": title, "blurb": blurb,
            "progress": progress, "total": total, "status": "reading",
        }
        desc = blurb or f"a paperback ({progress}/{total} chapters in)"
        return new, title, desc, None
    # finished → pick a new book (avoid the one she just read)
    pool = [b for b in BOOK_TITLES if b[0] != title] or list(BOOK_TITLES)
    new_title, new_blurb = rng.choice(pool)
    new = {
        "title": new_title, "blurb": new_blurb,
        "progress": 0, "total": rng.randint(10, 16), "status": "reading",
    }
    return new, new_title, new_blurb, title


__all__ = [
    "TEA_POT_SLUG",
    "COOKIE_JAR_SLUG",
    "BOOK_SLUG",
    "TEA_FLAVORS",
    "COOKIE_FLAVORS",
    "BOOK_TITLES",
    "next_tea",
    "fresh_cookie_batch",
    "advance_book",
]
