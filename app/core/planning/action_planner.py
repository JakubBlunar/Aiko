from __future__ import annotations

from collections.abc import Callable

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

    @staticmethod
    def has_action_intent(user_text: str) -> bool:
        normalized = (user_text or "").lower()
        triggers = (
            "click",
            "press",
            "tap",
            "type",
            "write",
            "fill",
            "open",
            "select",
            "choose",
            "submit",
            "send",
            "scroll",
            "minimize",
            "minimise",
            "maximize",
            "restore",
            "read on screen",
            "read the article",
        )
        return any(token in normalized for token in triggers)

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
                is_own = el.get("source") == "uia" and own_title_lower in win_title
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
            windows_lines.append(f"- hwnd={w['hwnd']} \"{w['title']}\"{label_str}")
        windows_block = "\n".join(windows_lines) if windows_lines else "[none]"

        try:
            raw = self._planner_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an action planner for desktop UI automation. "
                            "Return exactly one JSON object with keys: "
                            "steps (array), description (string), needs_screen (bool). "
                            "Each step object has keys: type, x, y, text, hwnd, confidence, reason. "
                            "Allowed step types: none, click, type_text, focus_window, scroll, window_state. "
                            "Steps are executed in order - use multiple steps when needed "
                            "(e.g. focus_window first, then click, then type_text). "
                            "For click: x and y are the element center to click. "
                            "For type_text: text is the string to type; "
                            "x and y are the coordinates of the INPUT FIELD to click for focus "
                            "- pick the text input element, not a button. "
                            "For focus_window: hwnd is the integer window handle from the 'Open windows' list; "
                            "use this to restore a minimized window or bring a background window to the front "
                            "before performing a click inside it. "
                            "For scroll: set text to one of: down, up, down:8, up:8 (direction with optional strength), "
                            "and optionally set x/y as the on-screen anchor position to scroll around. "
                            "For window_state: set text to one of minimize, maximize, restore and provide hwnd. "
                            "Set x, y, text, hwnd to null when unused. "
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
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            f"Open windows (hwnd handles for focus_window):\n{windows_block}\n\n"
                            f"Detected UI elements (text -> screen coordinates):\n{elements_block}\n\n"
                            f"Full OCR text:\n{(screen_text or '[none]')[:3000]}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("action.plan", "Planner unavailable. Falling back to no action.")
            return ActionPlan(steps=[], description="Action planner unavailable")

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            self._trace("action.plan", "Planner output was not valid JSON. Falling back to no action.")
            return ActionPlan(steps=[], description="Planner output was not valid JSON")

        def _parse_step(s: dict) -> PlannedAction:
            raw_kind = str(s.get("type", "none")).strip().lower()
            if raw_kind not in {"none", "click", "type_text", "focus_window", "scroll", "window_state"}:
                raw_kind = "none"
            conf = float(s.get("confidence", 0.0) or 0.0)
            conf = max(0.0, min(conf, 1.0))
            return PlannedAction(
                kind=raw_kind,
                x=(int(s["x"]) if s.get("x") is not None else None),
                y=(int(s["y"]) if s.get("y") is not None else None),
                text=(str(s.get("text", "")).strip() or None),
                hwnd=(int(s["hwnd"]) if s.get("hwnd") is not None else None),
                confidence=conf,
                reason=str(s.get("reason", "")).strip(),
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
        self._trace(
            "action.plan",
            (
                f"Planned {len(plan.steps)} step(s): "
                + " -> ".join(f"{s.kind}(conf={round(s.confidence, 2)})" for s in plan.steps)
                or "no actions"
            ),
        )
        return plan
