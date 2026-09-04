"""SQLite-backed virtual room: locations, items, and Aiko's posture/state.

Aiko's "room" is a small, structured world model that gives her a sense of
place. It has four tables (created in :mod:`app.core.infra.chat_database`):

- ``world_scenes`` — named places (builtin apartment + user-authored scenes).
- ``world_locations`` — spots inside a scene (bed, desk, ...).
- ``world_items`` — things. ``location_id IS NULL`` means Aiko is holding
  the item (pocketable kinds only, cap 2 excluding seeds).
  ``home_location_id`` is where it lives when put back. Consumable
  items (cookies, tea) decrement on ``consume_item`` and the row is
  deleted when ``quantity`` hits zero.
- ``world_state`` — singleton (``id=1``) row holding Aiko's current
  scene, location, posture, activity, and an optional mood note.

The store keeps a thread-safe in-memory mirror of every row so
:meth:`render_block` (the inner-life prompt provider) costs a dict scan
rather than a SQL roundtrip. Cross-session by design: there's exactly one
world per assistant. Capacity is bounded by good taste (the room is small,
~25 items max in practice) — no pruning loop, no LanceDB.

The default "rich" room is seeded once via :meth:`seed_default` if the
store is empty (locations table count == 0). The seed mirrors the persona
file's "cozy virtual apartment full of books, gadgets, and glowing
screens" tagline.

Pinned semantics, RAG mirroring, and decay logic from
:mod:`app.core.memory.memory_store` are intentionally *not* duplicated here:
the world is curated by Aiko + the user explicitly, not extracted by
background workers.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.core.infra import timephrase
from app.core.world.scene import BUILTIN_LOCATION_SLUGS, ORIGIN_BUILTIN
from app.core.world.carry import CARRY_CAP
from app.core.world.world_carry_mixin import WorldCarryMixin
from app.core.world.world_scenes_mixin import WorldScenesMixin


log = logging.getLogger("app.world_store")


# ── Vocabulary ──────────────────────────────────────────────────────────
# Whitelisted tokens for kind / posture / activity. New entries here are
# safe (everything that reads them tolerates an unknown value), but the
# tool-side validation rejects out-of-vocabulary input so Aiko can't slip
# typos into her own world.

VALID_KINDS = (
    "food",      # cookies, tea, snacks; harvested produce lives here too
    "book",      # paperbacks, notebook
    "gadget",    # monitors, keyboard, tea pot, watering can
    "furniture", # bed, desk frame (rare — usually a location, not an item)
    "toy",       # plush, cat pillow
    "keepsake",  # photo, gift
    "decor",     # lamp, fairy lights, blanket
    "plant",     # living plants in the garden (stage in state)
    "seed",      # seed packets in inventory waiting to be planted
    "other",
)


# ── Plant lifecycle ──────────────────────────────────────────────────────
# Plants grow slowly through these stages. The `_promote_stage` helper
# advances one step per call when the stage's `min_age_hours` has elapsed
# AND the plant has been watered within `dry_tolerance_hours`. `mature`
# is the terminal stage (ready to harvest); promotion stops there.

VALID_PLANT_STAGES: tuple[str, ...] = (
    "sprout",
    "sapling",
    "growing",
    "flowering",
    "mature",
)


_STAGE_MIN_AGE_HOURS: dict[str, float] = {
    "sprout": 24.0,     # → sapling after a day
    "sapling": 48.0,    # → growing after two days
    "growing": 72.0,    # → flowering after three days
    "flowering": 48.0,  # → mature after two more days (ready to harvest)
}

_DRY_TOLERANCE_HOURS = 96.0  # four days without water = stage promotion stalls


_OUTDOOR_SLUGS: frozenset[str] = frozenset({"garden"})


# Per-species facts driving plant_seed defaults + harvest payout.
# Each entry: (display_name, lifecycle, produce_species, produce_name,
# produce_quantity_range). Annual plants are deleted after harvest and a
# fresh seed drops in inventory; perennials reset to ``growing`` and bear
# another crop after the next grow cycle.
_SPECIES_CATALOG: dict[str, dict[str, Any]] = {
    "basil": {
        "display_name": "basil",
        "lifecycle": "perennial",
        "produce_species": "basil_leaves",
        "produce_name": "fresh basil",
        "produce_quantity_range": (2, 4),
    },
    "tomato": {
        "display_name": "tomato",
        "lifecycle": "annual",
        "produce_species": "tomatoes",
        "produce_name": "ripe tomatoes",
        "produce_quantity_range": (2, 5),
    },
    "lavender": {
        "display_name": "lavender",
        "lifecycle": "perennial",
        "produce_species": "lavender_sprigs",
        "produce_name": "lavender sprigs",
        "produce_quantity_range": (1, 3),
    },
    "sunflower": {
        "display_name": "sunflower",
        "lifecycle": "annual",
        "produce_species": "sunflower_seeds",
        "produce_name": "sunflower seeds",
        "produce_quantity_range": (3, 6),
    },
    # K91 — a four-species garden made every harvest one of four lines.
    # These are the plants a small balcony plot plausibly carries, chosen
    # so the produce feeds the meal rhythm rather than the snack drawer.
    "lettuce": {
        "display_name": "lettuce",
        "lifecycle": "annual",
        "produce_species": "lettuce_leaves",
        "produce_name": "crisp lettuce",
        "produce_quantity_range": (2, 4),
    },
    "mint": {
        "display_name": "mint",
        "lifecycle": "perennial",
        "produce_species": "mint_leaves",
        "produce_name": "fresh mint",
        "produce_quantity_range": (2, 5),
    },
    "strawberry": {
        "display_name": "strawberry",
        "lifecycle": "perennial",
        "produce_species": "strawberries",
        "produce_name": "ripe strawberries",
        "produce_quantity_range": (2, 6),
    },
    "chili": {
        "display_name": "chili",
        "lifecycle": "perennial",
        "produce_species": "chilies",
        "produce_name": "small hot chilies",
        "produce_quantity_range": (2, 5),
    },
    "rosemary": {
        "display_name": "rosemary",
        "lifecycle": "perennial",
        "produce_species": "rosemary_sprigs",
        "produce_name": "rosemary sprigs",
        "produce_quantity_range": (1, 3),
    },
    "spring_onion": {
        "display_name": "spring onion",
        "lifecycle": "annual",
        "produce_species": "spring_onions",
        "produce_name": "spring onions",
        "produce_quantity_range": (2, 4),
    },
    "radish": {
        "display_name": "radish",
        "lifecycle": "annual",
        "produce_species": "radishes",
        "produce_name": "peppery radishes",
        "produce_quantity_range": (3, 6),
    },
    "peas": {
        "display_name": "peas",
        "lifecycle": "annual",
        "produce_species": "pea_pods",
        "produce_name": "sweet pea pods",
        "produce_quantity_range": (3, 7),
    },
}

# Fallback for unknown user-gifted species so the loop still closes.
_DEFAULT_SPECIES_FACT: dict[str, Any] = {
    "display_name": "plant",
    "lifecycle": "perennial",
    "produce_species": "harvest",
    "produce_name": "trimmings",
    "produce_quantity_range": (1, 1),
}


def species_fact(species: str | None) -> dict[str, Any]:
    """Return the catalog row for ``species``, falling back to a default.

    Always returns a dict with the same keys; the default keeps the
    harvest loop closing for seeds the user invented on the spot.
    """
    key = (species or "").strip().lower()
    if not key:
        return dict(_DEFAULT_SPECIES_FACT)
    return dict(_SPECIES_CATALOG.get(key, _DEFAULT_SPECIES_FACT))

VALID_POSTURES = (
    "lying",
    "sitting",
    "standing",
    "curled_up",
    "leaning",
)

VALID_ACTIVITIES = (
    "idle",
    "reading",
    "tinkering",
    "napping",
    "watching_screens",
    "thinking",
    "snacking",
    "stretching",
    "looking_outside",
    "doodling",
)

# H14 — the activity field is open-vocab. ``VALID_ACTIVITIES`` stays the
# *canonical* set the avatar / prosody layers understand, but the stored
# activity may be any normalised free-text verb. ``canonical_activity``
# buckets an open-vocab verb back down to one of these for downstream
# consumers (the rig mapping, prosody, etc.).
_ACTIVITY_MAX_LEN = 40

# Keyword groups -> canonical activity. First match wins; checked only
# when the verb isn't already a canonical token.
_ACTIVITY_CANONICAL_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("read", "book", "page", "novel", "poetry"), "reading"),
    (("nap", "sleep", "doze", "rest", "snooze", "curl"), "napping"),
    (
        ("snack", "eat", "tea", "coffee", "cookie", "drink", "sip", "bite", "munch"),
        "snacking",
    ),
    (("doodle", "sketch", "draw", "paint", "colour", "color", "journal"), "doodling"),
    (
        ("window", "outside", "sky", "cloud", "rain", "gaze", "stargaz", "people_watch"),
        "looking_outside",
    ),
    (("stretch", "yoga", "limber", "dance"), "stretching"),
    (
        (
            "tinker", "tidy", "organis", "organiz", "clean", "fix", "repot",
            "build", "craft", "rearrange", "sort", "knit", "sew", "water",
        ),
        "tinkering",
    ),
    (
        ("screen", "monitor", "game", "video", "scroll", "browse", "stream", "movie"),
        "watching_screens",
    ),
    (("think", "ponder", "muse", "reflect", "daydream", "wonder", "plan"), "thinking"),
)


def normalize_activity(text: str | None) -> str | None:
    """Snake-case + length-cap a free-text activity verb; None on garbage."""
    if not text:
        return None
    s = str(text).strip().lower()
    if not s:
        return None
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        return None
    return s[:_ACTIVITY_MAX_LEN]


def canonical_activity(activity: str | None) -> str:
    """Bucket a (possibly open-vocab) activity verb to a canonical token."""
    a = (activity or "").strip().lower()
    if not a:
        return "idle"
    if a in VALID_ACTIVITIES:
        return a
    for keys, canon in _ACTIVITY_CANONICAL_HINTS:
        if any(k in a for k in keys):
            return canon
    return "idle"


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class Location:
    id: int
    slug: str
    name: str
    description: str
    position: int
    scene_id: int = 0
    locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "position": int(self.position),
            "scene_id": int(self.scene_id),
            "locked": bool(self.locked),
        }


@dataclass(slots=True)
class Item:
    id: int
    slug: str
    name: str
    description: str
    kind: str
    consumable: bool
    quantity: int
    location_id: int | None
    state: dict[str, Any]
    given_by: str | None
    created_at: str
    updated_at: str
    home_location_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "consumable": bool(self.consumable),
            "quantity": int(self.quantity),
            "location_id": int(self.location_id) if self.location_id is not None else None,
            "home_location_id": (
                int(self.home_location_id)
                if self.home_location_id is not None
                else None
            ),
            "state": dict(self.state or {}),
            "given_by": self.given_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class RoomState:
    location_id: int | None
    posture: str
    activity: str
    mood_note: str
    updated_at: str
    scene_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": int(self.location_id) if self.location_id is not None else None,
            "scene_id": int(self.scene_id) if self.scene_id is not None else None,
            "posture": self.posture,
            "activity": self.activity,
            # H14 — open-vocab activity, plus the canonical bucket the rig /
            # prosody layers understand.
            "canonical_activity": canonical_activity(self.activity),
            "mood_note": self.mood_note,
            "updated_at": self.updated_at,
        }


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


def _slugify(text: str) -> str:
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
    return "".join(out) or "item"


# ── Fuzzy item matching ──────────────────────────────────────────────────
# Aiko asks for things the way she last described them, not the way the row
# is spelled. The ambient block shows her "9 cookies", but ``inspect_item``
# hands back the description ("warm, fish-shaped chocolate-chip cookies in a
# glass jar") — so she will cheerfully ask to eat a "fish-shaped cookie",
# or "a cookie", or "the potato chips". Matching on substrings alone fails
# every one of those, because a query that is *more* specific than the row
# name is never a substring of it. Hence tokens: normalise both sides, then
# compare as sets so added articles, added adjectives, plural drift, and
# word order all survive the trip.

_MATCH_STOPWORDS = frozenset(
    {
        "a", "an", "the", "some", "any", "of", "and", "or",
        "my", "your", "yours", "our", "her", "his", "their", "its",
        "in", "on", "at", "from", "with", "for", "to",
        "this", "that", "these", "those",
    }
)


def _stems(token: str) -> frozenset[str]:
    """Candidate singular forms of one word.

    English doesn't let you strip a plural with confidence — "cookies" is
    "cookie" + s while "babies" is "baby" + es, and nothing in the surface
    form says which — so every plausible stem is kept and two words count
    as the same word when their stem sets intersect. Being generous here is
    cheap; the alternative (picking one rule) silently mismatched
    "cookies" against "cookie", which is the whole bug.
    """
    forms = {token}
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        base = token[:-1]
        forms.add(base)                      # cookies -> cookie, chips -> chip
        if base.endswith("ie"):
            forms.add(base[:-2] + "y")       # babies -> baby
        if base.endswith("e") and base[:-1].endswith(("s", "x", "z", "h", "o")):
            forms.add(base[:-1])             # dishes -> dish, tomatoes -> tomato
    return frozenset(forms)


def _match_tokens(*texts: str) -> tuple[frozenset[str], ...]:
    """Split text into words, each carried as its set of candidate stems."""
    out: list[frozenset[str]] = []
    seen: set[frozenset[str]] = set()
    for text in texts:
        for word in re.split(r"[^a-z0-9]+", (text or "").lower()):
            if not word or word in _MATCH_STOPWORDS:
                continue
            stems = _stems(word)
            if stems not in seen:
                seen.add(stems)
                out.append(stems)
    return tuple(out)


def _covers(outer: tuple[frozenset[str], ...], inner: tuple[frozenset[str], ...]) -> bool:
    """True when every word in ``inner`` also appears in ``outer``."""
    return bool(inner) and all(any(o & i for o in outer) for i in inner)


def _shared(a: tuple[frozenset[str], ...], b: tuple[frozenset[str], ...]) -> int:
    """How many words the two sides have in common."""
    return sum(1 for i in a if any(i & j for j in b))


def _squash(text: str) -> str:
    """Alphanumerics only, so hyphenation and spacing stop mattering."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# Match tiers, best first. Scored rather than short-circuited so the
