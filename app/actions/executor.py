from __future__ import annotations

from dataclasses import dataclass
import ctypes
import ctypes.wintypes as wintypes
import time

from app.actions.emergency_stop import EmergencyStopState
from app.core.settings import ActionSettings


@dataclass(slots=True)
class PlannedAction:
    kind: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    confidence: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class ActionExecutionResult:
    executed: bool
    dry_run: bool
    blocked: bool
    requires_confirmation: bool
    message: str


class GuardedActionExecutor:
    def __init__(self, settings: ActionSettings, stop_state: EmergencyStopState) -> None:
        self._settings = settings
        self._stop_state = stop_state
        self._last_action_at = 0.0
        self._pending_action: PlannedAction | None = None

    @property
    def emergency_stopped(self) -> bool:
        return self._stop_state.triggered

    def reset_emergency_stop(self) -> None:
        self._stop_state.reset()

    @property
    def has_pending_action(self) -> bool:
        return self._pending_action is not None

    @property
    def pending_action(self) -> PlannedAction | None:
        return self._pending_action

    def execute(self, action: PlannedAction) -> ActionExecutionResult:
        if action.kind == "none":
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=False,
                requires_confirmation=False,
                message="No action planned.",
            )

        if not self._settings.enabled:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=True,
                requires_confirmation=False,
                message="Actions are disabled in config.",
            )

        if self._stop_state.triggered:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=True,
                requires_confirmation=False,
                message="Emergency stop is active. Reset required before actions can run.",
            )

        if action.confidence < self._settings.min_confidence:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=True,
                requires_confirmation=False,
                message=(
                    "Blocked by confidence threshold: "
                    f"{round(action.confidence, 2)} < {round(self._settings.min_confidence, 2)}"
                ),
            )

        min_interval = max(0.0, float(self._settings.min_action_interval_seconds))
        if min_interval > 0.0 and self._last_action_at > 0.0:
            elapsed = time.monotonic() - self._last_action_at
            if elapsed < min_interval:
                remaining = round(min_interval - elapsed, 2)
                return ActionExecutionResult(
                    executed=False,
                    dry_run=self._settings.dry_run,
                    blocked=True,
                    requires_confirmation=False,
                    message=(
                        "Blocked by action cooldown: "
                        f"wait {remaining}s (min interval {round(min_interval, 2)}s)"
                    ),
                )

        active_title = self._active_window_title()
        if self._settings.allowlist_window_titles:
            title_ok = any(
                token.lower() in active_title.lower()
                for token in self._settings.allowlist_window_titles
            )
            if not title_ok:
                return ActionExecutionResult(
                    executed=False,
                    dry_run=self._settings.dry_run,
                    blocked=True,
                    requires_confirmation=False,
                    message=(
                        "Blocked by window allowlist. "
                        f"Active title: {active_title or '[unknown]'}"
                    ),
                )

        if self._settings.require_confirmation:
            self._pending_action = action
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=False,
                requires_confirmation=True,
                message=f"Awaiting confirmation for action: {self._describe(action)}",
            )

        if self._settings.dry_run:
            self._last_action_at = time.monotonic()
            return ActionExecutionResult(
                executed=False,
                dry_run=True,
                blocked=False,
                requires_confirmation=False,
                message=f"Dry-run action: {self._describe(action)}",
            )

        pyautogui = self._load_pyautogui()
        if pyautogui is None:
            return ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=True,
                requires_confirmation=False,
                message=(
                    "Real actions require pyautogui. "
                    "Install with: pip install -e .[actions]"
                ),
            )

        if action.kind == "click":
            if action.x is None or action.y is None:
                return ActionExecutionResult(
                    executed=False,
                    dry_run=False,
                    blocked=True,
                    requires_confirmation=False,
                    message="Click action missing coordinates.",
                )
            pyautogui.click(action.x, action.y)
            self._last_action_at = time.monotonic()
            return ActionExecutionResult(
                executed=True,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message=f"Executed click at ({action.x}, {action.y}).",
            )

        if action.kind == "type_text":
            text = (action.text or "").strip()
            if not text:
                return ActionExecutionResult(
                    executed=False,
                    dry_run=False,
                    blocked=True,
                    requires_confirmation=False,
                    message="Type action missing text.",
                )
            pyautogui.write(text)
            self._last_action_at = time.monotonic()
            return ActionExecutionResult(
                executed=True,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message=f"Executed typing ({len(text)} chars).",
            )

        return ActionExecutionResult(
            executed=False,
            dry_run=self._settings.dry_run,
            blocked=True,
            requires_confirmation=False,
            message=f"Unsupported action kind: {action.kind}",
        )

    def approve_pending_action(self) -> ActionExecutionResult:
        pending = self._pending_action
        if pending is None:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=True,
                requires_confirmation=False,
                message="No pending action to approve.",
            )

        require_confirmation_original = self._settings.require_confirmation
        try:
            self._settings.require_confirmation = False
            result = self.execute(pending)
        finally:
            self._settings.require_confirmation = require_confirmation_original

        if result.executed or result.dry_run or result.blocked:
            self._pending_action = None

        return result

    def reject_pending_action(self) -> ActionExecutionResult:
        pending = self._pending_action
        if pending is None:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=True,
                requires_confirmation=False,
                message="No pending action to reject.",
            )

        description = self._describe(pending)
        self._pending_action = None
        return ActionExecutionResult(
            executed=False,
            dry_run=self._settings.dry_run,
            blocked=False,
            requires_confirmation=False,
            message=f"Rejected pending action: {description}",
        )

    @staticmethod
    def _load_pyautogui():
        try:
            import pyautogui  # type: ignore

            return pyautogui
        except Exception:
            return None

    @staticmethod
    def _describe(action: PlannedAction) -> str:
        if action.kind == "click":
            return f"click x={action.x} y={action.y} confidence={round(action.confidence, 2)}"
        if action.kind == "type_text":
            text = (action.text or "").strip()
            preview = text if len(text) <= 32 else f"{text[:29]}..."
            return (
                "type_text "
                f"chars={len(text)} preview='{preview}' "
                f"confidence={round(action.confidence, 2)}"
            )
        return action.kind

    @staticmethod
    def _active_window_title() -> str:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""

        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, ctypes.cast(buffer, wintypes.LPWSTR), length + 1)
        return str(buffer.value or "")
