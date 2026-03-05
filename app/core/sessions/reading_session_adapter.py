from __future__ import annotations

from collections.abc import Callable

from app.core.sessions.reading_session import ReadingSessionManager
from app.core.sessions.session_types import SessionRuntimeContext, SessionTurnSignals
from app.core.tooling.runtime.action_runtime import ActionPlan, PlannedAction


class ReadingSessionAdapter:
    session_type = "reading"

    def __init__(self, manager: ReadingSessionManager) -> None:
        self._manager = manager

    @property
    def manager(self) -> ReadingSessionManager:
        return self._manager

    def detect_turn_signals(self, user_text: str) -> SessionTurnSignals:
        return SessionTurnSignals(
            wants_screen_context=self._manager.is_reading_intent(user_text),
            wants_evidence=self._manager.is_reading_evidence_request(user_text),
            wants_continue=self._manager.is_continue_reading_request(user_text),
        )

    def on_screen_text(
        self,
        *,
        user_text: str,
        screen_text: str | None,
        foreground_window_title: str,
        trace: Callable[[str, str], None],
    ) -> None:
        self._manager.update(
            user_text=user_text,
            screen_text=screen_text,
            foreground_window_title=foreground_window_title,
            trace=trace,
        )

    def build_prompt_context(self) -> str:
        return self._manager.build_context_for_prompt()

    def build_evidence_block(self, trace: Callable[[str, str], None]) -> str:
        return self._manager.build_evidence_block(trace)

    def continue_after_approval(self, context: SessionRuntimeContext) -> str:
        if not self._manager.can_continue_after_approval():
            return ""
        if not context.actions_enabled or not context.screen_enabled:
            return ""
        if not self._manager.is_trusted_window(context.foreground_window_title):
            context.trace(
                "reading.blocked",
                (
                    "Autopilot blocked in untrusted window: "
                    f"{context.foreground_window_title or '[unknown]'}"
                ),
            )
            return ""

        remaining = max(0, int(self._manager.max_scroll_steps) - int(self._manager.scroll_steps))
        if remaining <= 0:
            return ""

        executed_steps = 0
        duplicate_streak = 0
        require_confirmation_original = bool(context.get_require_confirmation())
        execute_action_plan = context.execute_action_plan
        if not callable(execute_action_plan):
            context.trace("reading.blocked", "Autopilot unavailable: execute_action_plan callback missing.")
            return ""
        try:
            # One-approval flow: bounded continuation executes without re-prompting.
            context.set_require_confirmation(False)
            for _ in range(remaining):
                before_hashes = self._manager.chunk_hash_count
                execute_result = execute_action_plan(
                    ActionPlan(
                        description="Continue reading article by scrolling down.",
                        needs_screen=False,
                        steps=[
                            PlannedAction(
                                kind="scroll",
                                x=None,
                                y=None,
                                text="down:10",
                                hwnd=None,
                                confidence=0.95,
                                reason="Continue reading content below.",
                            )
                        ],
                    )
                )
                if not bool(getattr(execute_result, "executed", False)):
                    context.trace("reading.stop", f"Autopilot scroll execution failed: {getattr(execute_result, 'message', 'unknown')}")
                    break

                executed_steps += 1
                self._manager.increment_scroll_step()
                context.trace("reading.scroll", f"step={self._manager.scroll_steps}")

                captured = context.capture_screen_text(decision_source="reading-autopilot")
                self.on_screen_text(
                    user_text="continue reading",
                    screen_text=captured,
                    foreground_window_title=context.foreground_window_title,
                    trace=context.trace,
                )
                if self._manager.chunk_hash_count == before_hashes:
                    duplicate_streak += 1
                else:
                    duplicate_streak = 0

                if duplicate_streak >= 2:
                    context.trace("reading.stop", "Autopilot stopped (no new readable content).")
                    break
        finally:
            context.set_require_confirmation(require_confirmation_original)

        if executed_steps < 1:
            return ""

        lead = f"I continued reading automatically for {executed_steps} scroll step(s)."
        evidence = self.build_evidence_block(context.trace)
        if evidence:
            return f"{lead}\n{evidence}"
        return lead

    def is_active(self) -> bool:
        return bool(self._manager.active)

    def stop(self, trace: Callable[[str, str], None]) -> bool:
        return self._manager.stop(trace)

    def get_status(self) -> dict[str, bool | int | str]:
        return self._manager.get_status()