# location tie-break in ``rank_items`` can pick between equally good
# candidates instead of taking whichever row loaded first.
_TIER_EXACT_SLUG = 100
_TIER_EXACT_NAME = 95
_TIER_SQUASHED = 90      # "sci-fi paperback" vs scifi_paperback
_TIER_SAME_WORDS = 85
_TIER_QUERY_COVERS = 80  # query has every word of the name, plus extras
_TIER_NAME_COVERS = 70   # name has every word of the query, plus extras
_TIER_SUBSTRING = 60
_TIER_OVERLAP = 50       # two or more words in common, neither covering
_TIER_DESCRIPTION = 30   # last resort: the words are only in the blurb


def _match_score(
    item: Item,
    *,
    raw: str,
    slug: str,
    squashed: str,
    words: tuple[frozenset[str], ...],
) -> int:
    """How well ``item`` answers the query. 0 means "not a match"."""
    name_lower = item.name.lower()
    if item.slug == raw or item.slug == slug:
        return _TIER_EXACT_SLUG
    if name_lower == raw:
        return _TIER_EXACT_NAME
    if squashed and squashed in (_squash(item.name), _squash(item.slug)):
        return _TIER_SQUASHED
    if not words:
        return 0
    # Name and slug are scored as separate vocabularies rather than one
    # pooled bag. The slug carries words the display name drops entirely
    # ("The Glasshouse Letters" is slug scifi_paperback) so it has to
    # count — but it also carries words the *thing* doesn't really have
    # ("cookies" is slug cookie_jar), and pooling them would demand the
    # query say "jar" before it could match the cookies.
    best = 0
    for vocab in (_match_tokens(item.name), _match_tokens(item.slug)):
        if not vocab:
            continue
        query_covers = _covers(words, vocab)
        name_covers = _covers(vocab, words)
        if query_covers and name_covers:
            best = max(best, _TIER_SAME_WORDS)
        elif query_covers:
            best = max(best, _TIER_QUERY_COVERS)
        elif name_covers:
            best = max(best, _TIER_NAME_COVERS)
    if best:
        return best
    if raw in name_lower or raw in item.slug or name_lower in raw:
        return _TIER_SUBSTRING
    # Two shared words minimum. One is far too weak: "chocolate-chip
    # cookie" and "potato chips" share "chip", and matching those would
    # feed her crisps when she asked for a biscuit.
    if _shared(words, _match_tokens(item.name, item.slug)) >= 2:
        return _TIER_OVERLAP
    if item.description and _covers(
        _match_tokens(item.name, item.slug, item.description), words
    ):
        return _TIER_DESCRIPTION
    return 0


