"""Bounded Live reaction tone and intensity. Never rig IDs or titles."""
from __future__ import annotations

from typing import Any, Mapping

from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.presence import user_interrupting


REACTION_TONES = (
    "neutral",
    "warm",
    "curious",
    "amused",
    "proud",
    "concerned",
    "drowsy",
)
REACTION_TONE_SET = frozenset(REACTION_TONES)
REACTION_INTENSITIES = ("low", "mid", "high")
REACTION_INTENSITY_SET = frozenset(REACTION_INTENSITIES)
INTENSITY_VALUE = {"low": 0.33, "mid": 0.66, "high": 1.0}

TONE_EXPRESSION = {
    "warm": "warm",
    "curious": "curious",
    "amused": "amused",
    "proud": "proud",
    "concerned": "concerned",
    "drowsy": "drowsy",
}
TONE_BODY = {
    "warm": "settle",
    "curious": "lean_in",
    "amused": "perk",
    "proud": "open",
    "concerned": "settle",
    "drowsy": "slump",
}


def band_from_value(value: float) -> str:
    if value <= INTENSITY_VALUE["low"]:
        return "low"
    if value <= INTENSITY_VALUE["mid"]:
        return "mid"
    return "high"


def _has_user_meaning(frame: LiveSituationFrame) -> bool:
    if user_interrupting(frame):
        return True
    return bool(str(frame.interaction.last_user_meaning or "").strip())


def clamp_reaction_tone(proposed: object, frame: LiveSituationFrame) -> str:
    """Admit a tone. Unknown tokens become neutral; evidence wins."""
    token = str(proposed or "").strip()
    if token not in REACTION_TONE_SET:
        token = "neutral"
    sleep = str(frame.constraints.sleep_status or "")
    if sleep in SLEEP_SPEECH_FORBID:
        return "drowsy"
    if bool(frame.constraints.dnd) or str(frame.shared.os_idle or "") == "locked":
        return "neutral"
    if token == "concerned" and not _has_user_meaning(frame):
        return "neutral"
    return token


def clamp_reaction_intensity(proposed: object, frame: LiveSituationFrame) -> str:
    """Snap to low/mid/high, then only lower to max_reaction_intensity."""
    token = str(proposed or "").strip()
    if token not in REACTION_INTENSITY_SET:
        token = "mid"
    cap = float(frame.constraints.max_reaction_intensity or 1.0)
    if cap < 0.0:
        cap = 0.0
    if cap > 1.0:
        cap = 1.0
    value = min(INTENSITY_VALUE[token], cap)
    return band_from_value(value)


def reaction_from_proposal(
    arguments: Mapping[str, Any] | None,
    *,
    frame: LiveSituationFrame,
) -> tuple[str, str]:
    raw = arguments if isinstance(arguments, Mapping) else {}
    tone = clamp_reaction_tone(raw.get("reaction_tone"), frame)
    intensity = clamp_reaction_intensity(raw.get("reaction_intensity"), frame)
    return tone, intensity


def tone_visuals(
    tone: str,
    *,
    gaze: str,
    body: str,
    expression: str,
    intensity: str,
) -> tuple[str, str, str]:
    """Map tone onto semantic expression/body classes. Gaze stays stance."""
    token = str(tone or "neutral")
    expr = TONE_EXPRESSION.get(token)
    if expr:
        expression = expr
    if token != "neutral" and str(intensity or "mid") != "low":
        overlay = TONE_BODY.get(token)
        if overlay:
            body = overlay
    return gaze, body, expression


__all__ = [
    "INTENSITY_VALUE",
    "REACTION_INTENSITIES",
    "REACTION_INTENSITY_SET",
    "REACTION_TONES",
    "REACTION_TONE_SET",
    "clamp_reaction_intensity",
    "clamp_reaction_tone",
    "reaction_from_proposal",
    "tone_visuals",
]
