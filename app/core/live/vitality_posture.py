"""Affect/vitality posture for Live. The 4B may only quiet; it never writes mood."""
from __future__ import annotations

from typing import Any, Mapping

from app.core.live.frame import LiveSituationFrame


VITALITY_POSTURES = ("keep", "soften", "settle")
VITALITY_POSTURE_SET = frozenset(VITALITY_POSTURES)
LOW_VITALITY_BANDS = frozenset({"low"})
_INTENSITY_RANK = {"low": 0, "mid": 1, "high": 2}
_QUIET_PRESENCE = "give_space"
_SHARED_STYLES = frozenset({"share_delight", "playful"})


def vitality_is_low(frame: LiveSituationFrame) -> bool:
    band = str(getattr(getattr(frame, "aiko", None), "vitality_band", "") or "")
    return band in LOW_VITALITY_BANDS


def clamp_vitality_posture(proposed: object, frame: LiveSituationFrame) -> str:
    """Admit keep/soften/settle. Unknown tokens become keep."""
    del frame
    token = str(proposed or "").strip()
    if token not in VITALITY_POSTURE_SET:
        return "keep"
    return token


def quieter_intensity(current: str, cap: str) -> str:
    now = _INTENSITY_RANK.get(str(current or "mid"), 1)
    limit = _INTENSITY_RANK.get(str(cap or "mid"), 1)
    if limit < now:
        return cap
    return current if current in _INTENSITY_RANK else "mid"


def apply_vitality_posture(
    posture: str,
    *,
    style: str,
    tone: str,
    intensity: str,
    frame: LiveSituationFrame,
) -> tuple[str, str, str]:
    """Lower presence/reaction only. Never raises a budget or mints playful."""
    token = clamp_vitality_posture(posture, frame)
    next_style = str(style or "neutral")
    next_tone = str(tone or "neutral")
    next_intensity = str(intensity or "mid")
    if vitality_is_low(frame) and next_style == "playful":
        next_style = _QUIET_PRESENCE
    if token == "soften":
        if next_style in _SHARED_STYLES:
            next_style = _QUIET_PRESENCE
        next_intensity = quieter_intensity(next_intensity, "mid")
    elif token == "settle":
        if next_style in _SHARED_STYLES or next_style == "playful":
            next_style = _QUIET_PRESENCE
        next_intensity = quieter_intensity(next_intensity, "low")
        if next_tone not in {"drowsy", "concerned"}:
            next_tone = "drowsy"
    return next_style, next_tone, next_intensity


def vitality_from_proposal(
    arguments: Mapping[str, Any] | None,
    *,
    frame: LiveSituationFrame,
) -> str:
    raw = arguments if isinstance(arguments, Mapping) else {}
    return clamp_vitality_posture(raw.get("vitality_posture"), frame)


__all__ = [
    "LOW_VITALITY_BANDS",
    "VITALITY_POSTURES",
    "VITALITY_POSTURE_SET",
    "apply_vitality_posture",
    "clamp_vitality_posture",
    "quieter_intensity",
    "vitality_from_proposal",
    "vitality_is_low",
]
