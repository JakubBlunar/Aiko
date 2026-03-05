from __future__ import annotations


class SessionRouter:
    def __init__(
        self,
        *,
        supported_session_types: set[str],
        default_session_type: str = "chat",
    ) -> None:
        self._supported = set(str(item).strip().lower() for item in supported_session_types if str(item).strip())
        self._default = str(default_session_type).strip().lower() or "chat"
        if self._default not in self._supported:
            self._supported.add(self._default)

    def resolve(
        self,
        *,
        inferred_session_type: str,
        inferred_goal: str,
        current_session_type: str,
    ) -> tuple[str, str]:
        candidate = str(inferred_session_type or "").strip().lower()
        if candidate in self._supported:
            return candidate, "model"

        goal = str(inferred_goal or "").strip().lower()
        if ("agentic" in goal or "autonom" in goal) and "agentic" in self._supported:
            return "agentic", "goal_fallback"
        if "read" in goal and "reading" in self._supported:
            return "reading", "goal_fallback"

        current = str(current_session_type or "").strip().lower()
        if current in self._supported:
            return current, "keep_current"

        return self._default, "default"