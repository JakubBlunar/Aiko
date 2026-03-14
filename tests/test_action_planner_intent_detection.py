from __future__ import annotations

import unittest

from app.core.action_intent import has_action_intent


class ActionIntentDetectionTests(unittest.TestCase):
    def test_detects_explicit_action_request(self) -> None:
        self.assertTrue(has_action_intent("Click the Save button."))

    def test_detects_polite_window_request(self) -> None:
        self.assertTrue(has_action_intent("Could you minimise the VSCode window?"))

    def test_ignores_non_action_message(self) -> None:
        self.assertFalse(has_action_intent("Can you summarize this conversation so far?"))


if __name__ == "__main__":
    unittest.main()
