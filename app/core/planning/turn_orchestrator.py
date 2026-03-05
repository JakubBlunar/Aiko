from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class TurnAutonomyPlan:
    strategy: str
    should_use_screen: bool
    should_plan_action: bool
    ask_followup: bool
    confidence: float
    action_intent: str = ""


@dataclass(slots=True)
class TurnOrchestratorPlan:
    strategy: str
    should_capture_screen: bool
    should_plan_action: bool
    requested_operations: tuple[str, ...]
    action_intent: str = ""
    reason: str = ""
    confidence: float = 0.0

    def has_operation(self, operation: str) -> bool:
        return str(operation).strip().lower() in set(self.requested_operations)


class TurnOrchestrator:
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

    def plan_turn(
        self,
        *,
        user_text: str,
        active_goal: str,
        autonomy_plan: TurnAutonomyPlan,
        require_confirmation: bool,
        screen_intent: bool,
        reading_intent: bool,
        continue_reading: bool,
    ) -> TurnOrchestratorPlan:
        default_plan = TurnOrchestratorPlan(
            strategy=autonomy_plan.strategy,
            should_capture_screen=bool(autonomy_plan.should_use_screen or screen_intent or continue_reading),
            should_plan_action=bool(autonomy_plan.should_plan_action),
            requested_operations=(
                tuple(op for op, enabled in (
                    ("include_session_evidence", bool(reading_intent or continue_reading)),
                    ("continue_session", bool(continue_reading)),
                ) if enabled)
            ),
            action_intent=str(autonomy_plan.action_intent or "").strip(),
            reason="legacy_fallback",
            confidence=float(autonomy_plan.confidence),
        )

        recent_memory = self._history_messages(6)
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
                            "You are a turn orchestrator for a desktop assistant. "
                            "Decide what operations the assistant should run this turn based on context. "
                            "Return exactly one JSON object with keys: "
                            "strategy, operations, action_intent, reason, confidence. "
                            "operations is an array using only: capture_screen, plan_action, include_session_evidence, continue_session, respond_only. "
                            "If an operation is not needed, omit it. "
                            "action_intent should be a short sentence only if plan_action is present; otherwise empty string. "
                            "If require_confirmation=false, do not suggest waiting for user approval in strategy/reason. "
                            "confidence must be 0.0-1.0."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Current goal: {active_goal}\n"
                            f"Legacy autonomy hint: strategy='{autonomy_plan.strategy}', "
                            f"use_screen={autonomy_plan.should_use_screen}, "
                            f"plan_action={autonomy_plan.should_plan_action}, "
                            f"action_intent='{autonomy_plan.action_intent}'\n"
                            f"require_confirmation={require_confirmation}\n\n"
                            f"Intent hints: screen_intent={screen_intent}, reading_intent={reading_intent}, "
                            f"continue_reading={continue_reading}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}"
                        ),
                    },
                ]
            )
        except Exception as exc:
            self._trace("orchestrator.plan", f"planner unavailable; fallback ({exc})")
            return default_plan

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            self._trace("orchestrator.plan", "invalid JSON; fallback")
            return default_plan

        ops_raw = payload.get("operations", [])
        ops = [str(item).strip().lower() for item in ops_raw if str(item).strip()] if isinstance(ops_raw, list) else []
        op_set = set(ops)

        confidence = float(payload.get("confidence", default_plan.confidence) or default_plan.confidence)
        confidence = max(0.0, min(confidence, 1.0))
        strategy = str(payload.get("strategy", default_plan.strategy)).strip() or default_plan.strategy
        reason = str(payload.get("reason", "")).strip()
        action_intent = str(payload.get("action_intent", "")).strip()

        if "respond_only" in op_set and len(op_set) > 1:
            op_set.remove("respond_only")

        plan = TurnOrchestratorPlan(
            strategy=strategy,
            should_capture_screen=("capture_screen" in op_set) or default_plan.should_capture_screen,
            should_plan_action=("plan_action" in op_set),
            requested_operations=tuple(sorted({
                *set(default_plan.requested_operations),
                *({"include_session_evidence"} if "include_session_evidence" in op_set else set()),
                *({"continue_session"} if "continue_session" in op_set else set()),
            })),
            action_intent=(action_intent if "plan_action" in op_set else ""),
            reason=reason,
            confidence=confidence,
        )

        if not plan.should_plan_action:
            plan.action_intent = ""

        self._trace(
            "orchestrator.plan",
            (
                f"strategy='{plan.strategy}' | capture={plan.should_capture_screen} | "
                f"action={plan.should_plan_action} | ops={list(plan.requested_operations)} | "
                f"confidence={round(plan.confidence, 2)}"
                + (f" | intent='{plan.action_intent}'" if plan.action_intent else "")
                + (f" | reason='{plan.reason}'" if plan.reason else "")
            ),
        )
        return plan
