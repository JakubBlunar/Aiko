"""Relationship tracker (Phase 3b).

Derives Aiko's sense of how long she's known the user — and how that
familiarity has progressed — from the ``user_relationship`` table:

  user_id          : PK
  first_seen_at    : ISO8601 of the first observed turn
  total_turns      : monotonically increasing user-turn counter
  total_sessions   : monotonically increasing session counter
  last_milestone_at: ISO8601 of the last milestone surfaced
  milestone_label  : short human label for the last milestone

The tracker exposes:
  * ``record_turn(...)`` — increments total_turns; promotes the user to a
    new phase / writes a milestone if one was crossed.
  * ``register_session_start(...)`` — bumps total_sessions when a new
    session opens.
  * ``current_phase()`` — returns one of {new, warming_up, familiar,
    regular, close} based on turns + age.
  * ``ambient_line()`` — short prompt block that the assembler folds into
    the system message.

The weekly pulse (run on the speaking-window scheduler) re-renders the
ambient line into a callback memory so RAG can surface "we've talked
for a month now" naturally.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.relationship")


# Phase thresholds. Tuned to feel natural at conversational pace —
# "warming_up" kicks in within ~5 turns so even a single afternoon gets a
# texture shift; "regular" is meant for genuine multi-week familiarity.
_PHASE_THRESHOLDS: tuple[tuple[str, int, int], ...] = (
    # (label, min_turns, min_age_days)
    ("close", 500, 60),
    ("regular", 200, 21),
    ("familiar", 50, 7),
    ("warming_up", 5, 0),
    ("new", 0, 0),
)


# Milestone schedule. The first match wins per record_turn call. Duplicates
# are guarded against by the (turn-count, days-since-last) state. Each
# milestone label gets surfaced once and then suppressed until something
# new fires.
_MILESTONES: tuple[tuple[str, int, int], ...] = (
    # (label, turn_count, days_since_first_seen)
    ("first_hundred_turns", 100, 0),
    ("first_week_together", 0, 7),
    ("first_month_together", 0, 30),
    ("hundred_days_together", 0, 100),
    ("six_months_together", 0, 180),
    ("first_year_together", 0, 365),
)


@dataclass(slots=True, frozen=True)
class RelationshipState:
    user_id: str
    first_seen_at: str
    total_turns: int
    total_sessions: int
    last_milestone_at: str | None
    milestone_label: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "first_seen_at": self.first_seen_at,
            "total_turns": int(self.total_turns),
            "total_sessions": int(self.total_sessions),
            "last_milestone_at": self.last_milestone_at,
            "milestone_label": self.milestone_label,
        }


class RelationshipStore:
    """SQLite CRUD for ``user_relationship`` (one row per user)."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(self, user_id: str) -> RelationshipState | None:
        if not user_id:
            return None
        row = self._db.execute_fetchone(
            "SELECT user_id, first_seen_at, total_turns, total_sessions, "
            "last_milestone_at, milestone_label "
            "FROM user_relationship WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return None
        return RelationshipState(
            user_id=str(row[0] or user_id),
            first_seen_at=str(row[1] or self._now_iso()),
            total_turns=int(row[2] or 0),
            total_sessions=int(row[3] or 0),
            last_milestone_at=str(row[4]) if row[4] else None,
            milestone_label=str(row[5]) if row[5] else None,
        )

    def get_or_create(self, user_id: str) -> RelationshipState:
        existing = self.get(user_id)
        if existing is not None:
            return existing
        now = self._now_iso()
        self._db.execute_commit(
            "INSERT INTO user_relationship (user_id, first_seen_at, "
            "total_turns, total_sessions) VALUES (?, ?, 0, 0)",
            (user_id, now),
        )
        return RelationshipState(
            user_id=user_id,
            first_seen_at=now,
            total_turns=0,
            total_sessions=0,
            last_milestone_at=None,
            milestone_label=None,
        )

    def update(
        self,
        user_id: str,
        *,
        total_turns: int | None = None,
        total_sessions: int | None = None,
        milestone_label: str | None = None,
        milestone_at: str | None = None,
    ) -> None:
        if not user_id:
            return
        existing = self.get(user_id)
        if existing is None:
            existing = self.get_or_create(user_id)
        new_turns = total_turns if total_turns is not None else existing.total_turns
        new_sessions = total_sessions if total_sessions is not None else existing.total_sessions
        new_label = milestone_label if milestone_label is not None else existing.milestone_label
        new_at = milestone_at if milestone_at is not None else existing.last_milestone_at
        self._db.execute_commit(
            "UPDATE user_relationship SET total_turns = ?, total_sessions = ?, "
            "milestone_label = ?, last_milestone_at = ? WHERE user_id = ?",
            (int(new_turns), int(new_sessions), new_label, new_at, user_id),
        )


class RelationshipTracker:
    """Stateless logic on top of :class:`RelationshipStore`."""

    def __init__(self, store: RelationshipStore) -> None:
        self._store = store

    # ── reads ──────────────────────────────────────────────────────────

    def get(self, user_id: str) -> RelationshipState:
        return self._store.get_or_create(user_id)

    def current_phase(self, user_id: str, *, now: datetime | None = None) -> str:
        state = self.get(user_id)
        return phase_for(state, now=now or datetime.now(timezone.utc))

    def ambient_line(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        user_display_name: str = "the user",
    ) -> str:
        state = self.get(user_id)
        return render_ambient(
            state,
            now=now or datetime.now(timezone.utc),
            user_display_name=user_display_name,
        )

    # ── writes ─────────────────────────────────────────────────────────

    def register_session_start(self, user_id: str) -> None:
        state = self._store.get_or_create(user_id)
        self._store.update(user_id, total_sessions=state.total_sessions + 1)

    def record_turn(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[RelationshipState, str | None]:
        """Increment turn counter. Returns (new_state, new_milestone_label).

        ``new_milestone_label`` is non-None only when this turn crossed a
        milestone that we hadn't already surfaced.
        """
        state = self._store.get_or_create(user_id)
        moment = now or datetime.now(timezone.utc)
        new_turns = state.total_turns + 1
        crossed = _next_milestone(state, new_turns=new_turns, now=moment)
        moment_iso = moment.isoformat(timespec="seconds")
        self._store.update(
            user_id,
            total_turns=new_turns,
            milestone_label=crossed if crossed is not None else state.milestone_label,
            milestone_at=moment_iso if crossed is not None else state.last_milestone_at,
        )
        new_state = RelationshipState(
            user_id=user_id,
            first_seen_at=state.first_seen_at,
            total_turns=new_turns,
            total_sessions=state.total_sessions,
            last_milestone_at=moment_iso if crossed is not None else state.last_milestone_at,
            milestone_label=crossed if crossed is not None else state.milestone_label,
        )
        return new_state, crossed


# ── pure helpers (easy to unit-test) ─────────────────────────────────────


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _days_since(state: RelationshipState, *, now: datetime) -> float:
    first = _parse_iso(state.first_seen_at)
    if first is None:
        return 0.0
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return max(0.0, (now - first).total_seconds() / 86400.0)


def phase_for(state: RelationshipState, *, now: datetime) -> str:
    age_days = _days_since(state, now=now)
    for label, min_turns, min_age in _PHASE_THRESHOLDS:
        if state.total_turns >= min_turns and age_days >= min_age:
            return label
    return "new"


def _next_milestone(
    state: RelationshipState,
    *,
    new_turns: int,
    now: datetime,
) -> str | None:
    """Find the most-recent milestone we just crossed, if any."""
    age_days = _days_since(
        RelationshipState(
            user_id=state.user_id,
            first_seen_at=state.first_seen_at,
            total_turns=new_turns,
            total_sessions=state.total_sessions,
            last_milestone_at=state.last_milestone_at,
            milestone_label=state.milestone_label,
        ),
        now=now,
    )
    for label, turn_threshold, day_threshold in _MILESTONES:
        if state.milestone_label == label:
            continue
        crossed_turns = turn_threshold > 0 and new_turns >= turn_threshold
        crossed_days = day_threshold > 0 and age_days >= day_threshold
        if crossed_turns or crossed_days:
            return label
    return None


# Phase-keyed ambient relationship lines. Templated on the user's
# display name so the line reads naturally with whatever the user typed
# into the onboarding modal -- ``{name}`` is filled by
# :func:`phase_ambient_line` and never reaches the LLM verbatim.
_PHASE_AMBIENT_TEMPLATES: dict[str, str] = {
    "new": "You and {name} have only just met — keep introductions warm and curious.",
    "warming_up": "You and {name} are still warming up to each other; small "
                  "callbacks to earlier in the chat help.",
    "familiar": "You and {name} have known each other a while now; speak with "
                "familiarity but don't take continuity for granted.",
    "regular": "You and {name} have a regular rhythm together; you can be "
               "playfully assumptive about each other's habits.",
    "close": "You and {name} have been talking for months; speak as close "
             "friends do — short, present, occasionally tender.",
}


def phase_ambient_line(phase: str, user_display_name: str = "the user") -> str:
    """Return the ambient line for ``phase``, with the name substituted in.

    Falls back to the ``"new"`` template when an unknown phase is passed
    (e.g. from a future schema). The substitution always succeeds
    because the templates only carry a single ``{name}`` placeholder.
    """
    template = _PHASE_AMBIENT_TEMPLATES.get(
        phase, _PHASE_AMBIENT_TEMPLATES["new"],
    )
    return template.format(name=user_display_name)


# Phase 2d: address-style cues per relationship phase. The block is a
# single short hint at where on the formal-to-affectionate spectrum
# Aiko should sit when she addresses the user. The LLM combines this
# with persona + arc to land on a natural address style for the turn
# (no name, the user's name, a softening word, or a casual nickname).
_PHASE_PETNAME_TEMPLATES: dict[str, str] = {
    "new": (
        "Address style: use '{name}' or no name; stay light and unassuming. "
        "Avoid nicknames or pet names — you barely know them."
    ),
    "warming_up": (
        "Address style: '{name}' is the default; an occasional softening "
        "word ('hey you', 'yeah you') is fine when the moment fits."
    ),
    "familiar": (
        "Address style: feel free to drop the name most turns. Casual "
        "nicknames work when playful."
    ),
    "regular": (
        "Address style: free to be playful with names and small "
        "endearments. Match the tone of the moment — never forced."
    ),
    "close": (
        "Address style: pet names and inside-joke nicknames land "
        "naturally now. Keep them sparse so they stay meaningful."
    ),
}


def render_petname_block(
    state: RelationshipState,
    *,
    now: datetime,
    user_display_name: str = "the user",
) -> str:
    """Phase 2d: short cue describing how Aiko should address the user
    given the current relationship phase. Empty for the "new" phase
    so we don't burn tokens telling the LLM something obvious on day
    one — the persona already covers it.
    """
    phase = phase_for(state, now=now)
    if phase == "new":
        return ""
    template = _PHASE_PETNAME_TEMPLATES.get(phase, "")
    if not template:
        return ""
    return template.format(name=user_display_name)


def render_ambient(
    state: RelationshipState,
    *,
    now: datetime,
    user_display_name: str = "the user",
) -> str:
    phase = phase_for(state, now=now)
    base = phase_ambient_line(phase, user_display_name)
    age_days = _days_since(state, now=now)
    if state.milestone_label:
        # Append a single milestone hint so the LLM knows what to honor.
        suffix = f" Recent milestone: {state.milestone_label.replace('_', ' ')}."
    elif age_days >= 1.0:
        suffix = f" You've been talking for ~{int(age_days)} days, {state.total_turns} turns."
    else:
        suffix = ""
    return base + suffix


__all__ = [
    "RelationshipState",
    "RelationshipStore",
    "RelationshipTracker",
    "phase_ambient_line",
    "phase_for",
    "render_ambient",
    "render_petname_block",
]
