from __future__ import annotations

from collections.abc import Callable
import json
import re
from typing import Any

from app.core.tooling.runtime.action_runtime import ActionPlan, PlannedAction


class ActionPlanner:
    def __init__(
        self,
        *,
        planner_chat: Callable[[list[dict[str, str]]], str],
        history_messages: Callable[[int], list[dict[str, str]]],
        extract_json_object: Callable[[str], object],
        trace: Callable[[str, str], None],
    ) -> None:
        self._planner_chat = planner_chat
        self._history_messages = history_messages
        self._extract_json_object = extract_json_object
        self._trace = trace

    def _trace_json(self, stage: str, payload: dict[str, Any]) -> None:
        try:
            self._trace(stage, json.dumps(payload, ensure_ascii=True, default=str))
        except Exception:
            self._trace(stage, str(payload))

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

        # Catch polite imperative phrasing that omits explicit low-level verbs.
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

    def has_action_intent_with_model(self, user_text: str) -> bool:
        """Use planner model for borderline intent detection with safe local fallback."""
        heuristic = self.has_action_intent(user_text)
        normalized = (user_text or "").strip()
        if not normalized:
            return False

        # Fast-path obvious explicit requests without another model turn.
        if heuristic:
            return True

        self._trace_json(
            "action.intent.model.request",
            {
                "user_text": normalized,
                "heuristic_result": heuristic,
            },
        )

        try:
            raw = self._planner_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Classify whether the latest user message asks for a desktop/app action. "
                            "Return exactly one JSON object: {\"action_intent\": <bool>}. "
                            "Use true for requests to click/type/open/focus/switch/minimize/maximize/restore/close/scroll "
                            "or otherwise manipulate desktop apps/windows. "
                            "Use false for pure conversation, explanation, brainstorming, or summaries."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Latest user message:\n{normalized}",
                    },
                ]
            )
            payload = self._extract_json_object(raw)
            if isinstance(payload, dict) and isinstance(payload.get("action_intent"), bool):
                decision = bool(payload["action_intent"])
                self._trace_json(
                    "action.intent.model.result",
                    {
                        "decision": decision,
                        "payload": payload,
                        "raw_preview": str(raw)[:500],
                    },
                )
                return decision
            self._trace_json(
                "action.intent.model.result",
                {
                    "decision": heuristic,
                    "fallback": "invalid_payload",
                    "payload": payload if isinstance(payload, dict) else str(payload),
                    "raw_preview": str(raw)[:500],
                },
            )
            self._trace("action.intent", "Model intent output invalid; using heuristic fallback.")
        except Exception as exc:
            self._trace_json(
                "action.intent.model.result",
                {
                    "decision": heuristic,
                    "fallback": "model_error",
                    "error": str(exc),
                },
            )
            self._trace("action.intent", "Model intent classification failed; using heuristic fallback.")

        return heuristic

    def plan_action(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        action_intent: str,
        active_goal: str,
        last_screen_elements: list[dict],
        all_windows: list[dict],
        available_tool_names: list[str],
        available_tool_schemas: dict[str, dict[str, Any]] | None = None,
        tool_error_feedback: str | None = None,
    ) -> ActionPlan:
        recent_memory = self._history_messages(4)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")

        own_title_lower = "assistant"
        elements_lines: list[str] = []
        for el in last_screen_elements[:60]:
            el_text = str(el.get("text", "")).strip()
            if el_text:
                win_title = str(el.get("window_title", "")).lower()
                is_own = own_title_lower in win_title
                own_tag = " [THIS APP - do not target]" if is_own else ""
                elements_lines.append(
                    f"- \"{el_text}\" at ({el['cx']}, {el['cy']}) size {el['w']}x{el['h']}{own_tag}"
                )
        elements_block = "\n".join(elements_lines) if elements_lines else "[none detected]"

        windows_lines: list[str] = []
        for w in all_windows[:20]:
            labels: list[str] = []
            if w.get("is_foreground"):
                labels.append("active")
            if w.get("is_minimized"):
                labels.append("MINIMIZED")
            label_str = f" [{', '.join(labels)}]" if labels else ""
            windows_lines.append(f"- \"{w['title']}\"{label_str}")
        windows_block = "\n".join(windows_lines) if windows_lines else "[none]"
        mcp_tools = [
            str(name).strip()
            for name in available_tool_names
            if str(name).strip().lower().startswith("mcp.")
        ]
        schema_map = available_tool_schemas if isinstance(available_tool_schemas, dict) else {}

        mcp_tool_lines: list[str] = []
        for name in sorted(mcp_tools)[:40]:
            schema = schema_map.get(name, {}) if isinstance(schema_map.get(name, {}), dict) else {}
            required = schema.get("required", []) if isinstance(schema.get("required", []), list) else []
            required_keys = [str(item).strip() for item in required if str(item).strip()]
            properties = schema.get("properties", {}) if isinstance(schema.get("properties", {}), dict) else {}
            enum_hints = schema.get("enum_hints", {}) if isinstance(schema.get("enum_hints", {}), dict) else {}
            optional_keys = [
                str(key).strip()
                for key in properties.keys()
                if str(key).strip() and str(key).strip() not in required_keys
            ][:8]
            line = f"- {name}"
            if required_keys:
                line += f" required={required_keys}"
            if optional_keys:
                line += f" optional={optional_keys}"
            if enum_hints:
                line += f" enum_hints={enum_hints}"
            mcp_tool_lines.append(line)
        mcp_tools_block = "\n".join(mcp_tool_lines) if mcp_tool_lines else "[none]"

        self._trace_json(
            "action.plan.model.request",
            {
                "active_goal": active_goal,
                "action_intent": action_intent,
                "user_text": user_text.strip(),
                "assistant_reply": assistant_reply.strip(),
                "tool_error_feedback": str(tool_error_feedback or "").strip(),
                "recent_conversation": recent_lines,
                "open_windows": windows_lines,
                "available_mcp_tools": sorted(mcp_tools),
                "available_mcp_schemas": {
                    name: schema_map.get(name, {})
                    for name in sorted(mcp_tools)
                },
                "detected_elements_count": len(elements_lines),
                "detected_elements_preview": elements_lines[:20],
                "screen_text_preview": (screen_text or "[none]")[:1000],
            },
        )

        try:
            raw = self._planner_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an action planner for desktop automation with MCP-first execution. "
                            "Return exactly one JSON object with keys: "
                            "steps (array), description (string), needs_screen (bool). "
                            "Each step object has keys: type, x, y, text, confidence, reason, args. "
                            "Allowed step types: none, click, type_text, scroll, mcp_tool. "
                            "Steps are executed in order - use multiple steps when needed "
                            "(e.g. click first, then type_text). "
                            "For click: x and y are the element center to click. "
                            "For type_text: text is the string to type; "
                            "x and y are the coordinates of the INPUT FIELD to click for focus "
                            "- pick the text input element, not a button. "
                            "For scroll: set text to one of: down, up, down:8, up:8 (direction with optional strength), "
                            "and optionally set x/y as the on-screen anchor position to scroll around. "
                            "For mcp_tool: set text to the full tool name (for example mcp.windows.App) "
                            "and put tool arguments in args as a JSON object. "
                            "For any mcp_tool step, args MUST include all required fields listed in the tool schema hints. "
                            "If tool_error_feedback is provided, treat this as a repair pass: "
                            "correct invalid tool name/args based on schema hints and prior error. "
                            "Do not repeat arguments that previously failed validation. "
                            "Do not use mcp.windows.Notification to approve internal mode/action switches. "
                            "Prefer mcp_tool whenever a listed MCP tool can perform the requested operation. "
                            "Prefer MCP tools over coordinate clicks for app/window operations. "
                            "When the user asks to minimize/maximize/restore/focus/switch apps, "
                            "prefer a single mcp_tool step if a suitable MCP tool is listed. "
                            "Set x, y, text to null when unused. "
                            "The 'Open windows' section is an inventory of visible app titles and states. "
                            "ALL coordinates MUST be exact values "
                            "copied from the 'Detected UI elements' list - do NOT invent coordinates. "
                            "CRITICAL: elements marked '[THIS APP - do not target]' belong to the assistant "
                            "application itself - NEVER click or type into them. "
                            "Always target elements in the user's intended application (e.g. Notepad, browser). "
                            "OCR may have minor typos (e.g. 'Seltings' for 'Settings'); "
                            "match labels by approximate similarity. "
                            "If 'Detected UI elements' shows [none detected] or the target app is not visible, "
                            "return needs_screen=true and steps=[] - the system will capture a fresh screenshot "
                            "and call you again with updated data. Only request this once. "
                            "confidence per step must be 0.0-1.0."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current goal: {active_goal}\n\n"
                            + (f"Action intent: {action_intent}\n\n" if action_intent else "")
                            + f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Assistant draft reply:\n{assistant_reply.strip()}\n\n"
                            + (
                                f"Tool execution feedback (repair this):\n{str(tool_error_feedback or '').strip()}\n\n"
                                if str(tool_error_feedback or "").strip()
                                else ""
                            )
                            +
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            f"Open windows (all_windows inventory):\n{windows_block}\n\n"
                            f"Available MCP tools:\n{mcp_tools_block}\n\n"
                            f"Detected UI elements (text -> screen coordinates):\n{elements_block}\n\n"
                            f"Full OCR text:\n{(screen_text or '[none]')[:3000]}"
                        ),
                    },
                ]
            )
        except Exception as exc:
            self._trace_json(
                "action.plan.model.result",
                {
                    "error": str(exc),
                },
            )
            self._trace("action.plan", "Planner unavailable. Falling back to no action.")
            return ActionPlan(steps=[], description="Action planner unavailable")

        payload = self._extract_json_object(raw)
        self._trace_json(
            "action.plan.model.raw_result",
            {
                "raw_preview": str(raw)[:2000],
                "payload_type": type(payload).__name__,
            },
        )
        if not isinstance(payload, dict):
            self._trace("action.plan", "Planner output was not valid JSON. Falling back to no action.")
            return ActionPlan(steps=[], description="Planner output was not valid JSON")

        def _parse_step(s: dict[str, Any]) -> PlannedAction:
            raw_kind = str(s.get("type", "none")).strip().lower()
            if raw_kind not in {"none", "click", "type_text", "scroll", "mcp_tool"}:
                raw_kind = "none"
            conf = float(s.get("confidence", 0.0) or 0.0)
            conf = max(0.0, min(conf, 1.0))
            raw_args = s.get("args", {})
            return PlannedAction(
                kind=raw_kind,
                x=(int(s["x"]) if s.get("x") is not None else None),
                y=(int(s["y"]) if s.get("y") is not None else None),
                text=(str(s.get("text", "")).strip() or None),
                hwnd=None,
                confidence=conf,
                reason=str(s.get("reason", "")).strip(),
                meta=(dict(raw_args) if isinstance(raw_args, dict) else None),
            )

        raw_steps = payload.get("steps")
        if isinstance(raw_steps, list):
            steps = [_parse_step(s) for s in raw_steps if isinstance(s, dict)]
        else:
            steps = [_parse_step(payload)]

        plan = ActionPlan(
            steps=[s for s in steps if s.kind != "none"],
            description=str(payload.get("description", "")).strip(),
            needs_screen=bool(payload.get("needs_screen", False)),
        )
        self._trace_json(
            "action.plan.model.parsed",
            {
                "description": plan.description,
                "needs_screen": plan.needs_screen,
                "step_count": len(plan.steps),
                "steps": [
                    {
                        "kind": s.kind,
                        "x": s.x,
                        "y": s.y,
                        "text": s.text,
                        "confidence": round(s.confidence, 3),
                        "reason": s.reason,
                        "args": s.meta,
                    }
                    for s in plan.steps
                ],
            },
        )
        self._trace(
            "action.plan",
            (
                f"Planned {len(plan.steps)} step(s): "
                + " -> ".join(f"{s.kind}(conf={round(s.confidence, 2)})" for s in plan.steps)
                or "no actions"
            ),
        )
        return plan
