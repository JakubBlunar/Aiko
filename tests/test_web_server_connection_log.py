"""The server's half of the connection-drop diagnostic.

A phone PWA loses its socket often -- the OS suspends the page, or the
link goes quiet long enough for uvicorn's ``ws_ping_timeout`` to hang up.
Either way the cost is the same and it is not obvious: the server sends
TTS audio only to the elected audio owner and never replays it, so every
word spoken while the socket is down is simply gone. The user sees the
reply text appear on reconnect and never hears it.

Until now a drop left almost no trace. ``audio-owner elected: owner=None
clients=0`` implied one had happened, but not how long the socket had
lived, how long it had been silent first, or what code closed it -- which
is exactly what separates "the OS froze the page" from "the ping timer
reaped a dead link".

These tests pin the two lines that close that gap.
"""
from __future__ import annotations

import re
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _FakeLiveSession:
    def __init__(self, *_args, **_kwargs) -> None:
        self._active = False
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def start(self) -> bool:
        with self._lock:
            self._active = True
        return True

    def stop(self) -> None:
        with self._lock:
            self._active = False


def _make_session() -> MagicMock:
    session = MagicMock()
    settings = MagicMock()
    settings.tts.enabled = True
    settings.assistant.user_display_name = "Tester"
    session._settings = settings
    session.session_key = "u:s"
    session.effective_chat_model = "test-model"
    session.context_window_size = 8192
    session.context_window_source = "fallback"
    session.avatar_payload.return_value = {}
    session.needs_onboarding = False
    return session


def _build_client() -> TestClient:
    with patch("app.web.server.LiveSession", _FakeLiveSession):
        app = create_web_app(_make_session())
    return TestClient(app)


def _hello(ws) -> dict:
    for _ in range(50):
        msg = ws.receive_json()
        if msg.get("type") == "hello":
            return msg
    raise AssertionError("no hello frame arrived")


def _find(records, needle: str) -> str:
    for record in records:
        line = record.getMessage()
        if needle in line:
            return line
    raise AssertionError(f"no log line containing {needle!r} in {records}")


class ConnectionLogTests(unittest.TestCase):
    def test_a_connect_names_the_client_and_the_headcount(self) -> None:
        client = _build_client()
        with self.assertLogs("app.web.server", level="INFO") as captured:
            with client.websocket_connect("/ws") as ws:
                hello = _hello(ws)
        line = _find(captured.records, "ws client connected")
        # The id is truncated in the log, so a reader can still pair the
        # connect with its disconnect without a 32-char hex wall.
        self.assertIn(hello["client_id"][:8], line)
        self.assertIn("clients=1", line)

    def test_a_disconnect_reports_lifetime_silence_and_headcount(self) -> None:
        client = _build_client()
        with self.assertLogs("app.web.server", level="INFO") as captured:
            with client.websocket_connect("/ws") as ws:
                hello = _hello(ws)
        line = _find(captured.records, "ws client gone")
        self.assertIn(hello["client_id"][:8], line)
        # Nobody is left, which is the state in which speech is lost.
        self.assertIn("remaining=0", line)
        self.assertRegex(line, r"up=\d+\.\d+s")
        self.assertRegex(line, r"silent=\d+\.\d+s")

    def test_the_silence_clock_restarts_on_every_inbound_frame(self) -> None:
        # The load-bearing field. A socket reaped by ``ws_ping_timeout``
        # has been silent for the length of that timeout; one closed by
        # the peer has usually just been heard from. Without this reset
        # the field would only ever measure connection age.
        client = _build_client()
        with self.assertLogs("app.web.server", level="INFO") as captured:
            with client.websocket_connect("/ws") as ws:
                _hello(ws)
                ws.send_json({"type": "ping"})
                for _ in range(50):
                    if ws.receive_json().get("type") == "pong":
                        break
        line = _find(captured.records, "ws client gone")
        silent = float(re.search(r"silent=(\d+\.\d+)s", line).group(1))
        up = float(re.search(r"up=(\d+\.\d+)s", line).group(1))
        self.assertLessEqual(silent, up)

    def test_a_second_client_leaves_the_first_counted(self) -> None:
        client = _build_client()
        with client.websocket_connect("/ws") as first:
            _hello(first)
            with self.assertLogs("app.web.server", level="INFO") as captured:
                with client.websocket_connect("/ws") as second:
                    _hello(second)
            self.assertIn("clients=2", _find(captured.records, "connected"))
            # The survivor still holds the socket, so speech is not lost.
            self.assertIn("remaining=1", _find(captured.records, "gone"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
