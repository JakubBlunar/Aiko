"""K24 -- Sensory anchoring layer.

Per-turn cadence layer that occasionally surfaces a "small physical
beat available" cue so Aiko can substitute a sensory detail for an
emotional statement -- "I'm pulling the blanket tighter while you
talk about it" instead of "I hear you". Pure mechanics; no LLM call,
no embedder, no DB. State is in-memory on the controller and resets
cleanly across restarts (worst case = one extra beat in the first
quiet window post-boot).

Posture: permission, not prescription. The cue suggests an *item*
+ *verb class* (e.g. "pick it up", "wrap it tighter"); Aiko's own
voice picks the actual word. The persona block's "Small physical
beats" section teaches the LLM to use it only when a sensory detail
would actually land -- chronic firing reads as performance, not
presence.

Design choices baked in:

- **Adaptive per-arc cadence.** ``_ARC_WEIGHTS`` keys off the live
  ``ArcStore`` arc: ``support`` / ``reflection`` get the highest beat
  probability + shortest cooldown; ``planning`` gets near-silence.
  The dict is hardcoded -- not a setting -- so the operator can't
  accidentally invert it.
- **Static posture-kind compatibility matrix.** ``_POSTURE_KIND_VERBS``
  encodes posture x ``Item.kind`` physics only (can her body reach
  this category of object). Empty tuples mean "incompatible -- drop
  silently". The runtime *also* gates on `WorldStore.list_items`
  (is the item actually in the room?); the table doesn't know or
  care about per-instance reality.
- **Activity is intentionally NOT gated** (deferred decision). The
  persona rule "use it only if it lands" handles the redundancy edge
  cases (``snacking`` + ``food`` cue) until we see enough fired
  beats to know whether it actually feels wrong.
- **No-repeat ring** (``_recent_slugs``) so the same beat doesn't
  fire twice in a row even if the dice cooperate.
- **K24 survives K16 ``replace`` mode.** The grounding paragraph
  says "you're sitting at the desk"; K24 says "you're holding the
  tea pot." Additive, not redundant -- the assembler does NOT add
  this provider to the K16 suppression matrix.

See :func:`pick_beat` for the selector and
:class:`SensoryAnchorCadence` for the per-controller state holder.
"""
from __future__ import annotations

import collections
import logging
import random as _random_module
from dataclasses import dataclass
from typing import Any, Iterable


log = logging.getLogger("app.sensory_anchor")


# ── Arc-weighted cadence ────────────────────────────────────────────


# Per-arc cadence. Value = (probability, min_cooldown_turns).
# Probability is rolled *after* the cooldown check passes; if both
# gates pass we look for an eligible item. Cooldown is armed on a
# successful fire so back-to-back beats can't happen even when the
# dice cooperate. Keyed by every value in
# ``app.core.conversation_arc.VALID_ARCS``.
_ARC_WEIGHTS: dict[str, tuple[float, int]] = {
    "support": (0.45, 4),
    "reflection": (0.45, 4),
    "casual_check_in": (0.25, 6),
    "playful": (0.25, 6),
    "silly": (0.10, 8),
    "planning": (0.05, 12),
}

# Fallback for any unknown / missing arc -- same cadence as
# ``casual_check_in`` so a misconfiguration doesn't accidentally
# silence the beat entirely.
_DEFAULT_ARC_WEIGHT: tuple[float, int] = (0.20, 8)


# ── Posture x kind compatibility ────────────────────────────────────


