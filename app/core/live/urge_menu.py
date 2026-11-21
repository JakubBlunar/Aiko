"""Request-local cue references for the Live 4B.

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
    purpose: str = ""

    @property
    def reference(self) -> str:
        return str(self.index)


def build_urge_menu(
    urges: Sequence[LiveUrge],
    *,
    limit: int = MAX_URGE_MENU,
) -> tuple[UrgeMenuItem, ...]:
    """Active urges in display order. Subjects are privacy-capped."""
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
                purpose=urge.purpose,
            )
        )
    return tuple(items)


def render_urge_menu(
    urges: Sequence[LiveUrge], *, now_mono_ms: float | None = None,
    menu: tuple[UrgeMenuItem, ...] | None = None,
) -> str:
    items = build_urge_menu(urges) if menu is None else menu
    if not items:
        return "CANDIDATE URGES: none"
    lines = [
        "CANDIDATE URGES (set selected_urge_id to a ref below, or null):",
    ]
    by_id = {urge.urge_id: urge for urge in urges}
    for item in items:
        parked = " parked" if item.parked else ""
        subject_bit = f" subject={item.subject}" if item.subject else ""
        purpose_bit = f" purpose={item.purpose}" if item.purpose else ""
        lifetime = ""
        if now_mono_ms is not None:
            urge = by_id[item.urge_id]
            remaining = max(0, int(
                urge.created_monotonic_ms + urge.expires_after_ms - now_mono_ms
            ))
            lifetime = f" attempt_remaining_ms={remaining} evaluations={urge.evaluation_count}"
            if urge.source_deadline_ms > 0:
                source_remaining = max(0, int(urge.source_deadline_ms - now_mono_ms))
                lifetime += f" source_remaining_ms={source_remaining}"
        lines.append(
            f'ref="{item.reference}" kind={item.kind}'
            f"{parked}{subject_bit}{purpose_bit}{lifetime}"
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
