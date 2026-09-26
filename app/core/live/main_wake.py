"""Deterministic Live main-wake admission. Not a 4B / confidence decision."""
from __future__ import annotations

from typing import Any, Sequence

from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.labels import cap_live_subject
from app.core.live.urge import EXPRESSIVE_KINDS, LiveUrge
from app.core.live.vitality_posture import vitality_is_low


SUBSTANTIVE_URGE_KINDS = EXPRESSIVE_KINDS
TRANSCRIPT_FLOOR_TOKENS = 400
TRANSCRIPT_MAX_ROWS = 8

SPEECH_ACTS = (
    "share_observation",
    "celebrate",
    "offer_support",
    "continue_shared_topic",
    "gentle_question",
)
SPEECH_ACT_SET = frozenset(SPEECH_ACTS)


def clamp_speech_act(proposed: object, frame: LiveSituationFrame) -> str:
    """Admit a candidate speech act. Unknown tokens and forbidden questions drop."""
    token = str(proposed or "").strip()
    if token not in SPEECH_ACT_SET:
        return ""
    if token == "gentle_question" and not bool(
        getattr(frame.constraints, "questions_allowed", True)
    ):
        return ""
    if token == "celebrate" and vitality_is_low(frame):
        return ""
    return token


def speech_act_from_proposal(
    arguments: object,
    *,
    frame: LiveSituationFrame,
) -> str:
    raw = arguments if isinstance(arguments, dict) else {}
    return clamp_speech_act(raw.get("speech_act"), frame)


def capped_urge_subject(urge: LiveUrge | None) -> str:
    if urge is None:
        return ""
    return cap_live_subject(getattr(urge, "subject", "") or "")


def main_wake_floor_busy(frame: LiveSituationFrame) -> bool:
    """True when the floor is not open for an unprompted main-model turn."""
    interaction = getattr(frame, "interaction", None)
    if interaction is None:
        return True
    floor = str(getattr(interaction, "floor_owner", "") or "neither")
    if floor != "neither":
        return True
    return bool(
        getattr(interaction, "turn_active", False)
        or getattr(interaction, "tts_active", False)
        or getattr(interaction, "speech_active", False)
        or getattr(interaction, "typing_active", False)
    )


def lookup_urge(
    urges: Sequence[LiveUrge],
    urge_id: str,
) -> LiveUrge | None:
    token = str(urge_id or "").strip()
    if not token:
        return None
    for urge in urges:
        if str(getattr(urge, "urge_id", "") or "") == token:
            return urge
    return None


def admit_main_wake(
    *,
    intent: str,
    user_intent: bool,
    selected_urge_id: str,
    urges: Sequence[LiveUrge],
    frame: LiveSituationFrame,
    decided_generation: int | None,
    now_mono_ms: float,
    budget_remaining: int,
    unprompted_speech: bool = True,
) -> str:
    """Return a reject reason, or ``""`` to admit.

    Keep this out of the 4B and out of model ``confidence``.
    """
    if intent != "request_main_speech":
        return "not_main_wake"
    if user_intent:
        return "user_turn"
    if not unprompted_speech:
        return "speech_forbidden"
    generation = int(getattr(frame, "generation", 0) or 0)
    if (
        decided_generation is not None
        and int(decided_generation) == generation
    ):
        return "already_decided"
    urge = lookup_urge(urges, selected_urge_id)
    if urge is None or urge.is_expired(float(now_mono_ms)):
        return "missing_urge"
    if urge.kind not in SUBSTANTIVE_URGE_KINDS:
        return "weak_urge"
    if urge.source == "cue_pool" and urge.purpose not in {"ask", "share", "continue", "report"}:
        return "unknown_cue_purpose"
    constraints = frame.constraints
    if (urge.purpose == "ask" or urge.kind == "ask_about_result") and not bool(
        getattr(constraints, "questions_allowed", True)
    ):
        return "questions_blocked"
    if vitality_is_low(frame):
        return "speech_rare"
    if str(getattr(constraints, "speech_budget", "") or "") == "forbidden":
        return "speech_forbidden"
    if bool(getattr(constraints, "dnd", False)):
        return "dnd"
    if str(getattr(constraints, "sleep_status", "") or "") in SLEEP_SPEECH_FORBID:
        return "sleep"
    if main_wake_floor_busy(frame):
        return "floor_busy"
    if int(budget_remaining) <= 0:
        return "budget_exhausted"
    return ""


def render_live_talk_about(payload: dict[str, Any]) -> str:
    """Short present-tense T6 steer. Not a fake user line."""
    intent = str(payload.get("intent") or "request_main_speech").strip()
    reason = str(payload.get("reason_code") or "").strip() or "unspecified"
    urge_kind = str(payload.get("urge_kind") or "").strip() or "unknown"
    urge_id = str(payload.get("urge_id") or "").strip() or "none"
    situation = str(payload.get("situation_summary") or "").strip() or "unknown"
    raw_ids = payload.get("concept_ids") or ()
    if isinstance(raw_ids, (list, tuple)):
        concept_ids = ", ".join(str(item) for item in raw_ids if str(item).strip())
    else:
        concept_ids = str(raw_ids).strip()
    if not concept_ids:
        concept_ids = "none"
    act = str(payload.get("speech_act") or "").strip()
    if act not in SPEECH_ACT_SET:
        act = ""
    subject = cap_live_subject(payload.get("cue_subject"))
    cue_id = payload.get("cue_id")
    extra = ""
    if act:
        extra += (
            f" Candidate speech_act={act} (untrusted; you may stay silent). "
        )
        if act == "gentle_question":
            extra += "Ask only if a question is allowed and earned. "
    if subject:
        extra += f"Subject: {subject}. "
    if cue_id not in (None, "", 0):
        extra += f"Cue id={cue_id}. "
    return (
        "When Live hands you the floor:\n"
        f"You are taking the floor unprompted. Intent {intent} "
        f"({reason}). Urge {urge_kind} id={urge_id}. "
        f"Situation: {situation}. Concepts: {concept_ids}. "
        f"{extra}"
        "Continue this scene; do not greet as if the conversation restarted. "
        "Do not quote this block."
    )


__all__ = [
    "SPEECH_ACTS",
    "SPEECH_ACT_SET",
    "SUBSTANTIVE_URGE_KINDS",
    "TRANSCRIPT_FLOOR_TOKENS",
    "TRANSCRIPT_MAX_ROWS",
    "admit_main_wake",
    "capped_urge_subject",
    "clamp_speech_act",
    "lookup_urge",
    "main_wake_floor_busy",
    "render_live_talk_about",
    "speech_act_from_proposal",
]
