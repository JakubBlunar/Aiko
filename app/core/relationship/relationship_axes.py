"""Relationship axes — closeness, humor, trust, comfort.

Four floats in [-1, 1] that drift per turn from cheap signals
(``[[reaction:…]]`` tags, ``[[moment:…]]`` tags, milestones, promise
transitions, world gifts) and slowly decay toward 0 over a ~30-day
half-life when no signal arrives. The axes feed two consumers today:

  * :func:`SessionController._render_axes_block` — terse, only renders
    when at least one axis exceeds ±0.5, and never enumerates every axis
    so the LLM doesn't become axis-obsessed.
  * The "Together" UI tab — four horizontal bars, live-updated from a
    debounced ``relationship_axes_updated`` WebSocket event.

Designed to feel slow and earned: each delta is small (≤ 0.08), clamped
to [-1, 1] on write, and the half-life ensures a noisy week doesn't pin
an axis at +1 indefinitely.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.relationship_axes")


# Decay half-life in days for an axis at rest. 30 days means an axis at
# +0.5 with no further signal drops to +0.25 after ~30 days.
_DECAY_HALF_LIFE_DAYS = 30.0

# Apply decay no more than once every ``_DECAY_MIN_INTERVAL_SECONDS``
# wall-clock seconds. Avoids hammering the table for sub-second updates.
_DECAY_MIN_INTERVAL_SECONDS = 60.0

# Threshold above which an axis is considered "notable" by the prompt
# renderer. Higher than the 0.5 cited in the plan because the axes
# accumulate slowly; 0.5 ends up being a meaningful place to land.
_NOTABLE_THRESHOLD = 0.5

# Cap on any single per-turn delta. Bumps larger than this are saturated.
_MAX_DELTA = 0.08


# Per-event deltas. Keys map to ``RelationshipAxesState`` field names.
# Adjusted small so several typical signals in one turn don't blow past
# the clamp; the values were tuned by hand to feel earned.
_DELTAS_REACTION: dict[str, dict[str, float]] = {
    "laugh": {"humor": 0.03, "closeness": 0.01},
    "giggle": {"humor": 0.02, "closeness": 0.01},
    "warm": {"closeness": 0.02},
    "tender": {"closeness": 0.03, "comfort": 0.02},
    "love": {"closeness": 0.04, "trust": 0.02},
    "loving": {"closeness": 0.04, "trust": 0.02},
    "awe": {"closeness": 0.02, "trust": 0.01},
    "surprise": {"closeness": 0.01},
    "joy": {"humor": 0.02, "closeness": 0.02},
    "joyful": {"humor": 0.02, "closeness": 0.02},
    "proud": {"trust": 0.03, "closeness": 0.02},
    "blush": {"closeness": 0.02, "comfort": 0.01},
    "shy": {"comfort": 0.01},
    "vulnerable": {"trust": 0.04, "comfort": 0.02, "closeness": 0.02},
    "sad": {"comfort": -0.02},
    "sadness": {"comfort": -0.02},
    "angry": {"comfort": -0.03, "trust": -0.01},
    "frustrated": {"comfort": -0.02},
}

# Vibe-specific deltas applied when a shared moment is added.
_DELTAS_MOMENT_VIBE: dict[str, dict[str, float]] = {
    "warm": {"closeness": 0.03, "comfort": 0.02},
    "playful": {"humor": 0.04, "closeness": 0.02},
    "tender": {"closeness": 0.05, "comfort": 0.03},
    "proud": {"trust": 0.05, "closeness": 0.02},
    "silly": {"humor": 0.04},
    "milestone": {"closeness": 0.06, "trust": 0.04, "comfort": 0.03},
    "gift": {"closeness": 0.04, "comfort": 0.02},
    "comfort": {"comfort": 0.05, "closeness": 0.02},
    "victory": {"trust": 0.05, "closeness": 0.03, "humor": 0.02},
    "creative": {"closeness": 0.03},
    "vulnerable": {"trust": 0.06, "closeness": 0.03, "comfort": 0.03},
    "general": {"closeness": 0.02},
}


@dataclass(slots=True)
class RelationshipAxesState:
    """Snapshot of the four axes for a single user."""

    user_id: str
    closeness: float = 0.0
    humor: float = 0.0
    trust: float = 0.0
    comfort: float = 0.0
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def clamp(self) -> None:
        self.closeness = max(-1.0, min(1.0, float(self.closeness)))
        self.humor = max(-1.0, min(1.0, float(self.humor)))
        self.trust = max(-1.0, min(1.0, float(self.trust)))
        self.comfort = max(-1.0, min(1.0, float(self.comfort)))


# ── store ───────────────────────────────────────────────────────────────


class RelationshipAxesStore:
    """SQLite-backed read/write helper for the ``relationship_axes`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_raw(self, user_id: str) -> RelationshipAxesState:
        """Return the persisted row WITHOUT applying decay."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT user_id, closeness, humor, trust, comfort, updated_at "
            "FROM relationship_axes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return RelationshipAxesState(user_id=user_id, updated_at=self._now())
        return RelationshipAxesState(
            user_id=str(row[0]),
            closeness=float(row[1]),
            humor=float(row[2]),
            trust=float(row[3]),
            comfort=float(row[4]),
            updated_at=str(row[5]),
        )

    def get(self, user_id: str, *, now: datetime | None = None) -> RelationshipAxesState:
        """Return the state with decay-on-read applied (and persisted)."""
        state = self.get_raw(user_id)
        decayed = apply_decay(state, now=now or datetime.now(timezone.utc))
        if decayed is not state:
            self.save(decayed)
        return decayed

    def save(self, state: RelationshipAxesState) -> None:
        state.clamp()
        state.updated_at = self._now()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO relationship_axes ("
            "  user_id, closeness, humor, trust, comfort, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  closeness = excluded.closeness, "
            "  humor = excluded.humor, "
            "  trust = excluded.trust, "
            "  comfort = excluded.comfort, "
            "  updated_at = excluded.updated_at",
            (
                state.user_id,
                float(state.closeness),
                float(state.humor),
                float(state.trust),
                float(state.comfort),
                state.updated_at,
            ),
        )
        conn.commit()


# ── decay ───────────────────────────────────────────────────────────────


def apply_decay(
    state: RelationshipAxesState,
    *,
    now: datetime,
    half_life_days: float = _DECAY_HALF_LIFE_DAYS,
    min_interval_seconds: float = _DECAY_MIN_INTERVAL_SECONDS,
) -> RelationshipAxesState:
    """Apply exponential decay toward 0 since ``state.updated_at``.

    Returns the (possibly new) state. The caller should ``save`` if the
    return is not the same identity as the input.

    No-op if ``updated_at`` is unparseable or the elapsed delta is below
    ``min_interval_seconds`` (cheap guard against decaying on every tick).
    """
    try:
        last = datetime.fromisoformat(state.updated_at.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return state
    elapsed = (now - last).total_seconds()
    if elapsed < min_interval_seconds:
        return state
    if elapsed <= 0:
        return state
    # value *= 2 ** (-elapsed_days / half_life_days)
    days = elapsed / 86400.0
    factor = math.pow(0.5, days / max(0.1, float(half_life_days)))
    if factor >= 0.999:
        return state
    new_state = RelationshipAxesState(
        user_id=state.user_id,
        closeness=state.closeness * factor,
        humor=state.humor * factor,
        trust=state.trust * factor,
        comfort=state.comfort * factor,
        updated_at=now.isoformat(),
    )
    new_state.clamp()
    return new_state


# ── updater ─────────────────────────────────────────────────────────────


class RelationshipAxesUpdater:
    """Mutates :class:`RelationshipAxesState` after each turn (no LLM)."""

    def __init__(self, store: RelationshipAxesStore) -> None:
        self._store = store

    def apply_turn(
        self,
        user_id: str,
        *,
        reaction_tags: Iterable[str] = (),
        moment_vibes: Iterable[str] = (),
        milestone: str | None = None,
        gift_received: bool = False,
        promise_kept: bool = False,
        user_text: str = "",
        engagement_delta: float = 0.0,
    ) -> RelationshipAxesState:
        """Apply per-event drift and persist. Returns the new state.

        ``engagement_delta`` is the K14 implicit-engagement contribution
        (short snappy replies + above-baseline length nudge it up;
        long voice gaps + below-baseline length nudge it down). The
        tracker pre-clamps it to its own per-turn cap so we just
        accumulate the value into the ``closeness`` axis here; the
        existing ``_MAX_DELTA`` clamp still applies to the final sum so
        a reaction-tag stack plus engagement can't blow past 0.08.
        """
        state = self._store.get(user_id)
        deltas: dict[str, float] = {
            "closeness": 0.0,
            "humor": 0.0,
            "trust": 0.0,
            "comfort": 0.0,
        }

        for tag in reaction_tags:
            for axis, value in _DELTAS_REACTION.get(str(tag).lower(), {}).items():
                deltas[axis] += value

        for vibe in moment_vibes:
            for axis, value in _DELTAS_MOMENT_VIBE.get(str(vibe).lower(), {}).items():
                deltas[axis] += value

        if milestone:
            # Milestones are the biggest single-turn jump we allow because
            # they only fire once per calendar threshold (see
            # :data:`app.core.relationship.relationship._MILESTONES`).
            deltas["closeness"] += 0.08
            deltas["trust"] += 0.04
            deltas["comfort"] += 0.04

        if gift_received:
            deltas["closeness"] += 0.03
            deltas["comfort"] += 0.02

        if promise_kept:
            deltas["trust"] += 0.06

        # Tiny user-text hint (mirrors the affect updater approach). Stays
        # at ±0.02 so cheap keyword detection doesn't dominate.
        text = (user_text or "").lower()
        if text:
            warm_terms = ("love you", "miss you", "thanks", "thank you", "appreciate")
            cold_terms = ("hate this", "annoying", "shut up", "go away", "leave me alone")
            for term in warm_terms:
                if term in text:
                    deltas["closeness"] += 0.02
                    break
            for term in cold_terms:
                if term in text:
                    deltas["closeness"] -= 0.02
                    deltas["comfort"] -= 0.02
                    break

        # K14: implicit engagement signal. Tracker pre-clamps to its
        # own configurable cap (default 0.04); the per-axis ``_MAX_DELTA``
        # below still saturates the sum so a hot reaction-tag turn plus
        # engagement can't push closeness past 0.08 in a single turn.
        if engagement_delta:
            deltas["closeness"] += float(engagement_delta)

        # Cap each axis's per-turn delta so a wild reaction-tag stack
        # doesn't spike anything by more than _MAX_DELTA.
        for axis in deltas:
            deltas[axis] = max(-_MAX_DELTA, min(_MAX_DELTA, deltas[axis]))

        if all(abs(v) < 1e-6 for v in deltas.values()):
            # Nothing happened. Don't bump updated_at (that would extend
            # decay) but still return current state.
            return state

        state.closeness += deltas["closeness"]
        state.humor += deltas["humor"]
        state.trust += deltas["trust"]
        state.comfort += deltas["comfort"]
        state.clamp()
        self._store.save(state)
        return state

    def apply_user_reaction(
        self,
        user_id: str,
        *,
        kind: str,
        daily_cap: float = 0.15,
    ) -> RelationshipAxesState | None:
        """K32: apply a single user-reaction click to the axes.

        Routes through :func:`user_reactions.compute_deltas` (per-kind
        delta table + soft cap) and :func:`user_reactions.apply_daily_cap`
        (running per-axis daily budget persisted in ``kv_meta``). On a
        zero-delta path (``surprise`` kind, or cap fully spent) returns
        the unmodified state without writing the row.

        Persistence + clamping match :meth:`apply_turn` exactly.
        """
        from datetime import datetime, timezone

        from app.core.relationship import user_reactions as _ur

        proposed = _ur.compute_deltas(kind)
        if not proposed:
            return self._store.get(user_id)

        chat_db = getattr(self._store, "_db", None)
        if chat_db is None:
            return None

        now = datetime.now(timezone.utc)
        cap_state = _ur.load_daily_state(chat_db)
        verdict = _ur.apply_daily_cap(
            proposed, cap_state, now=now, daily_cap=daily_cap,
        )

        # Persist the daily-cap state regardless of whether any delta
        # survived -- we want the ``daily_date`` rollover to land even
        # on a fully-capped click so tomorrow's first reaction starts
        # from a fresh ledger.
        _ur.save_daily_state(chat_db, verdict.new_state)

        if not verdict.effective_deltas:
            return self._store.get(user_id)

        state = self._store.get(user_id)
        deltas: dict[str, float] = {
            "closeness": 0.0,
            "humor": 0.0,
            "trust": 0.0,
            "comfort": 0.0,
        }
        for axis, value in verdict.effective_deltas.items():
            if axis in deltas:
                deltas[axis] += float(value)

        # Mirror :meth:`apply_turn`'s per-axis hard clamp so a delta
        # that survived the daily cap can never blow past the
        # per-turn-style ceiling either. Belt-and-braces -- the
        # K32 per-reaction soft cap (0.04) is already half of this.
        for axis in deltas:
            deltas[axis] = max(-_MAX_DELTA, min(_MAX_DELTA, deltas[axis]))

        if all(abs(v) < 1e-6 for v in deltas.values()):
            return state

        state.closeness += deltas["closeness"]
        state.humor += deltas["humor"]
        state.trust += deltas["trust"]
        state.comfort += deltas["comfort"]
        state.clamp()
        self._store.save(state)
        if verdict.capped_axes:
            log.info(
                "user_reaction axes: user=%s kind=%s applied=%s capped=%s",
                user_id,
                kind,
                verdict.effective_deltas,
                list(verdict.capped_axes),
            )
        return state


# ── rendering ───────────────────────────────────────────────────────────


# Axis phrases templated on the user's display name. The placeholder
# ``{name}`` (and ``{them}`` for the pronoun-y variants) is filled by
# :func:`render_axes_block`; templates with no placeholder render
# verbatim so we don't pay the format() cost for free-text lines.
_AXIS_PHRASES_POS: dict[str, tuple[str, str]] = {
    # axis -> (mid phrase, high phrase)
    "closeness": (
        "you feel close to {name} right now",
        "you feel especially close with {name} right now",
    ),
    "humor": (
        "the humor's been easy with {them} lately",
        "you and {name} have been playful with each other lately",
    ),
    "trust": (
        "trust runs steady between you",
        "trust feels solid between you",
    ),
    "comfort": (
        "things feel comfortable between you",
        "you feel calm and at home around {them}",
    ),
}
_AXIS_PHRASES_NEG: dict[str, tuple[str, str]] = {
    "closeness": (
        "things have felt a little distant",
        "you've felt distant from {name} lately",
    ),
    "humor": (
        "things have been less playful lately",
        "the humor's gone quiet between you",
    ),
    "trust": (
        "trust feels a bit tender right now",
        "trust feels frayed right now",
    ),
    "comfort": (
        "things have felt a little uneasy",
        "you've felt a bit on-edge around {them}",
    ),
}


def render_axes_block(
    state: RelationshipAxesState,
    *,
    threshold: float = _NOTABLE_THRESHOLD,
    user_display_name: str = "the user",
) -> str:
    """Return a terse 1-line block, or '' if nothing crosses ``threshold``.

    We deliberately render at most TWO axes (the two most extreme) so the
    LLM doesn't get a dashboard of numbers it'll start citing verbatim.
    """
    axes = [
        ("closeness", state.closeness),
        ("humor", state.humor),
        ("trust", state.trust),
        ("comfort", state.comfort),
    ]
    notable = [(name, value) for name, value in axes if abs(value) >= threshold]
    if not notable:
        return ""
    # Sort by magnitude desc, keep the top two.
    notable.sort(key=lambda pair: abs(pair[1]), reverse=True)
    notable = notable[:2]

    # ``them`` is the third-person pronoun stand-in for the user. We
    # use a generic "them" rather than guessing gender from the typed
    # name; tone-wise it reads as warmer than "<name>" repeated.
    name = user_display_name or "the user"
    them = "them"
    parts: list[str] = []
    for axis, value in notable:
        intensity = "high" if abs(value) >= 0.75 else "mid"
        idx = 1 if intensity == "high" else 0
        if value >= 0:
            phrase = _AXIS_PHRASES_POS.get(axis, ("", ""))[idx]
        else:
            phrase = _AXIS_PHRASES_NEG.get(axis, ("", ""))[idx]
        if phrase:
            # Templates may or may not carry {name} / {them}; .format
            # is safe either way and cheap enough on the hot path.
            parts.append(phrase.format(name=name, them=them))
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else f"{parts[0]} — {parts[1]}"
    return f"How the relationship feels: {joined}."


# ── relationship stage (J4) ──────────────────────────────────────────────
#
# A coarse, legible *bond stage* derived from a blend of the depth axes
# (closeness / trust / comfort — humor is flavour, not depth) gated by
# tenure (days known). This is distinct from
# :func:`app.core.relationship.relationship.phase_for`, which is purely
# tenure + turn-count: the phase answers "how long have we known each
# other?", the stage answers "how close are we, *given* how long it's
# been?". The stage is meant to *colour behaviour* (gesture / tease /
# self-disclosure gates in J8-J10) and to subtly tune register — it is
# never named at the user ("we've reached level 3").

STAGE_NEW = "new"
STAGE_FAMILIAR = "familiar"
STAGE_CLOSE = "close"
STAGE_INTIMATE = "intimate"

# Ordered shallow -> deep. Index is the stage rank.
STAGE_ORDER: tuple[str, ...] = (
    STAGE_NEW,
    STAGE_FAMILIAR,
    STAGE_CLOSE,
    STAGE_INTIMATE,
)

# Bond = weighted blend of the three depth axes. Humor is deliberately
# excluded — two people can banter without being close.
_BOND_WEIGHTS: dict[str, float] = {
    "closeness": 0.40,
    "trust": 0.35,
    "comfort": 0.25,
}

# Base bond thresholds (bond in [-1, 1]) to *reach* familiar / close /
# intimate. Tuned against the slow axis accumulation: the axes are hard
# to push past ~0.6, so 0.60 is a meaningful "intimate" bar.
_BOND_THRESHOLDS: tuple[float, float, float] = (0.12, 0.35, 0.60)

# Hysteresis margin: promote only when bond clears threshold + margin,
# demote only when it drops below threshold - margin. Stops the stage
# flapping turn-to-turn around a boundary.
_STAGE_HYSTERESIS = 0.05


def relationship_bond(state: RelationshipAxesState) -> float:
    """Weighted blend of the depth axes, clamped to [-1, 1]."""
    bond = (
        state.closeness * _BOND_WEIGHTS["closeness"]
        + state.trust * _BOND_WEIGHTS["trust"]
        + state.comfort * _BOND_WEIGHTS["comfort"]
    )
    return max(-1.0, min(1.0, bond))


def stage_rank(stage: str | None) -> int:
    """Rank (0..3) for a stage label; unknown / None -> 0 (new)."""
    if not stage:
        return 0
    try:
        return STAGE_ORDER.index(str(stage))
    except ValueError:
        return 0


def _tenure_ceiling_rank(tenure_days: float) -> int:
    """Max stage reachable at a given tenure — you can't be intimate on day 1."""
    if tenure_days < 3.0:
        return 1  # familiar
    if tenure_days < 14.0:
        return 2  # close
    return 3  # intimate


def _tenure_floor_rank(tenure_days: float) -> int:
    """Min stage once a relationship has simply *lasted* (cold or not)."""
    if tenure_days >= 8.0:
        return 1  # familiar
    return 0  # new


def relationship_stage(
    state: RelationshipAxesState,
    *,
    tenure_days: float,
    current_stage: str | None = None,
) -> str:
    """Resolve the bond stage from axes + tenure, with hysteresis.

    ``current_stage`` (the previously-resolved stage) feeds the
    hysteresis band so the stage only promotes when bond clears
    ``threshold + margin`` and only demotes below ``threshold - margin``.
    Pass ``None`` for a cold resolve (conservative — every boundary uses
    the promote threshold).

    Tenure gates the result: a ceiling (can't skip ahead before enough
    time has passed) and a floor (a long-lasting relationship reads as at
    least ``familiar`` even at neutral axes).
    """
    bond = relationship_bond(state)
    cur_rank = stage_rank(current_stage)

    rank = 0
    for boundary, base in enumerate(_BOND_THRESHOLDS):
        # ``boundary`` is the divider between rank ``boundary`` and
        # ``boundary + 1``. If we're already on the upper side, the
        # threshold is sticky (base - margin); otherwise we must climb
        # past (base + margin).
        if cur_rank >= boundary + 1:
            thr = base - _STAGE_HYSTERESIS
        else:
            thr = base + _STAGE_HYSTERESIS
        if bond >= thr:
            rank = boundary + 1

    days = max(0.0, float(tenure_days))
    ceiling = _tenure_ceiling_rank(days)
    floor = min(_tenure_floor_rank(days), ceiling)
    rank = max(floor, min(rank, ceiling))
    return STAGE_ORDER[rank]


# Per-stage register nudge for the prompt. Only the deeper stages get a
# line — ``new`` / ``familiar`` are the default warmth the persona
# already carries, so surfacing a cue there is noise. ``{name}`` is
# filled by :func:`stage_register_hint`.
_STAGE_REGISTER_HINTS: dict[str, str] = {
    STAGE_CLOSE: (
        "You and {name} are close now — easy warmth, inside references, "
        "and light teasing land well; you don't have to keep your guard up."
    ),
    STAGE_INTIMATE: (
        "You and {name} are intimate — deep familiarity and trust between you both, comfortable "
        "silences, candor and softness are all welcome; speak like someone "
        "who knows them well."
    ),
}


def stage_register_hint(stage: str, *, user_display_name: str = "the user") -> str:
    """Return a subtle register nudge for the stage, or '' for shallow stages.

    Never names the stage at the user — it only colours how Aiko speaks.
    """
    template = _STAGE_REGISTER_HINTS.get(str(stage), "")
    if not template:
        return ""
    return template.format(name=user_display_name or "the user")