# Verb classes are slugs (``picking_up``, ``wrap_in``, ...); the
# render layer prettifies them. The persona block teaches Aiko to
# use them as hints, not exact wording -- her voice picks the
# actual word.
#
# Empty tuples = combination dropped silently (no reach / no
# affordance). ``furniture`` is excluded across the board -- the
# room *is* the furniture; you don't pick up a bed.
_POSTURE_KIND_VERBS: dict[tuple[str, str], tuple[str, ...]] = {
    # ── lying ────────────────────────────────────────────────
    ("lying", "food"): ("nibbling", "picking_up"),
    ("lying", "book"): ("thumbing_through", "setting_down"),
    ("lying", "gadget"): (),
    ("lying", "furniture"): (),
    ("lying", "toy"): ("hugging", "tucked_with"),
    ("lying", "keepsake"): ("holding", "tucked_with"),
    ("lying", "decor"): ("wrapping_in", "pulling_closer"),
    ("lying", "plant"): (),
    ("lying", "seed"): (),
    ("lying", "other"): ("holding",),

    # ── sitting ──────────────────────────────────────────────
    ("sitting", "food"): ("picking_up", "nibbling", "setting_down"),
    ("sitting", "book"): ("thumbing_through", "setting_down", "picking_up"),
    ("sitting", "gadget"): ("tapping", "setting_down", "picking_up"),
    ("sitting", "furniture"): (),
    ("sitting", "toy"): ("holding", "setting_down"),
    ("sitting", "keepsake"): ("holding", "setting_down"),
    ("sitting", "decor"): ("pulling_closer", "leaning_toward"),
    ("sitting", "plant"): ("looking_at",),
    ("sitting", "seed"): ("rolling_in_hands", "setting_down"),
    ("sitting", "other"): ("holding", "setting_down"),

    # ── standing ─────────────────────────────────────────────
    ("standing", "food"): ("picking_up", "nibbling"),
    ("standing", "book"): ("picking_up", "setting_down"),
    ("standing", "gadget"): ("picking_up", "setting_down"),
    ("standing", "furniture"): ("leaning_against",),
    ("standing", "toy"): ("picking_up",),
    ("standing", "keepsake"): ("picking_up", "looking_at"),
    ("standing", "decor"): ("leaning_toward", "straightening"),
    ("standing", "plant"): ("watering", "looking_at"),
    ("standing", "seed"): ("picking_up", "rolling_in_hands"),
    ("standing", "other"): ("picking_up",),

    # ── curled_up ────────────────────────────────────────────
    ("curled_up", "food"): ("nibbling",),
    ("curled_up", "book"): ("thumbing_through",),
    ("curled_up", "gadget"): (),
    ("curled_up", "furniture"): (),
    ("curled_up", "toy"): ("hugging", "burrowing_into", "tucked_with"),
    ("curled_up", "keepsake"): ("holding",),
    ("curled_up", "decor"): ("wrapped_in", "burrowing_into"),
    ("curled_up", "plant"): (),
    ("curled_up", "seed"): (),
    ("curled_up", "other"): ("holding",),

    # ── leaning ──────────────────────────────────────────────
    ("leaning", "food"): ("picking_up",),
    ("leaning", "book"): ("picking_up", "setting_down"),
    ("leaning", "gadget"): ("tapping", "picking_up"),
    ("leaning", "furniture"): ("leaning_against",),
    ("leaning", "toy"): (),
    ("leaning", "keepsake"): ("looking_at",),
    ("leaning", "decor"): ("leaning_toward",),
    ("leaning", "plant"): ("looking_at", "watering"),
    ("leaning", "seed"): (),
    ("leaning", "other"): ("looking_at",),
}


# Verb class slug → human-readable hint phrase. The render layer
# emits ONE of these as a parenthetical hint so Aiko's voice has
# a concrete suggestion to anchor on.
_VERB_CLASS_HINT: dict[str, str] = {
    "picking_up": "pick it up",
    "setting_down": "set it down",
    "nibbling": "take a small bite",
    "thumbing_through": "thumb through it",
    "holding": "hold it",
    "tucked_with": "have it tucked beside you",
    "hugging": "hug it close",
    "wrapping_in": "wrap yourself in it",
    "pulling_closer": "pull it closer",
    "tapping": "tap on it",
    "leaning_toward": "lean toward it",
    "leaning_against": "lean against it",
    "looking_at": "glance at it",
    "rolling_in_hands": "roll it in your hands",
    "watering": "give it a sip of water",
    "straightening": "straighten it absently",
    "burrowing_into": "burrow into it",
    "wrapped_in": "stay wrapped in it",
}


# Mark this dataclass mutable so an item slug doesn't accidentally
# become the slot key for an unrelated table later.
@dataclass(frozen=True, slots=True)
class SensoryBeat:
    """One pickable beat. Returned by :func:`pick_beat`, consumed
    by :func:`render_inner_life_block`."""

    item_slug: str
    item_name: str
    verb_class: str
    arc: str
    posture: str


# ── Pure selector ───────────────────────────────────────────────────


