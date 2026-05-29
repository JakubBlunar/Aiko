"""End-to-end tests for the voice-owner lock + binary frame routing.

We boot a stripped FastAPI app with a real ``_Hub`` plus a mock
:class:`SessionController` so we can drive the WebSocket endpoint via
``TestClient`` and assert on:

  - ``hello`` now carries a per-connection ``client_id`` plus the
    current ``voice_owner_id``.
  - ``voice_start`` claims the lock and broadcasts a
    ``voice_owner_changed`` event to *both* clients.
  - A second client's ``voice_start`` takes over (latest-claim-wins).
  - ``voice_stop`` releases the lock and re-broadcasts the change.
  - Disconnect releases the lock.
  - Binary ``mic_pcm`` frames from a non-owner are dropped; from the
    owner they reach ``feed_audio_frame``.

We patch :class:`LiveSession` so ``start``/``stop`` are no-ops; the
hub's voice ownership is independent of the live-capture worker
thread and we don't want the daemon threads spinning against a
MagicMock session.
"""
from __future__ import annotations

import struct
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.web import audio_frames as frames
from app.web.server import create_web_app


class _FakeLiveSession:
    """Drop-in stand-in for :class:`LiveSession` in tests.

    The real LiveSession spawns capture / processing threads against
    the controller; against a MagicMock that's a quick route to
    flaky tests. We only need ``is_active`` / ``start`` / ``stop``
    on the surface for the WS endpoint.
    """

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
    """Build a SessionController-compatible mock for the WS endpoint."""
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


def _build_client() -> tuple[TestClient, MagicMock]:
    session = _make_session()
    with patch("app.web.server.LiveSession", _FakeLiveSession):
        app = create_web_app(session)
    return TestClient(app), session


def _drain_until(ws, predicate, *, max_messages: int = 50):
    """Read JSON frames until ``predicate`` matches or we run out."""
    for _ in range(max_messages):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message never arrived")


class VoiceOwnerLockTests(unittest.TestCase):
    def test_hello_includes_client_id_and_voice_owner_id(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
        self.assertEqual(hello["type"], "hello")
        self.assertIn("client_id", hello)
        self.assertTrue(isinstance(hello["client_id"], str))
        self.assertEqual(len(hello["client_id"]), 32)
        # Fresh server: nobody owns the mic yet.
        self.assertIsNone(hello["voice_owner_id"])

    def test_voice_start_broadcasts_owner_change(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            hello_a = ws_a.receive_json()
            _hello_b = ws_b.receive_json()
            ws_a.send_json({"type": "voice_start"})
            # Both sockets must see the new owner. The displaced /
            # voice_state events also fire; pull until we see the
            # owner-change.
            owner_evt_a = _drain_until(
                ws_a, lambda m: m.get("type") == "voice_owner_changed",
            )
            owner_evt_b = _drain_until(
                ws_b, lambda m: m.get("type") == "voice_owner_changed",
            )
            self.assertEqual(owner_evt_a["owner_id"], hello_a["client_id"])
            self.assertEqual(owner_evt_b["owner_id"], hello_a["client_id"])

    def test_takeover_swaps_owner(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            hello_a = ws_a.receive_json()
            hello_b = ws_b.receive_json()
            ws_a.send_json({"type": "voice_start"})
            _drain_until(ws_a, lambda m: m.get("type") == "voice_owner_changed")
            _drain_until(ws_b, lambda m: m.get("type") == "voice_owner_changed")
            ws_b.send_json({"type": "voice_start"})
            owner_evt_a = _drain_until(
                ws_a, lambda m: m.get("type") == "voice_owner_changed",
            )
            self.assertEqual(owner_evt_a["owner_id"], hello_b["client_id"])
            self.assertNotEqual(owner_evt_a["owner_id"], hello_a["client_id"])

    def test_voice_stop_releases_owner(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a:
            _hello = ws_a.receive_json()
            ws_a.send_json({"type": "voice_start"})
            _drain_until(ws_a, lambda m: m.get("type") == "voice_owner_changed")
            ws_a.send_json({"type": "voice_stop"})
            release = _drain_until(
                ws_a, lambda m: m.get("type") == "voice_owner_changed",
            )
            self.assertIsNone(release["owner_id"])

    def test_disconnect_releases_owner_and_notifies_others(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            _hello_a = ws_a.receive_json()
            _hello_b = ws_b.receive_json()
            ws_a.send_json({"type": "voice_start"})
            _drain_until(ws_a, lambda m: m.get("type") == "voice_owner_changed")
            _drain_until(ws_b, lambda m: m.get("type") == "voice_owner_changed")
            # Close A; B should see owner_id flip back to None.
            ws_a.close()
            release_b = _drain_until(
                ws_b, lambda m: m.get("type") == "voice_owner_changed",
            )
            self.assertIsNone(release_b["owner_id"])


class BinaryFrameRoutingTests(unittest.TestCase):
    """The owner's mic frames flow through to ``feed_audio_frame``;
    non-owner frames are dropped."""

    def test_owner_mic_pcm_reaches_session(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws_a:
            _hello = ws_a.receive_json()
            ws_a.send_json({"type": "voice_start"})
            _drain_until(ws_a, lambda m: m.get("type") == "voice_owner_changed")
            # Send mic_start (0x02) + mic_pcm (0x01) frames.
            start_payload = struct.pack(">IBB", 48000, 1, 0b111)
            ws_a.send_bytes(bytes([frames.FRAME_MIC_START]) + start_payload)
            ws_a.send_bytes(bytes([frames.FRAME_MIC_PCM]) + b"\x00\x01" * 100)
        # The session mock should have been called for the start and
        # at least one PCM frame.
        session.feed_audio_start.assert_called()
        sample_rate, channels, dsp_flags = session.feed_audio_start.call_args.args
        self.assertEqual(sample_rate, 48000)
        self.assertEqual(channels, 1)
        self.assertEqual(dsp_flags, 0b111)
        session.feed_audio_frame.assert_called()

    def test_non_owner_mic_pcm_is_dropped(self) -> None:
        client, session = _build_client()
        with client.websocket_connect("/ws") as ws_a:
            _hello = ws_a.receive_json()
            # Skip voice_start so this client never owns the mic.
            ws_a.send_bytes(bytes([frames.FRAME_MIC_PCM]) + b"\x00\x01" * 100)
        session.feed_audio_frame.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
