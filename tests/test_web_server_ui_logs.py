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
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.core.infra import crash_logging
from app.web.server import create_web_app
from web_fake_session import FakeSession


@dataclass
class _AgentBlock:
    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 240.0
    proactive_cooldown_seconds_typed: float = 600.0
    proactive_typed_when_away: bool = False
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
    session = FakeSession()
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


class PostUiCrashTests(unittest.TestCase):
    """``POST /api/logs/ui-crash`` (I8 React error boundary).

    Unlike the debug bridge above, this endpoint is *always on* — a
    white-screen crash must be captured even when ``ui_log_enabled`` is
    off — and it logs at ERROR on the ``app.ui`` logger.
    """

    def setUp(self) -> None:
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

    def test_logs_even_when_ui_logging_disabled(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=False)
        response = client.post(
            "/api/logs/ui-crash",
            json={
                "message": "Cannot read properties of undefined (reading 'foo')",
                "stack": "TypeError: ...\n  at Live2DAvatar (...)",
                "componentStack": "\n    in Live2DAvatar\n    in App",
                "source": "render",
                "url": "http://localhost:5173/",
                "userAgent": "vitest",
                "ts": "2026-06-28T18:00:00.000Z",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["logged"])
        self.assertEqual(len(self.capture.records), 1)
        record = self.capture.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        message = record.getMessage()
        self.assertIn("[ui] crash", message)
        self.assertIn("source=render", message)
        self.assertIn("Cannot read properties of undefined", message)

    def test_caps_oversized_fields(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=False)
        huge = "x" * 50_000
        response = client.post(
            "/api/logs/ui-crash",
            json={"message": "boom", "stack": huge, "source": "render"},
        )
        self.assertEqual(response.status_code, 200)
        message = self.capture.records[0].getMessage()
        # The raw 50k blob must not survive verbatim; the clip marker does.
        self.assertNotIn(huge, message)
        self.assertIn("more)", message)

    def test_blank_report_still_logs_a_line(self) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=False)
        response = client.post("/api/logs/ui-crash", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["logged"])
        self.assertIn("(no message)", self.capture.records[0].getMessage())


class CrashDiagnosticsTests(unittest.TestCase):
    """The parts of a crash report that make it *actionable*.

    A message and a stack rarely identify a cause on their own. These
    cover the two additions that usually do — the breadcrumb trail and
    the context snapshot — plus the requirement that a client which
    doesn't send them (or sends junk) still gets its crash logged.
    """

    def setUp(self) -> None:
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

    def _post(self, payload: dict) -> None:
        client, _session, _settings = _build_client(ui_log_enabled=False)
        response = client.post("/api/logs/ui-crash", json=payload)
        self.assertEqual(response.status_code, 200)

    def test_breadcrumbs_reach_the_log_in_order(self) -> None:
        self._post({
            "message": "boom",
            "source": "render",
            "breadcrumbs": [
                {"t": 100, "cat": "ws", "msg": "open"},
                {"t": 8200, "cat": "console", "msg": "error: hook order changed"},
                {"t": 8300, "cat": "api", "msg": "GET /api/concepts → 500"},
            ],
        })
        message = self.capture.records[0].getMessage()
        self.assertIn("breadcrumbs:", message)
        # Order is the whole point of a trail — the socket opening long
        # before the failure reads very differently from just after it.
        first = message.index("[ws] open")
        second = message.index("hook order changed")
        third = message.index("/api/concepts")
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertIn("+   8200ms", message)

    def test_a_repeated_breadcrumb_shows_its_count(self) -> None:
        self._post({
            "message": "boom",
            "source": "render",
            "breadcrumbs": [{"t": 5, "cat": "ws", "msg": "error", "count": 42}],
        })
        self.assertIn("x42", self.capture.records[0].getMessage())

    def test_context_is_rendered_as_key_values(self) -> None:
        self._post({
            "message": "boom",
            "source": "render",
            "context": {"build": "abc123", "voiceMode": "listening", "heapPct": "94"},
        })
        message = self.capture.records[0].getMessage()
        self.assertIn("build=abc123", message)
        self.assertIn("voiceMode=listening", message)
        self.assertIn("heapPct=94", message)

    def test_the_breadcrumb_trail_is_capped(self) -> None:
        # The endpoint is unauthenticated by design, so a client can post
        # anything; the trail must not become an unbounded log write.
        self._post({
            "message": "boom",
            "source": "render",
            "breadcrumbs": [
                {"t": i, "cat": "spam", "msg": f"crumb-{i}"} for i in range(500)
            ],
        })
        message = self.capture.records[0].getMessage()
        self.assertIn("crumb-0", message)
        self.assertNotIn("crumb-400", message)

    def test_malformed_breadcrumbs_do_not_break_the_report(self) -> None:
        self._post({
            "message": "boom",
            "source": "render",
            # A string where a list belongs, and non-dict entries inside.
            "breadcrumbs": ["not-a-dict", 7, None, {"cat": "ok", "msg": "kept"}],
        })
        message = self.capture.records[0].getMessage()
        self.assertIn("boom", message)
        self.assertIn("kept", message)

    def test_a_non_list_breadcrumbs_field_is_ignored(self) -> None:
        self._post({"message": "boom", "source": "render", "breadcrumbs": "nope"})
        self.assertIn("boom", self.capture.records[0].getMessage())

    def test_a_non_dict_context_is_ignored(self) -> None:
        self._post({"message": "boom", "source": "render", "context": ["a", "b"]})
        self.assertIn("boom", self.capture.records[0].getMessage())

    def test_an_oversized_breadcrumb_detail_is_clipped(self) -> None:
        huge = "z" * 40_000
        self._post({
            "message": "boom",
            "source": "render",
            "breadcrumbs": [{"t": 1, "cat": "api", "msg": "resp", "detail": huge}],
        })
        message = self.capture.records[0].getMessage()
        self.assertNotIn(huge, message)
        self.assertIn("more)", message)

    def test_a_report_without_the_new_fields_still_logs(self) -> None:
        # An older client (or a cached bundle) sends the original shape.
        self._post({"message": "legacy", "source": "render", "stack": "at x"})
        message = self.capture.records[0].getMessage()
        self.assertIn("legacy", message)
        self.assertIn("breadcrumbs: -", message)
        self.assertIn("context: -", message)


