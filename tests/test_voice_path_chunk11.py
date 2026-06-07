"""Tests for chunk 11 — voice path swap through the brain queue.

The voice path is the last user-message producer to migrate to the
queue (typed WS landed in chunk 8, MCP in chunk 7). This chunk adds
three optional fields to :class:`UserMessageEvent` so the audio
capture thread can carry the same metadata
:meth:`SessionController.chat_once_streaming` used to receive as
direct kwargs:

* ``resume_message_id`` — the existing user-row id when the merge
  buffer folded phrase B's text into phrase A's row.
* ``capture_ms`` — audio capture wall time.
* ``stt_ms`` — Whisper wall time.

Coverage here:

* Event construction: defaults are neutral / typed-safe.
* Handler routing: the three new fields are threaded into
  ``chat_once_streaming`` via the right kwargs
  (``_resume_message_id`` / ``capture_ms`` / ``stt_ms``).
* ``enqueue_user_message`` accepts the new kwargs and stamps them
  on the event (queue path) AND threads them into the direct
  fallback (when ``agent.tasks_enabled=False``).
* The "voice" mode in :data:`_USER_MESSAGE_MODE_MAP` resolves to
  ``"live"`` for ``chat_once_streaming``.
* The merge-buffer logic in
  :meth:`SessionController.process_live_capture` is unchanged
  (chunk 11 only swaps the final call site, not the buffer
  decision).
* The disabled-fallback path is still the byte-identical legacy
  shape (the merge tests already pin this; we just add a
  positive assertion that the voice metadata reaches
  ``chat_once_streaming`` through the fallback).
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.core.brain import KIND_USER_MESSAGE, UserMessageEvent
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import load_settings
from app.core.session.task_orchestration_mixin import (
    _USER_MESSAGE_MODE_MAP,
    TaskOrchestrationMixin,
)


# ── shared helpers (mirror test_task_orchestration_mixin.py shape) ─────


class _FakeTts:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def is_active(self) -> bool:
        return bool(self.active)


class _Host(TaskOrchestrationMixin):
    """Minimal mixin host that records every ``chat_once_streaming`` call.

    Same shape as the stub in ``test_task_orchestration_mixin.py`` —
    duplicated here rather than imported because importing a test
    module from another test module is fragile under pytest's
    collection rules and the stub is small.
    """

    def __init__(
        self,
        *,
        chat_db: ChatDatabase,
        settings: Any,
        user_id: str = "voice-user",
    ) -> None:
        self._chat_db = chat_db
        self._settings = settings
        self._user_id = user_id
        self._turn_in_progress: bool = False
        self._tts = _FakeTts(active=False)
        self._last_user_activity_at: float = -float("inf")
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_reply: str = "voice reply"

    @property
    def session_key(self) -> str:
        return f"session-{self._user_id}"

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        mode: str = "typed",
        on_token: Any = None,
        on_generation_status: Any = None,
        stop_requested: Any = None,
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
        _resume_message_id: int | None = None,
        **_extra: Any,
    ) -> str:
        self.chat_calls.append(
            {
                "user_text": user_text,
                "mode": mode,
                "capture_ms": float(capture_ms),
                "stt_ms": float(stt_ms),
                "_resume_message_id": _resume_message_id,
            }
        )
        return self.chat_reply


class _Fixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self.db_path = self.path / "chat.db"
        self.chat_db = ChatDatabase(self.db_path)
        self.settings = load_settings(None)

    def host(self, **agent_overrides: Any) -> _Host:
        if agent_overrides:
            agent = dataclasses.replace(self.settings.agent, **agent_overrides)
            settings = dataclasses.replace(self.settings, agent=agent)
        else:
            settings = self.settings
        return _Host(chat_db=self.chat_db, settings=settings)

    def cleanup(self) -> None:
        conn = getattr(self.chat_db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.chat_db._local.conn = None  # type: ignore[union-attr]
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


# ── 1. UserMessageEvent field defaults ─────────────────────────────────


class UserMessageEventChunk11Tests(unittest.TestCase):
    """Pin the three new fields' defaults and discriminator."""

    def test_defaults_are_neutral(self) -> None:
        event = UserMessageEvent(text="hi")
        self.assertIsNone(event.resume_message_id)
        self.assertEqual(event.capture_ms, 0.0)
        self.assertEqual(event.stt_ms, 0.0)

    def test_kind_unchanged(self) -> None:
        self.assertEqual(UserMessageEvent.kind, KIND_USER_MESSAGE)

    def test_event_carries_voice_metadata_when_constructed(self) -> None:
        event = UserMessageEvent(
            text="phrase a phrase b",
            mode="voice",
            resume_message_id=42,
            capture_ms=812.5,
            stt_ms=137.0,
        )
        self.assertEqual(event.resume_message_id, 42)
        self.assertAlmostEqual(event.capture_ms, 812.5)
        self.assertAlmostEqual(event.stt_ms, 137.0)

    def test_event_is_hashable_with_voice_metadata(self) -> None:
        # Frozen+slotted: stays hashable even with new fields.
        event = UserMessageEvent(
            text="x", resume_message_id=1, capture_ms=10.0, stt_ms=5.0,
        )
        self.assertEqual(hash(event), hash(event))


