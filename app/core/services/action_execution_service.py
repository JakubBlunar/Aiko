from __future__ import annotations

from collections.abc import Callable

from app.core.planning.action_planner import ActionPlanner
from app.core.tooling.runtime.action_runtime import ActionExecutionResult, ActionPlan, PlannedAction


class ActionExecutionService:
    def __init__(
        self,
        *,
        actions_settings: object,
        action_planner: ActionPlanner,
        capture_screen_text: Callable[..., str | None],
        invoke_tool: Callable[..., object],
        trace: Callable[[str, str], None],
        screen_enabled: Callable[[], bool],
        active_goal: Callable[[], str],
        last_screen_elements: Callable[[], list[dict]],
        all_windows: Callable[[], list[dict]],
    ) -> None:
        self._actions_settings = actions_settings
        self._action_planner = action_planner
        self._capture_screen_text = capture_screen_text
        self._invoke_tool = invoke_tool
        self._trace = trace
        self._screen_enabled = screen_enabled
        self._active_goal = active_goal
        self._last_screen_elements = last_screen_elements
        self._all_windows = all_windows

    def maybe_execute_action(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        allow_planning_override: bool = False,
        action_intent: str = "",
        on_token: Callable[[str], None] | None = None,
    ) -> ActionExecutionResult | None:
        if not bool(getattr(self._actions_settings, "enabled", False)):
            wants_action = bool(action_intent) or self.has_action_intent(user_text)
            if wants_action:
                self._trace("action.plan", f"Actions disabled; intent was: {action_intent or user_text[:80]}")
                return ActionExecutionResult(
                    executed=False,
                    dry_run=False,
                    blocked=True,
                    requires_confirmation=False,
                    message="I can't perform UI actions right now - actions are disabled in settings.",
                )
            return None

        if int(getattr(self._actions_settings, "max_actions_per_turn", 0)) < 1:
            return None

        mode = (getattr(self._actions_settings, "decision_mode", "explicit_only") or "explicit_only").lower().strip()
        if mode == "explicit_only" and not allow_planning_override and not self.has_action_intent(user_text):
            self._trace("action.plan", "Skipped action planning (no explicit action intent).")
            return None

        if not screen_text and self._screen_enabled():
            screen_text = self._capture_screen_text(decision_source="action")

        max_replan = 2
        planned = ActionPlan(steps=[], description="not yet planned")
        for attempt in range(max_replan + 1):
            planned = self._plan_action(
                user_text=user_text,
                assistant_reply=assistant_reply,
                screen_text=screen_text,
                action_intent=action_intent,
            )
            if planned.needs_screen and attempt < max_replan and self._screen_enabled():
                self._trace(
                    "action.replan",
                    f"Planner requested screen capture (attempt {attempt + 1}/{max_replan}); re-capturing.",
                )
                screen_text = self._capture_screen_text(decision_source="action-replan")
                continue
            break

        if not planned.steps:
            fallback_plan = self._build_minimize_assistant_plan(user_text)
            if fallback_plan is not None:
                planned = fallback_plan
                self._trace(
                    "action.plan",
                    "Applied deterministic fallback plan: minimize assistant window.",
                )

        if planned.steps and on_token:
            hwnd_to_title = {w["hwnd"]: w["title"] for w in self._all_windows()}
            plain_summary = self.summarize_action_plan_plain(planned, hwnd_to_title)
            if plain_summary:
                on_token(f"\n\n{plain_summary}\n")
            lines: list[str] = []
            for idx, step in enumerate(planned.steps, start=1):
                if step.kind == "focus_window":
                    title = hwnd_to_title.get(step.hwnd or 0, str(step.hwnd))
                    line = f"{idx}. focus_window('{title}')"
                elif step.kind == "click":
                    line = f"{idx}. click({step.x}, {step.y})"
                elif step.kind == "type_text":
                    snippet = (step.text or "")[:24]
                    line = f"{idx}. type_text({snippet!r})"
                elif step.kind == "scroll":
                    scroll_mode = (step.text or "down:8").strip() or "down:8"
                    if step.x is not None and step.y is not None:
                        line = f"{idx}. scroll({scroll_mode!r}, at=({step.x}, {step.y}))"
                    else:
                        line = f"{idx}. scroll({scroll_mode!r})"
                elif step.kind == "window_state":
                    state = (step.text or "restore").strip() or "restore"
                    line = f"{idx}. window_state({state!r}, hwnd={step.hwnd})"
                else:
                    line = f"{idx}. {step.kind}"
                if step.reason:
                    line += f" - {step.reason}"
                lines.append(line)
            plan_text = "\n".join(lines)
            on_token(f"\n\n[Plan]\n{plan_text}\n")

        plan_payload = {
            "description": planned.description,
            "needs_screen": planned.needs_screen,
            "steps": [
                {
                    "kind": step.kind,
                    "x": step.x,
                    "y": step.y,
                    "text": step.text,
                    "hwnd": step.hwnd,
                    "confidence": step.confidence,
                    "reason": step.reason,
                }
                for step in planned.steps
            ],
        }
        execute_result = self._invoke_tool(
            "action.execute_plan",
            args={"plan": plan_payload},
        )
        if execute_result.success:
            result = ActionExecutionResult(
                executed=bool(execute_result.data.get("executed", False)),
                dry_run=bool(execute_result.data.get("dry_run", False)),
                blocked=bool(execute_result.data.get("blocked", False)),
                requires_confirmation=bool(execute_result.data.get("requires_confirmation", False)),
                message=str(execute_result.data.get("message", "")),
            )
        else:
            result = ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=True,
                requires_confirmation=False,
                message=(
                    execute_result.error.message
                    if execute_result.error
                    else "Action tool execution failed."
                ),
            )
        self._trace("action.execute", result.message)
        return result

    @staticmethod
    def has_action_intent(user_text: str) -> bool:
        return ActionPlanner.has_action_intent(user_text)

    def _build_minimize_assistant_plan(self, user_text: str) -> ActionPlan | None:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return None

        wants_minimize = ("minimize" in lowered) or ("minimise" in lowered)
        if not wants_minimize:
            return None
        if "assistant" not in lowered:
            return None

        hwnd_to_title = {
            int(w.get("hwnd", 0) or 0): str(w.get("title", "")).strip()
            for w in self._all_windows()
            if isinstance(w, dict)
        }
        assistant_candidates: list[tuple[int, bool]] = []
        for window in self._all_windows():
            if not isinstance(window, dict):
                continue
            title = str(window.get("title", "")).strip().lower()
            hwnd = int(window.get("hwnd", 0) or 0)
            if hwnd <= 0:
                continue
            if "assistant" in title:
                assistant_candidates.append((hwnd, bool(window.get("is_foreground", False))))

        if not assistant_candidates:
            return None

        assistant_candidates.sort(key=lambda item: (not item[1], item[0]))
        hwnd = int(assistant_candidates[0][0])
        title = hwnd_to_title.get(hwnd, "assistant window")
        return ActionPlan(
            steps=[
                PlannedAction(
                    kind="window_state",
                    hwnd=hwnd,
                    text="minimize",
                    confidence=1.0,
                    reason="Explicit user request to minimize the assistant window.",
                )
            ],
            description=f"Minimize '{title}'.",
            needs_screen=False,
        )

    def _plan_action(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        action_intent: str,
    ) -> ActionPlan:
        return self._action_planner.plan_action(
            user_text=user_text,
            assistant_reply=assistant_reply,
            screen_text=screen_text,
            action_intent=action_intent,
            active_goal=self._active_goal(),
            last_screen_elements=self._last_screen_elements(),
            all_windows=self._all_windows(),
        )

    @staticmethod
    def summarize_action_plan_plain(
        planned: ActionPlan,
        hwnd_to_title: dict[int, str],
    ) -> str:
        description = str(planned.description or "").strip()
        if description:
            if description[-1] not in ".!?":
                description = f"{description}."
            return f"I will try this next: {description}"

        step_phrases: list[str] = []
        for step in planned.steps[:3]:
            if step.kind == "focus_window":
                title = str(hwnd_to_title.get(step.hwnd or 0, "that window")).strip() or "that window"
                step_phrases.append(f"focus {title}")
            elif step.kind == "click":
                step_phrases.append("click the target")
            elif step.kind == "type_text":
                step_phrases.append("type into the target field")
            elif step.kind == "scroll":
                scroll_mode = str(step.text or "down").strip().lower()
                if scroll_mode.startswith("up"):
                    step_phrases.append("scroll up to read earlier content")
                else:
                    step_phrases.append("scroll down to continue reading")
            elif step.kind == "window_state":
                state = str(step.text or "restore").strip().lower()
                if state == "minimize":
                    step_phrases.append("minimize the target window")
                elif state == "maximize":
                    step_phrases.append("maximize the target window")
                else:
                    step_phrases.append("restore the target window")

        if not step_phrases:
            return ""

        if len(step_phrases) == 1:
            return f"I will {step_phrases[0]} now."
        if len(step_phrases) == 2:
            return f"I will {step_phrases[0]}, then {step_phrases[1]}."
        return f"I will {step_phrases[0]}, then {step_phrases[1]}, then {step_phrases[2]}."