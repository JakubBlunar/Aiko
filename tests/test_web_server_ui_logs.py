"""Tests for the ``POST /api/logs/ui`` debug-log bridge.

The browser POSTs batched entries here when the
``logging.ui_log_enabled`` toggle is on, and the handler hands each one
to :func:`crash_logging.log_ui_event` which renders an
``INFO [ui] {source} {kind} …`` line into ``data/app.log``. The endpoint
also enforces a category allow-list and a batch cap so a misbehaving
client cannot smother the rotating log.

What we cover here:

  - Disabled toggle returns ``403`` (no events accepted).
  - Enabled toggle accepts a well-formed batch and emits one line per
    entry on the ``app.ui`` logger.
  - Entries whose ``source`` is outside the allow-list are dropped.
  - Entries beyond ``ui_log_max_batch`` are dropped (counted as
    ``dropped`` in the response).
  - Oversized ``payload`` is truncated to a tiny replacement marker so
    the log file stays bounded.
"""
from __future__ import annotations

import json
import logging
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


@dataclass
class _AgentBlock:
    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 240.0
    proactive_cooldown_seconds_typed: float = 600.0
    activity_awareness_enabled: bool = False


@dataclass
class _ToolsBlock:
    enabled: bool = True
    get_time: bool = True
    recall: bool = True
    web_search: bool = True
    world: bool = True


@dataclass
class _EndpointingBlock:
    enabled: bool = True
    use_partial_transcript: bool = True
    phrase_silence_seconds: float = 1.0
    turn_silence_seconds: float = 3.0
    fast_close_silence_seconds: float = 0.6
    hesitation_extend_to_turn: bool = True
    barge_in_min_speech_seconds: float = 0.7


@dataclass
class _OllamaBlock:
    temperature: float = 0.6


@dataclass
class _ChatLlmBlock:
    max_tokens: int = 512


@dataclass
class _SttBlock:
    language: str | None = None


@dataclass
class _TtsBlock:
    enabled: bool = True


@dataclass
class _AudioBlock:
    pass


@dataclass
class _LoggingBlock:
    ui_log_enabled: bool = False
    ui_log_categories: list[str] = field(
        default_factory=lambda: ["ws", "channel", "settings", "voice"],
    )
    ui_log_max_batch: int = 50
    ui_log_max_payload_bytes: int = 2048


@dataclass
class _SettingsStub:
    agent: _AgentBlock = field(default_factory=_AgentBlock)
    tools: _ToolsBlock = field(default_factory=_ToolsBlock)
    endpointing: _EndpointingBlock = field(default_factory=_EndpointingBlock)
    ollama: _OllamaBlock = field(default_factory=_OllamaBlock)
    chat_llm: _ChatLlmBlock = field(default_factory=_ChatLlmBlock)
    stt: _SttBlock = field(default_factory=_SttBlock)
    tts: _TtsBlock = field(default_factory=_TtsBlock)
    audio: _AudioBlock = field(default_factory=_AudioBlock)
    logging: _LoggingBlock = field(default_factory=_LoggingBlock)


def _build_client(*, ui_log_enabled: bool = True) -> tuple[
    TestClient, MagicMock, _SettingsStub,
]:
    settings = _SettingsStub()
    settings.logging.ui_log_enabled = ui_log_enabled
    session = MagicMock()
    session._settings = settings
    session.session_key = "u:s"
    session.effective_chat_model = "test-model"
    session.context_window_size = 8192
    session.context_window_source = "fallback"
    session.tts_provider = "fake"
    session.tts_voice = "fake"
    session.stt_model = "fake"
    session.vad_level_threshold = 0.02
    session.vad_silence_seconds = 1.0
    session.barge_in_enabled.return_value = False
    session.available_tool_names.return_value = []
    app = create_web_app(session)
    return TestClient(app), session, settings


