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
- ``FollowUpWorker`` drafts exactly one cue into the kv ring per
  future_plan around ``event_time``, and is idempotent across ticks.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.memory.memory_decay_worker import MemoryDecayWorker
from app.core.memory.memory_extractor import (
    MemoryExtractor,
    _build_system_prompt,
    _derive_relevance_until,
    _parse_iso,
)
from app.core.memory.memory_store import (
    VALID_TEMPORAL_TYPES,
    MemoryStore,
    _coerce_temporal_type,
)
from app.core.rag.rag_retriever import (
    RagHit,
    RagRetriever,
    _humanize_future,
    _humanize_past,
    _recorded_suffix,
    _temporal_filter_drops,
    _temporal_suffix,
)
from app.core.rag.rag_store import MemoryRecord, MessageRecord
from app.core.proactive.follow_up_worker import FollowUpWorker


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


class DeicticWriteGuardTests(unittest.TestCase):
    """K-time10 Layer 2 — the ``MemoryStore.add()`` backstop.

    ``durable`` is the default temporal type and renders with no time tag,
    so a note worded "today" reaches the prompt months later still
    claiming the present. Every long-term write funnels through ``add()``,
    which makes it the one place that covers all ~35 producers at once.
    """

    def test_relative_wording_is_reclassified(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "Jacob mowed the lawn today",
            "fact",
            _emb("lawn"),
            temporal_type="durable",
        )
        self.assertIsNotNone(mem)
        self.assertEqual(mem.temporal_type, "past_event")
        self.assertTrue(mem.event_time)

    def test_the_text_itself_is_left_alone(self) -> None:
        # Mis-tagging is recoverable; rewriting what was recorded is not.
        _, store = _store_factory()
        mem = store.add(
            "Jacob is currently between jobs",
            "fact",
            _emb("jobs"),
            temporal_type="durable",
        )
        self.assertEqual(mem.content, "Jacob is currently between jobs")

    def test_preferences_are_guarded_too(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "Jacob is really into ambient records lately",
            "preference",
            _emb("ambient"),
            temporal_type="preference",
        )
        self.assertEqual(mem.temporal_type, "past_event")

    def test_genuinely_durable_facts_are_untouched(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "Jacob is a software engineer",
            "fact",
            _emb("engineer"),
            temporal_type="durable",
        )
        self.assertEqual(mem.temporal_type, "durable")
        self.assertIsNone(mem.event_time)

    def test_an_explicit_event_time_is_respected(self) -> None:
        # The guard supplies an anchor where none exists; it does not
        # overwrite one the caller took the trouble to compute.
        _, store = _store_factory()
        stated = "2026-05-27T18:00:00+00:00"
        mem = store.add(
            "Jacob mowed the lawn today",
            "event",
            _emb("lawn 2"),
            temporal_type="durable",
            event_time=stated,
        )
        self.assertEqual(mem.event_time, stated)
        self.assertEqual(mem.temporal_type, "past_event")

    def test_other_temporal_types_are_not_downgraded(self) -> None:
        # A future_plan phrased "tomorrow" is correctly typed already --
        # flipping it to past_event would invert its meaning.
        _, store = _store_factory()
        mem = store.add(
            "Jacob has the interview tomorrow",
            "event",
            _emb("interview"),
            temporal_type="future_plan",
            event_time="2026-06-01T09:00:00+00:00",
        )
        self.assertEqual(mem.temporal_type, "future_plan")


