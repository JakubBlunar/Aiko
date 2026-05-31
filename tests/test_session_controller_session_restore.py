"""Tests for last-active-session restoration in
:class:`app.core.session.session_controller.SessionController`.

When the user reloads the browser tab or restarts the app, the
controller should bring them back to the conversation they were
viewing rather than always defaulting to ``"main"``. The plumbing
involves two pieces:

1. ``switch_session`` persists the current session id under
   ``session.last_active_id`` in ``user.json`` (best-effort).
2. ``__init__`` reads that persisted id back via
   ``_resolve_initial_session_id`` with a chained fallback: persisted
   id → most-recently-active session in the chat DB → ``"main"``.

The tests below construct a partially-instantiated controller (via
``__new__``) so they can exercise just the resolver / switch surface
without booting the full app stack (TTS, MCP, model client, …).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

from app.core.infra import settings as settings_mod
from app.core.session.session_controller import SessionController


def _make_controller() -> SessionController:
    """Build the smallest possible SessionController stub for resolver
    tests: ``_user_id`` set, ``_chat_db`` mocked, no other
    dependencies wired."""
    controller = SessionController.__new__(SessionController)
    controller._user_id = "default"
    controller._chat_db = MagicMock()
    controller._chat_db.list_sessions = MagicMock(return_value=[])
    controller._merge_buffer = {}
    controller._vocal_tone_lock = MagicMock()
    controller._vocal_tone_lock.__enter__ = MagicMock(return_value=None)
    controller._vocal_tone_lock.__exit__ = MagicMock(return_value=False)
    controller._last_vocal_tone = None
    return controller


class ResolveInitialSessionIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(settings_mod, "USER_CONFIG_PATH", self.user_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_user_json(self, payload: dict[str, Any]) -> None:
        self.user_json.write_text(json.dumps(payload), encoding="utf-8")

    def test_persisted_last_active_id_wins_over_db(self) -> None:
        # user.json points at "abc123" — that should beat the most-recent
        # session in the DB even when the DB lists something else.
        self._write_user_json({"session": {"last_active_id": "abc123"}})
        controller = _make_controller()
        controller._chat_db.list_sessions.return_value = [
            {"session_id": "default:newest", "message_count": 5},
        ]
        self.assertEqual(controller._resolve_initial_session_id(), "abc123")

    def test_persisted_id_is_honoured_even_for_empty_session(self) -> None:
        # "New session" → close tab → reopen: the freshly-created empty
        # session should still be the one we land on rather than being
        # silently rerouted to whichever older session has messages.
        self._write_user_json({"session": {"last_active_id": "fresh-empty"}})
        controller = _make_controller()
        controller._chat_db.list_sessions.return_value = [
            {"session_id": "default:older-but-has-messages", "message_count": 12},
        ]
        self.assertEqual(controller._resolve_initial_session_id(), "fresh-empty")

    def test_falls_back_to_most_recent_when_no_persisted_id(self) -> None:
        # No user.json entry at all → use whichever session the DB lists
        # first (DB returns rows ordered by ``last_at DESC``).
        controller = _make_controller()
        controller._chat_db.list_sessions.return_value = [
            {"session_id": "default:recent", "message_count": 3},
            {"session_id": "default:older", "message_count": 17},
        ]
        self.assertEqual(controller._resolve_initial_session_id(), "recent")

    def test_strips_user_prefix_from_db_session_id(self) -> None:
        # ``list_sessions`` returns the composite ``user:id`` key; the
        # in-memory ``_session_id`` is the bare id (the ``session_key``
        # property re-attaches the prefix). The resolver must split.
        controller = _make_controller()
        controller._chat_db.list_sessions.return_value = [
            {"session_id": "alice:my-session", "message_count": 2},
        ]
        self.assertEqual(controller._resolve_initial_session_id(), "my-session")

    def test_falls_back_to_default_when_db_is_empty(self) -> None:
        # First-run experience: no user.json, no DB rows → land on the
        # primordial "main" conversation (the existing legacy default).
        controller = _make_controller()
        self.assertEqual(controller._resolve_initial_session_id(), "main")
        self.assertEqual(
            controller._resolve_initial_session_id(default="zero"), "zero",
        )

    def test_blank_persisted_id_is_ignored(self) -> None:
        # Defensive: someone hand-edited user.json to an empty string.
        # Treat it like "no preference" and continue down the chain.
        self._write_user_json({"session": {"last_active_id": "   "}})
        controller = _make_controller()
        controller._chat_db.list_sessions.return_value = [
            {"session_id": "default:from-db", "message_count": 1},
        ]
        self.assertEqual(controller._resolve_initial_session_id(), "from-db")

    def test_corrupt_user_json_does_not_crash_resolver(self) -> None:
        # A truncated / invalid JSON shouldn't take the app down — the
        # resolver should swallow the read failure and fall through.
        self.user_json.write_text("{not valid json", encoding="utf-8")
        controller = _make_controller()
        controller._chat_db.list_sessions.return_value = [
            {"session_id": "default:rescue", "message_count": 1},
        ]
        self.assertEqual(controller._resolve_initial_session_id(), "rescue")

    def test_db_failure_does_not_crash_resolver(self) -> None:
        # An exception from list_sessions (e.g. DB locked at startup)
        # should not propagate; the resolver lands on ``default``.
        controller = _make_controller()
        controller._chat_db.list_sessions.side_effect = RuntimeError("db locked")
        self.assertEqual(controller._resolve_initial_session_id(), "main")


class SwitchSessionPersistenceTests(unittest.TestCase):
    """Calling ``switch_session`` should write the new id to user.json
    so the next ``_resolve_initial_session_id`` finds it."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(settings_mod, "USER_CONFIG_PATH", self.user_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_switch_writes_last_active_id(self) -> None:
        controller = _make_controller()
        controller._session_id = "main"
        controller._clear_merge_buffer = MagicMock()
        controller.switch_session("abc123")
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body, {"session": {"last_active_id": "abc123"}})
        self.assertEqual(controller._session_id, "abc123")

    def test_switch_preserves_unrelated_keys(self) -> None:
        # An existing avatar / tts override block must survive the switch.
        self.user_json.write_text(
            json.dumps({
                "avatar": {"scale_multiplier": 1.6},
                "tts": {"voice": "aiko1.safetensors"},
            }),
            encoding="utf-8",
        )
        controller = _make_controller()
        controller._session_id = "main"
        controller._clear_merge_buffer = MagicMock()
        controller.switch_session("def456")
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body["avatar"], {"scale_multiplier": 1.6})
        self.assertEqual(body["tts"], {"voice": "aiko1.safetensors"})
        self.assertEqual(body["session"], {"last_active_id": "def456"})

    def test_blank_id_is_a_noop(self) -> None:
        # An empty / whitespace switch should not mutate state or write
        # the file (the API layer already validates non-empty ids, but
        # the controller is the last line of defence).
        controller = _make_controller()
        controller._session_id = "main"
        controller._clear_merge_buffer = MagicMock()
        controller.switch_session("   ")
        self.assertEqual(controller._session_id, "main")
        self.assertFalse(self.user_json.exists())

    def test_repeated_switches_overwrite_prior_value(self) -> None:
        controller = _make_controller()
        controller._session_id = "main"
        controller._clear_merge_buffer = MagicMock()
        controller.switch_session("first")
        controller.switch_session("second")
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body["session"]["last_active_id"], "second")

    def test_persistence_failure_does_not_break_switch(self) -> None:
        # The in-memory state must still flip even if the disk write
        # blows up — the user shouldn't be stuck on the old session
        # because user.json happens to be locked.
        controller = _make_controller()
        controller._session_id = "main"
        controller._clear_merge_buffer = MagicMock()
        with mock.patch(
            "app.core.session.session_controller.persist_user_overrides",
            side_effect=OSError("locked"),
        ):
            controller.switch_session("survives")
        self.assertEqual(controller._session_id, "survives")

    def test_round_trip_through_resolver(self) -> None:
        # End-to-end: switch persists, resolver reads back the value,
        # the next ``__init__`` would land the user on the same session.
        controller = _make_controller()
        controller._session_id = "main"
        controller._clear_merge_buffer = MagicMock()
        controller.switch_session("round-trip-ok")
        # Drop the cached read so the resolver hits the freshly-written
        # file, not whatever was cached before the persist call.
        settings_mod._config_cache.pop(str(self.user_json), None)
        fresh = _make_controller()
        self.assertEqual(
            fresh._resolve_initial_session_id(),
            "round-trip-ok",
        )


if __name__ == "__main__":
    unittest.main()
