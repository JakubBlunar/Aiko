from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.sessions.agentic_session import AgenticSessionConfig, AgenticSessionManager
from app.core.sessions.agentic_session_adapter import AgenticSessionAdapter
from app.core.sessions.chat_session import ChatSession
from app.core.sessions.session_types import SessionNativeToolFlowContext
from app.llm.ollama_client import OllamaChatResponse, OllamaToolCall


class AgenticNativeToolFlowTests(unittest.TestCase):
    def test_chat_session_native_flow_is_noop(self) -> None:
        chat = ChatSession()
        result = chat.run_native_tool_flow(
            messages=[],
            generation_options={},
            tools=[],
            flow_context=SessionNativeToolFlowContext(trace=lambda *_: None),
        )
        self.assertFalse(result.handled)

    def test_agentic_native_flow_executes_and_returns_second_pass(self) -> None:
        manager = AgenticSessionManager(AgenticSessionConfig(enabled=True, max_auto_steps=3))
        adapter = AgenticSessionAdapter(manager)

        calls = [
            OllamaChatResponse(
                content="",
                tool_calls=[
                    OllamaToolCall(
                        name="mcp.greet",
                        arguments={"name": "John"},
                        call_id="call_1",
                    )
                ],
            ),
            OllamaChatResponse(
                content="Hello, John!",
                tool_calls=[],
            ),
        ]
        chat_invocations: list[dict[str, object]] = []

        def fake_chat_with_tools(messages, options=None, tools=None):
            chat_invocations.append(
                {
                    "messages_len": len(messages),
                    "has_tools": bool(tools),
                    "options": dict(options or {}),
                }
            )
            return calls.pop(0)

        invoked_tools: list[tuple[str, dict[str, object]]] = []

        def fake_invoke_tool(name: str, *, args=None, cancel_token=None):
            _ = cancel_token
            invoked_tools.append((name, dict(args or {})))
            return SimpleNamespace(
                success=True,
                data={"text": "Hello, John! Welcome!"},
                error=None,
                requires_confirmation=False,
            )

        emitted_tokens: list[str] = []
        spoken: list[str] = []

        result = adapter.run_native_tool_flow(
            messages=[{"role": "user", "content": "Greet John"}],
            generation_options={"num_predict": 128},
            tools=[{"type": "function", "function": {"name": "mcp.greet"}}],
            flow_context=SessionNativeToolFlowContext(
                trace=lambda *_: None,
                chat_with_tools=fake_chat_with_tools,
                on_token=emitted_tokens.append,
                stop_requested=lambda: False,
                narration_enabled=True,
                speak_text=lambda text: (spoken.append(text) or True),
                build_pre_execution_summary=lambda tool_calls: "I will greet John now.",
                invoke_tool=fake_invoke_tool,
                tool_result_to_message_content=lambda _name, result_obj: str(result_obj.data.get("text", "")),
                sanitize_text=lambda text: str(text or "").strip(),
            ),
        )

        self.assertTrue(result.handled)
        self.assertTrue(result.tool_calls_executed)
        self.assertEqual(result.response, "Hello, John!")
        self.assertEqual(len(chat_invocations), 2)
        self.assertEqual(invoked_tools, [("mcp.greet", {"name": "John"})])
        self.assertTrue(any("I will greet John now." in item for item in emitted_tokens))
        self.assertEqual(spoken, ["I will greet John now."])


if __name__ == "__main__":
    unittest.main()
