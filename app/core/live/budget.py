"""Deterministic Live behavior budgets. Exhaustion becomes wait, not polling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


BUDGET_CLASSES = (
    "attention_switch",
    "motion",
    "expression",
    "backchannel",
    "micro_speech",
    "main_wake",
)

_CAPACITY = {
    "attention_switch": 8,
    "motion": 6,
    "expression": 10,
    "backchannel": 4,
    "micro_speech": 2,
    "main_wake": 1,
}
_WINDOW_MS = {
    "attention_switch": 60_000,
    "motion": 60_000,
    "expression": 60_000,
    "backchannel": 60_000,
    "micro_speech": 60_000,
    "main_wake": 600_000,
}
MAIN_WAKE_HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    remaining: dict[str, int]
    exhausted: tuple[str, ...]
    next_replenish_ms: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "remaining": dict(self.remaining),
            "exhausted": list(self.exhausted),
            "next_replenish_ms": self.next_replenish_ms,
        }


class LiveBehaviorBudget:
    """Token buckets consumed by admitted/executed behavior, not proposals."""

    def __init__(self, *, main_wake_max_per_hour: int | None = None) -> None:
        self._capacity = dict(_CAPACITY)
        self._window_ms = dict(_WINDOW_MS)
        self._tokens = dict(_CAPACITY)
        self._last_ms = {name: 0.0 for name in BUDGET_CLASSES}
        if main_wake_max_per_hour is not None:
            self.configure_main_wake(main_wake_max_per_hour)

    def configure_main_wake(self, max_per_hour: int) -> None:
        """Replace the hardcoded 1 / 600s main-wake bucket.

        ``0`` forbids main-wake. Otherwise one token replenishes every
        ``hour / max_per_hour`` so default ``6`` stays 1 / 10 minutes.
        """
        try:
            cap = max(0, min(30, int(max_per_hour)))
        except (TypeError, ValueError):
            cap = 6
        if cap <= 0:
            self._capacity["main_wake"] = 0
            self._tokens["main_wake"] = 0
            self._window_ms["main_wake"] = MAIN_WAKE_HOUR_MS
            return
        self._capacity["main_wake"] = 1
        self._window_ms["main_wake"] = max(1, MAIN_WAKE_HOUR_MS // cap)
        self._tokens["main_wake"] = min(int(self._tokens.get("main_wake", 1)), 1)

    def snapshot(self, now_mono_ms: float) -> BudgetSnapshot:
        self._replenish(now_mono_ms)
        remaining = {name: int(self._tokens[name]) for name in BUDGET_CLASSES}
        exhausted = tuple(
            name for name, value in remaining.items() if value <= 0
        )
        next_ms = 0
        if exhausted:
            waits: list[int] = []
            now = float(now_mono_ms)
            for name in exhausted:
                last = self._last_ms[name] or now
                window = float(self._window_ms[name])
                elapsed = max(0.0, now - last)
                waits.append(max(0, int(window - (elapsed % window))))
            next_ms = min(waits) if waits else 0
        return BudgetSnapshot(
            remaining=remaining,
            exhausted=exhausted,
            next_replenish_ms=next_ms,
        )

    def consume(self, class_name: str, *, now_mono_ms: float) -> bool:
        """Spend one token. False means exhausted (caller should wait)."""
        name = str(class_name)
        if name not in self._capacity:
            return False
        self._replenish(now_mono_ms)
        if self._tokens[name] <= 0:
            return False
        self._tokens[name] -= 1
        return True

    def refund(self, class_name: str, *, now_mono_ms: float) -> None:
        name = str(class_name)
        if name not in self._capacity:
            return
        self._replenish(now_mono_ms)
        self._tokens[name] = min(self._capacity[name], self._tokens[name] + 1)

    def remaining(self, class_name: str, *, now_mono_ms: float) -> int:
        self._replenish(now_mono_ms)
        return int(self._tokens.get(class_name, 0))

    def _replenish(self, now_mono_ms: float) -> None:
        now = float(now_mono_ms)
        for name in BUDGET_CLASSES:
            last = self._last_ms[name]
            if last <= 0:
                self._last_ms[name] = now
                continue
            window = float(self._window_ms[name])
            elapsed = now - last
            cycles = int(elapsed // window)
            if cycles <= 0:
                continue
            cap = self._capacity[name]
            self._tokens[name] = min(cap, self._tokens[name] + cycles * cap)
            self._last_ms[name] = last + cycles * window
