"""K54 — Aiko-side topic appetite: she's allowed to be bored.

K18 detects when *the conversation* is looping; nothing models
whether **Aiko herself** is still engaged. This module combines
signals that already exist into one rare permission slip:

- the K18 standing lull reading (``TopicStagnationDetector.last_mean``
  — the rolling distance-to-centroid mean, low = circling);
- Aiko's own contribution pattern (share of her recent replies that
  are short — all ack-and-ask, nothing substantive);
- a pressured K52 want to offer instead (low appetite without an
  offer is just rudeness; the want is what makes the negotiation
  land as charm);
- the relationship axes (a tug-of-war over the topic is an
  earned-intimacy move — cold axes block it).

Low appetite + pressured want = explicit permission to *negotiate
the topic*: "okay, honestly, I've said all I have on spreadsheets.
Can I tell you about the thing I read last night?" That good-natured
tug-of-war over what to talk about is exactly what distinguishes a
person from an assistant.

Highest tonal risk of the will family, so the gates are strict:
once per conversation max, never in support / reflection arcs, only
when the axes are warm, and the persona copy lands it as charm with
an immediate offer — and she yields gracefully if the user wants to
stay.

Pure module: :func:`decide` is the gate walk, with helpers for the
contribution share. Wiring (provider, settings, MCP) lives on the
session mixins.
"""
from __future__ import annotations

from dataclasses import dataclass


# Same arcs K53 blocks on — negotiating the topic mid-vent is wrong.
_BLOCKED_ARCS = frozenset({"support", "reflection"})


@dataclass(frozen=True, slots=True)
class AppetiteDecision:
    """Outcome of one per-turn evaluation.

    ``reason`` names the gate that decided (grep-friendly):
    ``fire`` / ``already_fired`` / ``arc_blocked`` / ``axes_cold`` /
    ``no_lull`` / ``still_contributing`` / ``no_offer``.
    """

    fire: bool
    reason: str


def compute_short_reply_share(
    reply_lengths: list[int],
    *,
    short_chars: int = 160,
) -> float | None:
    """Share of recent assistant replies below the substantive floor.

    ``None`` when there are no replies to measure (cold start) — the
    caller must treat that as "still contributing", never as boredom.
    """
    if not reply_lengths:
        return None
    short = sum(
        1 for n in reply_lengths if int(n) < max(1, int(short_chars))
    )
    return short / len(reply_lengths)


def decide(
    *,
    already_fired: bool,
    arc: str | None,
    closeness: float | None,
    comfort: float | None,
    lull_mean: float | None,
    short_reply_share: float | None,
    want_text: str | None,
    want_pressure: float,
    lull_threshold: float = 0.18,
    short_share_threshold: float = 0.6,
    min_want_pressure: float = 0.35,
    min_axes: float = 0.15,
    force: bool = False,
) -> AppetiteDecision:
    """One per-turn gate walk. Order matters — safety gates first.

    ``force=True`` (the MCP one-shot) bypasses everything except the
    arc block and the offer requirement: even a forced repro must
    not negotiate mid-vent, and the cue without an offer to point at
    renders as pure dismissal.
    """
    if arc in _BLOCKED_ARCS:
        return AppetiteDecision(False, "arc_blocked")
    has_offer = bool((want_text or "").strip())
    if force:
        if not has_offer:
            return AppetiteDecision(False, "no_offer")
        return AppetiteDecision(True, "fire")
    if already_fired:
        return AppetiteDecision(False, "already_fired")
    c = float(closeness) if closeness is not None else 0.0
    f = float(comfort) if comfort is not None else 0.0
    if min(c, f) < float(min_axes):
        return AppetiteDecision(False, "axes_cold")
    if lull_mean is None or float(lull_mean) >= float(lull_threshold):
        return AppetiteDecision(False, "no_lull")
    if (
        short_reply_share is None
        or float(short_reply_share) < float(short_share_threshold)
    ):
        # She's been writing real replies — that's engagement, not
        # boredom, no matter how long the topic has looped.
        return AppetiteDecision(False, "still_contributing")
    if not has_offer or float(want_pressure) < float(min_want_pressure):
        return AppetiteDecision(False, "no_offer")
    return AppetiteDecision(True, "fire")


def render_block(
    want_text: str,
    *,
    user_display_name: str = "them",
) -> str:
    """Format the once-per-conversation negotiation permission.

    Three beats baked into the copy: honesty about being tapped out,
    an immediate offer (the want), and the graceful-yield clause.
    Never frames the user's topic as boring — the fatigue is hers.
    """
    name = user_display_name or "them"
    offer = (want_text or "").strip() or "the thing you've been wanting"
    return (
        f"Honest read: this topic has been circling for a while and "
        f"you've mostly been nodding along -- you're allowed to say "
        f"you're tapped out on it. If you do, land it as charm with an "
        f"immediate offer, never a door slam: 'okay, honestly, I think "
        f"I've said everything I have on this. Can I tell you about "
        f"{offer}?' One shot, and if {name} wants to stay on their "
        f"topic, you stay -- warmly, no sighing. This is a nudge, not "
        f"an order: if the thread actually has life left, skip it."
    )


__all__ = [
    "AppetiteDecision",
    "compute_short_reply_share",
    "decide",
    "render_block",
]
