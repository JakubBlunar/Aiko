"""Single authority for autonomous room-state mutation admission."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.core.infra import timephrase


WORLD_INTENTIONAL_STATE_KEY = "world.intentional_state_at"
STATE_MUTATIONS = frozenset({"location", "scene", "posture", "activity"})


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True, slots=True)
class WorldMutationDecision:
    allowed: bool
    reason: str
    mutation_classes: tuple[str, ...]
    situation_generation: int = 0
    checked_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class WorldMutationGuard:
    """Combine deliberate-placement and shared-conversation leases."""

    def __init__(
        self,
        *,
        kv_get: Callable[[str], str | None],
        situation_snapshot_provider: Callable[[], Any] | None,
        intentional_hold_seconds: float,
        sleep_state_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._kv_get = kv_get
        self._situation_snapshot_provider = situation_snapshot_provider
        self._intentional_hold_seconds = max(0.0, float(intentional_hold_seconds))
        self._sleep_state_provider = sleep_state_provider
        self._lock = threading.Lock()
        self._last_decision = WorldMutationDecision(
            allowed=True,
            reason="not_checked",
            mutation_classes=(),
        )

    def check_autonomous(
        self,
        mutation_classes: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> WorldMutationDecision:
        classes = tuple(sorted({str(value) for value in mutation_classes if value}))
        checked_at = (now or timephrase.utcnow()).isoformat(timespec="seconds")
        if not (set(classes) & STATE_MUTATIONS):
            return self._record(
                WorldMutationDecision(True, "item_only", classes, checked_at=checked_at)
            )

        current = now or timephrase.utcnow()
        if self._sleep_state_provider is not None:
            try:
                sleep = self._sleep_state_provider()
                status = (
                    sleep.get("status")
                    if isinstance(sleep, dict)
                    else getattr(sleep, "status", sleep)
                )
            except Exception:
                status = "awake"
            if str(status or "").strip().lower() == "asleep":
                return self._record(
                    WorldMutationDecision(
                        False,
                        "aiko_asleep",
                        classes,
                        checked_at=checked_at,
                    )
                )
        if self._intentional_hold_active(current):
            return self._record(
                WorldMutationDecision(
                    False,
                    "intentional_hold",
                    classes,
                    checked_at=checked_at,
                )
            )

        snapshot = None
        if self._situation_snapshot_provider is not None:
            try:
                snapshot = self._situation_snapshot_provider()
            except Exception:
                snapshot = None
        if snapshot is not None and bool(
            getattr(snapshot, "shared_commitment_active", False)
        ):
            return self._record(
                WorldMutationDecision(
                    False,
                    "active_shared_conversation_situation",
                    classes,
                    situation_generation=int(
                        getattr(snapshot, "generation", 0) or 0
                    ),
                    checked_at=checked_at,
                )
            )
        return self._record(
            WorldMutationDecision(True, "allowed", classes, checked_at=checked_at)
        )

    def last_decision(self) -> dict[str, Any]:
        with self._lock:
            return self._last_decision.to_payload()

    def _record(self, decision: WorldMutationDecision) -> WorldMutationDecision:
        with self._lock:
            self._last_decision = decision
        return decision

    def _intentional_hold_active(self, now: datetime) -> bool:
        if self._intentional_hold_seconds <= 0:
            return False
        try:
            stamped = _parse_iso(self._kv_get(WORLD_INTENTIONAL_STATE_KEY))
        except Exception:
            stamped = None
        if stamped is None:
            return False
        return (now - stamped).total_seconds() < self._intentional_hold_seconds


__all__ = [
    "STATE_MUTATIONS",
    "WORLD_INTENTIONAL_STATE_KEY",
    "WorldMutationDecision",
    "WorldMutationGuard",
]
