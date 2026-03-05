from __future__ import annotations

import unittest

from app.core.settings import ActionSettings
from app.core.tooling.runtime.action_runtime import GuardedActionExecutor
from app.core.tooling.runtime.emergency_stop import EmergencyStopState
from app.core.tooling.tools.action_tools import ActionExecutePlanTool
from app.core.tooling.types import ToolContext


class ToolingActionToolTests(unittest.TestCase):
    def test_action_execute_plan_dry_run(self) -> None:
        settings = ActionSettings(
            enabled=True,
            dry_run=True,
            require_confirmation=False,
            decision_mode="explicit_only",
            max_actions_per_turn=3,
            min_confidence=0.2,
            min_action_interval_seconds=0.0,
            emergency_hotkey="ctrl+alt+f12",
            allowlist_window_titles=[],
        )
        runtime = GuardedActionExecutor(settings, EmergencyStopState())
        tool = ActionExecutePlanTool(runtime)

        result = tool.run(
            ToolContext(),
            {
                "plan": {
                    "description": "dry-run test",
                    "steps": [
                        {"kind": "click", "x": 10, "y": 10, "confidence": 0.9, "reason": "test"}
                    ],
                }
            },
        )

        self.assertTrue(result.success)
        self.assertTrue(result.data.get("dry_run"))
        self.assertFalse(result.data.get("executed"))


if __name__ == "__main__":
    unittest.main()
