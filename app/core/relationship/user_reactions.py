"""User-side reactions on Aiko's chat bubbles (K32 personality backlog).

Pure data layer for the 6-emoji reaction tray that hovers over Aiko's
bubbles in chat (and lives inline on the :class:`PersonaActionBanner`
in overlay mode). A tap on an emoji:

  1. persists the reaction on ``messages.reactions`` JSON,
  2. bumps relationship axes per the :data:`_DELTAS_USER_REACTION`
     table (small +closeness / +trust / +humor / +comfort per kind),
  3. arms a one-shot ``user_reactions`` inner-life cue so Aiko notices
     "Jacob just hearted that line" on her next turn,
  4. broadcasts ``message_reaction_updated`` over WS so both windows
     re-render the bubble's reaction strip.

This module owns the **taxonomy + axes-delta table + daily cap state
machine**. The axes write happens via
:meth:`RelationshipAxesUpdater.apply_user_reaction`; the persistence
and WS broadcast happen in :class:`SessionController.apply_user_reaction`.

Design choices:

- **Six kinds, one is signal-only**. ``surprise`` (🫢) reads as
  "huh, interesting" -- Aiko notices but it doesn't move the
  relationship. Kept in the tray so the user can register noticing
  without committing a tiny vote on the axes.
- **Small, named axis deltas**. Each kind moves at most 0.04 on
  any single axis -- half of the per-turn hard clamp in
  :data:`relationship_axes._MAX_DELTA`. The intent is that 4-5
  reactions across a session feels meaningful but doesn't pin
  closeness to +1 from clicks alone.
- **Daily cap on axis movement from reactions**. Without a cap the
  user could grind closeness to +1 by clicking the heart on every
  bubble; with the cap (default 0.15 / axis / day) the K32 path is
  a topping-up signal, not a replacement for actual interaction.
  Tracked via ``kv_meta`` under the per-day key (rotates at UTC
  midnight, same shape as :class:`TouchService`).
- **Soft enforcement on the cap**. When the daily cap is hit, the
  reaction still PERSISTS (UI keeps the click feedback) and the
  inner-life cue still ARMS (Aiko still notices), but the axes
  delta is suppressed. Tests pin this so a future "hard cap"
  variant has to be opt-in.

The pure module has no I/O beyond the ``ChatDatabase`` it persists
the daily-cap state through; the controller wiring lives in
[`session_controller.py`](../session/session_controller.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.core.relationship.user_reactions")


# ── kv_meta key ─────────────────────────────────────────────────────


# Single key under the ``aiko.*`` namespace. The JSON shape pivots
# on UTC date so the daily cap rolls without a separate worker.
KV_USER_REACTIONS_DAILY = "aiko.user_reactions_daily"


# ── Taxonomy ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReactionKind:
    """One entry in the reaction tray.

    ``kind`` is the canonical lowercase identifier stored in
    ``messages.reactions`` JSON and surfaced over WS. ``emoji`` is
    the glyph rendered on the tray button. ``label`` is the verbatim
    phrase the inner-life cue uses ("just hearted your reply", "just
    laughed at your reply") -- chosen to slot into a one-line nudge
    without needing post-processing.
    """

    kind: str
    emoji: str
    label: str


# Six entries, ordered by how often they're likely to be used (most
# casual first). The order matters only for the diagnostic dump in
# :func:`reactions_metadata` and for the canonical tray rendering;
# everything else looks up by kind.
REACTION_KINDS: tuple[str, ...] = (
    "heart",
    "hug",
    "laugh",
    "thumbs",
    "rose",
    "grateful",
    "blush",
    "eyeroll",
    "moved",
    "surprise",
)


_REACTION_KINDS: dict[str, ReactionKind] = {
    "heart": ReactionKind(
        kind="heart",
        emoji="💛",
        label="just hearted your reply",
    ),
    "hug": ReactionKind(
        kind="hug",
        emoji="🫂",
        label="just hugged you back",
    ),
    "laugh": ReactionKind(
        kind="laugh",
        emoji="😂",
        label="just laughed at your reply",
    ),
    "thumbs": ReactionKind(
        kind="thumbs",
        emoji="👍",
        label="just thumbs-upped your reply",
    ),
    "rose": ReactionKind(
        kind="rose",
        emoji="🌹",
        label="just sent you a rose",
    ),
    "grateful": ReactionKind(
        kind="grateful",
        emoji="🙏",
        label="just thanked you for that",
    ),
    "blush": ReactionKind(
        kind="blush",
        emoji="🥰",
        label="just melted a little at your reply",
    ),
    "eyeroll": ReactionKind(
        kind="eyeroll",
        emoji="🙄",
        label="just playfully rolled their eyes at you",
    ),
    "moved": ReactionKind(
        kind="moved",
        emoji="🥺",
        label="just got a little emotional at your reply",
    ),
    "surprise": ReactionKind(
        kind="surprise",
        emoji="🫢",
        label="just reacted with a small startled noise",
    ),
}


# Per-axis deltas, indexed by kind. ``surprise`` is intentionally
# empty -- it's a signal-only reaction that arms the inner-life
# cue but doesn't move the relationship. Each entry maps axis name
# to a tiny positive nudge; all values are well under the per-turn
# clamp in :data:`relationship_axes._MAX_DELTA` so reactions
# can never blow past 0.04 on any single axis in one click.
_DELTAS_USER_REACTION: dict[str, dict[str, float]] = {
    "heart": {"closeness": 0.03},
    "hug": {"closeness": 0.04, "trust": 0.02, "comfort": 0.02},
    "laugh": {"humor": 0.04, "closeness": 0.01},
    "thumbs": {"trust": 0.03},
    "rose": {"closeness": 0.04, "comfort": 0.02},
    "grateful": {"closeness": 0.02, "trust": 0.03},
    "blush": {"closeness": 0.04, "comfort": 0.02},
    "eyeroll": {"humor": 0.03, "closeness": 0.01},
    "moved": {"comfort": 0.03, "trust": 0.02, "closeness": 0.01},
    "surprise": {},
}


# Per-reaction soft cap. Any single ``apply_user_reaction`` call
# saturates at ``±_PER_REACTION_SOFT_CAP`` on each axis even if the
# delta table is misconfigured. Lower than the per-turn
# ``_MAX_DELTA`` (0.08) because a single click should feel
# *quieter* than a full reaction-tag stack at LLM time.
_PER_REACTION_SOFT_CAP = 0.04


def get_reaction_kind(kind: str) -> ReactionKind | None:
    if not kind:
        return None
    return _REACTION_KINDS.get(str(kind).strip().lower())


def is_valid_kind(kind: str) -> bool:
    return get_reaction_kind(kind) is not None


def reactions_metadata() -> tuple[dict[str, Any], ...]:
    """JSON-friendly taxonomy snapshot for MCP debugging + tests.

    The returned tuple stays in :data:`REACTION_KINDS` order; each
    entry includes the delta table so the MCP debug tool can show
    "what does a heart click do?" without re-reading the source.
    """
    return tuple(
        {
            "kind": k,
            "emoji": _REACTION_KINDS[k].emoji,
            "label": _REACTION_KINDS[k].label,
            "deltas": dict(_DELTAS_USER_REACTION.get(k, {})),
        }
        for k in REACTION_KINDS
    )


def compute_deltas(
    kind: str,
    *,
    soft_cap: float = _PER_REACTION_SOFT_CAP,
) -> dict[str, float]:
    """Return clamped axis deltas for a single reaction click.

    Soft-caps each value at ``±soft_cap`` so a typo in the table
    can't blow past the design intent. ``surprise`` and unknown
    kinds return an empty dict.
    """
    raw = _DELTAS_USER_REACTION.get(str(kind).strip().lower())
    if not raw:
        return {}
    cap = max(0.0, float(soft_cap))
    return {
        axis: max(-cap, min(cap, float(value)))
        for axis, value in raw.items()
    }


# ── Daily-cap state machine ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DailyCapState:
    """Per-day accumulated axis movement from user reactions.

    Stored as a single JSON blob under :data:`KV_USER_REACTIONS_DAILY`.
    ``daily_date`` is the UTC date the counters apply to; when the
    next call lands on a different date the counters are zeroed
    before the increment lands. ``axis_totals`` is keyed by axis
    name (``closeness`` / ``humor`` / ``trust`` / ``comfort``) and
    stores the absolute sum of nudges applied today.
    """

    daily_date: str
    axis_totals: dict[str, float]


_EMPTY_DAILY = DailyCapState(daily_date="", axis_totals={})


def serialize_daily_state(state: DailyCapState) -> str:
    return json.dumps(
        {
            "daily_date": str(state.daily_date),
            "axis_totals": {k: float(v) for k, v in state.axis_totals.items()},
        },
        sort_keys=True,
    )


def deserialize_daily_state(text: str | None) -> DailyCapState:
    if not text:
        return _EMPTY_DAILY
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return _EMPTY_DAILY
    if not isinstance(data, dict):
        return _EMPTY_DAILY
    raw_date = data.get("daily_date", "")
    daily_date = str(raw_date) if isinstance(raw_date, str) else ""
    totals_raw = data.get("axis_totals", {})
    axis_totals: dict[str, float] = {}
    if isinstance(totals_raw, dict):
        for axis, value in totals_raw.items():
            if not isinstance(axis, str):
                continue
            try:
                axis_totals[axis] = float(value)
            except (TypeError, ValueError):
                continue
    return DailyCapState(daily_date=daily_date, axis_totals=axis_totals)


def _today_utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class DailyCapVerdict:
    """Result of :func:`apply_daily_cap`.

    ``effective_deltas`` is the post-cap dict to actually write
    against the axes. ``capped_axes`` lists any axes whose delta
    was reduced (so the controller can log it). ``new_state`` is
    the daily-cap state to persist back to ``kv_meta``.
    """

    effective_deltas: dict[str, float]
    capped_axes: tuple[str, ...]
    new_state: DailyCapState


def apply_daily_cap(
    proposed: dict[str, float],
    state: DailyCapState,
    *,
    now: datetime,
    daily_cap: float,
) -> DailyCapVerdict:
    """Trim ``proposed`` so today's cumulative axis movement stays
    under ``daily_cap``.

    Both ``proposed`` and ``daily_cap`` are absolute-value-based:
    a +0.03 closeness click and a -0.03 closeness click both
    consume 0.03 of the day's budget. (Today only positive deltas
    exist; the absolute-value design future-proofs against ever
    adding a "thumbs-down" kind that subtracts.)

    Returns ``DailyCapVerdict``; callers persist ``new_state``.
    """
    today = _today_utc(now)
    if state.daily_date != today:
        # Date rolled -- start from a fresh per-axis ledger.
        running: dict[str, float] = {}
    else:
        running = dict(state.axis_totals)

    cap = max(0.0, float(daily_cap))

    effective: dict[str, float] = {}
    capped: list[str] = []
    for axis, delta in proposed.items():
        if cap <= 0:
            # Cap of zero means "block reactions entirely from
            # moving the axes today". Every axis is capped.
            capped.append(axis)
            continue
        used = float(running.get(axis, 0.0))
        remaining = max(0.0, cap - used)
        abs_delta = abs(float(delta))
        if abs_delta <= remaining:
            effective[axis] = float(delta)
            running[axis] = used + abs_delta
        elif remaining > 0:
            sign = 1.0 if delta >= 0 else -1.0
            effective[axis] = sign * remaining
            running[axis] = used + remaining
            capped.append(axis)
        else:
            capped.append(axis)

    return DailyCapVerdict(
        effective_deltas=effective,
        capped_axes=tuple(capped),
        new_state=DailyCapState(daily_date=today, axis_totals=running),
    )


# ── kv_meta load / save helpers ─────────────────────────────────────


def load_daily_state(chat_db: "ChatDatabase") -> DailyCapState:
    try:
        raw = chat_db.kv_get(KV_USER_REACTIONS_DAILY)
    except Exception:
        log.debug("user_reactions kv_get failed", exc_info=True)
        return _EMPTY_DAILY
    return deserialize_daily_state(raw)


def save_daily_state(chat_db: "ChatDatabase", state: DailyCapState) -> None:
    try:
        chat_db.kv_set(KV_USER_REACTIONS_DAILY, serialize_daily_state(state))
    except Exception:
        log.debug("user_reactions kv_set failed", exc_info=True)


def reset_daily_state(chat_db: "ChatDatabase") -> None:
    """Clear the daily-cap counters (MCP debug helper)."""
    try:
        chat_db.kv_delete(KV_USER_REACTIONS_DAILY)
    except Exception:
        log.debug("user_reactions kv_delete failed", exc_info=True)


# ── Inner-life cue ──────────────────────────────────────────────────


def render_user_reactions_block(
    pending: list[tuple[int, str]],
    *,
    user_display_name: str = "they",
) -> str:
    """Render the one-shot "Jacob just hearted that line" prompt cue.

    ``pending`` is a list of ``(message_id, kind)`` tuples drained
    from the controller's ``_pending_user_reactions`` queue. Drops
    duplicates / unknown kinds defensively so a noisy queue can't
    produce a malformed cue.

    Shape:

    - Empty queue -> ``""`` (no cue).
    - Single reaction -> "Heads-up: {name} just hearted your reply."
    - Multiple reactions, all same kind -> "Heads-up: {name} just
      hearted a few of your recent replies."
    - Multiple reactions, mixed kinds -> "Heads-up: {name} just
      reacted (heart, laugh) to your recent replies."
    """
    if not pending:
        return ""
    name = user_display_name or "they"
    valid: list[tuple[int, str]] = []
    for entry in pending:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        _, kind = entry
        if is_valid_kind(kind):
            valid.append((int(entry[0]), str(kind).strip().lower()))
    if not valid:
        return ""

    if len(valid) == 1:
        _, kind = valid[0]
        meta = get_reaction_kind(kind)
        if meta is None:
            return ""
        return f"Heads-up: {name} {meta.label}."

    unique_kinds = list(dict.fromkeys(k for _, k in valid))
    if len(unique_kinds) == 1:
        meta = get_reaction_kind(unique_kinds[0])
        if meta is None:
            return ""
        # Pluralise "your reply" -> "a few of your recent replies"
        # rather than re-using the single-shot label so the cue
        # doesn't read like a copy-paste.
        return (
            f"Heads-up: {name} reacted to several of your recent replies "
            f"({meta.emoji} {meta.kind})."
        )
    # Mixed kinds: list the first three for the cue (more reads as
    # spam at prompt-cue length).
    summary = ", ".join(unique_kinds[:3])
    return (
        f"Heads-up: {name} just reacted ({summary}) to your recent replies."
    )


__all__ = [
    "DailyCapState",
    "DailyCapVerdict",
    "KV_USER_REACTIONS_DAILY",
    "REACTION_KINDS",
    "ReactionKind",
    "apply_daily_cap",
    "compute_deltas",
    "deserialize_daily_state",
    "get_reaction_kind",
    "is_valid_kind",
    "load_daily_state",
    "reactions_metadata",
    "render_user_reactions_block",
    "reset_daily_state",
    "save_daily_state",
    "serialize_daily_state",
]
