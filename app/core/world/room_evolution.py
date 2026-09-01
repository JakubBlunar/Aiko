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


# Seed / leftover name that is a genre, not a title. Treated as untitled
# so a live room never spends a whole book called "sci-fi paperback".
GENERIC_BOOK_TITLE = "sci-fi paperback"


def is_generic_book_title(title: str | None) -> bool:
    """True when ``title`` is missing or the seeded genre placeholder."""
    cleaned = str(title or "").strip().lower()
    if not cleaned:
        return True
    return cleaned == GENERIC_BOOK_TITLE


def _norm_book(state: dict | None) -> tuple[str, str, int, int]:
    state = state or {}
    raw = str(state.get("title") or "").strip()
    title = "" if is_generic_book_title(raw) else raw
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


def _book_state(
    title: str,
    blurb: str,
    progress: int,
    total: int,
    *,
    status: str = "reading",
) -> dict:
    return {
        "title": title,
        "blurb": blurb,
        "progress": max(0, progress),
        "total": max(1, total),
        "status": status,
    }


def pick_book_title(
    rng: random.Random, *, exclude: str = "",
) -> tuple[str, str]:
    """Pick ``(title, blurb)`` from :data:`BOOK_TITLES`, avoiding ``exclude``."""
    skip = str(exclude or "").strip()
    pool = [b for b in BOOK_TITLES if b[0] != skip] or list(BOOK_TITLES)
    return rng.choice(pool)


def ensure_book_titled(
    state: dict | None, rng: random.Random, *, exclude: str = "",
) -> dict:
    """Guarantee ``state`` has a real :data:`BOOK_TITLES` title.

    Progress and total are preserved. A missing or generic title
    (``sci-fi paperback``) is replaced; an already-named book is
    returned unchanged. Used to heal live rooms that were seeded
    before titles existed, without resetting the chapter count.
    """
    title, blurb, progress, total = _norm_book(state)
    if title:
        return _book_state(title, blurb, progress, total)
    new_title, new_blurb = pick_book_title(rng, exclude=exclude)
    return _book_state(new_title, new_blurb, progress, total)


def stamp_book_title(
    state: dict | None,
    title: str,
    blurb: str,
    rng: random.Random,
    *,
    reset_progress: bool,
) -> dict:
    """Write a (possibly invented) title onto the paperback.

    Returning to the same title keeps the chapter count. A *new*
    title starts at progress 0 with a fresh length.
    """
    named = str(title or "").strip()
    detail = str(blurb or "").strip()
    old_title, old_blurb, progress, total = _norm_book(state)
    if not named:
        return ensure_book_titled(state, rng)
    if old_title and old_title.lower() == named.lower() and not reset_progress:
        return _book_state(named, detail or old_blurb, progress, total)
    return _book_state(named, detail, 0, rng.randint(10, 16))


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

    Untitled / generic books are titled first (progress kept) so a live
    install never advances a paperback whose name is still the genre.
    """
    titled = ensure_book_titled(state, rng)
    title, blurb, progress, total = _norm_book(titled)
    progress += 1
    if progress < total:
        new = _book_state(title, blurb, progress, total)
        desc = blurb or f"a paperback ({progress}/{total} chapters in)"
        return new, title, desc, None
    # finished → pick a new book (avoid the one she just read)
    new_title, new_blurb = pick_book_title(rng, exclude=title)
    new = _book_state(new_title, new_blurb, 0, rng.randint(10, 16))
    return new, new_title, new_blurb, title


__all__ = [
    "TEA_POT_SLUG",
    "COOKIE_JAR_SLUG",
    "BOOK_SLUG",
    "GENERIC_BOOK_TITLE",
    "TEA_FLAVORS",
    "COOKIE_FLAVORS",
    "BOOK_TITLES",
    "is_generic_book_title",
    "next_tea",
    "fresh_cookie_batch",
    "pick_book_title",
    "ensure_book_titled",
    "stamp_book_title",
    "advance_book",
]
