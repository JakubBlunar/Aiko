"""Inspectable Live admission record. Authorization stays deterministic.

The 12 gates match the Live backlog order. This module names every input
and the first reject; it never reads model ``confidence``, titles, or
rig IDs. Execution side effects stay in the controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.core.live.arbiter import LiveArbiterResult
from app.core.live.capabilities import SemanticCapabilities
from app.core.live.frame import LiveSituationFrame
from app.core.live.proposal import LivePolicyProposal
from app.core.live.resolver import FORBIDDEN_RIG_MARKERS


ADMISSION_GATES = (
    "schema",
    "generation",
    "freshness",
    "world_truth",
    "floor",
    "playback",
    "constraints",
    "hysteresis",
    "repetition",
    "capability",
    "main_wake",
    "executor",
)

_SCHEMA_REJECTS = frozenset({
    "invalid_json",
    "unknown_intent",
    "not_allowed",
    "rig_identifier",
    "unknown_urge",
    "missing_urge",
})
_CONSTRAINT_REASONS = frozenset({
    "budget_exhausted",
    "speech_forbidden",
    "dnd",
    "sleep",
    "questions_blocked",
    "speech_rare",
    "speech_gap",
})
_FLOOR_REASONS = frozenset({
    "floor_busy",
    "user_active",
    "user_turn",
})
_PLAYBACK_REASONS = frozenset({"turn_or_tts"})


@dataclass(frozen=True, slots=True)
class AdmissionGate:
    name: str
    status: str
    reason: str = ""
    inputs: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {"name": self.name, "status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        if self.inputs:
            payload["inputs"] = dict(self.inputs)
        return payload


@dataclass(frozen=True, slots=True)
class LiveAdmissionRecord:
    intent: str
    generation: int
    accepted: bool
    reason: str
    rejected_gate: str = ""
    gates: tuple[AdmissionGate, ...] = ()
    action_id: str = ""
    talk_about: bool = False

    def to_payload(self) -> dict[str, Any]:
        blob = {
            "intent": self.intent,
            "generation": self.generation,
            "accepted": self.accepted,
            "reason": self.reason,
            "rejected_gate": self.rejected_gate,
            "talk_about": self.talk_about,
            "gates": [gate.to_payload() for gate in self.gates],
        }
        if self.action_id:
            blob["action_id"] = self.action_id
        return blob


def _gate(
    name: str,
    *,
    status: str,
    reason: str = "",
    inputs: dict[str, Any] | None = None,
) -> AdmissionGate:
    return AdmissionGate(name=name, status=status, reason=reason, inputs=inputs)


def _fill_skips(
    gates: list[AdmissionGate],
    *,
    reason: str,
) -> list[AdmissionGate]:
    seen = {gate.name for gate in gates}
    for name in ADMISSION_GATES:
        if name not in seen:
            gates.append(_gate(name, status="skip", reason=reason))
    return gates


def parse_error_record(
    *,
    generation: int,
    reason: str = "invalid_json",
) -> LiveAdmissionRecord:
    gates = _fill_skips(
        [_gate("schema", status="reject", reason=reason)],
        reason="schema",
    )
    return LiveAdmissionRecord(
        intent="",
        generation=int(generation),
        accepted=False,
        reason=reason,
        rejected_gate="schema",
        gates=tuple(gates),
    )


def cancelled_record(*, generation: int, intent: str = "") -> LiveAdmissionRecord:
    gates = _fill_skips(
        [
            _gate("schema", status="skip", reason="no_proposal"),
            _gate("generation", status="reject", reason="cancelled"),
        ],
        reason="generation",
    )
    return LiveAdmissionRecord(
        intent=str(intent or ""),
        generation=int(generation),
        accepted=False,
        reason="cancelled",
        rejected_gate="generation",
        gates=tuple(gates),
    )


def _status_for(
    extra: Mapping[str, Any],
    keys: tuple[str, ...],
    reasons: frozenset[str],
) -> tuple[str, str]:
    for key in keys:
        token = str(extra.get(key) or "").strip()
        if token in reasons:
            return "reject", token
    return "pass", ""


def _finish(
    intent: str,
    generation: int,
    accepted: bool,
    reason: str,
    rejected_gate: str,
    gates: Sequence[AdmissionGate],
    action_id: str,
    talk_about: bool,
) -> LiveAdmissionRecord:
    return LiveAdmissionRecord(
        intent=intent,
        generation=generation,
        accepted=accepted,
        reason=reason,
        rejected_gate=rejected_gate,
        gates=tuple(gates),
        action_id=str(action_id or ""),
        talk_about=talk_about,
    )


def build_admission_record(
    *,
    frame: LiveSituationFrame,
    proposal: LivePolicyProposal | None,
    arbiter: LiveArbiterResult,
    extra: Mapping[str, Any] | None = None,
    executed: bool = False,
    cancelled: bool = False,
    capabilities: SemanticCapabilities | None = None,
    action_id: str = "",
) -> LiveAdmissionRecord:
    """Assemble the 12-gate record from arbiter + execute leftovers.

    Does not change authorization. Never copies titles, spoken text,
    or ``confidence``.
    """
    payload = extra if isinstance(extra, Mapping) else {}
    caps = capabilities or SemanticCapabilities()
    intent = str(getattr(proposal, "intent", "") or "")
    generation = int(getattr(frame, "generation", 0) or 0)
    talk_about = bool(arbiter.talk_about)
    gates: list[AdmissionGate] = []

    schema_reason = str(arbiter.reason or "")
    if schema_reason in _SCHEMA_REJECTS:
        gates.append(_gate("schema", status="reject", reason=schema_reason, inputs={
            "intent": intent,
        }))
        _fill_skips(gates, reason="schema")
        return LiveAdmissionRecord(
            intent=intent, generation=generation, accepted=False,
            reason=schema_reason, rejected_gate="schema",
            gates=tuple(gates), action_id=str(action_id or ""),
            talk_about=talk_about,
        )
    gates.append(_gate("schema", status="pass", inputs={"intent": intent}))

    if cancelled or schema_reason == "cancelled":
        gates.append(_gate("generation", status="reject", reason="cancelled", inputs={
            "generation": generation,
        }))
        _fill_skips(gates, reason="generation")
        return LiveAdmissionRecord(
            intent=intent, generation=generation, accepted=False,
            reason="cancelled", rejected_gate="generation",
            gates=tuple(gates), action_id=str(action_id or ""),
            talk_about=talk_about,
        )
    gates.append(_gate("generation", status="pass", inputs={
        "generation": generation,
        "stamped": True,
    }))

    stale = tuple(frame.constraints.stale_sources or ())
    gates.append(_gate("freshness", status="pass", inputs={
        "stale_sources": list(stale),
        "situation_age_ms": int(frame.temporal.situation_age_ms or 0),
    }))
    gates.append(_gate("world_truth", status="pass", inputs={
        "inferred": str(frame.shared.inferred.label or ""),
        "sharing": str(frame.shared.sharing or ""),
        "os_idle": str(frame.shared.os_idle or ""),
    }))

    floor_status, floor_reason = _status_for(
        payload, ("micro_skipped", "main_wake_rejected"),
        _FLOOR_REASONS,
    )
    if str(payload.get("main_wake_rejected") or "") == "floor_busy":
        floor_status, floor_reason = "reject", "floor_busy"
    gates.append(_gate("floor", status=floor_status, reason=floor_reason, inputs={
        "floor_owner": str(frame.interaction.floor_owner or ""),
        "typing": bool(frame.interaction.typing_active),
        "speech": bool(frame.interaction.speech_active),
    }))
    if floor_status == "reject":
        _fill_skips(gates, reason="floor")
        return _finish(
            intent, generation, False, floor_reason, "floor", gates,
            action_id, talk_about,
        )

    play_status, play_reason = _status_for(
        payload, ("micro_skipped", "main_wake_rejected"),
        _PLAYBACK_REASONS,
    )
    gates.append(_gate("playback", status=play_status, reason=play_reason, inputs={
        "turn": bool(frame.interaction.turn_active),
        "tts": bool(frame.interaction.tts_active),
        "playback": bool(frame.interaction.playback_active),
        "capture": bool(frame.interaction.capture_available),
    }))
    if play_status == "reject":
        _fill_skips(gates, reason="playback")
        return _finish(
            intent, generation, False, play_reason, "playback", gates,
            action_id, talk_about,
        )

    constraint_reason = ""
    if payload.get("budget_exhausted"):
        constraint_reason = "budget_exhausted"
    else:
        _, constraint_reason = _status_for(
            payload, ("micro_skipped", "main_wake_rejected"),
            _CONSTRAINT_REASONS,
        )
    constraint_status = "reject" if constraint_reason else "pass"
    gates.append(_gate(
        "constraints", status=constraint_status, reason=constraint_reason,
        inputs={
            "dnd": bool(frame.constraints.dnd),
            "sleep": str(frame.constraints.sleep_status or ""),
            "speech_budget": str(frame.constraints.speech_budget or ""),
        },
    ))
    if constraint_status == "reject":
        _fill_skips(gates, reason="constraints")
        return _finish(
            intent, generation, False, constraint_reason, "constraints",
            gates, action_id, talk_about,
        )

    held = str(payload.get("attention_held") or "")
    gates.append(_gate("hysteresis", status="pass", reason=held, inputs={
        "min_hold_remaining_ms": int(frame.temporal.min_hold_remaining_ms or 0),
        "attention_target": str(frame.attention.target or ""),
        "attention_held": held,
    }))

    if payload.get("repeat_suppressed"):
        gates.append(_gate("repetition", status="reject", reason="repeat_suppressed"))
        _fill_skips(gates, reason="repetition")
        return _finish(
            intent, generation, False, "repeat_suppressed", "repetition",
            gates, action_id, talk_about,
        )
    gates.append(_gate("repetition", status="pass"))

    gates.append(_gate("capability", status="pass", inputs={
        "can_orient": bool(caps.can_orient),
        "can_express": bool(caps.can_express),
        "can_breathe": bool(caps.can_breathe),
        "can_motion": bool(caps.can_motion),
        "privacy": str(frame.constraints.privacy or ""),
    }))

    if intent == "request_main_speech":
        wake_reason = str(payload.get("main_wake_rejected") or "")
        if payload.get("main_wake_admitted"):
            gates.append(_gate("main_wake", status="pass", reason="admitted"))
        elif wake_reason:
            gates.append(_gate("main_wake", status="reject", reason=wake_reason))
            _fill_skips(gates, reason="main_wake")
            return _finish(
                intent, generation, False, wake_reason, "main_wake",
                gates, action_id, talk_about,
            )
        else:
            gates.append(_gate("main_wake", status="skip", reason="not_decided"))
    else:
        gates.append(_gate("main_wake", status="skip", reason="not_requested"))

    exec_reason = str(payload.get("micro_skipped") or "")
    if exec_reason in {"invalid", "missing_text", "delivery_failed"}:
        gates.append(_gate("executor", status="reject", reason=exec_reason))
        return _finish(
            intent, generation, False, exec_reason, "executor",
            gates, action_id, talk_about,
        )
    exec_status = "pass" if executed else "skip"
    if executed:
        exec_status = "pass"
    elif arbiter.accepted:
        exec_status = "pass"
    gates.append(_gate("executor", status=exec_status, inputs={
        "executed": bool(executed),
        "resource_contention": str(frame.constraints.resource_contention or ""),
    }))

    accepted = arbiter.accepted and all(
        gate.status != "reject" for gate in gates
    )
    reason = "ok" if accepted else str(arbiter.reason or "not_executed")
    rejected = next((g.name for g in gates if g.status == "reject"), "")
    if not accepted and rejected:
        reason = next(g.reason for g in gates if g.name == rejected) or reason
    return LiveAdmissionRecord(
        intent=intent,
        generation=generation,
        accepted=accepted,
        reason=reason,
        rejected_gate=rejected,
        gates=tuple(gates),
        action_id=str(action_id or ""),
        talk_about=talk_about,
    )


def admission_contains_forbidden(record: LiveAdmissionRecord) -> bool:
    blob = str(record.to_payload())
    if '"confidence"' in blob.lower():
        return True
    return any(marker in blob for marker in FORBIDDEN_RIG_MARKERS)


__all__ = [
    "ADMISSION_GATES",
    "AdmissionGate",
    "LiveAdmissionRecord",
    "admission_contains_forbidden",
    "build_admission_record",
    "cancelled_record",
    "parse_error_record",
]
