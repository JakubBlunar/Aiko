from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class AgenticSessionConfig:
    enabled: bool = True
    max_auto_steps: int = 3


class AgenticSessionManager:
    def __init__(self, config: AgenticSessionConfig) -> None:
        self._enabled = bool(config.enabled)
        self._max_auto_steps = max(1, int(config.max_auto_steps))
        self._active = False
        self._objective = ""
        self._last_screen_text = ""
        self._auto_steps = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def max_auto_steps(self) -> int:
        return self._max_auto_steps

    @property
    def auto_steps(self) -> int:
        return self._auto_steps

    @property
    def objective(self) -> str:
        return str(self._objective or "")

    @staticmethod
    def is_agentic_intent(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = (
            "go fully automatic",
            "fully automatic",
            "enter agentic mode",
            "start agentic session",
            "agentic mode",
            "work autonomously",
        )
        return any(token in lowered for token in tokens)

    @staticmethod
    def is_continue_request(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = (
            "continue autonomously",
            "keep going",
            "next step",
            "continue agentic",
        )
        return any(token in lowered for token in tokens)

    @staticmethod
    def is_evidence_request(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = (
            "what did you do",
            "show your plan",
            "status",
            "progress",
            "evidence",
        )
        return any(token in lowered for token in tokens)

    def activate(self, *, objective: str, trace: Callable[[str, str], None]) -> None:
        if not self._enabled:
            trace("agentic.blocked", "Agentic session is disabled by config.")
            return
        self._active = True
        self._auto_steps = 0
        self._objective = str(objective or "").strip()
        trace("agentic.start", f"objective={self._objective or '[none]'}")

    def set_objective(self, *, objective: str, trace: Callable[[str, str], None]) -> None:
        text = str(objective or "").strip()
        if not text:
            return
        previous = self._objective
        self._objective = text
        if previous != text:
            trace("agentic.objective", f"objective updated: {text}")

    def ensure_objective(self, *, fallback: str, trace: Callable[[str, str], None]) -> None:
        if self._objective.strip():
            return
        candidate = str(fallback or "").strip()
        if not candidate:
            candidate = "Advance toward the active goal safely and report progress."
        self._objective = candidate
        trace("agentic.objective", f"objective initialized: {self._objective}")

    def stop(self, trace: Callable[[str, str], None]) -> bool:
        was_active = bool(self._active)
        self._active = False
        self._auto_steps = 0
        self._objective = ""
        self._last_screen_text = ""
        trace("agentic.stop", "Agentic session cleared by user request.")
        return was_active

    def update(
        self,
        *,
        user_text: str,
        screen_text: str | None,
        trace: Callable[[str, str], None],
    ) -> None:
        if not self._enabled:
            return
        if self.is_agentic_intent(user_text) and not self._active:
            self.activate(objective=user_text, trace=trace)
        if screen_text:
            self._last_screen_text = str(screen_text)

    def can_continue_after_approval(self) -> bool:
        if not self._enabled or not self._active:
            return False
        return self._auto_steps < self._max_auto_steps

    def increment_step(self) -> None:
        self._auto_steps += 1

    def build_context_for_prompt(self) -> str:
        if not self._active:
            return ""
        objective = self._objective or "No explicit objective set."
        screen_tail = self._last_screen_text[:1000].strip()
        if screen_tail:
            return (
                "Active agentic session:\n"
                f"Objective: {objective}\n"
                f"Auto steps used: {self._auto_steps}/{self._max_auto_steps}\n"
                "Latest screen context:\n"
                f"{screen_tail}"
            )
        return (
            "Active agentic session:\n"
            f"Objective: {objective}\n"
            f"Auto steps used: {self._auto_steps}/{self._max_auto_steps}"
        )

    def build_evidence_block(self, trace: Callable[[str, str], None]) -> str:
        if not self._active:
            return ""
        summary = (
            "Agentic evidence:\n"
            f"- objective: {self._objective or '[none]'}\n"
            f"- auto_steps: {self._auto_steps}/{self._max_auto_steps}"
        )
        trace("agentic.summary", summary.replace("\n", " | "))
        return summary

    def get_status(self) -> dict[str, bool | int | str]:
        return {
            "active": bool(self._active),
            "objective": str(self._objective or ""),
            "auto_steps": int(self._auto_steps),
            "max_auto_steps": int(self._max_auto_steps),
        }
