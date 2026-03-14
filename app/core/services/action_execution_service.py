from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from app.core.tooling.runtime.action_runtime import ActionExecutionResult, ActionPlan, PlannedAction


class ActionExecutionService:
    def __init__(
        self,
        *,
        actions_settings: object,
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
        normalized = (user_text or "").strip().lower()
        if not normalized:
            return False
        triggers = (
            "click",
            "press",
            "tap",
            "type",
            "write",
            "fill",
            "open",
            "launch",
            "start",
            "select",
            "choose",
            "submit",
            "send",
            "scroll",
            "focus",
            "activate",
            "switch to",
            "switch",
            "bring",
            "move",
            "drag",
            "drop",
            "minimize",
            "minimise",
            "maximize",
            "restore",
            "close",
            "show",
            "hide",
            "resize",
            "arrange",
            "make active",
            "bring to front",
            "read on screen",
            "read the article",
        )
        if any(token in normalized for token in triggers):
            return True

        return bool(
            re.search(
                r"\b(can you|could you|please|would you)\b.*\b(minimi[sz]e|maximi[sz]e|restore|focus|switch|open|close|activate|show|hide|resize|arrange)\b",
                normalized,
            )
            or re.search(
                r"\b(make|set)\b.*\b(active|foreground|front)\b",
                normalized,
            )
        )

    def _detect_action_intent(self, user_text: str) -> bool:
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
        _ = assistant_reply
        _ = screen_text
        normalized = str(action_intent or user_text or "").strip().lower()
        available_tool_names = list(self._list_available_tools() or [])
        available_tool_schemas = self._list_available_tool_schemas() or {}

        tool_name = self._select_mcp_tool_name(
            normalized_text=normalized,
            available_tool_names=available_tool_names,
        )
        if tool_name:
            args = self._build_mcp_args(
                tool_name=tool_name,
                user_text=user_text,
                action_intent=action_intent,
                schemas=available_tool_schemas,
                feedback=tool_error_feedback,
            )
            return ActionPlan(
                steps=[
                    PlannedAction(
                        kind="mcp_tool",
                        text=tool_name,
                        meta=args,
                        confidence=0.8,
                        reason="Deterministic MCP action plan.",
                    )
                ],
                description="Execute the requested app action using MCP.",
                needs_screen=False,
            )

        windows = list(self._all_windows() or [])
        if windows and any(token in normalized for token in ("minimize", "minimise", "maximize", "restore")):
            target = next((item for item in windows if bool(item.get("is_foreground"))), windows[0])
            state = "minimize" if ("minimize" in normalized or "minimise" in normalized) else "restore"
            if "maximize" in normalized:
                state = "maximize"
            return ActionPlan(
                steps=[
                    PlannedAction(
                        kind="window_state",
                        hwnd=int(target.get("hwnd") or 0) or None,
                        text=state,
                        confidence=0.75,
                        reason="Apply requested window state.",
                    )
                ],
                description=f"Set window state to {state}.",
                needs_screen=False,
            )

        if "scroll" in normalized:
            direction = "up" if "up" in normalized else "down"
            return ActionPlan(
                steps=[
                    PlannedAction(
                        kind="scroll",
                        text=f"{direction}:8",
                        confidence=0.75,
                        reason="Continue reading flow.",
                    )
                ],
                description=f"Scroll {direction}.",
                needs_screen=False,
            )

        return ActionPlan(
            steps=[],
            description="No deterministic action steps were inferred.",
            needs_screen=False,
        )

    @staticmethod
    def _select_mcp_tool_name(*, normalized_text: str, available_tool_names: list[str]) -> str:
        mcp_tools = [name for name in available_tool_names if str(name).strip().lower().startswith("mcp.")]
        if not mcp_tools:
            return ""
        if len(mcp_tools) == 1:
            return str(mcp_tools[0])

        if "notification" in normalized_text:
            for name in mcp_tools:
                if "notification" in str(name).lower():
                    return str(name)
        if any(token in normalized_text for token in ("window", "minimize", "minimise", "maximize", "switch", "focus", "active")):
            for name in mcp_tools:
                lower_name = str(name).lower()
                if "window" in lower_name or lower_name.endswith(".app"):
                    return str(name)
        return str(mcp_tools[0])

    def _build_mcp_args(
        self,
        *,
        tool_name: str,
        user_text: str,
        action_intent: str,
        schemas: dict[str, dict[str, object]],
        feedback: str | None,
    ) -> dict[str, object]:
        schema = schemas.get(str(tool_name), {}) if isinstance(schemas, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        enum_hints = schema.get("enum_hints", {}) if isinstance(schema, dict) else {}
        normalized = str(action_intent or user_text or "").strip().lower()

        args: dict[str, object] = {}
        required_keys = [str(item).strip() for item in required if str(item).strip()] if isinstance(required, list) else []
        all_keys = list(required_keys)
        if isinstance(properties, dict):
            for key in properties.keys():
                key_text = str(key).strip()
                if key_text and key_text not in all_keys:
                    all_keys.append(key_text)

        for key in all_keys:
            lower_key = key.lower()
            value: object | None = None
            if lower_key in {"action", "mode", "state"}:
                value = self._infer_mode_value(
                    normalized_text=normalized,
                    enum_hints=enum_hints,
                    key=key,
                )
            elif lower_key in {"target", "window", "window_title"}:
                value = self._extract_window_target(user_text)
            elif lower_key == "title" and "notification" in str(tool_name).lower():
                value = "Assistant"
            elif lower_key == "message" and "notification" in str(tool_name).lower():
                value = str(user_text or "").strip() or None

            if value is not None and str(value).strip():
                args[key] = value

        if feedback:
            key_name, allowed_values = self._parse_enum_feedback(feedback)
            if key_name and allowed_values:
                args[key_name] = allowed_values[0]

        return args

    @staticmethod
    def _infer_mode_value(
        *,
        normalized_text: str,
        enum_hints: object,
        key: str,
    ) -> str:
        if "minimize" in normalized_text or "minimise" in normalized_text:
            candidate = "minimize"
        elif "maximize" in normalized_text:
            candidate = "maximize"
        elif "restore" in normalized_text:
            candidate = "restore"
        elif "switch" in normalized_text or "focus" in normalized_text or "active" in normalized_text:
            candidate = "switch"
        elif "launch" in normalized_text or "open" in normalized_text:
            candidate = "launch"
        else:
            candidate = "switch"

        if isinstance(enum_hints, dict):
            allowed_raw = enum_hints.get(str(key), [])
            if isinstance(allowed_raw, list):
                allowed = [str(item).strip() for item in allowed_raw if str(item).strip()]
                if allowed and candidate not in allowed:
                    return candidate
        return candidate

    @staticmethod
    def _extract_window_target(user_text: str) -> str:
        text = str(user_text or "").strip()
        quoted = re.findall(r"'([^']+)'|\"([^\"]+)\"", text)
        if quoted:
            for left, right in quoted:
                value = (left or right or "").strip()
                if value:
                    return value
        match = re.search(
            r"\b(vscode|visual studio code|notepad|chrome|firefox|edge|terminal|calculator)\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return str(match.group(1)).strip()
        return "active window"

    @staticmethod
    def _parse_enum_feedback(feedback: str) -> tuple[str, list[str]]:
        key_match = re.search(r"Invalid value for '([^']+)'", str(feedback or ""))
        allowed_match = re.search(r"Allowed values:\s*\[([^\]]+)\]", str(feedback or ""))
        key_name = str(key_match.group(1)).strip() if key_match else ""
        if not allowed_match:
            return key_name, []

        allowed_values: list[str] = []
        for raw in allowed_match.group(1).split(","):
            cleaned = raw.strip().strip("'").strip('"')
            if cleaned:
                allowed_values.append(cleaned)
        return key_name, allowed_values

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