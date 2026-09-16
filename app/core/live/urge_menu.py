"""Numbered peek-only urge menu for the Live 4B.

The model may copy ``selected_urge_id`` from this list or omit it.
It never takes a cue, never sees titles or cue bodies, and never
invents ids.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.core.live.labels import cap_live_subject
from app.core.live.urge import ACTIVE_STATES, URGE_RANK, LiveUrge


MAX_URGE_MENU = 8


@dataclass(frozen=True, slots=True)
class UrgeMenuItem:
    index: int
    urge_id: str
    kind: str
    subject: str = ""
    parked: bool = False


def build_urge_menu(
    urges: Sequence[LiveUrge],
    *,
    limit: int = MAX_URGE_MENU,
) -> tuple[UrgeMenuItem, ...]:
    """Active urges as a numbered pick list. Subjects are privacy-capped."""
    cap = max(0, int(limit))
    active = [
        urge for urge in urges
        if str(getattr(urge, "state", "") or "") in ACTIVE_STATES
        and str(getattr(urge, "urge_id", "") or "").strip()
    ]
    active.sort(
        key=lambda urge: (
            -int(URGE_RANK.get(str(urge.kind), 0)),
            -float(urge.created_monotonic_ms or 0.0),
        )
    )
    items: list[UrgeMenuItem] = []
    for index, urge in enumerate(active[:cap], start=1):
        items.append(
            UrgeMenuItem(
                index=index,
                urge_id=str(urge.urge_id),
                kind=str(urge.kind or ""),
                subject=cap_live_subject(urge.subject),
                parked=str(urge.state) == "parked",
            )
        )
    return tuple(items)


def render_urge_menu(urges: Sequence[LiveUrge]) -> str:
    items = build_urge_menu(urges)
    if not items:
        return "CANDIDATE URGES: none"
    lines = [
        "CANDIDATE URGES (pick selected_urge_id or omit for wait/noop):",
    ]
    for item in items:
        parked = " parked" if item.parked else ""
        subject_bit = f" subject={item.subject}" if item.subject else ""
        lines.append(
            f"{item.index}. id={item.urge_id} kind={item.kind}"
            f"{parked}{subject_bit}"
        )
    return "\n".join(lines)


def menu_urge_ids(urges: Sequence[LiveUrge]) -> tuple[str, ...]:
    return tuple(item.urge_id for item in build_urge_menu(urges))


__all__ = [
    "MAX_URGE_MENU",
    "UrgeMenuItem",
    "build_urge_menu",
    "menu_urge_ids",
    "render_urge_menu",
]
