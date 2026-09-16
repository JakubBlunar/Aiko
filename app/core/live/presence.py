"""Bounded Live presence style and attention-target admission.

The 4B may pick a style and, for attend/remain_present, a target. Python
clamps both. Style cannot mint a shared activity, raise a budget, or
override sleep/DND/lock. Hysteresis and the attention-switch budget still
own target changes.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.core.activity.evidence import CODING_CONFIDENCE_FLOOR
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.vitality_posture import vitality_is_low


PRESENCE_STYLES = (
    "neutral",
    "cofocus",
    "give_space",
    "share_delight",
    "soft_support",
    "playful",
)
PRESENCE_STYLE_SET = frozenset(PRESENCE_STYLES)
ATTENTION_TARGETS = (
    "user",
    "cursor",
    "shared_activity",
    "world_entity",
    "none",
)
ATTENTION_TARGET_SET = frozenset(ATTENTION_TARGETS)
ATTENTION_INTENTS = frozenset({"attend", "remain_present"})
SHARED_STYLES = frozenset({"share_delight", "playful"})
QUIET_PRESENCE = "give_space"

STYLE_SPEECH_CAP = {
    "neutral": "open",
    "cofocus": "rare",
    "give_space": "rare",
    "share_delight": "rare",
    "soft_support": "rare",
    "playful": "normal",
}
STYLE_INTENSITY_CAP = {
    "neutral": 1.0,
    "cofocus": 0.6,
    "give_space": 0.3,
    "share_delight": 0.5,
    "soft_support": 0.5,
    "playful": 0.7,
}

_FOCUS_LABELS = frozenset({"user_coding", "user_focused"})
_IDLE_WORLD = frozenset({"", "idle", "relaxing"})
_SPEECH_RANK = {"forbidden": 0, "rare": 1, "normal": 2, "open": 3}


def quieter_speech(current: str, cap: str) -> str:
    if _SPEECH_RANK.get(cap, 2) < _SPEECH_RANK.get(current, 2):
        return cap
    return current


def _coding_fresh(frame: LiveSituationFrame) -> bool:
    inferred = str(frame.shared.inferred.label or "")
    os_idle = str(frame.shared.os_idle or "missing")
    confidence = float(frame.shared.inferred.confidence or 0.0)
    c6_stale = "c6" in frame.constraints.stale_sources
    return bool(
        inferred in _FOCUS_LABELS
        and not c6_stale
        and os_idle != "idle"
        and (
            confidence >= CODING_CONFIDENCE_FLOOR
            or os_idle == "missing"
        )
    )


def _quiet_constraint(frame: LiveSituationFrame) -> bool:
    if bool(frame.constraints.dnd):
        return True
    if str(frame.constraints.sleep_status or "") in SLEEP_SPEECH_FORBID:
        return True
    return str(frame.shared.os_idle or "missing") == "locked"


def user_interrupting(frame: LiveSituationFrame) -> bool:
    interaction = frame.interaction
    if interaction.typing_active or interaction.speech_active:
        return True
    return str(interaction.floor_owner or "") in {"user", "transition"}


def sharing_active(frame: LiveSituationFrame) -> bool:
    return (
        str(frame.shared.sharing or "") == "shared"
        and not _coding_fresh(frame)
    )


def allowed_attention_targets(frame: LiveSituationFrame) -> frozenset[str]:
    if str(frame.constraints.sleep_status or "") in SLEEP_SPEECH_FORBID:
        return frozenset({"none"})
    allowed = {"none", "user", "cursor"}
    if sharing_active(frame):
        allowed.add("shared_activity")
    activity = str(frame.shared.world_activity or "").strip().lower()
    if activity not in _IDLE_WORLD:
        allowed.add("world_entity")
    return frozenset(allowed)


def keep_flag(raw: object) -> bool:
    """Admit a keep_* boolean. Unknown tokens are false."""
    if raw is True or raw == 1:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def hold_flags_from_proposal(
    arguments: Mapping[str, Any] | None,
) -> tuple[bool, bool]:
    raw = arguments if isinstance(arguments, Mapping) else {}
    return keep_flag(raw.get("keep_attention")), keep_flag(raw.get("keep_style"))


def clamp_presence_style(
    proposed: object,
    frame: LiveSituationFrame,
    *,
    keep_style: bool = False,
    current_style: str = "",
) -> str:
    """Admit a style. Unknown tokens become neutral; constraints win."""
    if keep_style:
        held = str(current_style or "").strip()
        if held in PRESENCE_STYLE_SET:
            proposed = held
    token = str(proposed or "").strip()
    if token not in PRESENCE_STYLE_SET:
        token = "neutral"
    if _quiet_constraint(frame):
        return QUIET_PRESENCE
    if vitality_is_low(frame) and token == "playful":
        return QUIET_PRESENCE
    coding = _coding_fresh(frame)
    interrupting = user_interrupting(frame)
    shared = sharing_active(frame)
    if token in SHARED_STYLES and (coding or not shared):
        return "cofocus" if interrupting else QUIET_PRESENCE
    if coding and interrupting and token not in {"cofocus", QUIET_PRESENCE}:
        return "cofocus"
    if coding and token not in {"neutral", "cofocus", QUIET_PRESENCE, "soft_support"}:
        return QUIET_PRESENCE
    return token


def admit_attention_target(
    proposed: object,
    *,
    intent: str,
    frame: LiveSituationFrame,
    keep_attention: bool = False,
) -> str:
    """Keep assembler hysteresis. Policy cannot mint a shared target."""
    current = str(frame.attention.target or "none")
    if str(intent or "") not in ATTENTION_INTENTS:
        return current
    allowed = allowed_attention_targets(frame)
    if current not in allowed:
        current = "none" if "none" in allowed else next(iter(allowed))
    token = str(proposed or "").strip()
    interrupting = user_interrupting(frame)
    if interrupting:
        if token == "user" and "user" in allowed:
            return "user"
        return current
    if keep_attention:
        return current
    if token not in ATTENTION_TARGET_SET or token not in allowed:
        return current
    if token == current:
        return current
    held = int(frame.temporal.min_hold_remaining_ms or 0) > 0
    if held:
        return current
    return token


def presence_from_proposal(
    arguments: Mapping[str, Any] | None,
    *,
    intent: str,
    frame: LiveSituationFrame,
    keep_attention: bool = False,
    keep_style: bool = False,
    current_style: str = "",
) -> tuple[str, str]:
    raw = arguments if isinstance(arguments, Mapping) else {}
    style = clamp_presence_style(
        raw.get("presence_style"),
        frame,
        keep_style=keep_style,
        current_style=current_style,
    )
    target = admit_attention_target(
        raw.get("attention_target"),
        intent=intent,
        frame=frame,
        keep_attention=keep_attention,
    )
    return style, target


def style_visuals(
    style: str,
    *,
    gaze: str,
    body: str,
    expression: str,
) -> tuple[str, str, str]:
    """Pick among already-allowed semantic classes. No new palette."""
    token = str(style or "neutral")
    if token == "give_space":
        return "rest", "settle", "content" if expression != "none" else "none"
    if token == "cofocus":
        return "user_eye_contact", "lean_in", "attentive"
    if token == "share_delight":
        return "rest", "settle", "content"
    if token == "soft_support":
        return "user_eye_contact", "lean_in", "attentive"
    if token == "playful":
        return gaze if gaze != "none" else "cursor_follow", "perk", "content"
    return gaze, body, expression


__all__ = [
    "ATTENTION_INTENTS",
    "ATTENTION_TARGETS",
    "ATTENTION_TARGET_SET",
    "PRESENCE_STYLES",
    "PRESENCE_STYLE_SET",
    "QUIET_PRESENCE",
    "STYLE_INTENSITY_CAP",
    "STYLE_SPEECH_CAP",
    "admit_attention_target",
    "allowed_attention_targets",
    "clamp_presence_style",
    "hold_flags_from_proposal",
    "keep_flag",
    "presence_from_proposal",
    "quieter_speech",
    "style_visuals",
    "user_interrupting",
]