def pick_beat(
    *,
    posture: str,
    items: Iterable[Any],
    arc: str,
    recent_slugs: Iterable[str] = (),
    rng: _random_module.Random | None = None,
    max_window: int = 6,
) -> SensoryBeat | None:
    """Pure selector: return a :class:`SensoryBeat` or ``None``.

    Algorithm:

    1. Take up to ``max_window`` items from ``items`` (cap the
       candidate pool so a future world with 50 items doesn't
       blow up the weighted-sample step).
    2. Drop items whose slug is in ``recent_slugs`` (no-repeat
       ring).
    3. Drop items whose ``(posture, kind)`` lookup yields an empty
       verb-class tuple in :data:`_POSTURE_KIND_VERBS`.
    4. Weighted random pick by quantity (clamped to ``[1, 6]`` so
       8 cookies don't drown out 1 plush completely).
    5. Pick a verb class from the matched tuple uniformly at random.

    Returns ``None`` if no item survives the filters. ``rng`` is
    used for the two random draws; pass a seeded ``random.Random``
    in tests for determinism.
    """
    rng_local = rng if rng is not None else _random_module
    pool: list[Any] = []
    recent_set = {str(s) for s in (recent_slugs or ()) if s}
    posture_norm = (posture or "").strip().lower()
    if not posture_norm:
        return None

    # 1 + 2: window + no-repeat ring.
    for item in items:
        if len(pool) >= int(max_window):
            break
        slug = (getattr(item, "slug", "") or "").strip()
        if not slug:
            continue
        if slug in recent_set:
            continue
        pool.append(item)

    if not pool:
        return None

    # 3: posture-kind compatibility filter.
    eligible: list[tuple[Any, tuple[str, ...]]] = []
    for item in pool:
        kind = (getattr(item, "kind", "") or "").strip().lower()
        if not kind:
            continue
        verb_classes = _POSTURE_KIND_VERBS.get((posture_norm, kind), ())
        if not verb_classes:
            continue
        eligible.append((item, verb_classes))

    if not eligible:
        return None

    # 4: weighted pick by quantity (clamped). Items with a missing
    # / zero quantity get weight 1 -- a single decor blanket should
    # still be pickable even if the seed pipeline forgot to stamp
    # ``quantity``.
    weights: list[float] = []
    for item, _ in eligible:
        try:
            qty = max(0, int(getattr(item, "quantity", 1) or 1))
        except (TypeError, ValueError):
            qty = 1
        weights.append(float(min(6, max(1, qty))))
    try:
        chosen_idx = rng_local.choices(
            range(len(eligible)), weights=weights, k=1,
        )[0]
    except Exception:
        # ``random.choices`` raises if all weights are zero -- which
        # we already guard against, but fall back to a uniform pick
        # so a future bug in weighting doesn't kill the feature.
        chosen_idx = rng_local.randrange(len(eligible))

    item, verb_classes = eligible[chosen_idx]
    verb_class = rng_local.choice(list(verb_classes))

    return SensoryBeat(
        item_slug=str(getattr(item, "slug", "") or ""),
        item_name=str(getattr(item, "name", "") or ""),
        verb_class=verb_class,
        arc=(arc or "").strip().lower() or "casual_check_in",
        posture=posture_norm,
    )


# ── Render ──────────────────────────────────────────────────────────


def render_inner_life_block(
    beat: SensoryBeat | None,
    *,
    user_display_name: str = "the user",
) -> str:
    """Return the one-line cue, or ``""`` when ``beat`` is ``None``.

    The tone matches the persona's "Where you are right now (the
    grounding paragraph)" register: a permission, not a directive.
    The verb-class hint is *one* concrete gesture; Aiko's voice
    picks the actual word.
    """
    if beat is None:
        return ""
    hint = _VERB_CLASS_HINT.get(beat.verb_class, "touch it briefly")
    name = (beat.item_name or beat.item_slug or "").strip() or "it"
    user = (user_display_name or "").strip() or "the user"
    return (
        f"Small physical beat available: the {name} is right here. "
        f"If a body anchor would land naturally this reply, "
        f"you could {hint} -- otherwise let it pass. "
        f"One beat per reply, and never narrate the room as if "
        f"{user} can see it."
    )


