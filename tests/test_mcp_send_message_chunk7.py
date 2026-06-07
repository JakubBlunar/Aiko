"""Chunk 7 verification — MCP ``send_message`` swaps onto the brain queue.

Before chunk 7 the tool body was::

    response = session.chat_once(message)

which called :meth:`SessionController.chat_once_streaming` directly on
the FastMCP request thread. After chunk 7 the tool routes through
:meth:`SessionController.enqueue_user_message` which puts a
:class:`UserMessageEvent` on the brain queue and blocks on a
:class:`concurrent.futures.Future` for the reply. This exercises the
full queue-driven path end-to-end from the producer side.

These tests use a session stub that records the queue call so we can
verify both the contract (right kwargs, ``mode="mcp"``, ``skip_tts``
threaded through) and the reply round-trip (the future-resolved string
makes it back to the tool's return value via ``FastMCP.call_tool``).
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any

from app.mcp.server import create_mcp_server


class _FakeSettingsTts:
    def __init__(self) -> None:
        self.enabled = True


class _FakeSettings:
    def __init__(self) -> None:
        self.tts = _FakeSettingsTts()


class _FakeSession:
    """Minimal :class:`SessionController` stand-in for MCP tool tests.

    Records every call to :meth:`enqueue_user_message` and
    :meth:`_notify_message`. Returns a configurable reply (the
    "future-resolved" string) so the MCP tool's return value can be
    asserted by the caller.
    """

    def __init__(self) -> None:
        self._settings = _FakeSettings()
        self._notify_calls: list[tuple[str, str]] = []
        self._enqueue_calls: list[dict[str, Any]] = []
        self._chat_once_calls: list[str] = []
        self.enqueue_reply: str = "hi from queue"
        self.enqueue_raise: Exception | None = None

    def _notify_message(self, who: str, text: str) -> None:
        self._notify_calls.append((who, text))

    def enqueue_user_message(
        self,
        *,
        text: str,
        mode: str = "mcp",
        skip_tts: bool = False,
        wait_for_reply: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        self._enqueue_calls.append(
            {
                "text": text,
                "mode": mode,
                "skip_tts": skip_tts,
                "wait_for_reply": wait_for_reply,
                "timeout": timeout,
            }
        )
        if self.enqueue_raise is not None:
            raise self.enqueue_raise
        return self.enqueue_reply if wait_for_reply else None

    # Legacy direct path. Should NOT be called after chunk 7 because
    # the MCP tool routes through ``enqueue_user_message``. We keep it
    # here so a regression where the swap is reverted gets caught
    # loudly by ``test_legacy_chat_once_is_not_called``.
    def chat_once(self, message: str) -> str:
        self._chat_once_calls.append(message)
        return "should-not-be-called"


def _call_tool(server: Any, name: str, args: dict[str, Any]) -> Any:
    """Sync wrapper around :meth:`FastMCP.call_tool` for unit tests."""
    blocks, structured = asyncio.run(server.call_tool(name, args))
    if structured and "result" in structured:
        return structured["result"]
    if blocks:
        return getattr(blocks[0], "text", None)
    return None


class McpSendMessageChunk7Tests(unittest.TestCase):
    """The MCP ``send_message`` tool now routes through the brain queue."""

    def test_calls_enqueue_user_message_not_chat_once(self) -> None:
        session = _FakeSession()
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        reply = _call_tool(server, "send_message", {"message": "hello there"})
        self.assertEqual(reply, "hi from queue")
        self.assertEqual(len(session._enqueue_calls), 1)
        # Legacy path must not be touched.
        self.assertEqual(session._chat_once_calls, [])

    def test_enqueue_kwargs_pin_the_contract(self) -> None:
        session = _FakeSession()
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        _call_tool(
            server, "send_message", {"message": "ping?", "skip_tts": True}
        )
        call = session._enqueue_calls[0]
        self.assertEqual(call["text"], "ping?")
        self.assertEqual(call["mode"], "mcp")
        self.assertTrue(call["skip_tts"])
        self.assertTrue(call["wait_for_reply"])
        # Timeout has to be a positive float so the producer doesn't
        # block forever on a stalled handler. The mixin defaults to
        # 120s and the MCP tool inherits or pins that value.
        self.assertIsInstance(call["timeout"], float)
        self.assertGreater(call["timeout"], 0.0)

    def test_skip_tts_defaults_to_false(self) -> None:
        session = _FakeSession()
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        _call_tool(server, "send_message", {"message": "hi"})
        self.assertFalse(session._enqueue_calls[0]["skip_tts"])

    def test_notify_message_called_for_both_sides(self) -> None:
        session = _FakeSession()
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        _call_tool(server, "send_message", {"message": "hello"})
        kinds = [who for who, _ in session._notify_calls]
        # One pre-call (You) and one post-call (Assistant). The
        # MCP tool preserves the legacy hub-notify contract.
        self.assertIn("You (MCP)", kinds)
        self.assertIn("Assistant", kinds)
        # Assistant payload is the queue reply, not "should-not-be-called".
        assistant_msgs = [
            text for who, text in session._notify_calls if who == "Assistant"
        ]
        self.assertEqual(assistant_msgs, ["hi from queue"])

    def test_empty_reply_falls_back_to_placeholder(self) -> None:
        session = _FakeSession()
        session.enqueue_reply = ""
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        reply = _call_tool(server, "send_message", {"message": "hi"})
        # Tool surfaces "(empty response)" to the MCP caller so debug
        # clients never see an empty string and assume failure.
        self.assertEqual(reply, "(empty response)")


if __name__ == "__main__":
    unittest.main()
