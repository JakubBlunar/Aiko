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

from app.web.server import _Hub, create_web_app


def _build_client() -> tuple[TestClient, MagicMock]:
    session = MagicMock()
    session.session_key = "u:s"
    session.effective_chat_model = "test-model"
    session.context_window_size = 8192
    session.context_window_source = "fallback"
    session.avatar_payload.return_value = {}
    # The hello frame reads ``_settings.tts.enabled``. MagicMock
    # bools-coerce truthy by default; setting it explicitly keeps the
    # JSON snapshot deterministic.
    session._settings.tts.enabled = True
    return TestClient(create_web_app(session)), session


class PresenceWsCommandTests(unittest.TestCase):
    # ``assert_any_call`` rather than ``assert_called_with`` because the
    # disconnect-cleanup path now also folds presence (one extra call
    # with ``False`` once the only client drops). We only care that the
    # frame-driven update fired with the right value.
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
        session.set_user_present.assert_any_call(True)

    def test_presence_visible_false(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "presence", "visible": False}))
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_present.assert_any_call(False)

    def test_presence_missing_field_defaults_true(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "presence"}))
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_present.assert_any_call(True)


class HubPresenceFoldTests(unittest.TestCase):
    """Per-client presence fold lives in :class:`_Hub`.

    The hub stores the latest ``presence`` frame per ``client_id`` and
    OR-folds the dict via :meth:`any_client_visible`. The WS layer
    feeds ``set_user_present(any_client_visible())`` so a multi-window
    session reports "present" iff *at least one* window is visible --
    which the original single-flag setup couldn't express.

    These tests poke the hub directly so we can assert the fold without
    needing the FastAPI TestClient's threading dance.
    """

    def _add(self, hub: _Hub, client_id: str) -> object:
        """Register a stub WS object with ``client_id``. The hub keeps
        a ``WebSocket -> client_id`` mapping but never calls anything on
        the socket itself in the presence path, so a bare ``object()``
        is enough."""
        ws = object()
        hub.add(ws, client_id)  # type: ignore[arg-type]
        return ws

    def test_empty_hub_reports_not_visible(self) -> None:
        # No connections -> no presence. Originally the boot default
        # was ``True`` which meant a backend with zero clients still
        # let the proactive timer fire; the fold flips that.
        hub = _Hub()
        self.assertFalse(hub.any_client_visible())

    def test_single_client_presence_round_trip(self) -> None:
        hub = _Hub()
        self._add(hub, "client-a")
        # Default after add is ``False`` so the hub never reports a
        # client as visible until it has actually claimed presence.
        self.assertFalse(hub.any_client_visible())
        hub.set_client_presence("client-a", True)
        self.assertTrue(hub.any_client_visible())
        hub.set_client_presence("client-a", False)
        self.assertFalse(hub.any_client_visible())

    def test_two_clients_or_fold(self) -> None:
        hub = _Hub()
        self._add(hub, "client-main")
        self._add(hub, "client-persona")
        hub.set_client_presence("client-main", True)
        hub.set_client_presence("client-persona", False)
        self.assertTrue(
            hub.any_client_visible(),
            "main visible + persona hidden -> still 'present'",
        )
        hub.set_client_presence("client-main", False)
        self.assertFalse(
            hub.any_client_visible(),
            "both hidden -> 'away'",
        )
        hub.set_client_presence("client-persona", True)
        self.assertTrue(
            hub.any_client_visible(),
            "persona alone visible flips fold back to 'present'",
        )

    def test_disconnect_drops_client_from_fold(self) -> None:
        # Closing the only-visible client must flip the fold even if
        # that client never sent a final ``presence:false`` frame.
        # That's the path that catches "user closed the last window
        # before the debounce flushed".
        hub = _Hub()
        ws_main = self._add(hub, "client-main")
        ws_persona = self._add(hub, "client-persona")
        hub.set_client_presence("client-main", True)
        hub.set_client_presence("client-persona", False)
        self.assertTrue(hub.any_client_visible())
        hub.discard(ws_main)  # type: ignore[arg-type]
        self.assertFalse(
            hub.any_client_visible(),
            "main disconnected -> only the hidden persona remains",
        )
        hub.discard(ws_persona)  # type: ignore[arg-type]
        self.assertFalse(hub.any_client_visible())

    def test_set_unknown_client_is_noop(self) -> None:
        # A presence frame can race a disconnect (the WS handler
        # reads the frame then notices the socket is gone); the hub
        # silently drops writes for unknown ids so a stale entry
        # can't sneak back in.
        hub = _Hub()
        hub.set_client_presence("ghost", True)
        self.assertFalse(hub.any_client_visible())

    def test_ws_presence_frame_propagates_folded_value(self) -> None:
        # Integration sanity: a single ``presence:false`` frame from a
        # one-client session should call ``set_user_present(False)``
        # because the fold over a single hidden client is False. The
        # disconnect-cleanup at end of context also calls
        # ``set_user_present(False)`` (empty hub -> False).
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # drain hello
            ws.send_text(json.dumps({"type": "presence", "visible": False}))
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
        session.set_user_present.assert_any_call(False)


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
