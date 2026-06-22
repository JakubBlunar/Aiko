"""J6 — conflict-repair memory helpers (pure).

K8 (``affect_rupture_detector``) detects an in-the-moment valence dip but
has no notion of *resolution*. J6 layers a small recovery tracker on top:
when a rupture fires we arm a :class:`RepairWatch`; on subsequent turns,
if the user's valence climbs back, we record a durable ``repair``-vibe
shared moment ("we worked through this") so Aiko can later reference the
resolution instead of re-litigating.

These helpers are pure (no I/O, no controller) so the recovery predicate
and the summary phrasing are unit-testable in isolation. The watch state
itself lives in-memory on the SessionController.

Tone guard (see ``docs/personality-backlog/moments.md`` § J6): the record
captures "we're good at sorting things out", never a grievance ledger.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RepairWatch:
    """In-flight tracking of a rupture waiting to be resolved."""

    recovery_target: float  # valence to climb back toward (pre-rupture)
    dip_floor: float        # valence at the dip (rupture current_valence)
    topic: str              # short hint of what the friction was about
    turns_left: int         # remaining post-turns to watch for recovery


def clean_topic(user_text: str | None, *, max_len: int = 60) -> str:
    """Collapse whitespace and clip the user's message to a short hint."""
    text = " ".join((user_text or "").split())
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "\u2026"
    return text


def has_recovered(
    current_valence: float,
    watch: RepairWatch,
    *,
    epsilon: float = 0.05,
    min_rise: float = 0.10,
) -> bool:
    """True when valence has climbed back from the dip.

    Two ways to qualify: it returned to (near) the pre-rupture level, OR
    it rose meaningfully above the dip floor (handles cases where the
    pre-rupture baseline was itself low).
    """
    back_to_baseline = current_valence >= (watch.recovery_target - epsilon)
    rose_from_floor = (current_valence - watch.dip_floor) >= min_rise
    return bool(back_to_baseline or rose_from_floor)


def build_repair_summary(user_display_name: str, topic: str) -> str:
    """Deterministic, tone-safe summary for the repair shared moment."""
    name = (user_display_name or "").strip() or "they"
    if topic:
        return (
            f"You and {name} hit a tense patch around \"{topic}\" and worked "
            "through it -- you talked it out and were okay after."
        )
    return (
        f"You and {name} hit a tense patch and worked through it -- you "
        "talked it out and were okay after."
    )
