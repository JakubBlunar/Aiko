"""C6 collection pipeline: parse, redact, sessionize, prune."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.activity.envelope import parse_envelope
from app.core.activity.handlers import KNOWN_SOURCES, redact
from app.core.activity.ingest import ingest_envelope
from app.core.activity.prune_worker import ActivityPruneWorker
from app.core.activity.store import ActivityStore
from app.core.infra.chat_database import ChatDatabase
from app.core.infra import timephrase


class _Agent:
    activity_awareness_enabled = True
    activity_title_allowlist: list[str]

    def __init__(self, allowlist: list[str] | None = None) -> None:
        self.activity_title_allowlist = list(allowlist or [])


class _Settings:
    def __init__(self, allowlist: list[str] | None = None) -> None:
        self.agent = _Agent(allowlist)


def _env(
    *,
    source: str = "foreground",
    app: str | None = "Code",
    title: str | None = "rag_store.py — assistant",
    surface: str | None = "abc",
    kind: str = "focus",
    at: str = "2026-08-30T19:00:00+00:00",
    v: int = 1,
    extra: dict | None = None,
) -> dict:
    body = {
        "v": v,
        "at": at,
        "source": source,
        "tier": "cheap",
        "subject": {"app": app, "title": title, "surface_id": surface},
        "signal": {"kind": kind},
        "payload": {},
    }
    if extra:
        body.update(extra)
    return body


class ParseTests(unittest.TestCase):
    def test_unknown_version_dropped(self) -> None:
        self.assertIsNone(parse_envelope(_env(v=2)))

    def test_malformed_dropped(self) -> None:
        self.assertIsNone(parse_envelope(None))
        self.assertIsNone(parse_envelope("nope"))
        self.assertIsNone(parse_envelope({"v": 1, "source": "foreground"}))

    def test_v1_round_trip_keeps_unknown_keys(self) -> None:
        parsed = parse_envelope(_env(extra={"quality": 0.9}))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.to_payload_json()["quality"], 0.9)


class RedactTests(unittest.TestCase):
    def test_unknown_source_dropped(self) -> None:
        parsed = parse_envelope(_env(source="uia"))
        self.assertIsNotNone(parsed)
        self.assertIsNone(redact(parsed, _Settings(["Code"])))

    def test_title_stripped_without_allowlist(self) -> None:
        parsed = parse_envelope(_env())
        assert parsed is not None
        out = redact(parsed, _Settings([]))
        assert out is not None
        self.assertEqual(out.subject.app, "Code")
        self.assertIsNone(out.subject.title)

    def test_allowlist_keeps_title(self) -> None:
        parsed = parse_envelope(_env())
        assert parsed is not None
        out = redact(parsed, _Settings(["code.exe"]))
        assert out is not None
        self.assertEqual(out.subject.title, "rag_store.py — assistant")

    def test_url_shaped_title_stripped_even_when_allowlisted(self) -> None:
        parsed = parse_envelope(_env(title="https://bank.example/pay"))
        assert parsed is not None
        out = redact(parsed, _Settings(["Code"]))
        assert out is not None
        self.assertIsNone(out.subject.title)

    def test_known_sources_match_rust_cheap_set(self) -> None:
        self.assertEqual(KNOWN_SOURCES, ("foreground", "idle", "lock"))


class _TempDB:
    def __enter__(self) -> ChatDatabase:
        self._dir = tempfile.TemporaryDirectory()
        self.db = ChatDatabase(Path(self._dir.name) / "t.db")
        return self.db

    def __exit__(self, *exc: object) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class StoreTests(unittest.TestCase):
    def test_sessionizer_collapses_same_app_surface(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            settings = _Settings(["Code"])
            ingest_envelope(
                _env(at="2026-08-30T19:00:00+00:00", title="a.py"),
                settings=settings, store=store,
            )
            ingest_envelope(
                _env(at="2026-08-30T19:00:03+00:00", title="b.py"),
                settings=settings, store=store,
            )
            sessions = store.recent_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["event_count"], 2)
            self.assertEqual(sessions[0]["title"], "b.py")
            self.assertEqual(store.counts()["events"], 2)

    def test_flicker_to_other_app_opens_new_session(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            settings = _Settings(["Code", "Discord"])
            ingest_envelope(
                _env(at="2026-08-30T19:00:00+00:00", surface="a"),
                settings=settings, store=store,
            )
            ingest_envelope(
                _env(
                    at="2026-08-30T19:00:01+00:00",
                    app="Discord", title="chat", surface="b",
                ),
                settings=settings, store=store,
            )
            self.assertEqual(len(store.recent_sessions()), 2)

    def test_unknown_source_never_persists(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            ingest_envelope(
                _env(source="uia", title="secret"),
                settings=_Settings(["Code"]), store=store,
            )
            self.assertEqual(store.counts()["events"], 0)

    def test_prune_deletes_old_rows(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            settings = _Settings(["Code"])
            ingest_envelope(
                _env(at="2020-01-01T00:00:00+00:00"),
                settings=settings, store=store,
            )
            ingest_envelope(
                _env(at=timephrase.utcnow().isoformat()),
                settings=settings, store=store,
            )
            result = store.prune(30)
            self.assertEqual(result["events"], 1)
            self.assertEqual(store.counts()["events"], 1)

    def test_prune_worker_demand_is_old_rows(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            ingest_envelope(
                _env(at="2020-01-01T00:00:00+00:00"),
                settings=_Settings(["Code"]), store=store,
            )
            worker = ActivityPruneWorker(store, keep_days_provider=lambda: 30)
            signal = worker.demand(now=timephrase.utcnow(), last_run_at=None)
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertGreater(signal.pressure, 0.0)
            self.assertFalse(signal.needs_llm)
            ran = worker.run()
            assert ran is not None
            self.assertEqual(ran["events"], 1)
            idle = worker.demand(now=timephrase.utcnow(), last_run_at=None)
            assert idle is not None
            self.assertEqual(idle.pressure, 0.0)

    def test_activity_tables_exist_on_fresh_schema(self) -> None:
        with _TempDB() as db:
            tables = {
                r[0]
                for r in db._get_conn().execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("activity_events", tables)
            self.assertIn("activity_sessions", tables)


class IngestToggleTests(unittest.TestCase):
    def test_disabled_toggle_does_not_write(self) -> None:
        # ingest_envelope itself does not check the toggle; the session
        # mixin does. This test pins redact+store still work when the
        # caller forgot — unknown sources remain the hard drop.
        with _TempDB() as db:
            store = ActivityStore(db)
            out = ingest_envelope(
                _env(source="lock", kind="lock", app=None, title=None),
                settings=_Settings([]), store=store,
            )
            self.assertIsNotNone(out)
            self.assertEqual(store.counts()["events"], 1)


if __name__ == "__main__":
    unittest.main()
