"""Live action records. Policy proposes; this module only names results."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from app.core.live.impulse import FORBIDDEN_PAYLOAD_KEYS

LiveActionState = Literal[
    "proposed",
    "authorized",
    "rejected",
    "executing",
    "completed",
    "cancelled",
]

ACTION_RESULT_STATES = frozenset({
    "rejected",
    "executing",
    "completed",
    "cancelled",
})
ACTION_RESULT_KINDS = frozenset({
    "aiko.action_rejected",
    "aiko.action_started",
    "aiko.action_completed",
    "aiko.action_cancelled",
})


def new_action_id() -> str:
    return uuid.uuid4().hex


def action_result_kind(state: str) -> str:
    token = str(state or "").strip()
    if token == "executing":
        return "aiko.action_started"
    if token in {"rejected", "completed", "cancelled"}:
        return f"aiko.action_{token}"
    return ""


@dataclass(frozen=True, slots=True)
class LiveActionRecord:
    action_id: str
    intent: str
    state: str
    generation: int
    reason: str = ""
    urge_id: str = ""
    created_monotonic_ms: float = 0.0
    updated_monotonic_ms: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "intent": self.intent,
            "state": self.state,
            "generation": self.generation,
            "reason": self.reason,
            "urge_id": self.urge_id,
        }

    def to_impulse_payload(self) -> dict[str, Any]:
        payload = self.to_payload()
        for key in list(payload):
            if key in FORBIDDEN_PAYLOAD_KEYS:
                payload.pop(key, None)
        payload.pop("title", None)
        payload.pop("text", None)
        return payload


def start_action(
    *,
    intent: str,
    generation: int,
    reason: str = "",
    urge_id: str = "",
    now_ms: float | None = None,
) -> LiveActionRecord:
    stamp = time.monotonic() * 1000.0 if now_ms is None else float(now_ms)
    return LiveActionRecord(
        action_id=new_action_id(),
        intent=str(intent or ""),
        state="proposed",
        generation=int(generation),
        reason=str(reason or ""),
        urge_id=str(urge_id or ""),
        created_monotonic_ms=stamp,
        updated_monotonic_ms=stamp,
    )


def advance_action(
    record: LiveActionRecord,
    state: str,
    *,
    reason: str = "",
    now_ms: float | None = None,
) -> LiveActionRecord:
    stamp = time.monotonic() * 1000.0 if now_ms is None else float(now_ms)
    return LiveActionRecord(
        action_id=record.action_id,
        intent=record.intent,
        state=str(state or record.state),
        generation=record.generation,
        reason=str(reason or record.reason),
        urge_id=record.urge_id,
        created_monotonic_ms=record.created_monotonic_ms,
        updated_monotonic_ms=stamp,
    )


__all__ = [
    "ACTION_RESULT_KINDS",
    "ACTION_RESULT_STATES",
    "LiveActionRecord",
    "LiveActionState",
    "action_result_kind",
    "advance_action",
    "new_action_id",
    "start_action",
]
