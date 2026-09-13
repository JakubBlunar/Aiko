"""Capability-derived semantic classes for Live behavior.

The policy and the situation frame see ``can_orient`` / ``can_express`` /
``can_motion`` / ``can_breathe``. They never see Param IDs, expression
filenames, or motion group names. Those stay inside the frontend
resolver that talks to the rig.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SemanticCapabilities:
    can_orient: bool = False
    can_express: bool = False
    can_motion: bool = False
    can_breathe: bool = False

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _flag(caps: Mapping[str, Any], *names: str) -> bool:
    return any(bool(caps.get(name)) for name in names)


def semantic_capabilities_from_profile(
    profile: Mapping[str, Any] | None,
) -> SemanticCapabilities:
    """Map an ``avatar_payload`` / AvatarProfile dict to semantic classes."""
    if not profile:
        return SemanticCapabilities()
    flags = profile.get("capabilities")
    if not isinstance(flags, Mapping):
        flags = {}
    expressions = profile.get("expressions") or ()
    mapping = profile.get("reaction_mapping") or {}
    motions = profile.get("motions") or {}
    idle_group = profile.get("idle_motion_group")
    return SemanticCapabilities(
        can_orient=_flag(flags, "has_body_angle_y", "has_body_angle_z"),
        can_express=bool(expressions) or bool(mapping),
        can_motion=bool(motions) or bool(idle_group),
        can_breathe=_flag(flags, "has_breath"),
    )
