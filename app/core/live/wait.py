"""Rich wait: silence with a bounded wake contract, not a no-op poll."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WAKE_ALLOWLIST = frozenset({
    "user.message_sent",
    "user.speech_final",
    "user.speech_started",
    "user.voice_start",
    "user.typing_started",
    "silence.wake",
    "world.activity_changed",
    "sleep.status_changed",
    "situation.shared_commitment_changed",
    "user.session_changed",
    "idle.reconsider",
})

CRITICAL_WAKES = frozenset({
    "user.message_sent",
    "user.speech_final",
    "user.voice_start",
    "user.session_changed",
})

MIN_WAIT_MS = 1_000
MAX_WAIT_MS = 60_000


@dataclass(frozen=True, slots=True)
class LiveWait:
    snapshot_generation: int
    selected_urge_id: str
    intent: str
    reconsider_after_ms: int
    deadline_monotonic_ms: float
    wake_on: tuple[str, ...]
    reason_code: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "snapshot_generation": self.snapshot_generation,
            "selected_urge_id": self.selected_urge_id,
            "intent": self.intent,
            "reconsider_after_ms": self.reconsider_after_ms,
            "deadline_monotonic_ms": self.deadline_monotonic_ms,
            "wake_on": list(self.wake_on),
            "reason_code": self.reason_code,
        }


class LiveWaitScheduler:
    """One outstanding wait. Expiry, critical wakes, and non-silence
    generation bumps cancel it. ``silence.wake`` does not, unless it
    is in that wait's ``wake_on``.
    """

    def __init__(self) -> None:
        self._wait: LiveWait | None = None

    @property
    def current(self) -> LiveWait | None:
        return self._wait

    def schedule(
        self,
        *,
        generation: int,
        urge_id: str,
        reconsider_after_ms: int,
        now_mono_ms: float,
        wake_on: tuple[str, ...],
        reason_code: str,
    ) -> LiveWait:
        clamped = max(MIN_WAIT_MS, min(MAX_WAIT_MS, int(reconsider_after_ms)))
        allowed = tuple(
            event for event in wake_on if event in WAKE_ALLOWLIST
        )
        wait = LiveWait(
            snapshot_generation=int(generation),
            selected_urge_id=str(urge_id or ""),
            intent="wait",
            reconsider_after_ms=clamped,
            deadline_monotonic_ms=float(now_mono_ms) + clamped,
            wake_on=allowed,
            reason_code=str(reason_code or "wait"),
        )
        self._wait = wait
        return wait

    def cancel(self, *, reason: str = "") -> None:
        self._wait = None

    def on_generation(self, generation: int, trigger_kind: str = "") -> None:
        wait = self._wait
        if wait is None:
            return
        if int(generation) == wait.snapshot_generation:
            return
        # silence.wake bumps generation even when the room did not change.
        # Keep the wait; should_suppress_inference decides the idle wake.
        if str(trigger_kind or "") == "silence.wake":
            return
        self._wait = None

    def should_suppress_inference(
        self,
        trigger_kind: str,
        *,
        now_mono_ms: float,
        generation: int,
    ) -> bool:
        """True when a wait is holding and this trigger is not a wake."""
        wait = self._wait
        if wait is None:
            return False
        token = str(trigger_kind or "")
        if token in CRITICAL_WAKES:
            self._wait = None
            return False
        if token in wait.wake_on:
            self._wait = None
            return False
        if now_mono_ms >= wait.deadline_monotonic_ms:
            self._wait = None
            return False
        if int(generation) != wait.snapshot_generation:
            if token != "silence.wake":
                self._wait = None
                return False
            return True
        return True
