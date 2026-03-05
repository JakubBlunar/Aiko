from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from app.core.planning.turn_orchestrator import TurnAutonomyPlan


@dataclass(slots=True)
class GoalInference:
    goal: str
    confidence: float
    reason: str
    description: str = ""
    session_type: str = "chat"


class AutonomyPlanner:
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

    def plan_turn_autonomy(
        self,
        *,
        user_text: str,
        active_goal: str,
        active_goal_description: str,
        max_strategy_chars: int,
        proactive_conversation: bool,
        allow_action_suggestions: bool,
        allow_proactive_actions: bool,
        actions_enabled: bool,
        available_tool_names: list[str],
    ) -> TurnAutonomyPlan:
        defaults = TurnAutonomyPlan(
            strategy="Respond naturally, concise first, ask one focused follow-up when useful.",
            should_use_screen=False,
            should_plan_action=False,
            ask_followup=True,
            confidence=0.4,
            action_intent="",
        )

        recent_memory = self._history_messages(6)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")

        caps: list[str] = ["- respond_to_user (always available)"]
        for tool_name in available_tool_names:
            caps.append(f"- tool:{tool_name}")
        if actions_enabled:
            caps.append("- execute_click: click a UI element at given screen coordinates")
            caps.append("- execute_type: type text into the focused field")
        caps_block = "\n".join(caps)

        goal_context = active_goal or "general_conversation"
        goal_desc = active_goal_description or ""

        try:
            raw = self._planner_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an autonomous assistant turn planner. "
                            "Given the current goal, available capabilities, and the user's message, "
                            "decide which capabilities are needed for this turn and plan the strategy. "
                            "Return exactly one JSON object with keys: "
                            "strategy, should_use_screen, should_plan_action, ask_followup, confidence, action_intent. "
                            "strategy: one short sentence under 180 chars describing the response approach. "
                            "should_use_screen: true if reading screen context is needed to answer this turn. "
                            "should_plan_action: true if a UI click or type action should be performed this turn. "
                            "action_intent: if should_plan_action is true, one sentence describing the intended UI action; otherwise empty string. "
                            "ask_followup: true if the assistant needs to ask the user a clarifying question before acting. "
                            "IMPORTANT: should_plan_action and ask_followup are mutually exclusive - "
                            "if you need more information first, set ask_followup=true and should_plan_action=false. "
                            "confidence: 0.0-1.0 reflecting plan certainty. "
                            "Use booleans for flags."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current goal: {goal_context}\n"
                            + (f"Goal description: {goal_desc}\n" if goal_desc else "")
                            + f"\nAvailable capabilities:\n{caps_block}\n\n"
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            "Settings:\n"
                            f"- proactive_conversation={proactive_conversation}\n"
                            f"- allow_action_suggestions={allow_action_suggestions}\n"
                            f"- allow_proactive_actions={allow_proactive_actions}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("autonomy.plan", "Autonomy planner unavailable; using defaults.")
            return defaults

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            self._trace("autonomy.plan", "Autonomy planner returned invalid JSON; using defaults.")
            return defaults

        strategy = str(payload.get("strategy", defaults.strategy)).strip() or defaults.strategy
        strategy = strategy[: max(40, int(max_strategy_chars))]

        confidence = float(payload.get("confidence", defaults.confidence) or defaults.confidence)
        confidence = max(0.0, min(confidence, 1.0))

        plan = TurnAutonomyPlan(
            strategy=strategy,
            should_use_screen=bool(payload.get("should_use_screen", False)),
            should_plan_action=bool(payload.get("should_plan_action", False)),
            ask_followup=bool(payload.get("ask_followup", True)),
            confidence=confidence,
            action_intent=str(payload.get("action_intent", "")).strip(),
        )

        if not allow_action_suggestions or not allow_proactive_actions:
            plan.should_plan_action = False
        if not proactive_conversation:
            plan.ask_followup = False
        if plan.ask_followup:
            plan.should_plan_action = False
        if not plan.should_plan_action:
            plan.action_intent = ""

        self._trace(
            "autonomy.plan",
            (
                f"strategy='{plan.strategy}' | screen={plan.should_use_screen} | "
                f"action={plan.should_plan_action} | followup={plan.ask_followup} | "
                f"confidence={round(plan.confidence, 2)}"
                + (f" | intent='{plan.action_intent}'" if plan.action_intent else "")
            ),
        )
        return plan

    def infer_goal(self, *, user_text: str, active_goal: str) -> GoalInference:
        recent_memory = self._history_messages(8)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")

        try:
            raw = self._planner_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Infer the most useful current conversation goal from dialogue context. "
                            "Return exactly one JSON object with keys: goal, confidence, reason, description, session_type. "
                            "goal: a short snake_case identifier for the task "
                            "(e.g. 'tic_tac_toe', 'english_practice', 'coding_help', 'general_conversation'). "
                            "description: one sentence describing what the user is trying to accomplish. "
                            "session_type: one of 'chat' or 'reading' (choose reading only when the user asks to read/continue reading on-screen content). "
                            "confidence: 0.0-1.0 how confident you are in the inferred goal. "
                            "reason: brief explanation of why you chose this goal."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current goal: {active_goal}\n\n"
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}"
                        ),
                    },
                ]
            )
        except Exception:
            return GoalInference(goal=active_goal, confidence=0.0, reason="goal-planner-unavailable")

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            return GoalInference(goal=active_goal, confidence=0.0, reason="invalid-goal-json")

        raw_goal = str(payload.get("goal", active_goal)).strip().lower()
        sanitized = re.sub(r"[^a-z0-9\s_]", "", raw_goal)
        sanitized = re.sub(r"[\s_]+", "_", sanitized).strip("_")
        goal = sanitized[:60] or active_goal

        confidence = float(payload.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(confidence, 1.0))
        reason = str(payload.get("reason", "")).strip()
        description = str(payload.get("description", "")).strip()
        session_type = str(payload.get("session_type", "chat")).strip().lower()
        if session_type not in {"chat", "reading"}:
            session_type = "chat"
        return GoalInference(
            goal=goal,
            confidence=confidence,
            reason=reason,
            description=description,
            session_type=session_type,
        )
