from __future__ import annotations

from collections.abc import Callable

from app.core.planning.action_planner import ActionPlanner
from app.core.tooling.runtime.action_runtime import ActionExecutionResult, ActionPlan


class ActionExecutionService:
    def __init__(
        self,
        *,
        actions_settings: object,
        action_planner: ActionPlanner,
        execute_action_plan: Callable[[ActionPlan], ActionExecutionResult],
        capture_screen_text: Callable[..., str | None],
        invoke_tool: Callable[..., object],
        trace: Callable[[str, str], None],
        screen_enabled: Callable[[], bool],
        active_goal: Callable[[], str],
        list_available_tools: Callable[[], list[str]],
        list_available_tool_schemas: Callable[[], dict[str, dict[str, object]]],
        last_screen_elements: Callable[[], list[dict]],
        all_windows: Callable[[], list[dict]],
    ) -> None:
        self._actions_settings = actions_settings
        self._action_planner = action_planner
        self._execute_action_plan = execute_action_plan
        self._capture_screen_text = capture_screen_text
        self._invoke_tool = invoke_tool
        self._trace = trace
        self._screen_enabled = screen_enabled
        self._active_goal = active_goal
        self._list_available_tools = list_available_tools
        self._list_available_tool_schemas = list_available_tool_schemas
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
        detected_action_intent = self._detect_action_intent(user_text)

        if not bool(getattr(self._actions_settings, "enabled", False)):
            wants_action = bool(action_intent) or detected_action_intent
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
        if mode == "explicit_only" and not allow_planning_override and not detected_action_intent:
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

        if planned.steps and on_token:
            hwnd_to_title = {w["hwnd"]: w["title"] for w in self._all_windows()}
            plain_summary = self.summarize_action_plan_plain(planned, hwnd_to_title)
            if plain_summary:
                on_token(f"\n\n{plain_summary}\n")
            lines: list[str] = []
            for idx, step in enumerate(planned.steps, start=1):
                if step.kind == "mcp_tool":
                    tool_name = str(step.text or "").strip() or "[missing_tool_name]"
                    line = f"{idx}. mcp_tool({tool_name})"
                    if isinstance(step.meta, dict) and step.meta:
                        line += f" args={step.meta}"
                elif step.kind == "focus_window":
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

        first_step = planned.steps[0] if planned.steps else None
        if first_step is not None and first_step.kind == "mcp_tool":
            return self._execute_mcp_with_repair(
                first_step=first_step,
                user_text=user_text,
                assistant_reply=assistant_reply,
                screen_text=screen_text,
                action_intent=action_intent,
            )

        result = self._execute_action_plan(planned)
        self._trace("action.execute", result.message)
        return result

    @staticmethod
    def has_action_intent(user_text: str) -> bool:
        return ActionPlanner.has_action_intent(user_text)

    def _detect_action_intent(self, user_text: str) -> bool:
        detector = getattr(self._action_planner, "has_action_intent_with_model", None)
        if callable(detector):
            return bool(detector(user_text))
        return self.has_action_intent(user_text)

    @staticmethod
    def _is_blank_value(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    def _validate_mcp_tool_args(self, tool_name: str, tool_args: dict[str, object]) -> str | None:
        schemas = self._list_available_tool_schemas()
        schema = schemas.get(str(tool_name), {}) if isinstance(schemas, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        if not isinstance(required, list):
            return None
        missing: list[str] = []
        for raw_key in required:
            key = str(raw_key or "").strip()
            if not key:
                continue
            if key not in tool_args or self._is_blank_value(tool_args.get(key)):
                missing.append(key)
        if missing:
            quoted = ", ".join(f"'{key}'" for key in missing)
            return f"Missing required argument(s): {quoted}."

        enum_hints = schema.get("enum_hints", []) if isinstance(schema, dict) else []
        if isinstance(enum_hints, dict):
            for raw_key, raw_allowed in enum_hints.items():
                key = str(raw_key or "").strip()
                if not key or key not in tool_args:
                    continue
                if not isinstance(raw_allowed, list):
                    continue
                allowed = [str(item).strip() for item in raw_allowed if str(item).strip()]
                if not allowed:
                    continue
                value = str(tool_args.get(key) or "").strip()
                if value and value not in allowed:
                    return f"Invalid value for '{key}': {value!r}. Allowed values: {allowed}."
        return None

    def _execute_mcp_with_repair(
        self,
        *,
        first_step: object,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        action_intent: str,
    ) -> ActionExecutionResult:
        step = first_step
        max_repair_attempts = max(
            0,
            min(20, int(getattr(self._actions_settings, "mcp_repair_attempts", 2) or 2)),
        )

        for attempt in range(max_repair_attempts + 1):
            tool_name = str(getattr(step, "text", "") or "").strip()
            tool_args = dict(getattr(step, "meta", {})) if isinstance(getattr(step, "meta", {}), dict) else {}
            if not tool_name:
                return ActionExecutionResult(
                    executed=False,
                    dry_run=False,
                    blocked=True,
                    requires_confirmation=False,
                    message="Planner requested mcp_tool without a tool name.",
                )

            validation_error = self._validate_mcp_tool_args(tool_name, tool_args)
            if validation_error:
                self._trace("action.plan", f"Blocked invalid mcp_tool step: {validation_error}")
                if attempt < max_repair_attempts:
                    repaired = self._repair_mcp_step(
                        user_text=user_text,
                        assistant_reply=assistant_reply,
                        screen_text=screen_text,
                        action_intent=action_intent,
                        failed_tool_name=tool_name,
                        failed_tool_args=tool_args,
                        failure_message=validation_error,
                    )
                    if repaired is not None:
                        step = repaired
                        continue
                return ActionExecutionResult(
                    executed=False,
                    dry_run=False,
                    blocked=True,
                    requires_confirmation=False,
                    message=f"MCP tool '{tool_name}' blocked: {validation_error}",
                )

            tool_result = self._invoke_tool(tool_name, args=tool_args)
            if tool_result.success:
                summary = str(tool_result.data.get("text") or tool_result.data.get("message") or "MCP tool executed.").strip()
                return ActionExecutionResult(
                    executed=True,
                    dry_run=False,
                    blocked=False,
                    requires_confirmation=False,
                    message=f"Executed MCP tool '{tool_name}'. {summary}".strip(),
                )

            error_message = "MCP tool execution failed."
            if tool_result.error is not None:
                error_message = str(tool_result.error.message or error_message)
            if attempt < max_repair_attempts:
                repaired = self._repair_mcp_step(
                    user_text=user_text,
                    assistant_reply=assistant_reply,
                    screen_text=screen_text,
                    action_intent=action_intent,
                    failed_tool_name=tool_name,
                    failed_tool_args=tool_args,
                    failure_message=error_message,
                )
                if repaired is not None:
                    step = repaired
                    continue
            return ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=True,
                requires_confirmation=False,
                message=f"MCP tool '{tool_name}' failed: {error_message}",
            )

        return ActionExecutionResult(
            executed=False,
            dry_run=False,
            blocked=True,
            requires_confirmation=False,
            message="MCP tool repair loop ended without a valid step.",
        )

    def _repair_mcp_step(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        action_intent: str,
        failed_tool_name: str,
        failed_tool_args: dict[str, object],
        failure_message: str,
    ):
        feedback = (
            f"Previous mcp_tool failed. tool={failed_tool_name} args={failed_tool_args} error={failure_message}. "
            "Repair by returning one corrected mcp_tool step with valid required fields and enum values."
        )
        self._trace("action.repair", feedback)
        repaired_plan = self._plan_action(
            user_text=user_text,
            assistant_reply=assistant_reply,
            screen_text=screen_text,
            action_intent=action_intent,
            tool_error_feedback=feedback,
        )
        if not repaired_plan.steps:
            return None
        repaired = repaired_plan.steps[0]
        if str(getattr(repaired, "kind", "")).strip().lower() != "mcp_tool":
            return None

        repaired_name = str(getattr(repaired, "text", "") or "").strip()
        repaired_args = dict(getattr(repaired, "meta", {})) if isinstance(getattr(repaired, "meta", {}), dict) else {}
        if repaired_name == failed_tool_name and repaired_args == failed_tool_args:
            self._trace("action.repair", "Planner repair returned identical mcp_tool step; skipping retry.")
            return None
        return repaired

    def _plan_action(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        action_intent: str,
        tool_error_feedback: str | None = None,
    ) -> ActionPlan:
        return self._action_planner.plan_action(
            user_text=user_text,
            assistant_reply=assistant_reply,
            screen_text=screen_text,
            action_intent=action_intent,
            active_goal=self._active_goal(),
            last_screen_elements=self._last_screen_elements(),
            all_windows=self._all_windows(),
            available_tool_names=self._list_available_tools(),
            available_tool_schemas=self._list_available_tool_schemas(),
            tool_error_feedback=tool_error_feedback,
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