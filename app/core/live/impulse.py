"""Typed Live impulse envelope."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.infra import timephrase


FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "draft",
    "audio",
    "pcm",
    "wav",
    "pointer",
    "title",
    "cursor",
})


def _occurred_at() -> str:
    stamp = timephrase.utcnow().isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def sanitize_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop keys that must never cross the Live contract."""
    if not payload:
        return {}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if str(key) in FORBIDDEN_PAYLOAD_KEYS:
            continue
        out[str(key)] = value
    return out


@dataclass(frozen=True, slots=True)
class LiveImpulse:
    event_id: str
    kind: str
    source: str
    session_key: str
    mode_generation: int
    sequence: int
    occurred_at: str
    monotonic_ms: float
    priority: str
    ttl_ms: int
    coalesce_key: str
    privacy: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source": self.source,
            "session_key": self.session_key,
            "mode_generation": self.mode_generation,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "monotonic_ms": self.monotonic_ms,
            "priority": self.priority,
            "ttl_ms": self.ttl_ms,
            "coalesce_key": self.coalesce_key,
            "privacy": self.privacy,
            "payload": dict(self.payload),
        }

    def is_expired(self, now_mono_ms: float | None = None) -> bool:
        if self.ttl_ms <= 0:
            return False
        now = time.monotonic() * 1000.0 if now_mono_ms is None else now_mono_ms
        return (now - self.monotonic_ms) > float(self.ttl_ms)


def new_impulse(
    *,
    kind: str,
    source: str,
    session_key: str,
    mode_generation: int,
    sequence: int,
    coalesce_key: str,
    privacy: str = "local_state",
    priority: str = "attention",
    ttl_ms: int = 8000,
    payload: Mapping[str, Any] | None = None,
) -> LiveImpulse:
    return LiveImpulse(
        event_id=uuid.uuid4().hex,
        kind=str(kind),
        source=str(source),
        session_key=str(session_key),
        mode_generation=int(mode_generation),
        sequence=int(sequence),
        occurred_at=_occurred_at(),
        monotonic_ms=time.monotonic() * 1000.0,
        priority=str(priority or "attention"),
        ttl_ms=max(0, int(ttl_ms)),
        coalesce_key=str(coalesce_key),
        privacy=str(privacy or "local_state"),
        payload=sanitize_payload(payload),
    )