# ── 2. Mode mapping ────────────────────────────────────────────────────


class VoiceModeMapTests(unittest.TestCase):
    def test_voice_maps_to_live(self) -> None:
        self.assertEqual(_USER_MESSAGE_MODE_MAP["voice"], "live")

    def test_typed_and_mcp_unchanged(self) -> None:
        self.assertEqual(_USER_MESSAGE_MODE_MAP["typed"], "typed")
        self.assertEqual(_USER_MESSAGE_MODE_MAP["mcp"], "typed")


# ── 3. Handler threads voice metadata through (queue path) ────────────


class VoiceMetadataRoutingTests(unittest.TestCase):
    """Queue path: event in, ``chat_once_streaming`` call out, fields preserved."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_voice_event_threads_all_three_fields(self) -> None:
        reply = self.host.enqueue_user_message(
            text="hello there",
            mode="voice",
            wait_for_reply=True,
            timeout=2.0,
            resume_message_id=99,
            capture_ms=850.0,
            stt_ms=120.0,
        )
        self.assertEqual(reply, "voice reply")
        self.assertEqual(len(self.host.chat_calls), 1)
        call = self.host.chat_calls[0]
        self.assertEqual(call["user_text"], "hello there")
        self.assertEqual(call["mode"], "live")
        self.assertEqual(call["_resume_message_id"], 99)
        self.assertAlmostEqual(call["capture_ms"], 850.0)
        self.assertAlmostEqual(call["stt_ms"], 120.0)

    def test_voice_event_without_resume_id_threads_none(self) -> None:
        # Fresh-turn voice case: no resume id, but capture / stt
        # still threaded as floats.
        self.host.enqueue_user_message(
            text="fresh",
            mode="voice",
            wait_for_reply=True,
            timeout=2.0,
            capture_ms=410.0,
            stt_ms=85.0,
        )
        call = self.host.chat_calls[0]
        self.assertIsNone(call["_resume_message_id"])
        self.assertAlmostEqual(call["capture_ms"], 410.0)
        self.assertAlmostEqual(call["stt_ms"], 85.0)

    def test_typed_event_keeps_neutral_voice_metadata(self) -> None:
        # Pre-chunk-11 producers (typed / MCP) shouldn't accidentally
        # start sending non-zero capture_ms.
        self.host.enqueue_user_message(
            text="typed input",
            mode="typed",
            wait_for_reply=True,
            timeout=2.0,
        )
        call = self.host.chat_calls[0]
        self.assertEqual(call["mode"], "typed")
        self.assertEqual(call["capture_ms"], 0.0)
        self.assertEqual(call["stt_ms"], 0.0)
        self.assertIsNone(call["_resume_message_id"])

    def test_resume_message_id_coerced_to_int(self) -> None:
        # Producers may pass an int-like (e.g. numpy int64 in the wild);
        # the mixin coerces to plain int before stamping the event.
        self.host.enqueue_user_message(
            text="x",
            mode="voice",
            wait_for_reply=True,
            timeout=2.0,
            resume_message_id=True,  # truthy int-like; pin coercion
        )
        call = self.host.chat_calls[0]
        self.assertEqual(call["_resume_message_id"], 1)
        self.assertIsInstance(call["_resume_message_id"], int)


# ── 4. Disabled-fallback path also threads voice metadata ─────────────


class VoiceMetadataDisabledFallbackTests(unittest.TestCase):
    """When ``agent.tasks_enabled=False``, ``enqueue_user_message`` calls
    ``chat_once_streaming`` directly. The voice metadata must still
    reach the controller — otherwise the merge / metrics paths break
    silently for anyone who disabled the subsystem.
    """

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        # Master switch off: the mixin installs the disabled stub.
        self.host = self.fx.host(tasks_enabled=False)
        self.host._init_task_orchestration()

    def test_disabled_path_threads_voice_metadata(self) -> None:
        reply = self.host.enqueue_user_message(
            text="voice via fallback",
            mode="voice",
            wait_for_reply=True,
            timeout=2.0,
            resume_message_id=7,
            capture_ms=600.0,
            stt_ms=110.0,
        )
        # When the subsystem is off, ``enqueue_user_message`` returns
        # the legacy direct-call reply synchronously without ever
        # touching the queue. The fallback path must thread voice
        # metadata too — otherwise users with tasks turned off would
        # silently lose merge / metrics in voice mode.
        self.assertEqual(reply, "voice reply")
        self.assertEqual(len(self.host.chat_calls), 1)
        call = self.host.chat_calls[0]
        self.assertEqual(call["user_text"], "voice via fallback")
        self.assertEqual(call["mode"], "live")
        self.assertEqual(call["_resume_message_id"], 7)
        self.assertAlmostEqual(call["capture_ms"], 600.0)
        self.assertAlmostEqual(call["stt_ms"], 110.0)

    def test_disabled_path_typed_has_no_resume_kwarg_leak(self) -> None:
        # The fallback only passes ``_resume_message_id`` to
        # ``chat_once_streaming`` when it's set, so typed callers
        # without a resume id should see ``None`` (the stub's default).
        self.host.enqueue_user_message(
            text="typed",
            mode="typed",
            wait_for_reply=True,
            timeout=2.0,
        )
        call = self.host.chat_calls[0]
        self.assertIsNone(call["_resume_message_id"])
        self.assertEqual(call["capture_ms"], 0.0)


# ── 5. Empty text early-return semantics for voice ────────────────────


class VoiceEmptyTextTests(unittest.TestCase):
    """The audio capture path can occasionally hand an empty string
    (silence + STT noise) to ``enqueue_user_message`` even though
    ``process_live_capture`` filters its own empties. The empty-text
    early-return must still resolve cleanly so the voice loop never
    blocks on a stale future.
    """

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_voice_empty_text_with_wait_returns_empty_string(self) -> None:
        result = self.host.enqueue_user_message(
            text="   ",
            mode="voice",
            wait_for_reply=True,
            timeout=2.0,
            capture_ms=200.0,
            stt_ms=30.0,
        )
        self.assertEqual(result, "")
        self.assertEqual(self.host.chat_calls, [])

    def test_voice_empty_text_without_wait_returns_none(self) -> None:
        result = self.host.enqueue_user_message(
            text="",
            mode="voice",
            wait_for_reply=False,
        )
        self.assertIsNone(result)
        self.assertEqual(self.host.chat_calls, [])


if __name__ == "__main__":
    unittest.main()
