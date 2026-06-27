"""End-to-end tests for the audio-playback owner election.

The desktop shell keeps the persona window's webview alive but hidden
in the background (``hide_persona_window`` in ``src-tauri``). Before this
fix every connected socket received the broadcast TTS / earcon PCM, so
the hidden persona webview AND the visible main window both played each
clip ~tens of ms apart — audible as an echo/mumble on the first sentence
of every turn.

The server now elects a single *audio owner* (preferring a visible
window) and sends binary audio frames only to that socket. These tests
drive the WebSocket endpoint via ``TestClient`` and assert on:

  - ``hello`` carries an ``audio_owner_id``; the first client to connect
    owns audio.
  - A second client (e.g. the hidden persona, which defaults to
    ``visible=False``) does NOT take ownership away from the incumbent.
  - A ``presence`` flip hands ownership to a now-visible sibling.
  - Disconnecting the owner re-elects a remaining client and broadcasts
    the change.

We reuse the ``_FakeLiveSession`` patch + mock-session harness from the
voice-owner tests so the daemon capture threads never spin up.
"""
from __future__ import annotations

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


def _build_client() -> tuple[TestClient, MagicMock]:
    session = _make_session()
    with patch("app.web.server.LiveSession", _FakeLiveSession):
        app = create_web_app(session)
    return TestClient(app), session


def _drain_until(ws, predicate, *, max_messages: int = 50):
    for _ in range(max_messages):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message never arrived")


class AudioOwnerElectionTests(unittest.TestCase):
    def test_hello_includes_audio_owner_and_first_client_owns(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
        self.assertIn("audio_owner_id", hello)
        # The lone client owns audio even before it reports visibility
        # (fallback to first-connected so audio is never lost).
        self.assertEqual(hello["audio_owner_id"], hello["client_id"])

    def test_second_hidden_client_does_not_take_over(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            hello_a = ws_a.receive_json()
            hello_b = ws_b.receive_json()
            # Both default to visible=False (no presence frame yet). The
            # incumbent (A) keeps ownership; B (the hidden persona) does
            # not steal it.
            self.assertEqual(hello_a["audio_owner_id"], hello_a["client_id"])
            self.assertEqual(hello_b["audio_owner_id"], hello_a["client_id"])
            self.assertNotEqual(hello_b["audio_owner_id"], hello_b["client_id"])

    def test_presence_hands_off_to_visible_sibling(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            hello_a = ws_a.receive_json()
            hello_b = ws_b.receive_json()
            # A is the incumbent owner. Both report visible so A stays.
            ws_a.send_json({"type": "presence", "visible": True})
            ws_b.send_json({"type": "presence", "visible": True})
            # Now A goes hidden — ownership must move to the visible B.
            ws_a.send_json({"type": "presence", "visible": False})
            evt_b = _drain_until(
                ws_b,
                lambda m: m.get("type") == "audio_owner_changed"
                and m.get("owner_id") == hello_b["client_id"],
            )
            self.assertEqual(evt_b["owner_id"], hello_b["client_id"])
            self.assertNotEqual(evt_b["owner_id"], hello_a["client_id"])

    def test_disconnect_owner_reelects_remaining_client(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            _hello_a = ws_a.receive_json()
            hello_b = ws_b.receive_json()
            # A owns audio; closing it hands ownership to B.
            ws_a.close()
            evt_b = _drain_until(
                ws_b,
                lambda m: m.get("type") == "audio_owner_changed",
            )
            self.assertEqual(evt_b["owner_id"], hello_b["client_id"])

    def test_most_recently_visible_client_takes_over(self) -> None:
        # "Most-recently-active wins": A is the incumbent, but once B
        # becomes visible *after* A, audio follows to B (the device the
        # user just picked up).
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            _hello_a = ws_a.receive_json()
            hello_b = ws_b.receive_json()
            ws_a.send_json({"type": "presence", "visible": True})
            ws_b.send_json({"type": "presence", "visible": True})
            evt_b = _drain_until(
                ws_b,
                lambda m: m.get("type") == "audio_owner_changed"
                and m.get("owner_id") == hello_b["client_id"],
            )
            self.assertEqual(evt_b["owner_id"], hello_b["client_id"])


class AudioMuteTests(unittest.TestCase):
    def test_muting_owner_hands_off_then_unmute_reclaims(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws_a, \
                client.websocket_connect("/ws") as ws_b:
            hello_a = ws_a.receive_json()
            hello_b = ws_b.receive_json()
            # Both visible; B becomes active last so B owns audio.
            ws_a.send_json({"type": "presence", "visible": True})
            ws_b.send_json({"type": "presence", "visible": True})
            _drain_until(
                ws_b,
                lambda m: m.get("type") == "audio_owner_changed"
                and m.get("owner_id") == hello_b["client_id"],
            )
            # Mute the owner (B) -> the visible, unmuted sibling A takes over.
            ws_b.send_json({"type": "audio_mute", "muted": True})
            evt = _drain_until(
                ws_a,
                lambda m: m.get("type") == "audio_owner_changed"
                and m.get("owner_id") == hello_a["client_id"],
            )
            self.assertEqual(evt["owner_id"], hello_a["client_id"])
            # Unmute B -> it is stamped active again and reclaims playback.
            ws_b.send_json({"type": "audio_mute", "muted": False})
            evt2 = _drain_until(
                ws_b,
                lambda m: m.get("type") == "audio_owner_changed"
                and m.get("owner_id") == hello_b["client_id"],
            )
            self.assertEqual(evt2["owner_id"], hello_b["client_id"])

    def test_muting_only_client_silences_everywhere(self) -> None:
        client, _ = _build_client()
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            self.assertEqual(hello["audio_owner_id"], hello["client_id"])
            ws.send_json({"type": "audio_mute", "muted": True})
            # No eligible (unmuted) client -> silence everywhere. Drain
            # past any connect-time owner event to the None re-election.
            evt = _drain_until(
                ws,
                lambda m: m.get("type") == "audio_owner_changed"
                and m.get("owner_id") is None,
            )
            self.assertIsNone(evt["owner_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