class DeicticDirectionGuardTests(unittest.TestCase):
    """H40 — the backstop used to invert every future-pointing note.

    The guard above reads a relative word as evidence the memory
    describes something already done. Five of the eighteen words it
    matches point the other way, and for those the conclusion was
    backwards: "the courier comes tomorrow" was filed as history and
    stamped at write time. The class above even documents the inversion
    as a hazard -- it just guarded only the case where the producer had
    already labelled the row correctly, which is the case that needed no
    guarding.
    """

    def test_future_wording_becomes_a_plan_not_a_past_event(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "Jacob expects a courier with the first hardware package "
            "tomorrow morning.",
            "event",
            _emb("courier"),
            temporal_type="durable",
        )
        self.assertEqual(mem.temporal_type, "future_plan")

    def test_a_plan_with_no_stated_time_gets_no_invented_one(self) -> None:
        """"soon" does not name a moment, and guessing one is the same
        fabrication the past branch is allowed to make only because there
        the guess is true by construction."""
        _, store = _store_factory()
        mem = store.add(
            "Jacob's premium chocolate cookies will arrive soon.",
            "event",
            _emb("cookies soon"),
            temporal_type="durable",
        )
        self.assertEqual(mem.temporal_type, "future_plan")
        self.assertIsNone(mem.event_time)

    def test_a_clockless_plan_is_still_retirable(self) -> None:
        """The reclassification has to recompute the expiry it invalidated.

        ``durable`` derives no ``relevance_until``, and
        ``list_by_temporal_type`` skips rows that have none -- so without
        this the promoted row would be invisible to every upkeep pass
        that could retire it, which is worse than the mislabelling.
        """
        _, store = _store_factory()
        mem = store.add(
            "Jacob plans a candlelit wine date next week.",
            "event",
            _emb("wine date"),
            temporal_type="durable",
        )
        self.assertEqual(mem.temporal_type, "future_plan")
        self.assertTrue(mem.relevance_until)
        far_future = "2099-01-01T00:00:00+00:00"
        self.assertIn(
            mem.id,
            [
                m.id for m in store.list_by_temporal_type(
                    "future_plan", relevance_until_before=far_future,
                )
            ],
        )

    def test_past_wording_still_anchors_at_write_time(self) -> None:
        # The original behaviour, which was right for this half.
        _, store = _store_factory()
        mem = store.add(
            "Jacob mowed the lawn today",
            "fact",
            _emb("lawn today"),
            temporal_type="durable",
        )
        self.assertEqual(mem.temporal_type, "past_event")
        self.assertTrue(mem.event_time)
        self.assertTrue(mem.relevance_until)

    def test_a_promoted_past_event_is_also_retirable(self) -> None:
        # Same recompute, other branch: durable -> past_event previously
        # kept durable's NULL relevance_until and never archived either.
        _, store = _store_factory()
        mem = store.add(
            "Jacob is currently between jobs",
            "fact",
            _emb("between jobs"),
            temporal_type="durable",
        )
        self.assertEqual(mem.temporal_type, "past_event")
        self.assertTrue(mem.relevance_until)


class TemporalDirectionValidationTests(unittest.TestCase):
    """H40 — a past_event may not be dated after its own write.

    Nothing checked this before, which is how 54 plans came to sit in
    ``past_event`` carrying an ``event_time`` in their own future while
    only 17 rows in 2,095 ever reached ``future_plan``. The two fields
    disagree about whether the thing has happened; ``event_time`` is the
    more specific claim and the label is the field producers get wrong.
    """

    def test_an_event_time_ahead_of_the_write_wins(self) -> None:
        _, store = _store_factory()
        ahead = (timephrase.utcnow() + timedelta(hours=9)).isoformat()
        mem = store.add(
            "Jacob will assemble the workstation after work",
            "event",
            _emb("assemble"),
            temporal_type="past_event",
            event_time=ahead,
        )
        self.assertEqual(mem.temporal_type, "future_plan")
        self.assertEqual(mem.event_time, ahead)

    def test_a_genuine_past_event_is_untouched(self) -> None:
        _, store = _store_factory()
        behind = (timephrase.utcnow() - timedelta(days=2)).isoformat()
        mem = store.add(
            "Jacob finished the dashboard",
            "event",
            _emb("dashboard"),
            temporal_type="past_event",
            event_time=behind,
        )
        self.assertEqual(mem.temporal_type, "past_event")

    def test_it_compares_against_the_write_not_against_now(self) -> None:
        """A row recorded after its event is history even if the clock has
        since moved: the comparison is write-time vs event-time, so
        replaying an old row cannot re-decide it."""
        _, store = _store_factory()
        behind = (timephrase.utcnow() - timedelta(minutes=1)).isoformat()
        mem = store.add(
            "Jacob got the delivery",
            "event",
            _emb("delivery got"),
            temporal_type="past_event",
            event_time=behind,
        )
        self.assertEqual(mem.temporal_type, "past_event")

    def test_a_past_event_with_no_event_time_is_left_alone(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "Jacob went to the coast",
            "event",
            _emb("coast"),
            temporal_type="past_event",
        )
        self.assertEqual(mem.temporal_type, "past_event")
        self.assertIsNone(mem.event_time)


