from __future__ import annotations

from dataclasses import dataclass, field
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
    hwnd: int | None = None
    confidence: float = 0.0
    reason: str = ""


@dataclass
class ActionPlan:
    """An ordered sequence of actions to execute as a single logical unit."""
    steps: list[PlannedAction] = field(default_factory=list)
    description: str = ""
    needs_screen: bool = False  # planner requests a fresh screen capture before committing


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
        self._pending_plan: ActionPlan | None = None

    @property
    def emergency_stopped(self) -> bool:
        return self._stop_state.triggered

    def reset_emergency_stop(self) -> None:
        self._stop_state.reset()

    @property
    def has_pending_action(self) -> bool:
        return self._pending_plan is not None

    @property
    def pending_action(self) -> PlannedAction | None:
        """Return the first step of the pending plan (for UI display)."""
        if self._pending_plan and self._pending_plan.steps:
            return self._pending_plan.steps[0]
        return None

    def execute(self, action: PlannedAction) -> ActionExecutionResult:
        """Execute a single action. Thin wrapper around execute_plan."""
        return self.execute_plan(ActionPlan(steps=[action], description=self._describe(action)))

    def execute_plan(self, plan: ActionPlan) -> ActionExecutionResult:
        """Execute an ordered sequence of actions, stopping on first failure."""
        steps = [s for s in plan.steps if s.kind != "none"]
        if not steps:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=False,
                requires_confirmation=False,
                message="No actions planned.",
            )

        # ── Plan-level guards (checked once for the whole plan) ──────────────
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
            self._pending_plan = ActionPlan(steps=steps, description=plan.description)
            step_lines = "\n".join(
                f"  {i + 1}. {self._describe(s)}" for i, s in enumerate(steps)
            )
            desc = f" — {plan.description}" if plan.description else ""
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=False,
                requires_confirmation=True,
                message=f"Awaiting confirmation for {len(steps)} action(s){desc}:\n{step_lines}",
            )

        if self._settings.dry_run:
            self._last_action_at = time.monotonic()
            step_desc = " → ".join(self._describe(s) for s in steps)
            return ActionExecutionResult(
                executed=False,
                dry_run=True,
                blocked=False,
                requires_confirmation=False,
                message=f"Dry-run plan ({len(steps)} step(s)): {step_desc}",
            )

        # ── Sequential step execution ─────────────────────────────────────────
        pyautogui = self._load_pyautogui()
        step_results: list[str] = []
        any_executed = False

        for i, step in enumerate(steps):
            # Per-step confidence check
            if step.confidence < self._settings.min_confidence:
                step_results.append(
                    f"Step {i + 1} ({step.kind}): blocked — confidence "
                    f"{round(step.confidence, 2)} < {round(self._settings.min_confidence, 2)}"
                )
                break

            # Honour cooldown between steps (sleep rather than hard-block)
            min_interval = max(0.0, float(self._settings.min_action_interval_seconds))
            if min_interval > 0.0 and self._last_action_at > 0.0:
                elapsed = time.monotonic() - self._last_action_at
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)

            result = self._execute_step(step, pyautogui)
            step_results.append(f"Step {i + 1} ({step.kind}): {result.message}")

            if result.executed:
                any_executed = True
                # Extra pause after focus_window so the window finishes animating
                if step.kind == "focus_window":
                    time.sleep(0.2)

            if not result.executed:
                break

        combined = "\n".join(step_results)
        return ActionExecutionResult(
            executed=any_executed,
            dry_run=False,
            blocked=(not any_executed),
            requires_confirmation=False,
            message=combined,
        )

    def _execute_step(
        self, action: PlannedAction, pyautogui
    ) -> ActionExecutionResult:
        """Raw step executor — no guards. Called only from execute_plan."""
        if action.kind == "focus_window":
            if action.hwnd is None:
                return ActionExecutionResult(
                    executed=False, dry_run=False, blocked=True,
                    requires_confirmation=False,
                    message="focus_window missing hwnd.",
                )
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow(action.hwnd, 9)  # SW_RESTORE
            time.sleep(0.3)
            user32.SetForegroundWindow(action.hwnd)
            self._last_action_at = time.monotonic()
            return ActionExecutionResult(
                executed=True, dry_run=False, blocked=False,
                requires_confirmation=False,
                message=f"Restored and focused window (hwnd={action.hwnd}).",
            )

        if pyautogui is None:
            return ActionExecutionResult(
                executed=False, dry_run=False, blocked=True,
                requires_confirmation=False,
                message="Real actions require pyautogui. Install with: pip install -e .[actions]",
            )

        if action.kind == "click":
            if action.x is None or action.y is None:
                return ActionExecutionResult(
                    executed=False, dry_run=False, blocked=True,
                    requires_confirmation=False,
                    message="Click action missing coordinates.",
                )
            pyautogui.click(action.x, action.y)
            self._last_action_at = time.monotonic()
            return ActionExecutionResult(
                executed=True, dry_run=False, blocked=False,
                requires_confirmation=False,
                message=f"Executed click at ({action.x}, {action.y}).",
            )

        if action.kind == "type_text":
            text = (action.text or "").strip()
            if not text:
                return ActionExecutionResult(
                    executed=False, dry_run=False, blocked=True,
                    requires_confirmation=False,
                    message="Type action missing text.",
                )
            if action.x is not None and action.y is not None:
                pyautogui.click(action.x, action.y)
                time.sleep(0.15)
            pyautogui.typewrite(text, interval=0.03)
            self._last_action_at = time.monotonic()
            click_note = (
                f" (clicked at ({action.x}, {action.y}) to focus)"
                if action.x is not None else ""
            )
            return ActionExecutionResult(
                executed=True, dry_run=False, blocked=False,
                requires_confirmation=False,
                message=f"Executed typing{click_note}: {repr(text)}",
            )

        return ActionExecutionResult(
            executed=False, dry_run=False, blocked=True,
            requires_confirmation=False,
            message=f"Unsupported action kind: {action.kind}",
        )

    def approve_pending_action(self) -> ActionExecutionResult:
        pending = self._pending_plan
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
            result = self.execute_plan(pending)
        finally:
            self._settings.require_confirmation = require_confirmation_original

        if result.executed or result.dry_run or result.blocked:
            self._pending_plan = None

        return result

    def reject_pending_action(self) -> ActionExecutionResult:
        pending = self._pending_plan
        if pending is None:
            return ActionExecutionResult(
                executed=False,
                dry_run=self._settings.dry_run,
                blocked=True,
                requires_confirmation=False,
                message="No pending action to reject.",
            )

        step_descs = " → ".join(self._describe(s) for s in pending.steps)
        self._pending_plan = None
        return ActionExecutionResult(
            executed=False,
            dry_run=self._settings.dry_run,
            blocked=False,
            requires_confirmation=False,
            message=f"Rejected {len(pending.steps)} pending action(s): {step_descs}",
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
        if action.kind == "focus_window":
            return f"focus_window hwnd={action.hwnd} confidence={round(action.confidence, 2)}"
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
