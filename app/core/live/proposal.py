"""Live policy proposal schema and client-side validation.

Ollama receives this object as ``format``. The dataclass parse is the
Pydantic-equivalent guard: strip ``confidence`` (never authorized),
check the intent enum, and leave arbiter policy in :mod:`arbiter`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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
        "arguments": {"type": "object"},
        "reason_code": {"type": "string"},
        "context_refs": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reconsider_after_ms": {"type": "integer"},
        "wake_on": {
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
        reconsider_raw = data.get("reconsider_after_ms")
        reconsider: int | None
        if reconsider_raw in (None, ""):
            reconsider = None
        else:
            reconsider = max(0, _as_int(reconsider_raw, 0))
        return cls(
            snapshot_generation=_as_int(data.get("snapshot_generation"), 0),
            selected_urge_id=urge_id,
            intent=intent,
            arguments=dict(arguments),
            reason_code=str(data.get("reason_code") or "").strip(),
            context_refs=_as_str_tuple(data.get("context_refs")),
            reconsider_after_ms=reconsider,
            wake_on=_as_str_tuple(data.get("wake_on")),
        )


__all__ = [
    "LIVE_POLICY_INTENTS",
    "LIVE_POLICY_JSON_SCHEMA",
    "LivePolicyProposal",
]
