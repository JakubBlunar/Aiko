from __future__ import annotations

import unittest

from app.core.services.action_execution_service import ActionExecutionService


class ActionIntentDetectionTests(unittest.TestCase):
    def test_detects_explicit_action_request(self) -> None:
        self.assertTrue(ActionExecutionService.has_action_intent("Click the Save button."))

    def test_detects_polite_window_request(self) -> None:
        self.assertTrue(ActionExecutionService.has_action_intent("Could you minimise the VSCode window?"))

    def test_ignores_non_action_message(self) -> None:
        self.assertFalse(ActionExecutionService.has_action_intent("Can you summarize this conversation so far?"))


if __name__ == "__main__":
    unittest.main()
