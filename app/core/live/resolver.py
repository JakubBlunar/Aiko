"""Deterministic LiveBehaviorResolver — semantic intention to plan.

Phase 5 has no policy model. This maps the situation frame (attention,
world activity, sleep, floor) onto a ``ResolvedBehaviorPlan`` whose
fields are semantic classes only: gaze/body/breath/expression/motion
names like ``rest`` / ``settle`` / ``slow``. It never emits Param IDs,
``.exp3`` / ``.motion3`` filenames, motion group names, or focus
coordinates. The frontend IdleLifeChannel is the only layer that
knows the rig.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.core.live.capabilities import SemanticCapabilities
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame


SEMANTIC_INTENTS = frozenset({
    "noop",
    "wait",
    "attend",
    "acknowledge_user",
    "react_affectively",
    "backchannel_user",
    "remain_present",
    "yield_floor",
    "request_main_speech",
})

GAZE_CLASSES = frozenset({
    "none",
    "rest",
    "window",
    "user_eye_contact",
    "cursor_follow",
})
BODY_CLASSES = frozenset({
    "none",
    "lean_in",
    "settle",
    "slump",
    "perk",
    "open",
})
BREATH_CLASSES = frozenset({"none", "normal", "slow", "quiet"})
EXPRESSION_CLASSES = frozenset({
    "none",
    "attentive",
    "content",
    "drowsy",
})
MOTION_CLASSES = frozenset({"none"})

FORBIDDEN_RIG_MARKERS = (
    "Param",
    ".exp3",
    ".motion3",
    "motion_group",
    "ParamBody",
    "ParamBreath",
    "ParamEye",
    "ParamAngle",
)

SLEEP_DEGRADE = SLEEP_SPEECH_FORBID

_WORLD_BODY = {
    "reading": "settle",
    "napping": "slump",
    "looking_outside": "open",
    "watching_screens": "settle",
    "tinkering": "lean_in",
    "stretching": "perk",
    "snacking": "settle",
    "doodling": "settle",
    "thinking": "settle",
}
_WORLD_GAZE = {
    "looking_outside": "window",
    "watching_screens": "rest",
    "reading": "rest",
    "napping": "rest",
    "doodling": "rest",
}
_WORLD_BREATH = {
    "napping": "quiet",
    "reading": "slow",
    "thinking": "slow",
}
_POSTURE_BODY = {
    "lying": "slump",
    "curled_up": "settle",
    "leaning": "lean_in",
}


@dataclass(frozen=True, slots=True)
class ResolvedBehaviorPlan:
    intent: str = "wait"
    attention_target: str = "none"
    gaze_class: str = "rest"
    body_class: str = "none"
    breath_class: str = "normal"
    expression_class: str = "none"
    motion_class: str = "none"
    required_capabilities: tuple[str, ...] = ()
    interruptible: bool = True
    ttl_ms: int = 0
    hold_attention: bool = False
    run_during_user_speech: bool = False
    run_during_typing: bool = False
    run_during_turn: bool = False
    run_during_tts: bool = False
    degrade_to_sleep: bool = False
    cancel_reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _activity_key(frame: LiveSituationFrame) -> str:
    return str(frame.aiko.activity or frame.shared.world_activity or "").strip().lower()


def _posture_key(frame: LiveSituationFrame) -> str:
    return str(frame.aiko.posture or frame.shared.world_posture or "").strip().lower()


NONVERBAL_POLICY_INTENTS = frozenset({
    "noop",
    "wait",
    "attend",
    "acknowledge_user",
    "react_affectively",
    "backchannel_user",
    "remain_present",
    "yield_floor",
})


def _visual_intent(intent: str) -> str:
    """Map a policy intent onto the Pass 5 gaze/body/expression tables."""
    if intent in {"acknowledge_user", "react_affectively", "backchannel_user"}:
        return "attend"
    if intent == "yield_floor":
        return "remain_present"
    if intent == "noop":
        return "wait"
    return intent


def intent_from_frame(frame: LiveSituationFrame) -> str:
    """Deterministic intention. Phase 5 does not call a policy model."""
    sleep = str(frame.constraints.sleep_status or "awake")
    if sleep in SLEEP_DEGRADE:
        return "remain_present"
    floor = frame.interaction.floor_owner
    if floor in {"user", "transition"} or frame.interaction.typing_active:
        return "attend"
    if frame.interaction.speech_active:
        return "attend"
    if frame.commitment.intention in SEMANTIC_INTENTS:
        return frame.commitment.intention
    if frame.attention.target == "user":
        return "attend"
    if frame.attention.target in {"shared_activity", "world_entity"}:
        return "remain_present"
    return "wait"


def _gaze_for(frame: LiveSituationFrame, intent: str) -> str:
    if intent == "attend":
        return "user_eye_contact"
    activity = _activity_key(frame)
    if activity in _WORLD_GAZE:
        return _WORLD_GAZE[activity]
    if frame.attention.target == "shared_activity":
        return "rest"
    if frame.attention.target == "world_entity":
        return _WORLD_GAZE.get(activity, "rest")
    return "cursor_follow"


def _body_for(frame: LiveSituationFrame, intent: str) -> str:
    if intent == "attend":
        return "lean_in"
    activity = _activity_key(frame)
    if activity in _WORLD_BODY:
        return _WORLD_BODY[activity]
    posture = _posture_key(frame)
    if posture in _POSTURE_BODY:
        return _POSTURE_BODY[posture]
    return "none"


def _breath_for(frame: LiveSituationFrame) -> str:
    activity = _activity_key(frame)
    if activity in _WORLD_BREATH:
        return _WORLD_BREATH[activity]
    return "normal"


def _expression_for(intent: str, body_class: str) -> str:
    if intent == "attend":
        return "attentive"
    if body_class in {"settle", "open"}:
        return "content"
    if body_class == "slump":
        return "drowsy"
    return "none"


def _degrade(
    plan: ResolvedBehaviorPlan,
    caps: SemanticCapabilities,
) -> ResolvedBehaviorPlan:
    body = plan.body_class if caps.can_orient else "none"
    breath = plan.breath_class if caps.can_breathe else "none"
    expression = plan.expression_class if caps.can_express else "none"
    motion = plan.motion_class if caps.can_motion else "none"
    required: list[str] = []
    if body != "none":
        required.append("can_orient")
    if breath not in {"none", "normal"}:
        required.append("can_breathe")
    if expression != "none":
        required.append("can_express")
    if motion != "none":
        required.append("can_motion")
    return dataclass_replace(plan, body=body, breath=breath,
                             expression=expression, motion=motion,
                             required=tuple(required))


def dataclass_replace(
    plan: ResolvedBehaviorPlan,
    *,
    body: str,
    breath: str,
    expression: str,
    motion: str,
    required: tuple[str, ...],
) -> ResolvedBehaviorPlan:
    return ResolvedBehaviorPlan(
        intent=plan.intent,
        attention_target=plan.attention_target,
        gaze_class=plan.gaze_class,
        body_class=body,
        breath_class=breath,
        expression_class=expression,
        motion_class=motion,
        required_capabilities=required,
        interruptible=plan.interruptible,
        ttl_ms=plan.ttl_ms,
        hold_attention=plan.hold_attention,
        run_during_user_speech=plan.run_during_user_speech,
        run_during_typing=plan.run_during_typing,
        run_during_turn=plan.run_during_turn,
        run_during_tts=plan.run_during_tts,
        degrade_to_sleep=plan.degrade_to_sleep,
        cancel_reason=plan.cancel_reason,
    )


def plan_contains_rig_identifiers(plan: ResolvedBehaviorPlan) -> bool:
    blob = str(plan.to_payload())
    return any(marker in blob for marker in FORBIDDEN_RIG_MARKERS)


class LiveBehaviorResolver:
    """Map a frame + semantic capabilities to a ResolvedBehaviorPlan."""

    def resolve(
        self,
        frame: LiveSituationFrame,
        capabilities: SemanticCapabilities | None = None,
        *,
        policy_intent: str | None = None,
        ttl_ms: int | None = None,
    ) -> ResolvedBehaviorPlan:
        caps = capabilities or SemanticCapabilities()
        sleep = str(frame.constraints.sleep_status or "awake")
        degrade_sleep = sleep in SLEEP_DEGRADE
        token = str(policy_intent or "").strip()
        if token in NONVERBAL_POLICY_INTENTS:
            intent = token
        else:
            intent = intent_from_frame(frame)
        if degrade_sleep:
            plan = ResolvedBehaviorPlan(
                intent="remain_present",
                attention_target="none",
                gaze_class="rest",
                body_class="none",
                breath_class="none",
                expression_class="none",
                motion_class="none",
                interruptible=True,
                ttl_ms=0,
                hold_attention=False,
                degrade_to_sleep=True,
                cancel_reason="sleep_channel",
            )
            return _degrade(plan, caps)
        visual = _visual_intent(intent)
        gaze = _gaze_for(frame, visual)
        body = _body_for(frame, visual)
        breath = _breath_for(frame)
        expression = _expression_for(visual, body)
        hold = frame.attention.target in {"shared_activity", "world_entity", "user"}
        if ttl_ms is None:
            plan_ttl = max(0, int(frame.temporal.min_hold_remaining_ms))
        else:
            plan_ttl = max(0, int(ttl_ms))
        plan = ResolvedBehaviorPlan(
            intent=intent,
            attention_target=frame.attention.target,
            gaze_class=gaze,
            body_class=body,
            breath_class=breath,
            expression_class=expression,
            motion_class="none",
            interruptible=True,
            ttl_ms=plan_ttl,
            hold_attention=hold,
            run_during_user_speech=False,
            run_during_typing=False,
            run_during_turn=False,
            run_during_tts=False,
            degrade_to_sleep=False,
        )
        return _degrade(plan, caps)