class ReadUiCrashesTests(unittest.TestCase):
    """``read_ui_crashes`` — the read-back behind the MCP tool.

    ``crashlog.txt`` is append-only JSONL with several record types
    interleaved, and ``faulthandler`` writes raw (non-JSON) tracebacks
    into the same file, so the parser has to be tolerant of everything
    that isn't a UI crash.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "crashlog.txt"
        self._prev = crash_logging.CRASH_LOG_PATH
        crash_logging.CRASH_LOG_PATH = self.path

    def tearDown(self) -> None:
        crash_logging.CRASH_LOG_PATH = self._prev
        self._tmp.cleanup()

    def _write(self, *lines: str) -> None:
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_returns_newest_first(self) -> None:
        self._write(
            json.dumps({"type": "ui_crash", "message": "old"}),
            json.dumps({"type": "ui_crash", "message": "new"}),
        )
        crashes = crash_logging.read_ui_crashes(limit=5)
        self.assertEqual([c["message"] for c in crashes], ["new", "old"])

    def test_skips_other_record_types(self) -> None:
        self._write(
            json.dumps({"type": "exception", "message": "backend"}),
            json.dumps({"type": "event", "stage": "error", "message": "evt"}),
            json.dumps({"type": "ui_crash", "message": "ui"}),
        )
        crashes = crash_logging.read_ui_crashes(limit=5)
        self.assertEqual([c["message"] for c in crashes], ["ui"])

    def test_tolerates_faulthandler_noise_and_truncated_lines(self) -> None:
        self._write(
            "Current thread 0x00007f0a (most recent call first):",
            '  File "app/web.py", line 12 in main',
            '{"type": "ui_crash", "message": "trunc',  # torn write
            json.dumps({"type": "ui_crash", "message": "good"}),
        )
        crashes = crash_logging.read_ui_crashes(limit=5)
        self.assertEqual([c["message"] for c in crashes], ["good"])

    def test_honours_the_limit(self) -> None:
        self._write(
            *[json.dumps({"type": "ui_crash", "message": f"m{i}"}) for i in range(10)]
        )
        self.assertEqual(len(crash_logging.read_ui_crashes(limit=3)), 3)

    def test_a_missing_file_is_empty_not_an_error(self) -> None:
        crash_logging.CRASH_LOG_PATH = Path(self._tmp.name) / "nope.txt"
        self.assertEqual(crash_logging.read_ui_crashes(limit=5), [])

    def test_a_zero_limit_asks_for_nothing(self) -> None:
        self._write(json.dumps({"type": "ui_crash", "message": "x"}))
        self.assertEqual(crash_logging.read_ui_crashes(limit=0), [])

    def test_round_trips_what_log_ui_crash_wrote(self) -> None:
        crash_logging.log_ui_crash({
            "message": "render blew up",
            "source": "render",
            "context": {"build": "abc"},
            "breadcrumbs": [{"t": 1, "cat": "ws", "msg": "open"}],
        })
        crashes = crash_logging.read_ui_crashes(limit=1)
        self.assertEqual(len(crashes), 1)
        self.assertEqual(crashes[0]["message"], "render blew up")
        self.assertEqual(crashes[0]["context"]["build"], "abc")
        self.assertEqual(crashes[0]["breadcrumbs"][0]["msg"], "open")


if __name__ == "__main__":
    unittest.main()