class UiLogCapture(logging.Handler):
    """Capture every ``INFO [ui] …`` line for assertions."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


class PostUiLogsTests(unittest.TestCase):
    def setUp(self) -> None:
        # ``crash_logging.log_ui_event`` emits on ``app.ui``. Attach a
        # capture handler so the assertions can inspect what landed in
        # the rotating-log stream without writing to disk.
        self.capture = UiLogCapture()
        self.ui_logger = logging.getLogger("app.ui")
        self._prev_level = self.ui_logger.level
        self._prev_propagate = self.ui_logger.propagate
        self.ui_logger.setLevel(logging.DEBUG)
        self.ui_logger.propagate = False
        self.ui_logger.addHandler(self.capture)

    def tearDown(self) -> None:
        self.ui_logger.removeHandler(self.capture)
        self.ui_logger.setLevel(self._prev_level)
        self.ui_logger.propagate = self._prev_propagate

    def test_disabled_returns_403(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=False)
        response = client.post(
            "/api/logs/ui",
            json={"entries": [{"ts": "x", "source": "ws", "kind": "hello"}]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.capture.records, [])

    def test_enabled_accepts_and_emits_one_line_per_entry(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=True)
        response = client.post(
            "/api/logs/ui",
            json={
                "entries": [
                    {"ts": "t0", "source": "ws", "kind": "hello", "payload": {"a": 1}},
                    {
                        "ts": "t1",
                        "source": "channel.expression",
                        "kind": "applyReaction",
                        "payload": {"reaction": "cheerful"},
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["accepted"], 2)
        self.assertEqual(body["dropped"], 0)
        messages = self.capture.messages()
        self.assertEqual(len(messages), 2)
        # The shape ``[ui] {source} {kind} {payload_json} ts={ts}`` is the
        # contract debugging scripts will grep for. Lock it in.
        self.assertIn("[ui] ws hello", messages[0])
        self.assertIn('{"a": 1}', messages[0])
        self.assertIn("[ui] channel.expression applyReaction", messages[1])
        self.assertIn('"reaction": "cheerful"', messages[1])

    def test_source_outside_allowlist_is_dropped(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=True)
        response = client.post(
            "/api/logs/ui",
            json={
                "entries": [
                    {"ts": "t0", "source": "random.thing", "kind": "noop"},
                    {"ts": "t1", "source": "channel.expression", "kind": "ok"},
                ],
            },
        )
        body = response.json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["dropped"], 1)
        self.assertEqual(len(self.capture.records), 1)

    def test_batch_overflow_dropped(self) -> None:
        client, _session, settings = _build_client(ui_log_enabled=True)
        settings.logging.ui_log_max_batch = 3
        response = client.post(
            "/api/logs/ui",
            json={
                "entries": [
                    {"ts": f"t{i}", "source": "ws", "kind": "hello"}
                    for i in range(7)
                ],
            },
        )
        body = response.json()
        # First 3 logged, remaining 4 counted as dropped overflow.
        self.assertEqual(body["accepted"], 3)
        self.assertEqual(body["dropped"], 4)
        self.assertEqual(len(self.capture.records), 3)

    def test_oversized_payload_truncated(self) -> None:
        client, _session, settings = _build_client(ui_log_enabled=True)
        settings.logging.ui_log_max_payload_bytes = 256
        large_blob = "x" * 2048
        response = client.post(
            "/api/logs/ui",
            json={
                "entries": [
                    {
                        "ts": "t0",
                        "source": "ws",
                        "kind": "hello",
                        "payload": {"blob": large_blob},
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.capture.records, self.capture.records)
        message = self.capture.messages()[0]
        # The original blob must NOT appear; instead we see the
        # truncation marker so the file stays bounded.
        self.assertNotIn(large_blob, message)
        self.assertIn('"truncated": true', message)

    def test_missing_required_fields_dropped(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=True)
        response = client.post(
            "/api/logs/ui",
            json={
                "entries": [
                    {"ts": "t0", "source": "ws"},  # no kind
                    {"ts": "t0", "kind": "hello"},  # no source
                    {"ts": "t0", "source": "ws", "kind": "hello"},  # OK
                ],
            },
        )
        body = response.json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["dropped"], 2)


if __name__ == "__main__":
    unittest.main()
