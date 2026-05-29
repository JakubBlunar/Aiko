"""Schema v10 temporal-awareness tests.

Covers the cross-cutting changes that make Aiko's memories temporally
honest:

- Schema v10 migration: ``event_time`` / ``temporal_type`` /
  ``relevance_until`` columns + indices land on a fresh DB and a v9
  upgrade.
- ``Memory`` dataclass + ``MemoryStore.add/update`` round-trip the
  three new fields, with validation falling back to ``'durable'`` on
  unknown ``temporal_type`` values.
- ``MemoryStore.reclassify()`` flips a future_plan to a past_event
  with a fresh ``relevance_until``.
- ``MemoryStore.list_by_temporal_type()`` filters correctly.
- ``MemoryExtractor`` parses temporal phrases out of the LLM JSON
  envelope and stamps ``temporal_type`` + ``event_time`` +
  ``relevance_until`` on the inserted row.
- ``RagRetriever.format_block()`` annotates retrieved bullets with
  the right time-tag suffix per ``temporal_type``.
- ``RagRetriever`` filters out ``past_event`` rows whose
  ``relevance_until`` already passed.
- ``MemoryDecayWorker`` reclassifies overdue future_plans and
  archives expired past_events.
- ``FollowUpWorker`` fires exactly one nudge per future_plan around
  ``event_time``, and is idempotent across ticks.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

from app.core.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.memory_decay_worker import MemoryDecayWorker
from app.core.memory_extractor import (
    MemoryExtractor,
    _build_system_prompt,
    _derive_relevance_until,
    _parse_iso,
)
from app.core.memory_store import (
    VALID_TEMPORAL_TYPES,
    Memory,
    MemoryStore,
    _coerce_temporal_type,
)
from app.core.prepared_nudge import PreparedNudgeStore
from app.core.rag_retriever import (
    RagHit,
    RagRetriever,
    _humanize_future,
    _humanize_past,
    _temporal_filter_drops,
    _temporal_suffix,
)
from app.core.rag_store import MemoryRecord
from app.core.follow_up_worker import FollowUpWorker


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


def _store_factory() -> "tuple[Path, MemoryStore]":
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    return path, store


def _emb(text: str) -> np.ndarray:
    return _FakeEmbedder().embed(text)


# ── 1. Schema migration ────────────────────────────────────────────


class TestSchemaV10Migration(unittest.TestCase):
    def test_fresh_database_has_v10_columns(self) -> None:
        path, _ = _store_factory()
        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        finally:
            conn.close()
        self.assertIn("event_time", cols)
        self.assertIn("temporal_type", cols)
        self.assertIn("relevance_until", cols)

    def test_fresh_database_has_v10_indices(self) -> None:
        path, _ = _store_factory()
        conn = sqlite3.connect(str(path))
        try:
            indices = {
                row[1]
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            conn.close()
        self.assertIn("idx_memories_event_time", indices)
        self.assertIn("idx_memories_temporal_type", indices)

    def test_v9_to_v10_migration(self) -> None:
        """A pre-existing v9 database opens cleanly and gets the new columns."""
        d = tempfile.mkdtemp()
        path = Path(d) / "v9.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (9);
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    salience REAL NOT NULL DEFAULT 0.5,
                    embedding BLOB NOT NULL,
                    source_session TEXT,
                    source_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    tier TEXT NOT NULL DEFAULT 'long_term',
                    revival_score REAL NOT NULL DEFAULT 0.0,
                    confidence REAL NOT NULL DEFAULT 0.7
                );
                INSERT INTO memories (
                    content, kind, salience, embedding, created_at
                ) VALUES (
                    'legacy fact', 'fact', 0.5, X'00', '2026-05-01T00:00:00Z'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        # Open via ChatDatabase -> should run the v9 -> v10 migration.
        ChatDatabase(path)
        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            row = conn.execute(
                "SELECT temporal_type, event_time, relevance_until FROM memories"
            ).fetchone()
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, _SCHEMA_VERSION)
        self.assertIn("temporal_type", cols)
        self.assertIn("event_time", cols)
        self.assertIn("relevance_until", cols)
        # Existing row backfilled to the safe defaults.
        self.assertEqual(row[0], "durable")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])


# ── 2. MemoryStore field round-trip + validation ─────────────────────


class TestMemoryStoreTemporal(unittest.TestCase):
    def test_add_round_trips_temporal_fields(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            content="Jacob is going to the gym tonight at 8",
            kind="event",
            embedding=_emb("gym tonight"),
            temporal_type="future_plan",
            event_time="2026-05-28T20:00:00+02:00",
            relevance_until="2026-05-29T20:00:00+02:00",
        )
        assert mem is not None
        self.assertEqual(mem.temporal_type, "future_plan")
        self.assertEqual(mem.event_time, "2026-05-28T20:00:00+02:00")
        self.assertEqual(mem.relevance_until, "2026-05-29T20:00:00+02:00")
        # Round-trip via reload
        store._reload_mirror()  # type: ignore[attr-defined]
        roundtrip = store.get(mem.id)
        assert roundtrip is not None
        self.assertEqual(roundtrip.temporal_type, "future_plan")
        self.assertEqual(roundtrip.event_time, "2026-05-28T20:00:00+02:00")

    def test_add_defaults_to_durable(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            content="Jacob lives in Prague",
            kind="fact",
            embedding=_emb("Prague"),
        )
        assert mem is not None
        self.assertEqual(mem.temporal_type, "durable")
        self.assertIsNone(mem.event_time)
        self.assertIsNone(mem.relevance_until)

    def test_temporal_type_validation_falls_back(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            content="Some statement",
            kind="fact",
            embedding=_emb("statement"),
            temporal_type="bogus_value",  # unknown
        )
        assert mem is not None
        self.assertEqual(mem.temporal_type, "durable")

    def test_coerce_temporal_type_helper(self) -> None:
        for valid in VALID_TEMPORAL_TYPES:
            self.assertEqual(_coerce_temporal_type(valid), valid)
        self.assertEqual(_coerce_temporal_type(None), "durable")
        self.assertEqual(_coerce_temporal_type(""), "durable")
        self.assertEqual(_coerce_temporal_type("garbage"), "durable")
        self.assertEqual(_coerce_temporal_type("FUTURE_PLAN"), "future_plan")

    def test_reclassify_future_to_past(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            content="Jacob plans to study tonight",
            kind="event",
            embedding=_emb("study"),
            temporal_type="future_plan",
            event_time="2026-05-27T20:00:00+02:00",
            relevance_until="2026-05-28T20:00:00+02:00",
        )
        assert mem is not None

        updated = store.reclassify(
            mem.id,
            temporal_type="past_event",
            relevance_until="2026-06-03T20:00:00+02:00",
        )
        assert updated is not None
        self.assertEqual(updated.temporal_type, "past_event")
        self.assertEqual(updated.relevance_until, "2026-06-03T20:00:00+02:00")
        # event_time should be untouched (the sentinel default).
        self.assertEqual(updated.event_time, "2026-05-27T20:00:00+02:00")

    def test_update_can_clear_event_time_with_explicit_none(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            content="Some plan",
            kind="event",
            embedding=_emb("plan"),
            temporal_type="future_plan",
            event_time="2026-05-28T20:00:00+02:00",
        )
        assert mem is not None
        updated = store.update(mem.id, event_time=None)
        assert updated is not None
        self.assertIsNone(updated.event_time)
        # ``update`` without the sentinel should NOT clobber.
        store.reclassify(mem.id, temporal_type="durable")
        re = store.get(mem.id)
        assert re is not None
        self.assertEqual(re.temporal_type, "durable")
        self.assertIsNone(re.event_time)

    def test_list_by_temporal_type_filters(self) -> None:
        _, store = _store_factory()
        store.add(
            content="future plan A",
            kind="event",
            embedding=_emb("future A"),
            temporal_type="future_plan",
            event_time="2026-05-28T08:00:00+00:00",
        )
        store.add(
            content="future plan B",
            kind="event",
            embedding=_emb("future B"),
            temporal_type="future_plan",
            event_time="2026-05-30T08:00:00+00:00",
        )
        store.add(
            content="something else",
            kind="fact",
            embedding=_emb("else"),
        )
        before = store.list_by_temporal_type(
            "future_plan", event_time_before="2026-05-29T00:00:00+00:00"
        )
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0].content, "future plan A")
        all_future = store.list_by_temporal_type("future_plan")
        self.assertEqual(len(all_future), 2)


# ── 3. Extractor temporal helpers ────────────────────────────────────


class TestMemoryExtractorTemporal(unittest.TestCase):
    def test_system_prompt_contains_today_anchor(self) -> None:
        today = datetime(2026, 5, 28, 11, 0, tzinfo=timezone.utc)
        prompt = _build_system_prompt("Jacob", today=today)
        self.assertIn("Today is", prompt)
        self.assertIn("2026", prompt)
        self.assertIn("temporal_type", prompt)
        for valid in VALID_TEMPORAL_TYPES:
            self.assertIn(valid, prompt)

    def test_derive_relevance_until_per_type(self) -> None:
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        # durable / preference -> None (timeless)
        self.assertIsNone(
            _derive_relevance_until("durable", event_time=None, created_at=now)
        )
        self.assertIsNone(
            _derive_relevance_until(
                "preference", event_time=None, created_at=now
            )
        )
        # past_event -> created_at + 7d
        until = _derive_relevance_until(
            "past_event", event_time=None, created_at=now
        )
        assert until is not None
        parsed = datetime.fromisoformat(until)
        self.assertEqual((parsed - now).days, 7)
        # future_plan -> event_time + 1d
        ev = datetime(2026, 5, 30, 20, 0, tzinfo=timezone.utc)
        until = _derive_relevance_until(
            "future_plan", event_time=ev, created_at=now
        )
        assert until is not None
        parsed = datetime.fromisoformat(until)
        self.assertEqual(parsed, ev + timedelta(days=1))
        # ongoing -> created_at + 30d
        until = _derive_relevance_until(
            "ongoing", event_time=None, created_at=now
        )
        assert until is not None
        parsed = datetime.fromisoformat(until)
        self.assertEqual((parsed - now).days, 30)

    def test_parse_iso_handles_z_and_naive(self) -> None:
        self.assertIsNone(_parse_iso(None))
        self.assertIsNone(_parse_iso(""))
        self.assertIsNone(_parse_iso("garbage"))
        z = _parse_iso("2026-05-28T20:00:00Z")
        self.assertIsNotNone(z)
        assert z is not None
        self.assertEqual(z.tzinfo, timezone.utc)
        # Naive becomes UTC
        n = _parse_iso("2026-05-28T20:00:00")
        assert n is not None
        self.assertEqual(n.tzinfo, timezone.utc)

    def test_extractor_persists_temporal_fields(self) -> None:
        """End-to-end stub: feed the extractor a canned LLM response and
        assert the inserted memory carries the right temporal fields.
        """
        _, store = _store_factory()
        embedder = _FakeEmbedder()
        # Stub Ollama: chat_json returns the canned envelope.
        canned = json.dumps(
            {
                "memories": [
                    {
                        "content": "Jacob worked on the dashboard yesterday",
                        "kind": "event",
                        "salience": 0.6,
                        "temporal_type": "past_event",
                        "event_time": "2026-05-27T18:00:00+00:00",
                    },
                    {
                        "content": "Jacob is going to the gym tonight at 20:00",
                        "kind": "event",
                        "salience": 0.7,
                        "temporal_type": "future_plan",
                        "event_time": "2026-05-28T20:00:00+00:00",
                    },
                ]
            }
        )

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50

        class _StubOllama:
            def chat_json(
                self,
                messages,
                *,
                model,
                timeout_seconds,
                options,
                format_json,
                **kwargs,
            ):
                return canned, _Usage()

        # Feed an actual chat row so the worker isn't skipped due to
        # min_window_messages.
        path = Path(store._db_path)  # type: ignore[attr-defined]
        db = ChatDatabase(path)
        for i in range(6):
            db.add_message("session-1", "user", f"hello {i}")
        extractor = MemoryExtractor(
            db=db,
            store=store,
            embedder=embedder,  # type: ignore[arg-type]
            ollama=_StubOllama(),  # type: ignore[arg-type]
            model="stub",
            min_window_messages=4,
        )

        inserted = extractor.extract_for_session("session-1")
        self.assertEqual(inserted, 2)

        # Verify the inserted rows carry temporal fields.
        all_mems = list(store._mirror.values())  # type: ignore[attr-defined]
        by_type = {m.temporal_type: m for m in all_mems}
        self.assertIn("past_event", by_type)
        self.assertIn("future_plan", by_type)
        past = by_type["past_event"]
        future = by_type["future_plan"]
        self.assertEqual(past.event_time, "2026-05-27T18:00:00+00:00")
        self.assertEqual(future.event_time, "2026-05-28T20:00:00+00:00")
        # past_event relevance_until is created_at + 7d (the precise
        # arithmetic is unit-tested separately in
        # ``test_derive_relevance_until_per_type``; here we just
        # confirm the worker actually populated the column).
        assert past.relevance_until is not None
        # future_plan relevance_until is event_time + 1d (deterministic
        # because event_time itself is canned).
        assert future.relevance_until is not None
        future_ru = datetime.fromisoformat(future.relevance_until)
        self.assertEqual(
            future_ru,
            datetime(2026, 5, 29, 20, 0, tzinfo=timezone.utc),
        )

    def test_extractor_invalid_temporal_type_falls_back(self) -> None:
        """A malformed ``temporal_type`` from the LLM should land as
        ``durable`` (the safe baseline), not crash the insert."""
        _, store = _store_factory()
        # Use the parser directly, bypassing the LLM call.
        embedder = _FakeEmbedder()

        class _Stub:
            pass

        path = Path(store._db_path)  # type: ignore[attr-defined]
        db = ChatDatabase(path)
        ex = MemoryExtractor(
            db=db,
            store=store,
            embedder=embedder,  # type: ignore[arg-type]
            ollama=_Stub(),  # type: ignore[arg-type]
            model="stub",
        )
        cands = ex._parse_response(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "this is content of useful length",
                            "kind": "fact",
                            "salience": 0.5,
                            "temporal_type": "absurd",
                        },
                    ]
                }
            )
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["temporal_type"], "durable")


# ── 4. Retriever annotation + filtering ──────────────────────────────


class TestRetrieverTemporalAnnotation(unittest.TestCase):
    def test_humanize_past(self) -> None:
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        # 1 day ago -> "yesterday"
        self.assertEqual(
            _humanize_past("2026-05-27T12:00:00+00:00", now),
            "yesterday",
        )
        # 3 days ago
        self.assertEqual(
            _humanize_past("2026-05-25T12:00:00+00:00", now),
            "3 days ago",
        )
        # 2 hours ago
        self.assertEqual(
            _humanize_past("2026-05-28T10:00:00+00:00", now),
            "2 hours ago",
        )
        # 2 weeks ago
        self.assertEqual(
            _humanize_past("2026-05-14T12:00:00+00:00", now),
            "2 weeks ago",
        )
        # 6 months ago
        self.assertIn(
            "month",
            _humanize_past("2025-11-28T12:00:00+00:00", now),
        )
        # garbage -> fallback
        self.assertEqual(_humanize_past("nonsense", now), "in the past")

    def test_humanize_future(self) -> None:
        # Use a real ``now`` and offset from it so we don't fight
        # local-TZ conversion (the helper does ``astimezone()`` to
        # local for the wall-clock string). All assertions stay on
        # phrasing, not specific clock values.
        now = datetime.now(timezone.utc)
        # 2 hours from now -> later today phrasing (tonight / this
        # afternoon / this morning depending on local hour).
        out = _humanize_future((now + timedelta(hours=2)).isoformat(), now)
        self.assertTrue(
            any(t in out for t in ("tonight", "afternoon", "morning")),
            f"expected later-today phrasing, got: {out}",
        )
        # tomorrow
        tomorrow = (now + timedelta(days=1, hours=2)).isoformat()
        out = _humanize_future(tomorrow, now)
        self.assertTrue("tomorrow" in out or "next" in out or "morning" in out)
        # garbage -> "soon"
        self.assertEqual(_humanize_future("garbage", now), "soon")
        self.assertEqual(_humanize_future(None, now), "soon")
        # past time -> "earlier"
        self.assertEqual(
            _humanize_future((now - timedelta(hours=2)).isoformat(), now),
            "earlier",
        )
        # week+ out -> "in N week(s)" / "next week"
        out = _humanize_future((now + timedelta(days=14)).isoformat(), now)
        self.assertTrue("week" in out, f"expected week phrasing, got: {out}")

    def test_temporal_suffix_per_type(self) -> None:
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        # durable / preference -> empty
        self.assertEqual(
            _temporal_suffix(
                temporal_type="durable",
                event_time=None,
                created_at="2026-05-01T00:00:00Z",
                now=now,
            ),
            "",
        )
        self.assertEqual(
            _temporal_suffix(
                temporal_type="preference",
                event_time=None,
                created_at="2026-05-01T00:00:00Z",
                now=now,
            ),
            "",
        )
        # ongoing
        self.assertEqual(
            _temporal_suffix(
                temporal_type="ongoing",
                event_time=None,
                created_at="2026-05-01T00:00:00Z",
                now=now,
            ),
            " (ongoing)",
        )
        # past_event
        suffix = _temporal_suffix(
            temporal_type="past_event",
            event_time="2026-05-27T12:00:00+00:00",
            created_at="2026-05-27T12:00:00Z",
            now=now,
        )
        self.assertIn("yesterday", suffix)
        # future_plan still ahead
        suffix = _temporal_suffix(
            temporal_type="future_plan",
            event_time="2026-05-30T20:00:00+00:00",
            created_at="2026-05-28T12:00:00Z",
            now=now,
        )
        self.assertIn("planned for", suffix)
        # future_plan that already passed -> "should be done by now"
        suffix = _temporal_suffix(
            temporal_type="future_plan",
            event_time="2026-05-28T08:00:00+00:00",
            created_at="2026-05-28T07:00:00Z",
            now=now,
        )
        self.assertIn("should be done by now", suffix)

    def test_format_block_renders_temporal_suffixes(self) -> None:
        # Use real-time deltas so we don't have to patch ``datetime``
        # (which breaks ``fromisoformat`` parsing inside the helpers
        # because they share the same module reference). Yesterday at
        # roughly the same wall-clock time and a plan a few days out
        # are stable phrasings.
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1, hours=2)).isoformat()
        in_three_days = (now + timedelta(days=3)).isoformat()
        rec_past = MemoryRecord(
            id="1",
            content="Jacob worked on the dashboard",
            kind="event",
            salience=0.5,
            source_session="s1",
            source_message_id=None,
            created_at=(now - timedelta(days=1, hours=2)).isoformat(),
            last_used_at=None,
            use_count=0,
        )
        hit_past = RagHit(
            source="memory",
            score=0.9,
            record=rec_past,
            temporal_type="past_event",
            event_time=yesterday,
        )
        rec_future = MemoryRecord(
            id="2",
            content="Jacob is going to the gym",
            kind="event",
            salience=0.5,
            source_session="s1",
            source_message_id=None,
            created_at=now.isoformat(),
            last_used_at=None,
            use_count=0,
        )
        hit_future = RagHit(
            source="memory",
            score=0.8,
            record=rec_future,
            temporal_type="future_plan",
            event_time=in_three_days,
        )
        block = RagRetriever.format_block(
            [hit_past, hit_future], user_display_name="Jacob"
        )
        self.assertIn("dashboard", block)
        # Yesterday's slot can render as either "yesterday" (24-48h
        # window) or "N hours ago" depending on the precise delta;
        # both phrasings are correct per the helper spec.
        self.assertTrue(
            "yesterday" in block or "hours ago" in block or "day" in block,
            f"expected past-tense suffix in block, got: {block}",
        )
        self.assertIn("planned for", block)

    def test_temporal_filter_drops_expired_past_events(self) -> None:
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        emb = np.zeros(16, dtype=np.float32)
        emb[0] = 1.0

        class _Mem:
            temporal_type = "past_event"
            relevance_until = "2026-05-20T00:00:00+00:00"

        self.assertTrue(_temporal_filter_drops(_Mem(), now))

        class _MemFresh:
            temporal_type = "past_event"
            relevance_until = "2026-06-10T00:00:00+00:00"

        self.assertFalse(_temporal_filter_drops(_MemFresh(), now))

        class _MemFuture:
            temporal_type = "future_plan"
            relevance_until = "2026-05-20T00:00:00+00:00"

        # future_plan is never dropped by this filter.
        self.assertFalse(_temporal_filter_drops(_MemFuture(), now))


# ── 5. Decay reclassification ────────────────────────────────────────


class _StubMemorySettings:
    tiers_enabled = True
    decay_worker_interval_seconds = 3600.0
    decay_rate_scratchpad = 0.0
    decay_rate_long_term = 0.0
    decay_rate_archive = 0.0
    revival_coefficient = 0.0
    revival_decay_per_day = 0.0
    decay_max_catchup_days = 1.0


class TestMemoryDecayWorkerReclassify(unittest.TestCase):
    def test_future_plan_flips_to_past_after_event_time(self) -> None:
        _, store = _store_factory()
        # event_time well in the past so the buffer is irrelevant.
        past_dt = (
            datetime.now(timezone.utc) - timedelta(hours=4)
        ).isoformat()
        mem = store.add(
            content="Jacob's gym plan",
            kind="event",
            embedding=_emb("gym"),
            temporal_type="future_plan",
            event_time=past_dt,
            relevance_until=(
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
        )
        assert mem is not None
        worker = MemoryDecayWorker(store, _StubMemorySettings())
        result = worker.run()
        self.assertGreaterEqual(result.get("future_plans_to_past", 0), 1)
        # Verify the row is now past_event.
        updated = store.get(mem.id)
        assert updated is not None
        self.assertEqual(updated.temporal_type, "past_event")
        # relevance_until should be event_time + 7 days.
        assert updated.relevance_until is not None
        ru = datetime.fromisoformat(updated.relevance_until)
        evt = datetime.fromisoformat(past_dt)
        self.assertEqual((ru - evt).days, 7)

    def test_past_event_archives_after_relevance_until(self) -> None:
        _, store = _store_factory()
        old_relevance = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
        mem = store.add(
            content="something old",
            kind="event",
            embedding=_emb("old"),
            temporal_type="past_event",
            event_time=(
                datetime.now(timezone.utc) - timedelta(days=10)
            ).isoformat(),
            relevance_until=old_relevance,
        )
        assert mem is not None
        # Confirm starting tier
        self.assertEqual(mem.tier, "long_term")
        worker = MemoryDecayWorker(store, _StubMemorySettings())
        result = worker.run()
        self.assertGreaterEqual(result.get("past_events_archived", 0), 1)
        updated = store.get(mem.id)
        assert updated is not None
        self.assertEqual(updated.tier, "archive")

    def test_future_plan_within_buffer_stays_future(self) -> None:
        """A plan whose event_time was 30 minutes ago is still
        considered "happening" — the 1-hour buffer keeps it as
        future_plan so retrieval reads as 'planned for ...'."""
        _, store = _store_factory()
        recent = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
        mem = store.add(
            content="ongoing event",
            kind="event",
            embedding=_emb("ongoing event"),
            temporal_type="future_plan",
            event_time=recent,
        )
        assert mem is not None
        worker = MemoryDecayWorker(store, _StubMemorySettings())
        worker.run()
        kept = store.get(mem.id)
        assert kept is not None
        self.assertEqual(kept.temporal_type, "future_plan")


# ── 6. Follow-up worker ──────────────────────────────────────────────


class TestFollowUpWorker(unittest.TestCase):
    def _setup(self) -> "tuple[MemoryStore, PreparedNudgeStore, FollowUpWorker]":
        d = tempfile.mkdtemp()
        path = Path(d) / "fu.db"
        db = ChatDatabase(path)
        store = MemoryStore(path)
        nudge_store = PreparedNudgeStore(db)
        worker = FollowUpWorker(
            memory_store=store,
            prepared_nudge_store=nudge_store,
            user_id_provider=lambda: "user-1",
            user_display_name_provider=lambda: "Jacob",
            interval_seconds=60.0,
        )
        return store, nudge_store, worker

    def test_nudge_fires_within_window(self) -> None:
        store, nudge_store, worker = self._setup()
        ev = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        mem = store.add(
            content="Jacob is going to the gym",
            kind="event",
            embedding=_emb("gym"),
            temporal_type="future_plan",
            event_time=ev,
        )
        assert mem is not None

        result = worker.run()
        self.assertEqual(result["fired"], 1)
        # Nudge persisted under the user key.
        nudge = nudge_store.get("user-1")
        self.assertIsNotNone(nudge)
        assert nudge is not None
        # Worker frames the nudge as a contingent (don't open with it)
        # callback. The user's display name + the contingent phrasing
        # are the two stable invariants.
        self.assertIn("Jacob", nudge.text)
        self.assertTrue(
            "drift" in nudge.text.lower() or "if " in nudge.text.lower(),
            f"expected contingent framing, got: {nudge.text}",
        )
        # Memory marked as fired.
        updated = store.get(mem.id)
        assert updated is not None
        self.assertIn("followup_fired_at", updated.metadata)

    def test_nudge_idempotent_across_ticks(self) -> None:
        store, nudge_store, worker = self._setup()
        ev = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        mem = store.add(
            content="Jacob has a meeting",
            kind="event",
            embedding=_emb("meeting"),
            temporal_type="future_plan",
            event_time=ev,
        )
        assert mem is not None
        first = worker.run()
        # Clear the prepared-nudge slot to simulate the user receiving
        # the line; the worker should still NOT fire again because the
        # memory is marked.
        nudge_store.delete("user-1")
        second = worker.run()
        self.assertEqual(first["fired"], 1)
        self.assertEqual(second["fired"], 0)
        self.assertGreaterEqual(second.get("skipped_already_fired", 0), 1)

    def test_nudge_skips_too_far_future(self) -> None:
        store, _, worker = self._setup()
        ev = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        store.add(
            content="far future plan",
            kind="event",
            embedding=_emb("far future"),
            temporal_type="future_plan",
            event_time=ev,
        )
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertGreaterEqual(result.get("skipped_out_of_window", 0), 1)

    def test_nudge_drops_too_far_past(self) -> None:
        store, _, worker = self._setup()
        ev = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        mem = store.add(
            content="long-gone plan",
            kind="event",
            embedding=_emb("long-gone"),
            temporal_type="future_plan",
            event_time=ev,
        )
        assert mem is not None
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        # Should have been marked so subsequent ticks skip it.
        updated = store.get(mem.id)
        assert updated is not None
        self.assertTrue(updated.metadata.get("followup_dropped"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
