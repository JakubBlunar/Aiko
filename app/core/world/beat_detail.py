"""K91 pass 1 — narrate an idle beat from the state it acted on.

Her room has always carried real per-item state in ``Item.state``: the
paperback tracks ``progress``/``total``, the tea pot tracks ``fullness``
and ``flavor``, every plant tracks ``stage`` and ``last_watered_at``. The
away-beat narrator ignored all of it and templated from the item's *name*
alone, so she could "curl up with The Glasshouse Letters" twice in a day
while the book sat at chapter three of sixteen, and water a garden where
both plants had just come into flower without mentioning either.

This module is the pure half of the fix: given a duck-typed item (any
object exposing ``name`` / ``kind`` / ``state`` / ``quantity``), it
produces the first-person past-tense clause the beat should journal, plus
the short parentheticals the H14 whole-beat prompt needs so the worker
model can see state instead of guessing from names.

Everything here is deterministic and total: garbage, missing and
wrong-typed state all degrade to the same clause the templates produced
before, because a missing key must never cost her a beat.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# Mirrors ``world_store._DRY_TOLERANCE_HOURS`` — the point at which a
# plant's growth stalls. Duplicated rather than imported to keep this
# module free of store imports (the worker already avoids that cycle).
DRY_TOLERANCE_DAYS = 4.0

# Below this, a plant isn't worth singling out as thirsty.
NOTICEABLE_DRY_DAYS = 1.5

# Small cardinals read better than digits in a clause she'll paraphrase.
_CARDINALS: tuple[str, ...] = (
    "no", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)

# Stages worth remarking on when she's in the garden anyway.
_STAGE_NOTE: dict[str, str] = {
    "flowering": "in flower",
    "mature": "ready to pick",
}


def _count(value: int) -> str:
    if 0 <= value < len(_CARDINALS):
        return _CARDINALS[value]
    return str(value)


def _state_of(item: Any) -> dict[str, Any]:
    state = getattr(item, "state", None)
    return state if isinstance(state, dict) else {}


def _int_of(state: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(state.get(key, default))
    except (TypeError, ValueError):
        return default


def _text_of(item: Any, attr: str) -> str:
    return str(getattr(item, attr, "") or "").strip()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ── reading ─────────────────────────────────────────────────────────────


def read_book_summary(item: Any) -> str:
    """Clause for the ``read_book`` beat, citing where she actually is.

    The point of difference from the old template is that two reading
    beats in a row now read as two *different* evenings, because the
    chapter count moved between them (pass 2 is what moves it).
    """
    name = _text_of(item, "name") or "my book"
    state = _state_of(item)
    title = str(state.get("title") or "").strip() or name
    total = _int_of(state, "total", 0)
    if total <= 0:
        return "curled up with " + title + " and read for a while"
    progress = max(0, _int_of(state, "progress", 0))
    if progress <= 0:
        return "curled up with " + title + " and started the first chapter"
    remaining = total - progress
    if remaining <= 0:
        return "finished the last few pages of " + title
    if remaining == 1:
        return "curled up with " + title + " — one chapter left now"
    return (
        "curled up with "
        + title
        + " for a while, "
        + _count(progress)
        + " chapters in now"
    )


# ── tea ─────────────────────────────────────────────────────────────────


def tea_summary(item: Any) -> str | None:
    """Clause for pouring from the pot, or ``None`` when it's empty.

    An empty pot is not a beat: returning ``None`` lets the caller drop
    the candidate rather than narrate a cup she couldn't have poured.
    """
    state = _state_of(item)
    fullness = str(state.get("fullness") or "full").strip().lower()
    flavor = str(state.get("flavor") or "").strip()
    if fullness == "empty":
        return None
    what = (flavor + " tea") if flavor else "tea"
    if fullness == "half":
        return "poured what was left of the " + what + " and drank it slowly"
    return "poured myself a cup of the " + what + " while it was still hot"


# ── snacking ────────────────────────────────────────────────────────────


# K91 — eating had one shape at every hour ("had some of the X"), so
# breakfast, lunch and a 2 a.m. raid on the biscuits were the same beat.
# Meals want garden produce; a late-night pick wants a treat.
_MEALS: dict[str, tuple[str, str]] = {
    "early_morning": ("breakfast", "produce"),
    "morning": ("breakfast", "produce"),
    "midday": ("lunch", "produce"),
    "afternoon": ("", "treat"),
    "evening": ("dinner", "produce"),
    "night": ("", "treat"),
    "late_night": ("midnight snack", "treat"),
}

# Slug fragments that mark a food as garden produce rather than a treat.
_PRODUCE_HINTS: tuple[str, ...] = (
    "tomato", "basil", "lettuce", "mint", "strawberr", "chili", "rosemary",
    "onion", "radish", "pea_pod", "lavender", "sunflower", "harvest",
)


def _is_produce(item: Any) -> bool:
    haystack = (
        str(getattr(item, "slug", "") or "")
        + " "
        + str(getattr(item, "name", "") or "")
    ).lower()
    if _state_of(item).get("species"):
        return True
    return any(hint in haystack for hint in _PRODUCE_HINTS)


def pick_food(items: list[Any], *, period: str = "") -> Any | None:
    """The food she'd plausibly reach for at this hour.

    Meals prefer what the garden gave her, late-night raids prefer the
    biscuit tin. Falls back to anything edible so an odd stock never
    costs her the beat.
    """
    edible = [
        i
        for i in items
        if getattr(i, "consumable", False)
        and getattr(i, "quantity", 0) > 0
        and getattr(i, "kind", "") == "food"
    ]
    if not edible:
        return None
    _label, want = _MEALS.get((period or "").strip(), ("", ""))
    if want == "produce":
        preferred = [i for i in edible if _is_produce(i)]
    elif want == "treat":
        preferred = [i for i in edible if not _is_produce(i)]
    else:
        preferred = []
    return (preferred or edible)[0]


def snack_summary(item: Any, *, period: str = "") -> str:
    """Clause for the ``snack`` beat, shaped by the hour and the stock."""
    name = _text_of(item, "name") or "a snack"
    state = _state_of(item)
    flavor = str(state.get("flavor") or "").strip()
    try:
        quantity = int(getattr(item, "quantity", 0) or 0)
    except (TypeError, ValueError):
        quantity = 0
    what = (flavor + " " + name) if flavor and flavor not in name else name
    if quantity == 1:
        return "ate the last of the " + what + ", which feels a bit tragic"
    if str(state.get("freshness") or "").strip().lower() == "stale":
        return "had some of the " + what + " — going a little stale, honestly"

    meal, _want = _MEALS.get((period or "").strip(), ("", ""))
    if meal == "midnight snack":
        return "crept out for a midnight snack — the " + what + ", obviously"
    if meal:
        return "made myself " + meal + " with the " + what
    return "had some of the " + what + " and enjoyed the quiet for a bit"


# ── plants ──────────────────────────────────────────────────────────────


def dryness_days(item: Any, *, now: datetime) -> float:
    """Days since this plant was last watered (0.0 when unknown)."""
    state = _state_of(item)
    watered = _parse_iso(state.get("last_watered_at"))
    if watered is None:
        try:
            return max(0.0, float(state.get("days_dry", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0
    return max(0.0, (now - watered).total_seconds() / 86400.0)


def thirstiest_plant(items: list[Any], *, now: datetime) -> Any | None:
    """The plant most in need of water, or ``None`` when none stands out.

    "One lettuce really needed water" only works if somebody looked for
    the driest pot instead of watering the set anonymously.
    """
    plants = [i for i in items if str(getattr(i, "kind", "")) == "plant"]
    if not plants:
        return None
    ranked = sorted(plants, key=lambda i: dryness_days(i, now=now), reverse=True)
    driest = ranked[0]
    if dryness_days(driest, now=now) < NOTICEABLE_DRY_DAYS:
        return None
    return driest


def plant_note(name: str, dry_days: float, stage: str = "") -> str | None:
    """Short note about one plant: thirst first, then a stage worth telling.

    Primitive arguments so the garden worker can call it with the dryness
    it measured *before* watering — once the can has been round, the
    evidence that anything was thirsty is gone.
    """
    label = (name or "").strip() or "the plant"
    if dry_days >= DRY_TOLERANCE_DAYS:
        return label + " was bone dry and looking sorry for itself"
    if dry_days >= NOTICEABLE_DRY_DAYS:
        return label + " really needed the water"
    note = _STAGE_NOTE.get((stage or "").strip().lower())
    if note:
        return label + " is " + note
    return None


def plant_state_note(item: Any, *, now: datetime) -> str | None:
    """:func:`plant_note` for a live item, measuring its dryness now."""
    return plant_note(
        _text_of(item, "name"),
        dryness_days(item, now=now),
        str(_state_of(item).get("stage") or ""),
    )


def garden_tend_summary(
    plants: list[Any],
    *,
    now: datetime,
    harvested: list[str] | None = None,
) -> str:
    """Clause for a garden round that names what actually needed doing."""
    harvested = [h for h in (harvested or []) if h]
    parts: list[str] = []
    thirsty = thirstiest_plant(plants, now=now)
    if thirsty is not None:
        note = plant_state_note(thirsty, now=now)
        round_clause = "went round with the watering can"
        if note:
            round_clause += " — " + note
        parts.append(round_clause)
    elif plants:
        parts.append("watered the plants")
    else:
        parts.append("pottered about in the garden")

    if harvested:
        parts.append("picked " + _join(harvested))
    elif thirsty is None:
        # Nothing was thirsty, so a stage note is the interesting thing.
        for plant in plants:
            note = plant_state_note(plant, now=now)
            if note:
                parts.append(note)
                break
    return " and ".join(parts)


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return names[0] + " and " + names[1]
    return ", ".join(names[:-1]) + " and " + names[-1]


# ── H14 prompt grounding ────────────────────────────────────────────────


def item_state_hint(item: Any, *, now: datetime) -> str | None:
    """Short parenthetical describing an item's live state, or ``None``.

    Feeds the H14 whole-beat prompt, which previously saw a bare list of
    names and so could only invent generic business around them.
    """
    kind = str(getattr(item, "kind", "") or "")
    state = _state_of(item)
    if kind == "plant":
        bits = []
        stage = str(state.get("stage") or "").strip().lower()
        if stage:
            bits.append(stage)
        dry = dryness_days(item, now=now)
        if dry >= DRY_TOLERANCE_DAYS:
            bits.append("badly needs water")
        elif dry >= NOTICEABLE_DRY_DAYS:
            bits.append("dry, wants watering")
        else:
            bits.append("watered recently")
        return ", ".join(bits)
    if kind == "book" or state.get("total"):
        total = _int_of(state, "total", 0)
        progress = _int_of(state, "progress", 0)
        if total > 0:
            hint = "reading, chapter " + str(progress) + " of " + str(total)
            blurb = str(state.get("blurb") or "").strip()
            return hint + "; " + blurb if blurb else hint
        return None
    fullness = str(state.get("fullness") or "").strip().lower()
    if fullness:
        flavor = str(state.get("flavor") or "").strip()
        return (fullness + " of " + flavor) if flavor else fullness
    flavor = str(state.get("flavor") or "").strip()
    if flavor:
        return flavor
    return None


def describe_items_for_prompt(
    items: list[Any], *, now: datetime, limit: int = 12,
) -> str:
    """Render ``name (state hint)`` lines for the H14 prompt."""
    parts: list[str] = []
    for item in items[:limit]:
        name = _text_of(item, "name")
        if not name:
            continue
        hint = item_state_hint(item, now=now)
        if hint:
            parts.append(name + " (" + hint + ")")
        else:
            parts.append(name)
    return ", ".join(parts) or "(nothing notable)"


__all__ = [
    "DRY_TOLERANCE_DAYS",
    "NOTICEABLE_DRY_DAYS",
    "read_book_summary",
    "tea_summary",
    "pick_food",
    "snack_summary",
    "dryness_days",
    "thirstiest_plant",
    "plant_note",
    "plant_state_note",
    "garden_tend_summary",
    "item_state_hint",
    "describe_items_for_prompt",
]
