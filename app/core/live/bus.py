"""Bounded Live impulse bus with coalesce, TTL, and generation drop."""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

from app.core.live.impulse import LiveImpulse, new_impulse


class LiveImpulseBus:
    """Shadow queue. Nothing here grants action permission."""

    def __init__(self, *, max_pending: int = 256, replay_size: int = 64) -> None:
        self._lock = threading.Lock()
        self._pending: deque[LiveImpulse] = deque()
        self._replay: deque[LiveImpulse] = deque(maxlen=max(1, int(replay_size)))
        self._max_pending = max(1, int(max_pending))
        self._sequence = 0
        self._dropped = 0
        self._coalesced = 0
        self._expired = 0
        self._generation_dropped = 0
        self._accepted = 0

    def publish(
        self,
        *,
        kind: str,
        source: str,
        session_key: str,
        mode_generation: int,
        coalesce_key: str,
        privacy: str = "local_state",
        priority: str = "attention",
        ttl_ms: int = 8000,
        payload: dict[str, Any] | None = None,
    ) -> LiveImpulse | None:
        with self._lock:
            self._sequence += 1
            impulse = new_impulse(
                kind=kind,
                source=source,
                session_key=session_key,
                mode_generation=mode_generation,
                sequence=self._sequence,
                coalesce_key=coalesce_key,
                privacy=privacy,
                priority=priority,
                ttl_ms=ttl_ms,
                payload=payload,
            )
            if coalesce_key:
                replaced = False
                kept: deque[LiveImpulse] = deque()
                for item in self._pending:
                    if item.coalesce_key == coalesce_key:
                        replaced = True
                        continue
                    kept.append(item)
                if replaced:
                    self._coalesced += 1
                self._pending = kept
            while len(self._pending) >= self._max_pending:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(impulse)
            self._replay.append(impulse)
            self._accepted += 1
            return impulse

    def drop_stale_generation(self, current_generation: int) -> int:
        """Drop pending impulses from a previous mode/session generation."""
        dropped = 0
        with self._lock:
            kept: deque[LiveImpulse] = deque()
            for item in self._pending:
                if item.mode_generation != int(current_generation):
                    dropped += 1
                    continue
                kept.append(item)
            self._pending = kept
            self._generation_dropped += dropped
        return dropped

    def snapshot(self, *, tail: int = 20) -> dict[str, Any]:
        with self._lock:
            alive: list[LiveImpulse] = []
            expired = 0
            kept: deque[LiveImpulse] = deque()
            for item in self._pending:
                if item.is_expired():
                    expired += 1
                    continue
                kept.append(item)
                alive.append(item)
            self._pending = kept
            self._expired += expired
            replay = list(self._replay)[-max(0, int(tail)) :]
            return {
                "pending": len(alive),
                "accepted": self._accepted,
                "dropped": self._dropped,
                "coalesced": self._coalesced,
                "expired": self._expired,
                "generation_dropped": self._generation_dropped,
                "capture_available": False,
                "tail": [item.to_dict() for item in replay],
            }

    def tail_impulses(self, *, tail: int = 32) -> tuple[LiveImpulse, ...]:
        with self._lock:
            items = list(self._replay)[-max(0, int(tail)) :]
            return tuple(items)
