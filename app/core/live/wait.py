"""Rich wait: silence with a bounded wake contract, not a no-op poll."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.live.frame import SLEEP_SPEECH_FORBID

WAKE_ALLOWLIST = frozenset({
    "user.message_sent",
    "user.speech_final",
    "user.voice_start",
    "user.typing_started",
    "silence.wake",
    "activity.session_changed",
    "activity.idle",
    "activity.lock",
    "sleep.status_changed",
    "situation.shared_commitment_changed",
    "user.session_changed",
    "idle.reconsider",
})

WAKE_ALIASES = {
    "user.speech_started": "user.voice_start",
    "world.activity_changed": "activity.session_changed",
}

CRITICAL_WAKES = frozenset({
    "user.message_sent",
    "user.speech_final",
    "user.voice_start",
    "user.session_changed",
})

MIN_WAIT_MS = 1_000
MAX_WAIT_MS = 60_000
DEFAULT_WAIT_MS = 15_000

WAIT_HORIZONS = ("short", "medium", "long")
WAIT_HORIZON_SET = frozenset(WAIT_HORIZONS)
DEFAULT_WAIT_HORIZON = "medium"
WAIT_HORIZON_MS = {
    "short": MIN_WAIT_MS,
    "medium": DEFAULT_WAIT_MS,
    "long": MAX_WAIT_MS,
}

WAKE_SETS = ("user_only", "user_or_activity", "user_or_silence")
WAKE_SET_SET = frozenset(WAKE_SETS)
DEFAULT_WAKE_SET = "user_only"
SPEECH_WAKES = frozenset({"silence.wake", "idle.reconsider"})

_USER_WAKES = (
    "user.message_sent",
    "user.speech_final",
    "user.voice_start",
    "user.typing_started",
    "user.session_changed",
)
_ACTIVITY_WAKES = (
    "activity.session_changed",
    "activity.idle",
    "activity.lock",
    "situation.shared_commitment_changed",
    "sleep.status_changed",
)
WAKE_SET_EVENTS = {
    "user_only": _USER_WAKES,
    "user_or_activity": _USER_WAKES + _ACTIVITY_WAKES,
    "user_or_silence": _USER_WAKES + ("silence.wake",),
}


def clamp_wait_horizon(proposed: object) -> str:
    """Admit short/medium/long. Unknown tokens become medium."""
    token = str(proposed or "").strip()
    if token not in WAIT_HORIZON_SET:
        return DEFAULT_WAIT_HORIZON
    return token


def wait_ms_for_horizon(horizon: object) -> int:
    token = clamp_wait_horizon(horizon)
    return int(WAIT_HORIZON_MS[token])


def clamp_wake_set(proposed: object) -> str:
    """Admit the wake-set enum. Unknown tokens become user_only."""
    token = str(proposed or "").strip()
    if token not in WAKE_SET_SET:
        return DEFAULT_WAKE_SET
    return token


def expand_wake_set(
    wake_set: object,
    *,
    sleep_status: str = "",
) -> tuple[str, ...]:
    """Expand one wake-set into allowlisted event names.

    Critical user input is always included. Sleep cannot pick a speech
    wake. Unknown tokens drop to the default set.
    """
    token = clamp_wake_set(wake_set)
    events: list[str] = []
    for item in WAKE_SET_EVENTS[token]:
        mapped = WAKE_ALIASES.get(str(item), str(item))
        if mapped in WAKE_ALLOWLIST and mapped not in events:
            events.append(mapped)
    for item in CRITICAL_WAKES:
        if item not in events:
            events.append(item)
    if str(sleep_status or "") in SLEEP_SPEECH_FORBID:
        events = [item for item in events if item not in SPEECH_WAKES]
    return tuple(events)


def wait_horizon_from_proposal(arguments: object) -> str:
    raw = arguments if isinstance(arguments, dict) else {}
    return clamp_wait_horizon(raw.get("wait_horizon"))


def wake_set_from_proposal(arguments: object) -> str:
    raw = arguments if isinstance(arguments, dict) else {}
    return clamp_wake_set(raw.get("wake_set"))



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
        allowed: list[str] = []
        for event in wake_on:
            token = WAKE_ALIASES.get(str(event), str(event))
            if token in WAKE_ALLOWLIST and token not in allowed:
                allowed.append(token)
        wait = LiveWait(
            snapshot_generation=int(generation),
            selected_urge_id=str(urge_id or ""),
            intent="wait",
            reconsider_after_ms=clamped,
            deadline_monotonic_ms=float(now_mono_ms) + clamped,
            wake_on=tuple(allowed),
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


__all__ = [
    "CRITICAL_WAKES",
    "DEFAULT_WAIT_HORIZON",
    "DEFAULT_WAIT_MS",
    "DEFAULT_WAKE_SET",
    "MAX_WAIT_MS",
    "MIN_WAIT_MS",
    "SPEECH_WAKES",
    "WAKE_ALIASES",
    "WAKE_ALLOWLIST",
    "WAKE_SETS",
    "WAKE_SET_SET",
    "WAIT_HORIZONS",
    "WAIT_HORIZON_MS",
    "WAIT_HORIZON_SET",
    "LiveWait",
    "LiveWaitScheduler",
    "clamp_wait_horizon",
    "clamp_wake_set",
    "expand_wake_set",
    "wait_horizon_from_proposal",
    "wait_ms_for_horizon",
    "wake_set_from_proposal",
]
