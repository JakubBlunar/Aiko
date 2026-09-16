"""Live policy proposal schema and client-side validation.

Ollama receives this object as ``format``. The dataclass parse is the
Pydantic-equivalent guard: strip ``confidence`` (never authorized),
check the intent enum, and leave arbiter policy in :mod:`arbiter`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.live.micro_utterance import MAX_CHARS, MICRO_DELIVERY, MICRO_INTENTS
from app.core.live.main_wake import SPEECH_ACTS, SPEECH_ACT_SET
from app.core.live.presence import (
    ATTENTION_INTENTS,
    ATTENTION_TARGET_SET,
    ATTENTION_TARGETS,
    PRESENCE_STYLE_SET,
    PRESENCE_STYLES,
    keep_flag,
)
from app.core.live.reaction import (
    REACTION_INTENSITIES,
    REACTION_INTENSITY_SET,
    REACTION_TONE_SET,
    REACTION_TONES,
)
from app.core.live.vitality_posture import (
    VITALITY_POSTURE_SET,
    VITALITY_POSTURES,
)
from app.core.live.wait import (
    WAKE_SETS,
    WAIT_HORIZONS,
    WAKE_SET_SET,
    WAIT_HORIZON_SET,
)

LIVE_POLICY_INTENTS = (
    "noop",
    "wait",
    "attend",
    "acknowledge_user",
    "react_affectively",
    "backchannel_user",
    "remain_present",
    "yield_floor",
    "request_main_speech",
)

LIVE_POLICY_JSON_SCHEMA: dict[str, Any] = {
    "title": "live_policy_proposal",
    "type": "object",
    "properties": {
        "snapshot_generation": {"type": "integer"},
        "selected_urge_id": {"type": ["string", "null"]},
        "intent": {"type": "string", "enum": list(LIVE_POLICY_INTENTS)},
        "arguments": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "delivery": {"type": "string"},
                "text": {"type": "string"},
                "presence_style": {
                    "type": "string",
                    "enum": list(PRESENCE_STYLES),
                },
                "attention_target": {
                    "type": "string",
                    "enum": list(ATTENTION_TARGETS),
                },
                "reaction_tone": {
                    "type": "string",
                    "enum": list(REACTION_TONES),
                },
                "reaction_intensity": {
                    "type": "string",
                    "enum": list(REACTION_INTENSITIES),
                },
                "vitality_posture": {
                    "type": "string",
                    "enum": list(VITALITY_POSTURES),
                },
                "wait_horizon": {
                    "type": "string",
                    "enum": list(WAIT_HORIZONS),
                },
                "wake_set": {
                    "type": "string",
                    "enum": list(WAKE_SETS),
                },
                "keep_attention": {"type": "boolean"},
                "keep_style": {"type": "boolean"},
                "speech_act": {
                    "type": "string",
                    "enum": list(SPEECH_ACTS),
                },
                "fallback_for": {"type": "string"},
            },
        },
        "reason_code": {"type": "string"},
        "context_refs": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "snapshot_generation",
        "selected_urge_id",
        "intent",
        "arguments",
        "reason_code",
        "context_refs",
    ],
    "additionalProperties": False,
}


_COMMON_ARGUMENT_KEYS = frozenset({
    "reasoning", "presence_style", "reaction_tone", "reaction_intensity",
    "vitality_posture", "wait_horizon", "wake_set",
    "keep_attention", "keep_style", "fallback_for",
})
_MICRO_ARGUMENT_KEYS = frozenset({
    "reasoning", "delivery", "text", "presence_style",
    "reaction_tone", "reaction_intensity", "vitality_posture",
    "wait_horizon", "wake_set", "keep_attention", "keep_style",
    "fallback_for",
})
_ATTEND_ARGUMENT_KEYS = frozenset({
    "reasoning", "presence_style", "attention_target",
    "reaction_tone", "reaction_intensity", "vitality_posture",
    "wait_horizon", "wake_set", "keep_attention", "keep_style",
    "fallback_for",
})
_SPEECH_ARGUMENT_KEYS = frozenset({
    "reasoning", "presence_style", "reaction_tone", "reaction_intensity",
    "vitality_posture", "speech_act", "wait_horizon", "wake_set",
    "keep_attention", "keep_style", "fallback_for",
})
_MAX_REASONING_CHARS = 240


def argument_keys_for(intent: str) -> frozenset[str]:
    token = str(intent or "")
    if token in MICRO_INTENTS:
        return _MICRO_ARGUMENT_KEYS
    if token in ATTENTION_INTENTS:
        return _ATTEND_ARGUMENT_KEYS
    if token == "request_main_speech":
        return _SPEECH_ARGUMENT_KEYS
    return _COMMON_ARGUMENT_KEYS


def sanitize_arguments(intent: str, raw: Any) -> dict[str, Any]:
    """Keep only the keys an intent may consume. Never titles or tools."""
    if not isinstance(raw, dict):
        return {}
    allowed = argument_keys_for(intent)
    out: dict[str, Any] = {}
    reasoning = str(raw.get("reasoning") or "").strip()
    if reasoning and "reasoning" in allowed:
        out["reasoning"] = reasoning[:_MAX_REASONING_CHARS]
    if "delivery" in allowed:
        delivery = str(raw.get("delivery") or "").strip()
        text = " ".join(str(raw.get("text") or "").replace("\n", " ").split())
        if delivery == MICRO_DELIVERY and text:
            out["delivery"] = MICRO_DELIVERY
            out["text"] = text[:MAX_CHARS]
    if "presence_style" in allowed:
        style = str(raw.get("presence_style") or "").strip()
        if style in PRESENCE_STYLE_SET:
            out["presence_style"] = style
    if "attention_target" in allowed:
        target = str(raw.get("attention_target") or "").strip()
        if target in ATTENTION_TARGET_SET:
            out["attention_target"] = target
    if "reaction_tone" in allowed:
        tone = str(raw.get("reaction_tone") or "").strip()
        if tone in REACTION_TONE_SET:
            out["reaction_tone"] = tone
    if "reaction_intensity" in allowed:
        intensity = str(raw.get("reaction_intensity") or "").strip()
        if intensity in REACTION_INTENSITY_SET:
            out["reaction_intensity"] = intensity
    if "vitality_posture" in allowed:
        posture = str(raw.get("vitality_posture") or "").strip()
        if posture in VITALITY_POSTURE_SET:
            out["vitality_posture"] = posture
    if "wait_horizon" in allowed:
        horizon = str(raw.get("wait_horizon") or "").strip()
        if horizon in WAIT_HORIZON_SET:
            out["wait_horizon"] = horizon
    if "wake_set" in allowed:
        wake_set = str(raw.get("wake_set") or "").strip()
        if wake_set in WAKE_SET_SET:
            out["wake_set"] = wake_set
    if "keep_attention" in allowed and keep_flag(raw.get("keep_attention")):
        out["keep_attention"] = True
    if "keep_style" in allowed and keep_flag(raw.get("keep_style")):
        out["keep_style"] = True
    if "speech_act" in allowed:
        act = str(raw.get("speech_act") or "").strip()
        if act in SPEECH_ACT_SET:
            out["speech_act"] = act
    if "fallback_for" in allowed:
        token = str(raw.get("fallback_for") or "").strip()
        if token.isalnum() and len(token) <= 32:
            out["fallback_for"] = token
    return out


def _as_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class LivePolicyProposal:
    snapshot_generation: int
    selected_urge_id: str
    intent: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason_code: str = ""
    context_refs: tuple[str, ...] = ()
    reconsider_after_ms: int | None = None
    wake_on: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "snapshot_generation": self.snapshot_generation,
            "selected_urge_id": self.selected_urge_id,
            "intent": self.intent,
            "arguments": dict(self.arguments),
            "reason_code": self.reason_code,
            "context_refs": list(self.context_refs),
        }
        if self.reconsider_after_ms is not None:
            payload["reconsider_after_ms"] = self.reconsider_after_ms
        if self.wake_on:
            payload["wake_on"] = list(self.wake_on)
        return payload

    @classmethod
    def model_validate(cls, raw: Any) -> "LivePolicyProposal":
        """Parse model JSON. Strips ``confidence`` before any other field."""
        payload = raw
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("live policy proposal must be a JSON object")
        data = dict(payload)
        data.pop("confidence", None)
        intent = str(data.get("intent") or "").strip()
        if intent not in LIVE_POLICY_INTENTS:
            raise ValueError(f"unknown live policy intent {intent!r}")
        urge_raw = data.get("selected_urge_id")
        urge_id = "" if urge_raw is None else str(urge_raw).strip()
        arguments = data.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        arguments = sanitize_arguments(intent, arguments)
        return cls(
            snapshot_generation=_as_int(data.get("snapshot_generation"), 0),
            selected_urge_id=urge_id,
            intent=intent,
            arguments=dict(arguments),
            reason_code=str(data.get("reason_code") or "").strip(),
            context_refs=_as_str_tuple(data.get("context_refs")),
            reconsider_after_ms=None,
            wake_on=(),
        )


__all__ = [
    "LIVE_POLICY_INTENTS",
    "LIVE_POLICY_JSON_SCHEMA",
    "LivePolicyProposal",
    "argument_keys_for",
    "sanitize_arguments",
]