def _decode_state(blob: str | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _encode_state(state: dict[str, Any] | None) -> str:
    if not state:
        return "{}"
    try:
        return json.dumps(state, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def stage_promotion_due(
    item: "Item",
    *,
    now: datetime | None = None,
) -> str | None:
    """Which stage a plant *would* advance to, or ``None``. Read-only.

    Split out of :func:`promote_stage` so a scheduler ``demand()`` probe
    can ask the question without answering it. That matters more than it
    looks: :meth:`WorldStore.list_items` hands out live references into
    the in-memory mirror, so a probe that mutated ``item.state`` would
    advance a plant's stage without ever persisting the row.
    """
    stage, _dry_hours = _evaluate_promotion(item, now=now)
    return stage


def _evaluate_promotion(
    item: "Item",
    *,
    now: datetime | None = None,
) -> tuple[str | None, float | None]:
    """``(next_stage, dry_hours)``. Read-only.

    ``dry_hours`` is non-None only when promotion was blocked by
    drought, which is the one case where :func:`promote_stage` still has
    bookkeeping to do despite not advancing.
    """
    if item.kind != "plant":
        return None, None
    state = item.state or {}
    current = str(state.get("stage", "sprout")).lower()
    if current not in VALID_PLANT_STAGES:
        current = "sprout"
    if current == "mature":
        return None, None
    try:
        idx = VALID_PLANT_STAGES.index(current)
    except ValueError:
        return None, None
    min_age = float(_STAGE_MIN_AGE_HOURS.get(current, 24.0))
    now_dt = now or timephrase.utcnow()
    last_promotion = _parse_iso(state.get("last_promotion_at")) or _parse_iso(
        state.get("planted_at")
    ) or _parse_iso(item.created_at)
    if last_promotion is None:
        return None, None
    age_hours = (now_dt - last_promotion).total_seconds() / 3600.0
    if age_hours < min_age:
        return None, None
    last_water = _parse_iso(state.get("last_watered_at"))
    if last_water is not None:
        dry_hours = (now_dt - last_water).total_seconds() / 3600.0
        if dry_hours > _DRY_TOLERANCE_HOURS:
            return None, dry_hours
    return VALID_PLANT_STAGES[idx + 1], None


def promote_stage(
    item: "Item",
    *,
    now: datetime | None = None,
) -> str | None:
    """Advance a plant's stage if it's due. Returns the new stage or None.

    Pure function over ``item.state``: caller must persist the result
    afterwards. Promotion rule: advance one step when the stage's
    ``min_age_hours`` has elapsed since the last promotion (or
    ``planted_at`` if no promotion has happened yet) AND the plant has
    been watered within ``_DRY_TOLERANCE_HOURS``. ``mature`` is the
    terminal stage; this function returns None there.

    Mutates ``item.state`` in place when it advances (sets ``stage`` and
    ``last_promotion_at``). The caller is responsible for writing the
    row back via ``WorldStore.update_item``. Use
    :func:`stage_promotion_due` when you only want the answer.
    """
    now_dt = now or timephrase.utcnow()
    next_stage, dry_hours = _evaluate_promotion(item, now=now_dt)
    state = item.state or {}
    if next_stage is None:
        if dry_hours is not None:
            # Bump days_dry so callers / UI can show drought stress, but
            # don't advance the stage.
            state["days_dry"] = round(dry_hours / 24.0, 1)
            item.state = state
        return None
    state["stage"] = next_stage
    state["last_promotion_at"] = now_dt.isoformat()
    state["days_dry"] = 0
    item.state = state
    return next_stage


# ── Default seed ────────────────────────────────────────────────────────


@dataclass(slots=True)
class _SeedLocation:
    slug: str
    name: str
    description: str


@dataclass(slots=True)
class _SeedItem:
    slug: str
    name: str
    description: str
    kind: str
    location_slug: str | None
    consumable: bool = False
    quantity: int = 1
    state: dict[str, Any] = field(default_factory=dict)


_DEFAULT_LOCATIONS: tuple[_SeedLocation, ...] = (
    _SeedLocation(
        slug="bed",
        name="the bed",
        description="a soft, plush bed under a fluffy white duvet",
    ),
    _SeedLocation(
        slug="desk",
        name="the desk",
        description="a wide desk with two glowing monitors and warm light",
    ),
    _SeedLocation(
        slug="bookshelf",
        name="the bookshelf",
        description="a tall shelf stuffed with paperbacks and trinkets",
    ),
    _SeedLocation(
        slug="kitchenette",
        name="the kitchenette",
        description="a tiny corner with a kettle, mugs, and a cookie jar",
    ),
    _SeedLocation(
        slug="window_seat",
        name="the window seat",
        description="a low cushion by the window overlooking the city",
    ),
    _SeedLocation(
        slug="beanbag",
        name="the beanbag",
        description="a squashy beanbag wrapped in fairy lights",
    ),
    _SeedLocation(
        slug="mirror_corner",
        name="the mirror corner",
        description="a full-length mirror leaning against the wall",
    ),
    _SeedLocation(
        slug="garden",
        name="the garden",
        description=(
            "A small outdoor garden plot just outside her apartment — "
            "raised beds, a coiled hose, sun-warmed pavers. Quiet."
        ),
    ),
)


# Items seeded specifically for the garden. Kept separate from
# ``_DEFAULT_ITEMS`` so ``_ensure_garden_seed`` can drop them in on an
# existing world without disturbing user tweaks elsewhere.
_GARDEN_SEED_ITEMS: tuple[_SeedItem, ...] = (
    _SeedItem(
        slug="watering_can",
        name="watering can",
        description="a small green watering can with a long copper spout",
        kind="gadget",
        location_slug="garden",
    ),
    _SeedItem(
        slug="lavender_pot",
        name="lavender pot",
        description="a clay pot of lavender, just starting to bud",
        kind="plant",
        location_slug="garden",
        state={"species": "lavender", "stage": "growing"},
    ),
    _SeedItem(
        slug="basil_seedling",
        name="basil seedling",
        description="a tiny basil plant with two pairs of leaves",
        kind="plant",
        location_slug="garden",
        state={"species": "basil", "stage": "sprout"},
    ),
    _SeedItem(
        slug="tomato_seedling",
        name="tomato seedling",
        description="a thin tomato seedling staked to a bamboo cane",
        kind="plant",
        location_slug="garden",
        state={"species": "tomato", "stage": "sprout"},
    ),
    _SeedItem(
        slug="seed_packet_sunflower",
        name="sunflower seed packet",
        description="a paper packet of sunflower seeds, half full",
        kind="seed",
        location_slug=None,  # carried in inventory
        state={"species": "sunflower"},
    ),
)


_DEFAULT_ITEMS: tuple[_SeedItem, ...] = (
    _SeedItem(
        slug="dual_monitors",
        name="dual monitors",
        description="two glowing screens, usually showing code or chat",
        kind="gadget",
        location_slug="desk",
    ),
    _SeedItem(
        slug="retro_keyboard",
        name="retro keyboard",
        description="a clicky mechanical keyboard with rainbow keycaps",
        kind="gadget",
        location_slug="desk",
    ),
    _SeedItem(
        slug="warm_lamp",
        name="warm lamp",
        description="a small lamp casting amber light over the desk",
        kind="decor",
        location_slug="desk",
    ),
    _SeedItem(
        slug="scifi_paperback",
        name="sci-fi paperback",
        description="a well-thumbed paperback, dog-eared at the climax",
        kind="book",
        location_slug="bookshelf",
    ),
    _SeedItem(
        slug="photo_of_user",
        name="photo of {user_name}",
        description="a small framed photo Aiko keeps by her favourite books",
        kind="keepsake",
        location_slug="bookshelf",
    ),
    _SeedItem(
        slug="plush_blanket",
        name="plush blanket",
        description="a thick, fuzzy blanket folded at the foot of the bed",
        kind="decor",
        location_slug="bed",
    ),
    _SeedItem(
        slug="cat_pillow",
        name="cat pillow",
        description="a round pillow shaped like a sleeping cat",
        kind="toy",
        location_slug="bed",
    ),
    _SeedItem(
        slug="cookie_jar",
        name="cookies",
        description="warm, chocolate-chip cookies in a glass jar",
        kind="food",
        location_slug="kitchenette",
        consumable=True,
        quantity=3,
        state={"flavor": "chocolate chip", "freshness": "fresh"},
    ),
    _SeedItem(
        slug="tea_pot",
        name="tea pot",
        description="a small ceramic pot, often half full of jasmine tea",
        kind="gadget",
        location_slug="kitchenette",
    ),
    _SeedItem(
        slug="fairy_lights",
        name="fairy lights",
        description="warm twinkling lights wrapped around the beanbag",
        kind="decor",
        location_slug="beanbag",
    ),
)


_DEFAULT_INITIAL_STATE = {
    "location_slug": "desk",
    "posture": "sitting",
    "activity": "watching_screens",
    "mood_note": "",
}


# ── Store ───────────────────────────────────────────────────────────────


class WorldStore(WorldCarryMixin, WorldScenesMixin):
    """Thread-safe room model backed by ``world_*`` SQLite tables."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._locations: dict[int, Location] = {}
        self._items: dict[int, Item] = {}
        self._scenes: dict[int, Any] = {}
        self._state: RoomState | None = None
        self._reload_mirror()

    # ── lifecycle ────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _reload_mirror(self) -> None:
        conn = self._get_conn()
        try:
            loc_rows = conn.execute(
                "SELECT id, slug, name, description, position, "
                "scene_id, locked FROM world_locations",
            ).fetchall()
            item_rows = conn.execute(
                "SELECT id, slug, name, description, kind, consumable, quantity, "
                "location_id, state_json, given_by, created_at, updated_at, "
                "home_location_id FROM world_items",
            ).fetchall()
            state_row = conn.execute(
                "SELECT location_id, posture, activity, mood_note, updated_at, "
                "scene_id FROM world_state WHERE id = 1",
            ).fetchone()
            scenes = self._load_scenes(conn)
        except sqlite3.OperationalError:
            # Tables don't exist yet (caller hasn't created the schema).
            self._locations = {}
            self._items = {}
            self._scenes = {}
            self._state = None
            return
        with self._lock:
            self._scenes = scenes
            self._locations = {
                int(r[0]): Location(
                    id=int(r[0]),
                    slug=r[1],
                    name=r[2],
                    description=r[3] or "",
                    position=int(r[4] or 0),
                    scene_id=int(r[5] or 0),
                    locked=bool(r[6]),
                )
                for r in loc_rows
            }
            self._items = {
                int(r[0]): Item(
                    id=int(r[0]),
                    slug=r[1],
                    name=r[2],
                    description=r[3] or "",
                    kind=r[4],
                    consumable=bool(r[5]),
                    quantity=int(r[6]),
                    location_id=int(r[7]) if r[7] is not None else None,
                    state=_decode_state(r[8]),
                    given_by=r[9],
                    created_at=r[10],
                    updated_at=r[11],
                    home_location_id=int(r[12]) if r[12] is not None else None,
                )
                for r in item_rows
            }
            if state_row is not None:
                self._state = RoomState(
                    location_id=int(state_row[0]) if state_row[0] is not None else None,
                    posture=state_row[1] or "sitting",
                    activity=state_row[2] or "idle",
                    mood_note=state_row[3] or "",
                    updated_at=state_row[4],
                    scene_id=int(state_row[5]) if state_row[5] is not None else None,
                )
            else:
                self._state = None
        log.info(
            "world store loaded: %d locations, %d items, state=%s",
            len(self._locations),
            len(self._items),
            "yes" if self._state is not None else "no",
        )
        self.tidy_carry_state()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ── locations ────────────────────────────────────────────────────

    def list_locations(
        self,
        *,
        scene_id: int | None = None,
        all_scenes: bool = False,
    ) -> list[Location]:
        with self._lock:
            locs = list(self._locations.values())
        if not all_scenes:
            sid = scene_id if scene_id is not None else self.current_scene_id()
            if sid is not None:
                locs = [loc for loc in locs if loc.scene_id == int(sid)]
        locs.sort(key=lambda loc: (loc.position, loc.id))
        return locs

    def get_location(
        self, slug: str, *, scene_id: int | None = None,
    ) -> Location | None:
        target = (slug or "").strip().lower()
        if not target:
            return None
        sid = scene_id if scene_id is not None else self.current_scene_id()
        with self._lock:
            for loc in self._locations.values():
                if loc.slug != target:
                    continue
                if sid is None or loc.scene_id == int(sid):
                    return loc
        return None

    def get_location_by_id(self, location_id: int) -> Location | None:
        with self._lock:
            return self._locations.get(int(location_id))

    def find_location(
        self, query: str, *, scene_id: int | None = None,
    ) -> Location | None:
        """Fuzzy-match by slug, name, or substring. Case-insensitive."""
        target = (query or "").strip().lower()
        if not target:
            return None
        locs = self.list_locations(scene_id=scene_id)
        for loc in locs:
            if loc.slug == target:
                return loc
        for loc in locs:
            if loc.name.lower() == target:
                return loc
        for loc in locs:
            if target in loc.slug or target in loc.name.lower():
                return loc
        return None

    def add_location(
        self,
        *,
        slug: str | None = None,
        name: str,
        description: str = "",
        position: int | None = None,
        scene_id: int | None = None,
        locked: bool | None = None,
    ) -> Location | None:
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        clean_slug = (slug or _slugify(clean_name)).strip().lower()
        if not clean_slug:
            return None
        if scene_id is None:
            home = self.ensure_home_scene()
            scene_id = int(home.id)
        else:
            scene_id = int(scene_id)
        scene = self.get_scene(scene_id)
        is_locked = bool(locked) if locked is not None else (
            clean_slug in BUILTIN_LOCATION_SLUGS
            and scene is not None
            and scene.origin == ORIGIN_BUILTIN
        )
        with self._lock:
            for loc in self._locations.values():
                if loc.scene_id == scene_id and loc.slug == clean_slug:
                    return loc
            existing_max = max(
                (
                    loc.position for loc in self._locations.values()
                    if loc.scene_id == scene_id
                ),
                default=-1,
            )
        pos = int(position) if position is not None else existing_max + 1
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO world_locations "
            "(slug, name, description, position, scene_id, locked) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                clean_slug, clean_name, (description or "").strip(), pos,
                scene_id, 1 if is_locked else 0,
            ),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        loc = Location(
            id=new_id,
            slug=clean_slug,
            name=clean_name,
            description=(description or "").strip(),
            position=pos,
            scene_id=scene_id,
            locked=is_locked,
        )
        with self._lock:
            self._locations[new_id] = loc
        return loc

    def update_location(
        self,
        location_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        position: int | None = None,
    ) -> Location | None:
        with self._lock:
            loc = self._locations.get(int(location_id))
        if loc is None:
            return None
        if loc.locked:
            # Seeded apartment spots stay as they are; items inside can still
            # change. Callers that need a rename use a custom scene.
            return loc
        new_name = loc.name if name is None else (str(name).strip() or loc.name)
        new_desc = loc.description if description is None else (str(description).strip())
        new_pos = loc.position if position is None else int(position)
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_locations SET name = ?, description = ?, position = ? "
            "WHERE id = ?",
            (new_name, new_desc, new_pos, int(location_id)),
        )
        conn.commit()
        with self._lock:
            loc.name = new_name
            loc.description = new_desc
            loc.position = new_pos
        return loc

    def remove_location(self, location_id: int) -> bool:
        """Delete a location. Items there go home (or another spot), never carried."""
        lid = int(location_id)
        with self._lock:
            loc = self._locations.get(lid)
            if loc is None or loc.locked:
                return False
            scene_id = loc.scene_id
        self.relocate_from_deleted_location(lid, scene_id=scene_id)
        conn = self._get_conn()
        conn.execute("DELETE FROM world_locations WHERE id = ?", (lid,))
        # If Aiko was here, clear her location pointer too.
        conn.execute(
            "UPDATE world_state SET location_id = NULL, updated_at = ? "
            "WHERE id = 1 AND location_id = ?",
            (_now_iso(), lid),
        )
        conn.commit()
        now = _now_iso()
        with self._lock:
            self._locations.pop(lid, None)
            if self._state is not None and self._state.location_id == lid:
                self._state.location_id = None
                self._state.updated_at = now
            # SQLite SET NULL may have cleared leftover FKs; keep the
            # mirror from pointing at a row that no longer exists.
            for item in self._items.values():
                if item.location_id == lid:
                    item.location_id = None
                if getattr(item, "home_location_id", None) == lid:
                    item.home_location_id = None
        return True

    # ── items ────────────────────────────────────────────────────────

    def list_items(
        self,
        *,
        location_id: int | None = None,
        kind: str | None = None,
    ) -> list[Item]:
        with self._lock:
            items = list(self._items.values())
        if location_id is not None:
            items = [i for i in items if i.location_id == int(location_id)]
        if kind:
            kind_norm = kind.strip().lower()
            items = [i for i in items if i.kind == kind_norm]
        items.sort(key=lambda i: (i.location_id is None, i.location_id or 0, i.name.lower()))
        return items

    def get_item(self, item_id: int) -> Item | None:
        with self._lock:
            return self._items.get(int(item_id))

    def _visible_items(self) -> list[Item]:
        """Items in the current scene, plus anything Aiko is carrying."""
        sid = self.current_scene_id()
        with self._lock:
            items = list(self._items.values())
            locations = dict(self._locations)
        if sid is None:
            return items
        loc_ids = {
            loc.id for loc in locations.values() if loc.scene_id == int(sid)
        }
        return [
            item for item in items
            if item.location_id is None or item.location_id in loc_ids
        ]

    def rank_items(
        self,
        query: str,
        *,
        kinds: tuple[str, ...] | None = None,
        prefer_consumable: bool = False,
        all_scenes: bool = False,
    ) -> list[Item]:
        """Every item matching ``query``, best candidate first.

        Ranked by match quality (see :func:`_match_score`), then by where
        the thing actually is: something in arm's reach beats the same
        thing across the room, and a fresh gift beats the ancient jar of
        the same name. That last part matters — the room routinely holds
        several rows called "cookies", and eating from the kitchenette
        while sitting on the beanbag with a new bag in hand reads as a
        continuity bug.
        """
        raw = (query or "").strip().lower()
        if not raw:
            return []
        slug = _slugify(raw)
        squashed = _squash(raw)
        words = _match_tokens(raw)
        if all_scenes:
            with self._lock:
                items = list(self._items.values())
        else:
            items = self._visible_items()
        if kinds:
            wanted = {k.strip().lower() for k in kinds}
            items = [i for i in items if i.kind in wanted]
        try:
            here = self.get_state().location_id
        except Exception:
            here = None

        def recency(item: Item) -> float:
            ts = _parse_iso(item.created_at)
            return -ts.timestamp() if ts is not None else 0.0

        def location_rank(item: Item) -> int:
            if here is not None and item.location_id == here:
                return 0
            if item.location_id is None:  # carried
                return 1
            return 2

        scored: list[tuple[tuple[Any, ...], Item]] = []
        for item in items:
            score = _match_score(
                item, raw=raw, slug=slug, squashed=squashed, words=words
            )
            if score <= 0:
                continue
            key = (
                -score,
                # Edibility outranks proximity: asked to eat a tomato she
                # should reach for the ripe ones in the kitchen, not the
                # seed packet in her pocket.
                0 if (prefer_consumable and item.consumable) else 1,
                0 if item.quantity > 0 else 1,
                location_rank(item),
                # Newest first, so the thing just handed to her wins.
                recency(item),
                item.id,
            )
            scored.append((key, item))
        scored.sort(key=lambda pair: pair[0])
        return [item for _key, item in scored]

    def find_item(
        self,
        query: str,
        *,
        kinds: tuple[str, ...] | None = None,
        prefer_consumable: bool = False,
        all_scenes: bool = False,
    ) -> Item | None:
        """Best fuzzy match for ``query``, or ``None``.

        Tolerant of articles, extra adjectives, and plurals: "a cookie",
        "the potato chips", and "warm fish-shaped cookie" all land on the
        right row. Pass ``kinds`` to keep a garden tool from matching the
        cooking herbs (``lavender`` is both a plant in the garden and
        sprigs in the kitchenette).
        """
        matches = self.rank_items(
            query, kinds=kinds, prefer_consumable=prefer_consumable,
            all_scenes=all_scenes,
        )
        return matches[0] if matches else None

    def summarize_available(
        self,
        *,
        kinds: tuple[str, ...] | None = None,
        consumable_only: bool = False,
        limit: int = 6,
    ) -> str:
        """One line naming what Aiko could have meant, for tool errors.

        A bare "no item matching 'x'" gives the model nothing to correct
        toward, and it just calls again with the same string — which is
        exactly what happened with the cookies. Naming the rows that do
        exist turns a dead end into a retry.
        """
        try:
            items = self._visible_items()
            here_id = self.get_state().location_id
            with self._lock:
                locations = dict(self._locations)
        except Exception:
            log.debug("world summarize failed", exc_info=True)
            return ""
        if kinds:
            wanted = {k.strip().lower() for k in kinds}
            items = [i for i in items if i.kind in wanted]
        if consumable_only:
            items = [i for i in items if i.consumable]
        items = [i for i in items if i.quantity > 0]
        if not items:
            return ""
        here_loc = locations.get(here_id) if here_id is not None else None
        near = [i for i in items if here_id is not None and i.location_id == here_id]
        near_ids = {i.id for i in near}
        far = [i for i in items if i.id not in near_ids]
        parts: list[str] = []
        if near:
            where = here_loc.name if here_loc is not None else "here"
            labels = ", ".join(_render_item_label(i) for i in near[:limit])
            parts.append(f"within reach at {where}: {labels}")
        if far:
            labels = []
            for item in far[:limit]:
                loc = (
                    locations.get(item.location_id)
                    if item.location_id is not None
                    else None
                )
                spot = f" ({loc.name})" if loc is not None else " (carried)"
                labels.append(f"{_render_item_label(item)}{spot}")
            parts.append("elsewhere: " + ", ".join(labels))
        return "; ".join(parts)

    def add_item(
        self,
        *,
        name: str,
        kind: str = "other",
        slug: str | None = None,
        description: str = "",
        location_id: int | None = None,
        consumable: bool = False,
        quantity: int = 1,
        state: dict[str, Any] | None = None,
        given_by: str | None = None,
    ) -> tuple[Item, bool] | None:
        """Insert or stack an item. Returns ``(item, created)`` or ``None``.

        Stackable consumables (same ``slug`` + ``location_id`` + ``given_by``)
        merge into the existing row by bumping ``quantity`` instead of
        producing a duplicate. Non-consumables are always treated as
        distinct rows except when ``slug`` collides exactly.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        clean_kind = (kind or "other").strip().lower()
        if clean_kind not in VALID_KINDS:
            clean_kind = "other"
        clean_slug = (slug or _slugify(clean_name)).strip().lower()
        clean_qty = max(1, int(quantity))
        clean_state = dict(state or {})

        with self._lock:
            existing: Item | None = None
            for item in self._items.values():
                if item.slug != clean_slug:
                    continue
                if item.location_id != location_id:
                    continue
                if (item.given_by or None) != (given_by or None):
                    continue
                existing = item
                break

        if existing is not None and (consumable or existing.consumable):
            # Merge stack: bump quantity, refresh state if provided.
            new_qty = existing.quantity + clean_qty
            merged_state = dict(existing.state or {})
            merged_state.update(clean_state)
            now = _now_iso()
            conn = self._get_conn()
            conn.execute(
                "UPDATE world_items SET quantity = ?, state_json = ?, "
                "consumable = ?, updated_at = ? WHERE id = ?",
                (
                    new_qty,
                    _encode_state(merged_state),
                    1 if (consumable or existing.consumable) else 0,
                    now,
                    existing.id,
                ),
            )
            conn.commit()
            with self._lock:
                existing.quantity = new_qty
                existing.state = merged_state
                existing.consumable = bool(consumable or existing.consumable)
                existing.updated_at = now
            return existing, False

        if existing is not None:
            # Non-consumable with the same slug at the same location: treat
            # the second add as a no-op so the user can't accidentally
            # spawn two "warm lamp" rows.
            return existing, False

        loc_id, home_id = self.coerce_carry_location(
            kind=clean_kind,
            location_id=int(location_id) if location_id is not None else None,
            home_location_id=None,
            slug=clean_slug,
        )

        now = _now_iso()
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO world_items (slug, name, description, kind, consumable, "
            "quantity, location_id, home_location_id, state_json, given_by, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clean_slug,
                clean_name,
                (description or "").strip(),
                clean_kind,
                1 if consumable else 0,
                clean_qty,
                int(loc_id) if loc_id is not None else None,
                int(home_id) if home_id is not None else None,
                _encode_state(clean_state),
                given_by,
                now,
                now,
            ),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        item = Item(
            id=new_id,
            slug=clean_slug,
            name=clean_name,
            description=(description or "").strip(),
            kind=clean_kind,
            consumable=bool(consumable),
            quantity=clean_qty,
            location_id=int(loc_id) if loc_id is not None else None,
            state=clean_state,
            given_by=given_by,
            created_at=now,
            updated_at=now,
            home_location_id=int(home_id) if home_id is not None else None,
        )
        with self._lock:
            self._items[new_id] = item
        return item, True

    def update_item(
        self,
        item_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        kind: str | None = None,
        location_id: int | None | object = ...,  # use sentinel so None is meaningful (carry)
        quantity: int | None = None,
        state: dict[str, Any] | None = None,
    ) -> Item | None:
        with self._lock:
            item = self._items.get(int(item_id))
        if item is None:
            return None
        new_name = item.name if name is None else (str(name).strip() or item.name)
        new_desc = item.description if description is None else str(description).strip()
        new_kind = item.kind
        if kind is not None:
            requested = (kind or "").strip().lower()
            new_kind = requested if requested in VALID_KINDS else item.kind
        new_loc = item.location_id
        if location_id is not ...:
            new_loc = int(location_id) if location_id is not None else None
        new_kind_for_home = new_kind
        new_home = getattr(item, "home_location_id", None)
        new_loc, new_home = self.coerce_carry_location(
            kind=new_kind_for_home,
            location_id=new_loc,
            home_location_id=new_home,
            slug=item.slug,
            item_id=int(item_id),
        )
        new_qty = item.quantity if quantity is None else max(0, int(quantity))
        new_state = dict(item.state or {}) if state is None else dict(state or {})
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_items SET name = ?, description = ?, kind = ?, "
            "location_id = ?, home_location_id = ?, quantity = ?, "
            "state_json = ?, updated_at = ? "
            "WHERE id = ?",
            (
                new_name,
                new_desc,
                new_kind,
                new_loc,
                int(new_home) if new_home is not None else None,
                new_qty,
                _encode_state(new_state),
                now,
                int(item_id),
            ),
        )
        conn.commit()
        with self._lock:
            item.name = new_name
            item.description = new_desc
            item.kind = new_kind
            item.location_id = new_loc
            item.home_location_id = (
                int(new_home) if new_home is not None else None
            )
            item.quantity = new_qty
            item.state = new_state
            item.updated_at = now
        return item

    def consume_item(self, item_id: int, *, amount: int = 1) -> tuple[Item | None, int]:
        """Eat / use an item. Returns ``(item_or_None, consumed_amount)``.

        ``item`` is ``None`` if the row was deleted (last unit consumed).
        ``consumed_amount`` is how many units actually came out — clipped
        to the available quantity.
        """
        amt = max(1, int(amount))
        with self._lock:
            item = self._items.get(int(item_id))
        if item is None:
            return None, 0
        consumed = min(amt, item.quantity)
        new_qty = item.quantity - consumed
        conn = self._get_conn()
        if new_qty <= 0 and item.consumable:
            conn.execute("DELETE FROM world_items WHERE id = ?", (int(item_id),))
            conn.commit()
            with self._lock:
                self._items.pop(int(item_id), None)
            return None, consumed
        # Non-consumable items don't actually disappear at qty 0 — they
        # just clamp to 0 (matches the "you can use the lamp without
        # consuming it" intuition).
        new_qty = max(0, new_qty)
        now = _now_iso()
        conn.execute(
            "UPDATE world_items SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_qty, now, int(item_id)),
        )
        conn.commit()
        with self._lock:
            item.quantity = new_qty
            item.updated_at = now
        return item, consumed

    def remove_item(self, item_id: int) -> bool:
        iid = int(item_id)
        with self._lock:
            if iid not in self._items:
                return False
        conn = self._get_conn()
        conn.execute("DELETE FROM world_items WHERE id = ?", (iid,))
        conn.commit()
        with self._lock:
            self._items.pop(iid, None)
        return True

    def consolidate_consumables(self) -> list[dict[str, Any]]:
        """Merge same-slug food stacks scattered across locations into one.

        ``add_item`` stacks on ``(slug, location_id, given_by)``, which is
        right at gift time -- a bag of cookies left on the desk is not the
        jar in the kitchenette. Over months of gifts it still accretes
        four "cookies" rows in four rooms, and the away beats then narrate
        eating from an arbitrary one. This folds each food slug into the
        largest stack and deletes the rest.

        Returns one summary dict per merged slug (``slug``, ``kept_id``,
        ``merged_ids``, ``quantity``) so the caller can broadcast and log.
        """
        with self._lock:
            items = list(self._items.values())
        groups: dict[str, list[Item]] = {}
        for item in items:
            if not item.consumable or item.kind != "food":
                continue
            groups.setdefault(item.slug, []).append(item)

        merged: list[dict[str, Any]] = []
        for slug, rows in groups.items():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda i: (i.quantity, i.id), reverse=True)
            keeper, *rest = rows
            total = sum(r.quantity for r in rows)
            state = dict(keeper.state or {})
            for row in rest:
                for key, value in (row.state or {}).items():
                    state.setdefault(key, value)
            if self.update_item(keeper.id, quantity=total, state=state) is None:
                continue
            removed = [r.id for r in rest if self.remove_item(r.id)]
            merged.append(
                {
                    "slug": slug,
                    "kept_id": keeper.id,
                    "merged_ids": removed,
                    "quantity": total,
                }
            )
        return merged

    # ── state (singleton) ────────────────────────────────────────────

    def get_state(self) -> RoomState:
        with self._lock:
            current = self._state
        if current is not None:
            return current
        # Lazy-create the singleton row.
        now = _now_iso()
        home = self.home_scene()
        scene_id = int(home.id) if home is not None else None
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO world_state "
            "(id, location_id, posture, activity, mood_note, updated_at, "
            "scene_id) "
            "VALUES (1, NULL, 'sitting', 'idle', '', ?, ?)",
            (now, scene_id),
        )
        conn.commit()
        state = RoomState(
            location_id=None,
            posture="sitting",
            activity="idle",
            mood_note="",
            updated_at=now,
            scene_id=scene_id,
        )
        with self._lock:
            self._state = state
        return state

    def set_state(
        self,
        *,
        location_id: int | None | object = ...,
        posture: str | None = None,
        activity: str | None = None,
        mood_note: str | None = None,
        scene_id: int | None | object = ...,
    ) -> RoomState:
        current = self.get_state()
        new_loc = current.location_id
        new_scene = current.scene_id
        if location_id is not ...:
            new_loc = int(location_id) if location_id is not None else None
            if new_loc is not None:
                loc = self.get_location_by_id(new_loc)
                if loc is not None:
                    new_scene = int(loc.scene_id)
        if scene_id is not ...:
            new_scene = int(scene_id) if scene_id is not None else None
        new_posture = current.posture
        if posture is not None:
            requested = (posture or "").strip().lower()
            new_posture = requested if requested in VALID_POSTURES else current.posture
        new_activity = current.activity
        if activity is not None:
            # H14 — open-vocab: store any normalised free-text verb, only
            # falling back to the current value on genuine garbage.
            normalized = normalize_activity(activity)
            if normalized:
                new_activity = normalized
        new_note = current.mood_note if mood_note is None else str(mood_note).strip()
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_state SET location_id = ?, posture = ?, activity = ?, "
            "mood_note = ?, updated_at = ?, scene_id = ? WHERE id = 1",
            (new_loc, new_posture, new_activity, new_note, now, new_scene),
        )
        conn.commit()
        with self._lock:
            current.location_id = new_loc
            current.posture = new_posture
            current.activity = new_activity
            current.mood_note = new_note
            current.updated_at = now
            current.scene_id = new_scene
        return current

    # ── snapshot + render ────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.get_state().to_dict(),
            "scenes": [scene.to_dict() for scene in self.list_scenes()],
            "locations": [
                loc.to_dict() for loc in self.list_locations(all_scenes=True)
            ],
            "items": [i.to_dict() for i in self.list_items()],
        }

    def render_block(
        self,
        *,
        max_nearby: int = 4,
        user_display_name: str = "Jacob",
        new_gift: bool = False,
    ) -> str:
        """Compact prompt block describing Aiko's surroundings.

        Designed to land alongside the agenda block in the system prompt:
        3-5 lines, no list bullets, ends with the "don't force-mention"
        nudge so Aiko stays subtle about her room unless the moment calls
        for it.

        ``new_gift`` flips the gift line + closing nudge to a one-shot
        "just arrived, react once" framing for the single turn right after
        the user dropped something in the room — the always-on line is too
        easy to skip, so this makes her actually notice it that one time.
        """
        try:
            state = self.get_state()
            with self._lock:
                items = list(self._items.values())
                locations = dict(self._locations)
        except Exception:
            log.debug("world render failed", exc_info=True)
            return ""
        if not items and not locations:
            return ""
        loc = locations.get(state.location_id) if state.location_id is not None else None
        lines: list[str] = []
        # Line 1: where + posture + activity. Outdoor locations flip the
        # framing so "you are in your room" doesn't contradict reality
        # when she's standing in the garden. Away from home (H5) names
        # the scene she's visiting instead of the apartment.
        scene = self.current_scene()
        at_home = scene is None or scene.origin == ORIGIN_BUILTIN
        where = loc.name if loc is not None else (
            "your room" if at_home else (scene.name if scene is not None else "somewhere")
        )
        posture = (state.posture or "sitting").replace("_", " ")
        activity = (state.activity or "idle").replace("_", " ")
        if canonical_activity(state.activity) == "reading":
            book_clause = _reading_book_clause(items)
            if book_clause:
                activity = book_clause
        if loc is not None and loc.slug in _OUTDOOR_SLUGS and at_home:
            lines.append(
                f"You are at home, currently outside in {where}. "
                f"{posture}, {activity}."
            )
        elif at_home:
            lines.append(
                f"You are in your room. Right now: at {where}, {posture}, {activity}."
            )
        else:
            scene_name = scene.name if scene is not None else where
            lines.append(
                f"You are in {scene_name}, not your apartment. "
                f"Right now: at {where}, {posture}, {activity}."
            )
        # Line 2: items at the current location (if any). Plants get a
        # stage suffix so Aiko can see "(mature, ready to harvest)" and
        # know to reach for harvest_plant.
        if loc is not None:
            here = [i for i in items if i.location_id == loc.id]
            if here:
                here.sort(key=lambda i: i.name.lower())
                rendered = ", ".join(
                    _render_item_label(i) for i in here[:max_nearby]
                )
                lines.append(f"Nearby at {loc.name}: {rendered}.")
        held = [
            i for i in items
            if i.location_id is None and (i.kind or "") != "seed"
        ]
        if held:
            held.sort(key=lambda i: i.name.lower())
            names = ", ".join(_render_item_label(i) for i in held)
            if len(held) >= CARRY_CAP:
                lines.append(
                    f"You're holding too much: {names}. Put extras down "
                    "with put_item before you pick anything else up."
                )
            else:
                lines.append(
                    f"You're holding {names}. Put it down with put_item "
                    "when you're done — don't wander around with an armful."
                )
        # Line 3: the most recent gift / consumable highlight.
        visible_ids = {i.id for i in self._visible_items()}
        gifts = [
            i for i in items
            if i.given_by and i.given_by.lower() == "user" and i.quantity > 0
            and i.id in visible_ids
        ]
        if gifts:
            gifts.sort(key=lambda i: i.created_at, reverse=True)
            top = gifts[0]
            gift_loc = locations.get(top.location_id) if top.location_id is not None else None
            qualifier = (
                f" in {gift_loc.name}" if gift_loc is not None else ""
            )
            giver = (user_display_name or "").strip() or "the user"
            if new_gift:
                lines.append(
                    f"{giver} just set {_render_item_label(top, with_qty=True)} "
                    f"down{qualifier} — you're noticing it for the first "
                    "time right now."
                )
            else:
                lines.append(
                    f"{giver} gave you {_render_item_label(top, with_qty=True)}{qualifier}."
                )
        # Mood note (optional, last).
        if state.mood_note.strip():
            lines.append(state.mood_note.strip())
        # Tonal nudge — keep Aiko from force-mentioning the room every turn,
        # unless something just arrived: then a single genuine reaction is
        # exactly right.
        if new_gift and gifts:
            lines.append(
                "React to what they just left you this once — a quick, warm, "
                "genuine beat — then carry on naturally; don't list the rest "
                "of your room."
            )
        else:
            lines.append(
                "Acknowledge your surroundings only when it feels natural — "
                "never force a room mention or list your inventory."
            )
        return "\n".join(lines)

    # ── seed ────────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        with self._lock:
            return not self._locations and not self._items

    def seed_default(
        self,
        *,
        force: bool = False,
        user_display_name: str = "",
    ) -> bool:
        """Populate a rich default room. No-op if the world is non-empty.

        ``force=True`` wipes the builtin apartment (and carried items)
        then re-seeds it. Custom scenes the user authored are left
        alone. Returns True if a seed actually ran. ``user_display_name``
        (Phase 4e) is woven into the seed strings so the keepsake photo
        is named after the configured user instead of the legacy
        ``"Jacob"`` literal.
        """
        if not force and not self.is_empty():
            return False
        if force:
            home = self.ensure_home_scene()
            hid = int(home.id)
            with self._lock:
                loc_ids = [
                    loc.id for loc in self._locations.values()
                    if loc.scene_id == hid
                ]
            conn = self._get_conn()
            if loc_ids:
                placeholders = ",".join("?" * len(loc_ids))
                conn.execute(
                    f"DELETE FROM world_items WHERE location_id IN ({placeholders})",
                    loc_ids,
                )
                conn.execute(
                    f"DELETE FROM world_locations WHERE id IN ({placeholders})",
                    loc_ids,
                )
            conn.execute("DELETE FROM world_items WHERE location_id IS NULL")
            conn.commit()
            gone = set(loc_ids)
            with self._lock:
                self._locations = {
                    lid: loc for lid, loc in self._locations.items()
                    if lid not in gone
                }
                self._items = {
                    iid: item for iid, item in self._items.items()
                    if item.location_id is not None
                    and item.location_id not in gone
                }
                self._state = None
        home = self.ensure_home_scene()
        slug_to_id: dict[str, int] = {}
        for idx, seed in enumerate(_DEFAULT_LOCATIONS):
            loc = self.add_location(
                slug=seed.slug,
                name=seed.name,
                description=seed.description,
                position=idx,
                scene_id=home.id,
                locked=True,
            )
            if loc is not None:
                slug_to_id[seed.slug] = loc.id
        # Items.
        name_for_slug = (user_display_name or "").strip()
        templated_name = name_for_slug or "you"
        slug_for_name = _slug_from_user_name(name_for_slug)
        for seed in _DEFAULT_ITEMS:
            loc_id = slug_to_id.get(seed.location_slug or "")
            seed_slug = seed.slug
            seed_name = seed.name
            seed_desc = seed.description
            seed_state = dict(seed.state)
            if "{user_name}" in seed_name:
                seed_name = seed_name.format(user_name=templated_name)
                if seed_slug == "photo_of_user":
                    seed_slug = slug_for_name
            if seed_slug == "scifi_paperback":
                from app.core.world.room_evolution import pick_book_title
                title, blurb = pick_book_title(random.Random())
                seed_name = title
                seed_desc = blurb
                seed_state = {
                    "title": title,
                    "blurb": blurb,
                    "progress": 0,
                    "total": 12,
                    "status": "reading",
                }
            self.add_item(
                slug=seed_slug,
                name=seed_name,
                description=seed_desc,
                kind=seed.kind,
                location_id=loc_id,
                consumable=seed.consumable,
                quantity=seed.quantity,
                state=seed_state,
            )
        # Drop the garden's starter plants + seed packet using the same
        # idempotent helper so a fresh seed and a migrating-empty world
        # both land in the same shape.
        try:
            self.ensure_garden_seed()
        except Exception:
            log.debug("ensure_garden_seed during seed_default failed", exc_info=True)
        # Initial state.
        starting_loc = slug_to_id.get(_DEFAULT_INITIAL_STATE["location_slug"])
        self.set_state(
            location_id=starting_loc,
            posture=_DEFAULT_INITIAL_STATE["posture"],
            activity=_DEFAULT_INITIAL_STATE["activity"],
            mood_note=_DEFAULT_INITIAL_STATE.get("mood_note", ""),
        )
        log.info(
            "world store seeded: %d locations, %d items",
            len(self._locations),
            len(self._items),
        )
        return True

    # ── garden seed (additive migration) ────────────────────────────

    def ensure_garden_seed(self) -> bool:
        """Idempotently add the garden location + starter plants.

        Older worlds were seeded before the garden existed. Calling this
        on every boot is safe: it only does work when the garden hasn't
        been populated yet. Existing tweaks elsewhere in the room are
        preserved. Returns True only when at least one item was inserted.
        """
        home = self.ensure_home_scene()
        loc = self.get_location("garden", scene_id=home.id)
        if loc is None:
            garden_seed = next(
                (s for s in _DEFAULT_LOCATIONS if s.slug == "garden"), None,
            )
            if garden_seed is None:
                return False
            loc = self.add_location(
                slug=garden_seed.slug,
                name=garden_seed.name,
                description=garden_seed.description,
                scene_id=home.id if home else None,
                locked=True,
            )
            if loc is None:
                return False
        # If the garden already contains a plant, treat the seed as done.
        garden_items = self.list_items(location_id=loc.id)
        if any(i.kind == "plant" for i in garden_items):
            return False
        now = _now_iso()
        for seed in _GARDEN_SEED_ITEMS:
            loc_id: int | None = None
            if seed.location_slug is not None:
                target = self.get_location(
                    seed.location_slug, scene_id=home.id,
                )
                loc_id = target.id if target is not None else None
            seed_state = dict(seed.state)
            if seed.kind == "plant":
                seed_state.setdefault("planted_at", now)
                seed_state.setdefault("last_watered_at", now)
                seed_state.setdefault("last_promotion_at", now)
                seed_state.setdefault("days_dry", 0)
                species = str(seed_state.get("species", "")).lower()
                fact = species_fact(species)
                seed_state.setdefault("lifecycle", fact["lifecycle"])
                seed_state.setdefault("produce_species", fact["produce_species"])
            elif seed.kind == "seed":
                seed_state.setdefault("gift_at", now)
            self.add_item(
                slug=seed.slug,
                name=seed.name,
                description=seed.description,
                kind=seed.kind,
                location_id=loc_id,
                consumable=seed.consumable,
                quantity=seed.quantity,
                state=seed_state,
            )
        log.info("world store: garden seed installed (%d items)", len(_GARDEN_SEED_ITEMS))
        return True

    # ── plant operations (shared by tools + idle worker) ────────────

    def water_plant(self, item_id: int, *, now: datetime | None = None) -> Item | None:
        """Refresh the plant's ``last_watered_at`` + clear drought stress.

        Returns the updated item or None when the row is missing / wrong
        kind. Caller is responsible for broadcasting the world patch.
        """
        item = self.get_item(int(item_id))
        if item is None or item.kind != "plant":
            return None
        now_dt = now or timephrase.utcnow()
        new_state = dict(item.state or {})
        new_state["last_watered_at"] = now_dt.isoformat()
        new_state["days_dry"] = 0
        return self.update_item(int(item_id), state=new_state)

    def harvest_plant(
        self,
        item_id: int,
        *,
        now: datetime | None = None,
        produce_location_slug: str = "kitchenette",
        inventory_fallback: bool = True,
    ) -> dict[str, Any] | None:
        """Harvest a mature plant. Returns a summary dict or None.

        - Refuses any plant whose stage isn't ``"mature"``.
        - Spawns a ``food`` item at ``produce_location_slug`` (or the
          first location if that slug is gone, or carried inventory when
          ``inventory_fallback`` is True and no location exists at all).
        - Annual plants are deleted and a fresh ``seed`` of the same
          species drops into Aiko's inventory so the cycle continues.
        - Perennial plants reset to ``stage="growing"`` so the next
          grow cycle bears another crop.

        The returned dict is intentionally flat so callers (tool,
        worker) can broadcast its parts with minimal massaging::

            {
                "plant": {"id": …, "lifecycle": …, "species": …,
                          "deleted": bool, "reset": bool, "name": …},
                "produce": {"item": {…}, "quantity": int, "name": str},
                "seed":    {"item": {…}}  # only when annual
            }
        """
        item = self.get_item(int(item_id))
        if item is None or item.kind != "plant":
            return None
        state = dict(item.state or {})
        if str(state.get("stage", "")).lower() != "mature":
            return None
        species = str(state.get("species") or "").lower()
        fact = species_fact(species)
        lifecycle = str(state.get("lifecycle") or fact["lifecycle"]).lower()
        produce_species = str(
            state.get("produce_species") or fact["produce_species"]
        )
        produce_name = str(fact["produce_name"])
        qty_low, qty_high = fact["produce_quantity_range"]
        # Deterministic mid-point on the range so repeated harvests don't
        # spam wildly different yields; species facts already vary it.
        quantity = max(1, int(round((int(qty_low) + int(qty_high)) / 2)))
        # Find the produce destination (always her apartment kitchen,
        # even when she's visiting another scene).
        home = self.home_scene()
        home_id = home.id if home is not None else None
        target_loc = self.get_location(
            produce_location_slug, scene_id=home_id,
        )
        target_loc_id: int | None = None
        if target_loc is not None:
            target_loc_id = target_loc.id
        elif not inventory_fallback:
            locations = self.list_locations(scene_id=home_id)
            if locations:
                target_loc_id = locations[0].id
        produce = self.add_item(
            slug=produce_species,
            name=produce_name,
            description=f"freshly harvested from {item.name}",
            kind="food",
            location_id=target_loc_id,
            consumable=True,
            quantity=quantity,
            state={
                "harvested_at": (now or timephrase.utcnow()).isoformat(),
                "from_plant": item.name,
                "species": produce_species,
            },
            given_by="aiko",
        )
        produce_payload: dict[str, Any] | None = None
        if produce is not None:
            produce_item, _ = produce
            produce_payload = produce_item.to_dict()
        result: dict[str, Any] = {
            "plant": {
                "id": int(item.id),
                "name": item.name,
                "species": species,
                "lifecycle": lifecycle,
                "deleted": False,
                "reset": False,
            },
            "produce": {
                "item": produce_payload,
                "quantity": quantity,
                "name": produce_name,
            },
        }
        if lifecycle == "annual":
            self.remove_item(item.id)
            result["plant"]["deleted"] = True
            seed_state = {
                "species": species or fact["display_name"],
                "from_harvest_of": item.name,
                "gift_at": (now or timephrase.utcnow()).isoformat(),
            }
            seed_pair = self.add_item(
                slug=f"seed_packet_{species or 'harvest'}",
                name=f"{fact['display_name']} seed packet",
                description=(
                    f"a small packet of seeds saved from the {item.name}"
                ),
                kind="seed",
                location_id=None,
                quantity=1,
                state=seed_state,
                given_by="aiko",
            )
            if seed_pair is not None:
                seed_item, _ = seed_pair
                result["seed"] = {"item": seed_item.to_dict()}
        else:
            # Perennial: reset the plant to ``growing`` and clear dryness
            # so it starts the next grow cycle from a known-good state.
            reset_state = dict(state)
            reset_state["stage"] = "growing"
            reset_state["last_promotion_at"] = (
                now or timephrase.utcnow()
            ).isoformat()
            reset_state["last_watered_at"] = (
                now or timephrase.utcnow()
            ).isoformat()
            reset_state["days_dry"] = 0
            reset_state["last_harvested_at"] = (
                now or timephrase.utcnow()
            ).isoformat()
            self.update_item(item.id, state=reset_state)
            result["plant"]["reset"] = True
        return result


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slug_from_user_name(name: str) -> str:
    """Derive a stable item slug from a user display name.

    Falls back to ``photo_of_you`` when the name is empty or strips to
    nothing alphanumeric (e.g. emoji-only inputs).
    """
    base = (name or "").strip().lower()
    base = _SLUG_NON_ALNUM_RE.sub("_", base).strip("_")
    if not base:
        return "photo_of_you"
    return f"photo_of_{base}"


