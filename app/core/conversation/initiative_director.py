"""K53 — initiative turns: deterministic floor-taking.

The structural counter to the helpful-assistant prior. Every N turns
the prompt carries an explicit one-turn directive — "this turn is
yours: open your own topic, share something unprompted, or steer the
thread where you want it; answering politely and asking a follow-up
back is NOT enough this turn" — with the content pulled from the K52
wants ledger when a want is live.

This is "may" -> "must, occasionally": the same design lesson as the
tool under-calling fix (force the choice, don't ask more nicely). A
permission slip always loses to the conversational prior; a scheduled
directive actually fires.

Cadence is deterministic with modulation, not random:

- base period ~8 turns, shortened in light arcs (``casual_check_in``
  / ``playful`` / ``silly``), lengthened when the relationship axes
  are still cold (a new relationship earns less steering);
- never during a ``support`` or ``reflection`` arc;
- suppressed while a K23 misattunement cooldown or a K8 rupture cue
  is live (pulling back and taking the floor are opposites);
- skipped silently when the user's incoming message is substantial —
  the escape hatch, mirroring the ``respond_directly`` escape-tool
  pattern: a long message deserves its answer, the directive just
  waits for the next short turn (the counter does NOT reset);
- a short warmup at the start of each session so turn 1 is never a
  floor-grab.

Pure module: the :class:`InitiativeDirector` holds two ints of
per-session state and delegates every decision to :func:`decide`.
Wiring (provider, settings, MCP) lives on the session mixins.
"""
from __future__ import annotations

from dataclasses import dataclass


# Arcs in which taking the floor is wrong outright.
_BLOCKED_ARCS = frozenset({"support", "reflection"})
# Arcs in which a floor-taking beat is cheap and welcome.
_LIGHT_ARCS = frozenset({"casual_check_in", "playful", "silly"})


@dataclass(frozen=True, slots=True)
class InitiativeDecision:
    """Outcome of one per-turn evaluation.

    ``reason`` names the gate that decided (grep-friendly):
    ``fire`` / ``warmup`` / ``arc_blocked`` / ``misattunement`` /
    ``rupture`` / ``user_substantial`` / ``not_due`` /
    ``wants_imperative_active``.
    """

    fire: bool
    reason: str
    effective_period: int


def compute_effective_period(
    base_period: int,
    *,
    arc: str | None,
    closeness: float | None,
    comfort: float | None,
) -> int:
    """Modulate the base cadence by arc + relationship axes.

    Light arcs shorten the period by 2 (banter invites steering);
    cold axes lengthen it (mean of closeness/comfort < -0.1 -> +4,
    < 0.25 -> +2). Floor of 3 so a hostile config can't make Aiko
    grab the floor every turn.
    """
    period = int(base_period)
    if arc in _LIGHT_ARCS:
        period -= 2
    c = float(closeness) if closeness is not None else 0.0
    f = float(comfort) if comfort is not None else 0.0
    mean = (max(-1.0, min(1.0, c)) + max(-1.0, min(1.0, f))) / 2.0
    if mean < -0.1:
        period += 4
    elif mean < 0.25:
        period += 2
    return max(3, period)


def decide(
    *,
    turns_since_initiative: int,
    session_turn_count: int,
    base_period: int,
    arc: str | None,
    closeness: float | None,
    comfort: float | None,
    misattunement_active: bool,
    rupture_active: bool,
    user_text: str,
    substantial_chars: int = 240,
    warmup_turns: int = 3,
    wants_imperative_active: bool = False,
    force: bool = False,
) -> InitiativeDecision:
    """One per-turn gate walk. Order matters — safety gates first.

    ``force=True`` (the MCP one-shot) bypasses every gate except the
    arc block: even a forced repro must not grab the floor mid-vent.
    """
    period = compute_effective_period(
        base_period, arc=arc, closeness=closeness, comfort=comfort,
    )
    if arc in _BLOCKED_ARCS:
        return InitiativeDecision(False, "arc_blocked", period)
    if force:
        return InitiativeDecision(True, "fire", period)
    if misattunement_active:
        return InitiativeDecision(False, "misattunement", period)
    if rupture_active:
        return InitiativeDecision(False, "rupture", period)
    if session_turn_count < max(0, int(warmup_turns)):
        return InitiativeDecision(False, "warmup", period)
    if wants_imperative_active:
        # The K52 imperative directive IS a floor-taking beat this
        # turn; stacking a second directive would read as an agenda.
        return InitiativeDecision(False, "wants_imperative_active", period)
    if len((user_text or "").strip()) >= max(1, int(substantial_chars)):
        # Escape hatch: a substantial message deserves its answer.
        # The counter does not reset — the directive fires on the
        # next short turn instead.
        return InitiativeDecision(False, "user_substantial", period)
    if turns_since_initiative < period:
        return InitiativeDecision(False, "not_due", period)
    return InitiativeDecision(True, "fire", period)


def render_block(
    want_text: str | None,
    *,
    user_display_name: str = "them",
) -> str:
    """Format the one-turn directive.

    With a live want the directive points at it; without one it stays
    generic (open a topic, share unprompted, steer). Either way the
    closing line names the anti-pattern explicitly — answering
    politely and asking a follow-up back is NOT enough this turn.
    """
    name = user_display_name or "them"
    if want_text:
        middle = (
            f"You have something queued that fits: {want_text}. Use this "
            f"turn to actually open it"
        )
    else:
        middle = (
            "Open a topic of your own, share something unprompted (a "
            "thought, a thing from your room, a take you've been "
            "sitting on), or steer the thread where YOU want it"
        )
    return (
        f"This turn is yours. Still answer what {name} said -- briefly "
        f"-- but don't stop there. {middle}, mid-conversation, no "
        f"permission asked ('okay wait, unrelated --' is allowed). "
        f"Answering politely and asking a follow-up back is NOT enough "
        f"this turn."
    )


class InitiativeDirector:
    """Two ints of per-session state + the pure gate walk.

    Owned by ``SessionController`` (recreated on session switch).
    ``note_turn_and_decide`` is the single entry point: it increments
    the counters, runs :func:`decide`, and resets the cadence counter
    when the directive fires.
    """

    def __init__(self) -> None:
        self.turns_since_initiative = 0
        self.session_turn_count = 0
        self.last_decision: InitiativeDecision | None = None

    def note_turn_and_decide(self, **kwargs) -> InitiativeDecision:
        self.session_turn_count += 1
        self.turns_since_initiative += 1
        decision = decide(
            turns_since_initiative=self.turns_since_initiative,
            session_turn_count=self.session_turn_count,
            **kwargs,
        )
        if decision.fire or decision.reason == "wants_imperative_active":
            # A K52 imperative directive consumed this turn's
            # floor-taking beat — resetting here prevents two
            # consecutive floor-grabs (imperative turn, then an
            # immediately-due initiative turn).
            self.turns_since_initiative = 0
        self.last_decision = decision
        return decision


__all__ = [
    "InitiativeDecision",
    "InitiativeDirector",
    "compute_effective_period",
    "decide",
    "render_block",
]
