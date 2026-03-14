from __future__ import annotations

import unittest

from app.core.sessions.agentic_session import AgenticSessionConfig, AgenticSessionManager
from app.core.sessions.agentic_session_adapter import AgenticSessionAdapter
from app.core.sessions.chat_session import ChatSession
from app.core.sessions.reading_session import ReadingSessionConfig, ReadingSessionManager
from app.core.sessions.reading_session_adapter import ReadingSessionAdapter
from app.core.sessions.session_types import SessionToolPolicy


class SessionToolPolicyTests(unittest.TestCase):
    def test_chat_session_is_conservative(self) -> None:
        policy = ChatSession().tool_policy()
        self.assertFalse(policy.native_tool_calls_enabled)
        self.assertEqual(policy.allowed_tool_prefixes, ())
        self.assertFalse(policy.pre_execution_narration_default)

    def test_agentic_session_enables_native_mcp_tools(self) -> None:
        manager = AgenticSessionManager(AgenticSessionConfig(enabled=True, max_auto_steps=3))
        policy = AgenticSessionAdapter(manager).tool_policy()
        self.assertTrue(policy.native_tool_calls_enabled)
        self.assertEqual(policy.allowed_tool_prefixes, ("mcp.",))
        self.assertTrue(policy.pre_execution_narration_default)

    def test_reading_session_is_conservative(self) -> None:
        manager = ReadingSessionManager(
            ReadingSessionConfig(
                memory_enabled=True,
                max_scroll_steps=3,
                max_quotes=3,
                max_quote_chars=300,
                trusted_window_titles=["chrome"],
            )
        )
        policy = ReadingSessionAdapter(manager).tool_policy()
        self.assertFalse(policy.native_tool_calls_enabled)
        self.assertEqual(policy.allowed_tool_prefixes, ())
        self.assertFalse(policy.pre_execution_narration_default)

    def test_chat_session_policy_can_be_configured(self) -> None:
        policy = ChatSession(
            policy=SessionToolPolicy(
                native_tool_calls_enabled=True,
                allowed_tool_prefixes=("mcp.",),
                pre_execution_narration_default=False,
            )
        ).tool_policy()
        self.assertTrue(policy.native_tool_calls_enabled)
        self.assertEqual(policy.allowed_tool_prefixes, ("mcp.",))
        self.assertFalse(policy.pre_execution_narration_default)

    def test_reading_session_policy_can_be_configured(self) -> None:
        manager = ReadingSessionManager(
            ReadingSessionConfig(
                memory_enabled=True,
                max_scroll_steps=3,
                max_quotes=3,
                max_quote_chars=300,
                trusted_window_titles=["chrome"],
            )
        )
        policy = ReadingSessionAdapter(
            manager,
            policy=SessionToolPolicy(
                native_tool_calls_enabled=True,
                allowed_tool_prefixes=("mcp.", "tool."),
                pre_execution_narration_default=False,
            ),
        ).tool_policy()
        self.assertTrue(policy.native_tool_calls_enabled)
        self.assertEqual(policy.allowed_tool_prefixes, ("mcp.", "tool."))
        self.assertFalse(policy.pre_execution_narration_default)


if __name__ == "__main__":
    unittest.main()
