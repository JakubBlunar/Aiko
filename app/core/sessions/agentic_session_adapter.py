from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from app.core.sessions.agentic_session import AgenticSessionManager
from app.core.sessions.session_types import SessionRuntimeContext, SessionTurnSignals


class AgenticSessionAdapter:
    session_type = "agentic"

    def __init__(self, manager: AgenticSessionManager) -> None:
        self._manager = manager

    @property
    def manager(self) -> AgenticSessionManager:
        return self._manager

    def detect_turn_signals(self, user_text: str) -> SessionTurnSignals:
        return SessionTurnSignals(
            wants_screen_context=(
                self._manager.active
                or self._manager.is_agentic_intent(user_text)
                or self._manager.is_continue_request(user_text)
            ),
            wants_evidence=self._manager.is_evidence_request(user_text),
            wants_continue=self._manager.is_continue_request(user_text),
        )

    def on_screen_text(
        self,
        *,
        user_text: str,
        screen_text: str | None,
        foreground_window_title: str,
        trace: Callable[[str, str], None],
    ) -> None:
        _ = foreground_window_title
        self._manager.update(user_text=user_text, screen_text=screen_text, trace=trace)

    def build_prompt_context(self) -> str:
        return self._manager.build_context_for_prompt()

    def build_evidence_block(self, trace: Callable[[str, str], None]) -> str:
        return self._manager.build_evidence_block(trace)

    def continue_after_approval(self, context: SessionRuntimeContext) -> str:
        if not self._manager.can_continue_after_approval():
            return ""
        if not context.actions_enabled:
            return ""

        self._manager.ensure_objective(fallback=context.active_goal, trace=context.trace)
        context.trace(
            "agentic.loop.start",
            self._event_json(
                objective=self._manager.objective,
                max_steps=self._manager.max_auto_steps,
                current_steps=self._manager.auto_steps,
            ),
        )
        self._narrate(
            context,
            (
                "Agentic loop started. "
                f"Objective: {self._manager.objective}. "
                f"Maximum {self._manager.max_auto_steps} steps."
            ),
        )
        planner = context.plan_agentic_step
        if not callable(planner):
            self._manager.increment_step()
            context.trace(
                "agentic.loop.fallback",
                self._event_json(
                    step=self._manager.auto_steps,
                    reason="planner_missing",
                ),
            )
            return (
                "Agentic session advanced one fallback step. "
                f"Progress: {self._manager.auto_steps}/{self._manager.max_auto_steps}."
            )

        require_confirmation_original = bool(context.get_require_confirmation())
        events: list[dict[str, Any]] = []
        lines: list[str] = []
        screen_text = None
        if context.screen_enabled:
            screen_text = context.capture_screen_text(decision_source="agentic-loop:init")
            self._manager.update(
                user_text="continue agentic",
                screen_text=screen_text,
                trace=context.trace,
            )

        try:
            # One approval can unlock a bounded autonomous chain for this loop.
            context.set_require_confirmation(False)

            while self._manager.can_continue_after_approval():
                remaining = max(0, self._manager.max_auto_steps - self._manager.auto_steps)
                if remaining <= 0:
                    break

                plan = planner(self._manager.objective, screen_text, list(events), remaining)
                done = bool(plan.get("done", False)) if isinstance(plan, dict) else True
                note = str(plan.get("progress_note", "")).strip() if isinstance(plan, dict) else ""
                context.trace(
                    "agentic.loop.plan",
                    self._event_json(
                        remaining=remaining,
                        done=done,
                        note=note,
                        next_tool=(str(plan.get("next_tool", "")).strip() if isinstance(plan, dict) else ""),
                    ),
                )
                narration_plan = "Planning next step."
                if note:
                    narration_plan = f"Planning next step. {note}"
                self._narrate(context, narration_plan)
                if note:
                    lines.append(note)
                if done:
                    context.trace("agentic.loop.goal", self._event_json(status="complete"))
                    self._narrate(context, "Goal check says objective is complete. Stopping loop.")
                    break

                tool_name = str(plan.get("next_tool", "")).strip() if isinstance(plan, dict) else ""
                tool_args = plan.get("next_args", {}) if isinstance(plan, dict) else {}
                if not tool_name:
                    context.trace("agentic.loop.stop", self._event_json(reason="missing_next_tool"))
                    break
                if not isinstance(tool_args, dict):
                    tool_args = {}

                context.trace(
                    "agentic.loop.invoke",
                    self._event_json(tool=tool_name, args=tool_args),
                )
                self._narrate(context, f"Invoking tool {tool_name}.")
                result = context.invoke_tool(tool_name, args=tool_args)
                success = bool(getattr(result, "success", False))
                requires_confirmation = bool(getattr(result, "requires_confirmation", False))
                result_data = getattr(result, "data", {})
                err_obj = getattr(result, "error", None)
                err_msg = ""
                if err_obj is not None:
                    err_msg = str(getattr(err_obj, "message", "")).strip()

                self._manager.increment_step()
                step_line = (
                    f"step {self._manager.auto_steps}: {tool_name} "
                    + ("ok" if success else f"failed ({err_msg or 'unknown'})")
                )
                lines.append(step_line)
                context.trace(
                    "agentic.loop.result",
                    self._event_json(
                        step=self._manager.auto_steps,
                        tool=tool_name,
                        success=success,
                        requires_confirmation=requires_confirmation,
                        error=err_msg,
                    ),
                )
                if success:
                    self._narrate(
                        context,
                        f"Result for {tool_name}: success. Step {self._manager.auto_steps} complete.",
                    )
                else:
                    self._narrate(
                        context,
                        (
                            f"Result for {tool_name}: failed. "
                            + (err_msg or "Unknown error.")
                        ),
                    )

                events.append(
                    {
                        "step": self._manager.auto_steps,
                        "tool": tool_name,
                        "args": dict(tool_args),
                        "success": success,
                        "requires_confirmation": requires_confirmation,
                        "error": err_msg,
                        "result_keys": (
                            list(result_data.keys())
                            if isinstance(result_data, dict)
                            else []
                        ),
                    }
                )

                if requires_confirmation:
                    lines.append("Paused: next step requires confirmation.")
                    context.trace("agentic.loop.stop", self._event_json(reason="confirmation_required"))
                    self._narrate(context, "Stopping loop. Additional confirmation is required.")
                    break
                if not success:
                    context.trace("agentic.loop.stop", self._event_json(reason="tool_failure"))
                    self._narrate(context, "Stopping loop due to tool failure.")
                    break

                if context.screen_enabled:
                    screen_text = context.capture_screen_text(decision_source="agentic-loop")
                    self._manager.update(
                        user_text="continue agentic",
                        screen_text=screen_text,
                        trace=context.trace,
                    )
        finally:
            context.set_require_confirmation(require_confirmation_original)

        if not lines:
            context.trace("agentic.loop.stop", self._event_json(reason="no_progress"))
            self._narrate(context, "No meaningful progress was made in this continuation loop.")
            return ""
        context.trace(
            "agentic.loop.done",
            self._event_json(
                steps_used=self._manager.auto_steps,
                max_steps=self._manager.max_auto_steps,
            ),
        )
        self._narrate(
            context,
            (
                "Agentic loop finished. "
                f"Progress is {self._manager.auto_steps} of {self._manager.max_auto_steps} steps."
            ),
        )
        summary_head = (
            "Agentic continuation completed. "
            f"Progress: {self._manager.auto_steps}/{self._manager.max_auto_steps}."
        )
        return summary_head + "\n" + "\n".join(f"- {line}" for line in lines[:8])

    @staticmethod
    def _event_json(**payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=True, default=str)
        except Exception:
            return str(payload)

    @staticmethod
    def _narrate(context: SessionRuntimeContext, text: str) -> None:
        speaker = context.narrate
        if callable(speaker):
            speaker(text)

    def is_active(self) -> bool:
        return bool(self._manager.active)

    def stop(self, trace: Callable[[str, str], None]) -> bool:
        return self._manager.stop(trace)

    def get_status(self) -> dict[str, bool | int | str]:
        return self._manager.get_status()
