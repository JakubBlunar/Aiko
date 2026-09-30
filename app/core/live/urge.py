"""Ephemeral Live urge records. Inclination is not permission."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

UrgeState = Literal[
    "candidate",
    "selected",
    "parked",
    "consumed",
    "expired",
    "withdrawn",
    "deferred",
]

URGE_KINDS = frozenset({
    "yield_floor",
    "acknowledge_user",
    "comfort",
    "ask_about_result",
    "remain_present",
    "share_delight",
    "share_observation",
    "wait",
})

EXPRESSIVE_KINDS = frozenset({
    "share_delight",
    "share_observation",
    "ask_about_result",
    "comfort",
})

URGE_RANK = {
    "yield_floor": 100,
    "acknowledge_user": 80,
    "comfort": 70,
    "ask_about_result": 50,
    "share_observation": 50,
    "remain_present": 40,
    "share_delight": 30,
    "wait": 10,
}

TERMINAL_STATES = frozenset({"consumed", "expired", "withdrawn"})
ACTIVE_STATES = frozenset({"candidate", "selected", "parked"})


@dataclass(frozen=True, slots=True)
class LiveUrge:
    urge_id: str
    kind: str
    subject: str
    created_at: str
    created_monotonic_ms: float
    expires_after_ms: int
    source: str
    source_ids: tuple[str, ...] = ()
    concept_ids: tuple[int, ...] = ()
    salience_inputs: dict[str, float] = field(default_factory=dict)
    state: str = "candidate"
    repetition_key: str = ""
    cue_id: int | None = None
    purpose: str = ""
    opportunity_seen: bool = False
    reconsidered: bool = False
    source_deadline_ms: float = 0.0
    evaluation_count: int = 0
    next_reconsideration_ms: float = 0.0
    resolution_reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_ids"] = list(self.source_ids)
        payload["concept_ids"] = list(self.concept_ids)
        return payload

    def evidence_key(self) -> str:
        return "|".join(str(item) for item in self.source_ids)

    def is_expired(self, now_mono_ms: float) -> bool:
        if self.expires_after_ms <= 0:
            return False
        return (now_mono_ms - self.created_monotonic_ms) >= float(
            self.expires_after_ms
        )


@dataclass(frozen=True, slots=True)
class LiveNotice:
    notice_id: str
    kind: str
    subject: str
    source: str
    source_ids: tuple[str, ...] = ()
    reason_code: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "notice_id": self.notice_id,
            "kind": self.kind,
            "subject": self.subject,
            "source": self.source,
            "source_ids": list(self.source_ids),
            "reason_code": self.reason_code,
        }
