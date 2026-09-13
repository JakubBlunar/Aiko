"""Pass 18: C7 live activity pull / get_activity tool."""
from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.core.activity.envelope import ActivityEnvelope, ActivitySubject, parse_envelope
from app.core.activity.handlers import KNOWN_SOURCES
from app.core.activity.ingest import ingest_envelope
from app.core.activity.pull import format_activity_view, pick_last_session
from app.core.activity.store import ActivityStore
from app.core.infra.chat_database import ChatDatabase
from app.core.session.session_controller import SessionController
from app.llm.tools.activity import GetActivityTool


_NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


def _env(
    *,
    app: str | None = "Code",
    title: str | None = "secret.md",
    surface: str | None = "abc",
    request_id: str | None = None,
    at: str = "2026-09-13T19:59:00+00:00",
    source: str = "foreground",
) -> dict:
    body = {
        "v": 1,
        "at": at,
        "source": source,
        "tier": "cheap",
        "subject": {"app": app, "title": title, "surface_id": surface},
        "signal": {"kind": "focus"},
        "payload": {},
    }
    if request_id is not None:
        body["request_id"] = request_id
    return body


class _TempDB:
    def __enter__(self) -> ChatDatabase:
        self._dir = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = ChatDatabase(Path(self._dir.name) / "t.db")
        return self.db

    def __exit__(self, *exc: object) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        self._dir.cleanup()


def _controller(*, enabled: bool = True, store: ActivityStore | None = None) -> SessionController:
    controller = SessionController.__new__(SessionController)
    controller._settings = SimpleNamespace(  # type: ignore[attr-defined]
        agent=SimpleNamespace(
            activity_awareness_enabled=enabled,
            activity_title_allowlist=["Code"],
        ),
        assistant=SimpleNamespace(user_display_name="Jacob"),
    )
    controller._user_active_app = None  # type: ignore[attr-defined]
    controller._typed_silence_lock = threading.Lock()  # type: ignore[attr-defined]
    controller._activity_store = store  # type: ignore[attr-defined]
    controller._activity_request_listeners = []  # type: ignore[attr-defined]
    return controller


class FormatViewTests(unittest.TestCase):
    def test_live_sample_has_no_surface_id(self) -> None:
        live = ActivityEnvelope(
            v=1,
            at="2026-09-13T19:59:50+00:00",
            source="foreground",
            tier="cheap",
            subject=ActivitySubject(app="Code", title="ok.py", surface_id="hwnd"),
            signal_kind="focus",
            request_id="abc",
        )
        view = format_activity_view(live=live, fresh=True, enabled=True, now=_NOW)
        self.assertTrue(view["fresh"])
        self.assertEqual(view["app"], "Code")
        self.assertEqual(view["title"], "ok.py")
        self.assertEqual(view["note"], "live sample")
        self.assertNotIn("surface_id", view)
        self.assertNotIn("request_id", view)

    def test_timeout_uses_last_session_not_error(self) -> None:
        row = {
            "source": "foreground",
            "app": "Cursor",
            "title": "pass18.py",
            "ended_at": "2026-09-13T19:50:00+00:00",
        }
        view = format_activity_view(
            session_row=row, fresh=False, enabled=True, now=_NOW,
        )
        self.assertFalse(view["fresh"])
        self.assertEqual(view["app"], "Cursor")
        self.assertIn("last stored", view["note"])
        self.assertNotIn("error", view["note"].lower())

    def test_awareness_off_note(self) -> None:
        view = format_activity_view(
            session_row={"app": "Code", "source": "foreground"},
            fresh=False, enabled=False, now=_NOW,
        )
        self.assertFalse(view["enabled"])
        self.assertIn("off", view["note"])

    def test_empty_shelf(self) -> None:
        view = format_activity_view(fresh=False, enabled=True, now=_NOW)
        self.assertIsNone(view["app"])
        self.assertIn("no recent", view["note"])

    def test_pick_last_session_prefers_foreground(self) -> None:
        picked = pick_last_session([
            {"source": "idle", "app": None},
            {"source": "foreground", "app": "Code"},
        ])
        assert picked is not None
        self.assertEqual(picked["app"], "Code")