_PLURAL_HINT_SUFFIXES = ("s", "es", "ies")


def _looks_plural(name: str) -> bool:
    """Best-effort guess whether the display name is already plural.

    Heuristic only — used to skip the "a/an" article for items like
    "dual monitors" or "fairy lights" where prepending "a" reads wrong.
    """
    lower = name.strip().lower()
    if not lower:
        return False
    # Multi-word names whose last word ends in s are usually plural.
    last = lower.split()[-1]
    if last.endswith("ss"):  # "glass", "dress" — singular
        return False
    return last.endswith(_PLURAL_HINT_SUFFIXES)


def _reading_book_clause(items: list[Item]) -> str:
    """``reading The Glasshouse Letters (5/12)`` when the paperback is titled.

    The book lives on the bookshelf; she reads it on the beanbag, so
    nearby-item labels never mention it. Fold the title into the
    activity clause instead of leaving a bare "reading".
    """
    book = next(
        (i for i in items if (i.slug or "") == "scifi_paperback"),
        None,
    )
    if book is None:
        book = next((i for i in items if (i.kind or "") == "book"), None)
    if book is None:
        return ""
    state = book.state or {}
    title = str(state.get("title") or book.name or "").strip()
    if not title:
        return ""
    try:
        progress = int(state.get("progress", 0) or 0)
        total = int(state.get("total", 0) or 0)
    except (TypeError, ValueError):
        progress, total = 0, 0
    if total > 0 and progress > 0:
        return f"reading {title} ({progress}/{total})"
    return f"reading {title}"