class DeliveryRegressionTests(unittest.TestCase):
    """H40 end to end, on the sequence that surfaced it.

    Aiko asked about a hardware delivery a day after helping unpack it.
    The rows she was reading are reproduced here as their producers wrote
    them, and the assertion is on what the retrieval bullet says, since
    that is the only part the model ever sees.
    """

    def _bullet(self, mem, now) -> str:
        return _temporal_suffix(
            temporal_type=mem.temporal_type,
            event_time=mem.event_time,
            created_at=mem.created_at,
            now=now,
        )

    def test_the_courier_row_reads_as_upcoming_not_as_done(self) -> None:
        _, store = _store_factory()
        # Written just after midnight, about a courier due that morning.
        due = timephrase.utcnow() + timedelta(hours=9)
        mem = store.add(
            "Jacob expects a courier with the first hardware package "
            "tomorrow morning.",
            "event",
            _emb("courier regression"),
            temporal_type="durable",
            event_time=due.isoformat(),
        )
        # Before H40 this was a past_event tagged "(moments ago)", which
        # reads as "he just said the courier is coming".
        self.assertEqual(mem.temporal_type, "future_plan")
        bullet = self._bullet(mem, timephrase.utcnow())
        self.assertIn("planned for", bullet)
        self.assertNotIn("ago", bullet)

    def test_once_the_courier_has_been_it_reads_as_overdue(self) -> None:
        """The plan lane has upkeep, which is the point of being in it.

        Rendered after ``event_time`` passes, the same row says so out
        loud instead of claiming freshness -- and the decay worker will
        demote it within the hour.
        """
        _, store = _store_factory()
        due = timephrase.utcnow() + timedelta(hours=2)
        mem = store.add(
            "Jacob expects a courier with the first hardware package.",
            "event",
            _emb("courier overdue"),
            temporal_type="future_plan",
            event_time=due.isoformat(),
        )
        later = timephrase.utcnow() + timedelta(hours=5)
        bullet = self._bullet(mem, later)
        self.assertIn("should be done by now", bullet)

    def test_a_same_day_past_event_is_not_flattened_to_just_now(self) -> None:
        """The other row in the pile: written at 02:48, stamped noon.

        The extractor's "a bare date means noon local" rule put the
        timestamp nine hours ahead of the write, so a build that had
        genuinely happened rendered as "(moments ago)" all morning.
        The store now reads that disagreement as a plan, and where a row
        already carries one, the suffix anchors on the write instead.
        """
        _, store = _store_factory()
        wrote_at = timephrase.utcnow() - timedelta(hours=3)
        bullet = _temporal_suffix(
            temporal_type="past_event",
            event_time=(timephrase.utcnow() + timedelta(hours=9)).isoformat(),
            created_at=wrote_at.isoformat(),
            now=timephrase.utcnow(),
        )
        self.assertEqual(bullet, " (3 hours ago)")

    def test_the_pile_no_longer_reads_as_all_equally_fresh(self) -> None:
        """The compounding failure: four rows from two days, one timestamp.

        Every one of them rendered "(moments ago)", so nothing in the
        prompt distinguished a plan from its outcome and the most recent
        thing Aiko could see was a courier that had not arrived yet.
        """
        now = timephrase.utcnow()
        rows = [
            # a plan, written before the event
            ("future_plan", now + timedelta(hours=6), now - timedelta(hours=30)),
            # the same thing after it happened
            ("past_event", now - timedelta(hours=20), now - timedelta(hours=20)),
            # a note written now about it
            ("past_event", now - timedelta(minutes=5), now - timedelta(minutes=5)),
        ]
        tags = [
            _temporal_suffix(
                temporal_type=t,
                event_time=ev.isoformat(),
                created_at=made.isoformat(),
                now=now,
            )
            for t, ev, made in rows
        ]
        self.assertEqual(len(set(tags)), len(tags), f"collapsed: {tags}")
        self.assertIn("planned for", tags[0])
        self.assertIn("ago", tags[1])
        self.assertIn("ago", tags[2])


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
        #
        # Anchor to local noon: ``_humanize_future`` buckets by LOCAL
        # calendar-day difference, so running the test late in the
        # evening would push ``now + 1 day + 2 h`` across two midnights
        # and read as "on <weekday>" instead of "tomorrow". Noon keeps
        # the +2h / +26h offsets inside today / tomorrow deterministically.
        now = (
            datetime.now()
            .astimezone()
            .replace(hour=12, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
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

    # ── K-time10: the recorded-at fallback + snippet stamps ───────────

    @staticmethod
    def _durable_hit(content: str, created_at: str) -> RagHit:
        return RagHit(
            source="memory",
            score=0.9,
            record=MemoryRecord(
                id="1",
                content=content,
                kind="fact",
                salience=0.5,
                source_session="s1",
                source_message_id=None,
                created_at=created_at,
                last_used_at=None,
                use_count=0,
            ),
            temporal_type="durable",
            event_time=None,
        )

    def test_recorded_suffix_only_past_the_threshold(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(
            _recorded_suffix((now - timedelta(hours=3)).isoformat(), now), "",
        )
        self.assertEqual(
            _recorded_suffix((now - timedelta(days=5)).isoformat(), now),
            " (noted 5 days ago)",
        )

    def test_recorded_suffix_tolerates_a_bad_timestamp(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(_recorded_suffix("not-a-date", now), "")
        self.assertEqual(_recorded_suffix(None, now), "")

    def test_untagged_memory_gets_a_noted_tag(self) -> None:
        # A durable row is untagged by ``_temporal_suffix``; without the
        # fallback the persona reads it as present-tense.
        now = datetime.now(timezone.utc)
        block = RagRetriever.format_block(
            [
                self._durable_hit(
                    "Jacob mowed the lawn today",
                    (now - timedelta(days=40)).isoformat(),
                )
            ],
            user_display_name="Jacob",
        )
        self.assertIn("(noted ", block)
        self.assertIn("month", block)

    def test_fresh_untagged_memory_stays_bare(self) -> None:
        now = datetime.now(timezone.utc)
        block = RagRetriever.format_block(
            [
                self._durable_hit(
                    "Jacob is a software engineer",
                    (now - timedelta(hours=2)).isoformat(),
                )
            ],
            user_display_name="Jacob",
        )
        self.assertIn("- Jacob is a software engineer", block)
        self.assertNotIn("(noted", block)

    def test_noted_tag_never_overrides_a_real_tense_tag(self) -> None:
        # "(3 days ago)" asserts when the event happened and must win over
        # "(noted ...)", which only says when it was written down.
        now = datetime.now(timezone.utc)
        hit = self._durable_hit(
            "Jacob shipped the release",
            (now - timedelta(days=40)).isoformat(),
        )
        hit.temporal_type = "past_event"
        hit.event_time = (now - timedelta(days=3)).isoformat()
        block = RagRetriever.format_block([hit], user_display_name="Jacob")
        self.assertIn("(3 days ago)", block)
        self.assertNotIn("(noted", block)

    def test_message_snippets_carry_a_timestamp(self) -> None:
        now = datetime.now(timezone.utc)
        hit = RagHit(
            source="message",
            score=0.9,
            record=MessageRecord(
                id="s1:4",
                session_id="s1",
                message_id=4,
                role="user",
                content="I finally booked the flights",
                created_at=(now - timedelta(days=3)).isoformat(),
            ),
        )
        block = RagRetriever.format_block([hit], user_display_name="Jacob")
        self.assertRegex(block, r"- \[[^\]]+\] Jacob said: ")

    def test_block_leads_with_the_date_anchor(self) -> None:
        # T3 is assembled before the T4 "right now it's ..." line, so the
        # recall block has to carry its own anchor or the ages below it
        # have nothing to be relative to.
        now = datetime.now(timezone.utc)
        block = RagRetriever.format_block(
            [self._durable_hit("Jacob likes ramen", now.isoformat())],
            user_display_name="Jacob",
        )
        self.assertTrue(block.startswith("(Today is "))

    def test_empty_hits_render_no_anchor(self) -> None:
        self.assertEqual(RagRetriever.format_block([]), "")

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

    def test_temporal_filter_drops_retired_promises(self) -> None:
        # H41: promises are written ``durable`` with a NULL
        # relevance_until, so the rule above could never reach them -- the
        # guard against "asking about progress on something that already
        # finished" was structurally blind to the rows most likely to
        # cause it, and rows nearly three months old were still scoring
        # into the block.
        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

        class _Promise:
            kind = "promise"
            temporal_type = "durable"
            relevance_until = None
            content = "Jacob promised: water the plants"

            def __init__(self, status: str) -> None:
                self.metadata = {"promise_status": status}

        self.assertTrue(_temporal_filter_drops(_Promise("dropped"), now))
        self.assertFalse(_temporal_filter_drops(_Promise("open"), now))
        self.assertFalse(_temporal_filter_drops(_Promise("surfaced"), now))
        # Fulfilled deliberately stays: it happened, which makes it
        # ordinary shared history. It is the ones that quietly expired
        # unkept that read as inattentive when raised.
        self.assertFalse(_temporal_filter_drops(_Promise("fulfilled"), now))


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

    def test_clockless_plan_retires_on_expired_relevance(self) -> None:
        """A plan nobody pinned to a clock still has to expire.

        "next week" / "soon" / "in the near future" produce a
        ``future_plan`` with no ``event_time``, which the event-time
        sweep cannot see. Before this, such rows were immortal: the
        live database had nine of them, the oldest 61 days past its
        ``relevance_until``, still being offered as things to ask
        about as though they were ahead.
        """
        _, store = _store_factory()
        mem = store.add(
            content="Jacob will schedule another date some free evening",
            kind="event",
            embedding=_emb("another date"),
            temporal_type="future_plan",
            relevance_until=(
                datetime.now(timezone.utc) - timedelta(days=38)
            ).isoformat(),
        )
        assert mem is not None
        self.assertIsNone(mem.event_time)

        result = MemoryDecayWorker(store, _StubMemorySettings()).run()

        self.assertGreaterEqual(result.get("future_plans_to_past", 0), 1)
        updated = store.get(mem.id)
        assert updated is not None
        self.assertEqual(updated.temporal_type, "past_event")

    def test_a_long_dead_plan_does_not_come_back_as_fresh(self) -> None:
        """The retrospective window anchors on when it went stale.

        Anchoring on ``now`` would hand a plan that expired weeks ago
        seven days of renewed relevance — the opposite of the point.
        """
        _, store = _store_factory()
        stale_at = datetime.now(timezone.utc) - timedelta(days=38)
        mem = store.add(
            content="Jacob plans to introduce Aiko to his friends",
            kind="event",
            embedding=_emb("friends"),
            temporal_type="future_plan",
            relevance_until=stale_at.isoformat(),
        )
        assert mem is not None

        MemoryDecayWorker(store, _StubMemorySettings()).run()

        updated = store.get(mem.id)
        assert updated is not None
        assert updated.relevance_until is not None
        self.assertEqual(
            (
                datetime.fromisoformat(updated.relevance_until) - stale_at
            ).days,
            7,
        )
        # Already past, so it belongs in the archive, not back in RAG.
        self.assertLess(
            datetime.fromisoformat(updated.relevance_until),
            datetime.now(timezone.utc),
        )

    def test_a_still_live_clockless_plan_stays_future(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            content="Jacob plans a quiet date with cookies",
            kind="event",
            embedding=_emb("cookies"),
            temporal_type="future_plan",
            relevance_until=(
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
        )
        assert mem is not None
        MemoryDecayWorker(store, _StubMemorySettings()).run()
        kept = store.get(mem.id)
        assert kept is not None
        self.assertEqual(kept.temporal_type, "future_plan")

    def test_a_recently_expired_clockless_plan_gets_its_grace(self) -> None:
        """"Next week" is not dead a day later.

        For a clockless plan ``relevance_until`` is ``created_at + 1
        day`` — a retrieval window, not a deadline. Retiring on it
        directly would make a vague plan unaskable almost immediately,
        which is the opposite failure to the one being fixed.
        """
        _, store = _store_factory()
        mem = store.add(
            content="Jacob will bring cookies next week",
            kind="event",
            embedding=_emb("cookies next week"),
            temporal_type="future_plan",
            relevance_until=(
                datetime.now(timezone.utc) - timedelta(days=3)
            ).isoformat(),
        )
        assert mem is not None
        MemoryDecayWorker(store, _StubMemorySettings()).run()
        kept = store.get(mem.id)
        assert kept is not None
        self.assertEqual(kept.temporal_type, "future_plan")

    def test_event_time_still_wins_the_anchor(self) -> None:
        """A row matching both sweeps is retired once, on its event_time."""
        _, store = _store_factory()
        event_at = datetime.now(timezone.utc) - timedelta(days=21)
        mem = store.add(
            content="the gym session",
            kind="event",
            embedding=_emb("gym"),
            temporal_type="future_plan",
            event_time=event_at.isoformat(),
            relevance_until=(event_at + timedelta(days=1)).isoformat(),
        )
        assert mem is not None
        result = MemoryDecayWorker(store, _StubMemorySettings()).run()
        # Counted once, not twice, despite matching both sweeps.
        self.assertEqual(result.get("future_plans_to_past", 0), 1)
        updated = store.get(mem.id)
        assert updated is not None
        assert updated.relevance_until is not None
        self.assertEqual(
            (
                datetime.fromisoformat(updated.relevance_until) - event_at
            ).days,
            7,
        )

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
    """FollowUpWorker is a silent *cue* producer (the K34 pattern).

    It no longer writes a verbatim line into the prepared-nudge slot
    (that leaked the internal directive into chat). It drafts a hint into
    the ``aiko.follow_up_cues`` kv ring; the ``_render_follow_up_block``
    provider surfaces it and Aiko phrases the check-in herself.
    """

    def _setup(self) -> "tuple[MemoryStore, ChatDatabase, FollowUpWorker]":
        d = tempfile.mkdtemp()
        path = Path(d) / "fu.db"
        db = ChatDatabase(path)
        store = MemoryStore(path)
        worker = FollowUpWorker(
            memory_store=store,
            kv_get=db.kv_get,
            kv_set=db.kv_set,
            user_id_provider=lambda: "user-1",
            user_display_name_provider=lambda: "Jacob",
            ollama=None,  # deterministic: no LLM question drafting
            interval_seconds=60.0,
        )
        return store, db, worker

    def test_cue_drafted_within_window(self) -> None:
        from app.core.proactive.follow_up_worker import load_follow_up_cues

        store, db, worker = self._setup()
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
        # A cue (NOT a verbatim line) lands in the kv ring.
        ring = load_follow_up_cues(db.kv_get)
        self.assertEqual(len(ring), 1)
        cue = ring[0]
        self.assertEqual(cue["source_id"], str(mem.id))
        plan = cue["plan"].lower()
        # Third-person memory reshaped to a second-person plan summary.
        self.assertIn("you", plan)
        self.assertNotIn("jacob", plan)
        # No LLM client -> no scripted question.
        self.assertEqual(cue.get("question", ""), "")
        # Memory marked as fired.
        updated = store.get(mem.id)
        assert updated is not None
        self.assertIn("followup_fired_at", updated.metadata)

    def test_plan_summary_reshapes_reported_case(self) -> None:
        # The exact memory that leaked the directive into chat.
        from app.core.proactive.follow_up_worker import _plan_summary

        plan = _plan_summary(
            "Jacob plans to take a bath and watch anime later this evening.",
            "Jacob",
        )
        self.assertEqual(
            plan,
            "you were planning to take a bath and watch anime later this "
            "evening",
        )
        self.assertNotIn("Jacob", plan)

    def test_plan_summary_handles_noun_phrase_plan(self) -> None:
        from app.core.proactive.follow_up_worker import _plan_summary

        plan = _plan_summary("Jacob has a dentist appointment", "Jacob")
        self.assertEqual(plan, "you had a dentist appointment")

    def test_plan_summary_falls_back_when_unparseable(self) -> None:
        from app.core.proactive.follow_up_worker import _plan_summary

        plan = _plan_summary("the big launch", "Jacob")
        # Still a usable summary snippet, never empty.
        self.assertIn("the big launch", plan)

    def test_cue_idempotent_across_ticks(self) -> None:
        store, db, worker = self._setup()
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
        # The memory is marked fired, so a second tick does NOT re-draft.
        second = worker.run()
        self.assertEqual(first["fired"], 1)
        self.assertEqual(second["fired"], 0)
        self.assertGreaterEqual(second.get("skipped_already_fired", 0), 1)

    def test_disabled_short_circuits(self) -> None:
        from app.core.proactive.follow_up_worker import load_follow_up_cues

        store, db, worker = self._setup()
        worker._enabled_provider = lambda: False
        ev = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        store.add(
            content="Jacob is going to the gym",
            kind="event",
            embedding=_emb("gym"),
            temporal_type="future_plan",
            event_time=ev,
        )
        result = worker.run()
        self.assertTrue(result.get("disabled"))
        self.assertEqual(load_follow_up_cues(db.kv_get), [])

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

    # ── demand ──────────────────────────────────────────────────────

    def _add_plan(self, store: MemoryStore, content: str, offset: timedelta):
        return store.add(
            content=content,
            kind="event",
            embedding=_emb(content),
            temporal_type="future_plan",
            event_time=(datetime.now(timezone.utc) + offset).isoformat(),
        )

    def test_demand_zero_with_no_plans(self) -> None:
        _store, _db, worker = self._setup()
        signal = worker.demand(now=datetime.now(timezone.utc), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)

    def test_demand_rises_with_plans_inside_the_window(self) -> None:
        store, _db, worker = self._setup()
        self._add_plan(store, "Jacob is going to the gym", timedelta(minutes=-5))
        now = datetime.now(timezone.utc)
        one = worker.demand(now=now, last_run_at=None)
        self.assertGreater(one.pressure, 0.0)

        self._add_plan(store, "Jacob has a dentist appointment",
                       timedelta(minutes=-10))
        self._add_plan(store, "Jacob is meeting Sam for coffee",
                       timedelta(minutes=-15))
        many = worker.demand(now=now, last_run_at=None)
        self.assertGreater(many.pressure, one.pressure)

    def test_demand_ignores_plans_outside_the_window(self) -> None:
        store, _db, worker = self._setup()
        self._add_plan(store, "far future plan", timedelta(hours=4))
        signal = worker.demand(now=datetime.now(timezone.utc), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)

    def test_stale_plans_are_ready_but_not_pressure(self) -> None:
        """Retiring a dropped plan is bookkeeping with no deadline."""
        store, _db, worker = self._setup()
        self._add_plan(store, "long-gone plan", timedelta(hours=-10))
        now = datetime.now(timezone.utc)
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertIn("retire", signal.reason)

    def test_demand_never_fires_or_marks_a_plan(self) -> None:
        from app.core.proactive.follow_up_worker import load_follow_up_cues

        store, db, worker = self._setup()
        mem = self._add_plan(
            store, "Jacob is going to the gym", timedelta(minutes=-5),
        )
        assert mem is not None
        now = datetime.now(timezone.utc)
        worker.demand(now=now, last_run_at=None)
        worker.is_ready(now=now, last_run_at=None)
        self.assertEqual(load_follow_up_cues(db.kv_get), [])
        updated = store.get(mem.id)
        assert updated is not None
        self.assertNotIn("followup_fired_at", updated.metadata)


class FollowUpPlanSummaryTests(unittest.TestCase):
    """K-time10 — the cue quotes the plan memory's own wording.

    A follow-up fires *after* the event, which is precisely when a plan
    recorded as "dinner with Sam tonight" would produce a cue saying
    "tonight" about an evening that has already been and gone.
    """

    def test_a_deictic_resolves_against_the_note(self) -> None:
        from app.core.proactive.follow_up_worker import _plan_summary

        written = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        out = _plan_summary("Jacob is having dinner with Sam tonight", "Jacob", written)
        self.assertNotIn("tonight", out)
        self.assertIn("that evening", out)

    def test_no_timestamp_leaves_the_wording_alone(self) -> None:
        from app.core.proactive.follow_up_worker import _plan_summary

        out = _plan_summary("Jacob is going to the gym", "Jacob")
        self.assertIn("gym", out)

    def test_the_second_person_rewrite_still_applies(self) -> None:
        from app.core.proactive.follow_up_worker import _plan_summary

        out = _plan_summary("Jacob is going to the gym", "Jacob")
        self.assertTrue(out.startswith("you "), out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