class PersistRequestIdTests(unittest.TestCase):
    def test_request_id_is_not_stored(self) -> None:
        settings = SimpleNamespace(
            agent=SimpleNamespace(
                activity_awareness_enabled=True,
                activity_title_allowlist=["Code"],
            ),
        )
        with _TempDB() as db:
            store = ActivityStore(db)
            ingest_envelope(
                _env(request_id="req-keep-out", title="ok.py"),
                settings=settings, store=store,
            )
            last = store.last_event()
            assert last is not None
            self.assertNotIn("request_id", last["payload"])
            self.assertEqual(last["app"], "Code")


class PullMixinTests(unittest.TestCase):
    def test_no_listeners_returns_last_session(self) -> None:
        settings = SimpleNamespace(
            agent=SimpleNamespace(
                activity_awareness_enabled=True,
                activity_title_allowlist=["Code"],
            ),
        )
        with _TempDB() as db:
            store = ActivityStore(db)
            ingest_envelope(_env(title="ok.py"), settings=settings, store=store)
            controller = _controller(store=store)
            view = controller.pull_activity()
        self.assertFalse(view["fresh"])
        self.assertEqual(view["app"], "Code")
        self.assertEqual(view["title"], "ok.py")
        self.assertNotIn("surface_id", view)

    def test_timeout_zero_skips_wait_with_listener(self) -> None:
        controller = _controller()
        controller.add_activity_request_listener(lambda _rid: None)
        view = controller.pull_activity(timeout_s=0)
        self.assertFalse(view["fresh"])
        self.assertIn("no recent", view["note"])

    def test_ingest_completes_waiter(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            controller = _controller(store=store)

            def _reply(rid: str) -> None:
                controller.ingest_activity_envelope(
                    _env(request_id=rid, title="ok.py"),
                )

            controller.add_activity_request_listener(_reply)
            view = controller.pull_activity()
        self.assertTrue(view["fresh"])
        self.assertEqual(view["app"], "Code")
        self.assertEqual(view["title"], "ok.py")
        self.assertEqual(view["note"], "live sample")
        self.assertNotIn("surface_id", view)

    def test_titles_stripped_without_allowlist(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            controller = _controller(store=store)
            controller._settings.agent.activity_title_allowlist = []  # type: ignore[attr-defined]

            def _reply(rid: str) -> None:
                controller.ingest_activity_envelope(
                    _env(request_id=rid, title="secret.md"),
                )

            controller.add_activity_request_listener(_reply)
            view = controller.pull_activity()
        self.assertTrue(view["fresh"])
        self.assertEqual(view["app"], "Code")
        self.assertIsNone(view["title"])

    def test_awareness_off_skips_wait(self) -> None:
        settings = SimpleNamespace(
            agent=SimpleNamespace(
                activity_awareness_enabled=True,
                activity_title_allowlist=["Code"],
            ),
        )
        with _TempDB() as db:
            store = ActivityStore(db)
            ingest_envelope(_env(title="ok.py"), settings=settings, store=store)
            controller = _controller(enabled=False, store=store)
            hung = {"called": False}

            def _should_not_fire(_rid: str) -> None:
                hung["called"] = True

            controller.add_activity_request_listener(_should_not_fire)
            view = controller.pull_activity()
        self.assertFalse(hung["called"])
        self.assertFalse(view["enabled"])
        self.assertFalse(view["fresh"])
        self.assertEqual(view["app"], "Code")

    def test_tool_json_round_trip(self) -> None:
        controller = _controller()
        raw = controller.pull_activity_for_tool()
        payload = json.loads(raw)
        self.assertIn("fresh", payload)
        self.assertIn("note", payload)
        self.assertNotIn("surface_id", payload)


class ToolSchemaTests(unittest.TestCase):
    def test_get_activity_schema_and_run(self) -> None:
        tool = GetActivityTool(lambda: '{"fresh": false}')
        schema = tool.schema()
        self.assertEqual(schema.name, "get_activity")
        self.assertEqual(tool.run({}), '{"fresh": false}')


class InvariantTests(unittest.TestCase):
    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = _controller()
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))
        self.assertFalse(hasattr(SessionController, "_live_mode_enabled"))

    def test_uia_is_not_a_known_source(self) -> None:
        self.assertNotIn("uia", KNOWN_SOURCES)

    def test_parse_keeps_request_id(self) -> None:
        parsed = parse_envelope(_env(request_id="snap-1"))
        assert parsed is not None
        self.assertEqual(parsed.request_id, "snap-1")


if __name__ == "__main__":
    unittest.main()