# ── Cadence state holder ────────────────────────────────────────────


class SensoryAnchorCadence:
    """Per-controller state holder for K24.

    Holds the turn-counter cooldown plus the no-repeat ring. Same
    shape as :class:`app.core.novelty_detector.NoveltyDetector`'s
    ring of recent vectors -- pure in-memory, no DB. Not
    thread-safe; the prompt-assembly path calls :meth:`tick` on the
    turn thread.
    """

    def __init__(self, *, max_recent: int = 4) -> None:
        self._cooldown_remaining: int = 0
        self._recent_slugs: collections.deque[str] = collections.deque(
            maxlen=max(1, int(max_recent)),
        )
        # Introspection state -- exposed via :meth:`to_debug_dict`.
        self._last_arc_seen: str | None = None
        self._last_fired_slug: str | None = None
        self._last_fired_verb_class: str | None = None
        self._fire_count: int = 0
        self._tick_count: int = 0

    def tick(
        self,
        *,
        posture: str,
        items: Iterable[Any],
        arc: str,
        min_turn_gap: int = 4,
        probability_scale: float = 1.0,
        max_window: int = 6,
        rng: _random_module.Random | None = None,
    ) -> SensoryBeat | None:
        """One per-turn cadence step. Returns a beat or ``None``.

        - If ``_cooldown_remaining > 0``, decrement and return
          ``None`` (silent turn).
        - Otherwise look up arc weights, roll
          ``rng() < probability * probability_scale``.
        - On a successful roll, call :func:`pick_beat`; on a hit,
          arm the cooldown (max of arc-min and ``min_turn_gap``)
          and push the slug into the no-repeat ring.

        ``rng`` defaults to the module-level ``random`` so callers
        don't need to pass one in production; tests pass a seeded
        ``random.Random`` for determinism.
        """
        self._tick_count += 1
        self._last_arc_seen = (arc or "").strip().lower() or None

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return None

        rng_local = rng if rng is not None else _random_module
        arc_key = self._last_arc_seen or "casual_check_in"
        probability, arc_min_gap = _ARC_WEIGHTS.get(
            arc_key, _DEFAULT_ARC_WEIGHT,
        )
        scaled = float(probability) * max(0.0, float(probability_scale))
        # Clamp at 1.0 -- ``random.random()`` is in ``[0, 1)`` so a
        # scaled value above 1.0 just means "always pass the dice
        # gate", never "skip the gate entirely".
        scaled = min(1.0, scaled)

        if rng_local.random() >= scaled:
            return None

        beat = pick_beat(
            posture=posture,
            items=items,
            arc=arc_key,
            recent_slugs=tuple(self._recent_slugs),
            rng=rng_local,
            max_window=max_window,
        )
        if beat is None:
            return None

        cooldown = max(int(arc_min_gap), max(1, int(min_turn_gap)))
        self._cooldown_remaining = cooldown
        self._recent_slugs.append(beat.item_slug)
        self._last_fired_slug = beat.item_slug
        self._last_fired_verb_class = beat.verb_class
        self._fire_count += 1
        log.info(
            "sensory-anchor: fire arc=%s posture=%s item=%s verb=%s cooldown=%d",
            arc_key,
            beat.posture,
            beat.item_slug,
            beat.verb_class,
            cooldown,
        )
        return beat

    # ── Introspection ────────────────────────────────────────────

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "cooldown_remaining": int(self._cooldown_remaining),
            "recent_slugs": list(self._recent_slugs),
            "last_arc_seen": self._last_arc_seen,
            "last_fired_slug": self._last_fired_slug,
            "last_fired_verb_class": self._last_fired_verb_class,
            "fire_count": int(self._fire_count),
            "tick_count": int(self._tick_count),
        }

    def reset(self) -> None:
        """Clear all in-memory state. Used by the MCP debug
        ``reset_sensory_anchor`` tool."""
        self._cooldown_remaining = 0
        self._recent_slugs.clear()
        self._last_arc_seen = None
        self._last_fired_slug = None
        self._last_fired_verb_class = None
        self._fire_count = 0
        self._tick_count = 0


__all__ = [
    "SensoryAnchorCadence",
    "SensoryBeat",
    "pick_beat",
    "render_inner_life_block",
]
