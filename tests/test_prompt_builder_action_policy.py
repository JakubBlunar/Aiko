from __future__ import annotations

import unittest

from app.core.turn_manager import TurnInput, TurnManager
from app.llm.prompt_builder import PromptContext, build_messages


class PromptBuilderActionPolicyTests(unittest.TestCase):
    def test_build_messages_includes_automatic_no_approval_instruction(self) -> None:
        messages = build_messages(
            PromptContext(
                user_text="Open Notepad.",
                autonomy_mode="automatic",
                action_confirmation_required=False,
            )
        )
        system = messages[0]["content"]

        self.assertIn("Autonomy mode: automatic.", system)
        self.assertIn("Action confirmation policy: disabled", system)
        self.assertIn("Do not ask the user to approve or reject actions", system)

    def test_turn_manager_passes_confirmation_context_to_prompt(self) -> None:
        manager = TurnManager()
        messages = manager.build_chat_messages(
            TurnInput(
                user_text="Switch to browser.",
                autonomy_mode="interactive",
                action_confirmation_required=True,
            )
        )
        system = messages[0]["content"]

        self.assertIn("Autonomy mode: interactive.", system)
        self.assertIn("Action confirmation policy: enabled.", system)


if __name__ == "__main__":
    unittest.main()
