"""Tests for the WS commands ``presence`` and ``user_activity``.

We use the FastAPI websocket TestClient with a MagicMock-backed
SessionController. The handler routes the two commands directly to
``session.set_user_present`` / ``session.set_user_active_app``; we
just need to verify the wiring (string parsing, default values,
gentle ignoring of malformed payloads).
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


def _build_client() -> tuple[TestClient, MagicMock]:
    session = MagicMock()
    session.session_key = "u:s"
    session.effective_chat_model = "test-model"
    session.context_window_size = 8192
    session.context_window_source = "fallback"
    session.avatar_payload.return_value = {}
    session.desktop_settings.return_value = {}
    # The hello frame reads ``_settings.tts.enabled``. MagicMock
    # bools-coerce truthy by default; setting it explicitly keeps the
    # JSON snapshot deterministic.
    session._settings.tts.enabled = True
    return TestClient(create_web_app(session)), session


class PresenceWsCommandTests(unittest.TestCase):
    def test_presence_visible_true(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            # Drain the hello frame so the next receive lines up.
            _hello = ws.receive_text()
            ws.send_text(json.dumps({"type": "presence", "visible": True}))
            ws.send_text(json.dumps({"type": "ping"}))
            self.assertEqual(
                json.loads(ws.receive_text()), {"type": "pong"},
            )
        session.set_user_present.assert_called_with(True)

    def test_presence_visible_false(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "presence", "visible": False}))
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_present.assert_called_with(False)

    def test_presence_missing_field_defaults_true(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "presence"}))
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_present.assert_called_with(True)


class UserActivityWsCommandTests(unittest.TestCase):
    def test_user_activity_with_app(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(
                json.dumps({"type": "user_activity", "app": "Code"}),
            )
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_active_app.assert_called_with("Code")

    def test_user_activity_null_app(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(
                json.dumps({"type": "user_activity", "app": None}),
            )
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_active_app.assert_called_with(None)

    def test_user_activity_missing_field_treated_as_null(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "user_activity"}))
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_active_app.assert_called_with(None)


if __name__ == "__main__":
    unittest.main()
