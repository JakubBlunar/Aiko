"""Deterministic Live action menu. Not authorization by itself."""
from __future__ import annotations

from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.proposal import LIVE_POLICY_INTENTS
from app.core.live.vitality_posture import vitality_is_low

NONVERBAL_MENU = (
    "noop",
    "wait",
    "attend",
    "acknowledge_user",
    "react_affectively",
    "remain_present",
    "yield_floor",
)
SPEECH_MENU = (
    "backchannel_user",
    "request_main_speech",
)
SLEEP_MENU = (
    "noop",
    "wait",
    "remain_present",
)


def speech_menu_open(frame: LiveSituationFrame) -> bool:
    """True when the 4B may even *see* speech intents."""
    constraints = frame.constraints
    if str(constraints.sleep_status or "") in SLEEP_SPEECH_FORBID:
        return False
    if bool(constraints.dnd):
        return False
    if str(constraints.speech_budget or "") == "forbidden":
        return False
    interaction = frame.interaction
    if interaction.turn_active or interaction.tts_active or interaction.playback_active:
        return False
    if str(interaction.floor_owner or "") in {"user", "aiko", "transition"}:
        return False
    if interaction.typing_active or interaction.speech_active:
        return False
    return True


def allowed_actions_for(frame: LiveSituationFrame) -> tuple[str, ...]:
    """Hint menu derived from floor, sleep, DND, and speech budget.

    Empty is not a legal result: the 4B can always wait. The arbiter may
    still reject a listed intent for a later gate (budget, urge, generation).
    """
    if str(frame.constraints.sleep_status or "") in SLEEP_SPEECH_FORBID:
        return SLEEP_MENU
    if speech_menu_open(frame):
        menu = NONVERBAL_MENU + SPEECH_MENU
        if vitality_is_low(frame):
            menu = tuple(
                item for item in menu if item != "request_main_speech"
            )
        return menu
    return NONVERBAL_MENU


def intent_is_allowed(intent: str, allowed: tuple[str, ...]) -> bool:
    """Unknown or empty menus do not authorize; they also do not reject."""
    token = str(intent or "").strip()
    if token not in LIVE_POLICY_INTENTS:
        return False
    if not allowed:
        return True
    return token in allowed


__all__ = [
    "NONVERBAL_MENU",
    "SLEEP_MENU",
    "SPEECH_MENU",
    "allowed_actions_for",
    "intent_is_allowed",
    "speech_menu_open",
]