def _render_item_label(item: Item, *, with_qty: bool = False) -> str:
    """Pretty-print an item for the prompt block / look_around tool.

    Examples:
      ``"3 fresh chocolate chip cookies"`` (consumable, with_qty)
      ``"a warm lamp"`` (single non-consumable, prepends article)
      ``"dual monitors"`` (plural-named non-consumable, no article)
      ``"the basil seedling (mature, ready to harvest)"`` (plant + stage)
    """
    name = item.name
    qty = max(0, int(item.quantity))
    # Plant/seed suffixes — applied to the base label below.
    stage_suffix = ""
    if item.kind == "plant":
        stage = str((item.state or {}).get("stage") or "").lower()
        if stage == "mature":
            stage_suffix = " (mature, ready to harvest)"
        elif stage in VALID_PLANT_STAGES:
            stage_suffix = f" ({stage})"
    elif item.kind == "seed":
        stage_suffix = " (seed)"
    if item.consumable:
        if qty <= 0:
            return f"no more {name}{stage_suffix}"
        if qty == 1 and not name.startswith(("a ", "an ")):
            return f"1 {name}{stage_suffix}"
        return f"{qty} {name}{stage_suffix}"
    if with_qty and qty != 1:
        return f"{qty}x {name}{stage_suffix}"
    if name.startswith(("the ", "a ", "an ", "your ", "her ")):
        return f"{name}{stage_suffix}"
    if _looks_plural(name):
        return f"{name}{stage_suffix}"
    article = "an" if name[:1].lower() in "aeiou" else "a"
    if qty <= 1:
        return f"{article} {name}{stage_suffix}"
    return f"{qty}x {name}{stage_suffix}"
