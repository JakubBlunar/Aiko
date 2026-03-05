from __future__ import annotations

from collections.abc import Callable

from app.core.tooling.runtime.action_runtime import ActionPlan, GuardedActionExecutor, PlannedAction
from app.core.tooling.types import ToolContext, ToolError, ToolResult, ToolSpec


class ActionExecutePlanTool:
    def __init__(self, runtime: GuardedActionExecutor) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="action.execute_plan",
            description="Execute a planned ordered list of desktop actions with runtime guardrails.",
            is_mutating=False,
            input_schema={
                "required": ["plan"],
                "properties": {
                    "plan": "dict",
                },
            },
            output_schema={
                "executed": "bool",
                "dry_run": "bool",
                "blocked": "bool",
                "requires_confirmation": "bool",
                "message": "str",
            },
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))

        plan_raw = args.get("plan")
        if not isinstance(plan_raw, dict):
            return ToolResult(success=False, error=ToolError(code="invalid_plan", message="'plan' must be an object."))

        steps_raw = plan_raw.get("steps", [])
        if not isinstance(steps_raw, list):
            return ToolResult(success=False, error=ToolError(code="invalid_plan_steps", message="'plan.steps' must be a list."))

        steps: list[PlannedAction] = []
        for raw in steps_raw:
            if not isinstance(raw, dict):
                continue
            steps.append(
                PlannedAction(
                    kind=str(raw.get("kind", "none")).strip().lower() or "none",
                    x=(int(raw["x"]) if raw.get("x") is not None else None),
                    y=(int(raw["y"]) if raw.get("y") is not None else None),
                    text=(str(raw.get("text", "")).strip() or None),
                    hwnd=(int(raw["hwnd"]) if raw.get("hwnd") is not None else None),
                    confidence=float(raw.get("confidence", 0.0) or 0.0),
                    reason=str(raw.get("reason", "")).strip(),
                )
            )

        plan = ActionPlan(
            steps=steps,
            description=str(plan_raw.get("description", "")).strip(),
            needs_screen=bool(plan_raw.get("needs_screen", False)),
        )

        outcome = self._runtime.execute_plan(plan)
        return ToolResult(
            success=bool(outcome.executed or outcome.requires_confirmation or outcome.dry_run or not outcome.blocked),
            data={
                "executed": bool(outcome.executed),
                "dry_run": bool(outcome.dry_run),
                "blocked": bool(outcome.blocked),
                "requires_confirmation": bool(outcome.requires_confirmation),
                "message": str(outcome.message),
            },
        )
