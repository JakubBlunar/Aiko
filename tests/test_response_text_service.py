from __future__ import annotations

import unittest

from app.core.services.response_text_service import strip_action_meta_for_tts


class ResponseTextServiceTests(unittest.TestCase):
    def test_strip_inline_action_suffix(self) -> None:
        source = (
            'Assistant: I will send the Win+M shortcut to minimize VSCode! '
            "[Action] Executed MCP tool 'mcp.windows.Shortcut'. Pressed Win+M."
        )

        cleaned = strip_action_meta_for_tts(source)

        self.assertNotIn("[Action]", cleaned)
        self.assertNotIn("Executed MCP tool", cleaned)
        self.assertIn("I will send the Win+M shortcut", cleaned)


if __name__ == "__main__":
    unittest.main()
