"""Tests for the append-only concept discovery timeline.

Covers :class:`ConceptEventStore` (add / newest-first ordering /
``before_id`` paging / subject + event_type filters) and the v21->v22
migration backfill (one ``discovered`` row per pre-existing concept,
idempotent on re-run, and left untouched when a concept is later
deleted).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts.concept_event_store import ConceptEvent, ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.infra.chat_database import ChatDatabase


def _db() -> ChatDatabase:
    tmp = tempfile.mkdtemp()
    return ChatDatabase(Path(tmp) / "test.db")


def _event(**kw) -> ConceptEvent:
    base = dict(
        event_type="discovered",
        kind="identity",
        subject="user",
        label="Jacob values understanding systems",
        confidence=0.8,
        novelty=0.9,
        evidence_count=3,
        distinct_source_count=3,
        source_kinds="cluster",
        reason="First abstraction connecting 3 topic clusters.",
        concept_id=42,
    )
    base.update(kw)
    return ConceptEvent(**base)


class EventStoreCrudTests(unittest.TestCase):
    def test_add_returns_id_and_counts(self) -> None:
        store = ConceptEventStore(_db())
        self.assertEqual(store.count(), 0)
        eid = store.add(_event())
        self.assertGreater(eid, 0)
        self.assertEqual(store.count(), 1)

    def test_list_is_newest_first(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(label="first", created_at="2026-01-01T00:00:00+00:00"))
        store.add(_event(label="second", created_at="2026-02-01T00:00:00+00:00"))
        store.add(_event(label="third", created_at="2026-03-01T00:00:00+00:00"))
        got = [e.label for e in store.list(limit=10)]
        self.assertEqual(got, ["third", "second", "first"])

    def test_before_id_pages_backwards(self) -> None:
        store = ConceptEventStore(_db())
        ids = [
            store.add(_event(label=f"e{i}", created_at=f"2026-01-0{i+1}T00:00:00+00:00"))
            for i in range(3)
        ]
        # Newest-first is e2, e1, e0. Page after the oldest-shown id.
        first_page = store.list(limit=1)
        self.assertEqual(first_page[0].label, "e2")
        next_page = store.list(limit=10, before_id=first_page[0].event_id)
        self.assertEqual([e.label for e in next_page], ["e1", "e0"])
        self.assertNotIn(ids[2], [e.event_id for e in next_page])

    def test_subject_and_type_filters(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(subject="user"))
        store.add(_event(subject="aiko"))
        store.add(_event(subject="aiko", event_type="reinforced"))
        self.assertEqual(len(store.list(subject="aiko")), 2)
        self.assertEqual(len(store.list(subject="user")), 1)
        self.assertEqual(len(store.list(event_type="reinforced")), 1)
        self.assertEqual(
            len(store.list(subject="aiko", event_type="discovered")), 1
        )

    def test_nullable_concept_id_roundtrips(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(concept_id=None))
        got = store.list(limit=1)[0]
        self.assertIsNone(got.concept_id)

    def test_concept_id_filter(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(concept_id=1))
        store.add(_event(concept_id=2))
        store.add(_event(concept_id=2, event_type="promoted"))
        self.assertEqual(len(store.list(concept_id=2)), 2)
        self.assertEqual(len(store.list(concept_id=1)), 1)
        self.assertEqual(len(store.list(concept_id=99)), 0)
        # Composes with the other filters rather than replacing them.
        self.assertEqual(
            len(store.list(concept_id=2, event_type="promoted")), 1
        )


class TrajectoryTests(unittest.TestCase):
    """L17a: one concept's path, read forwards."""

    def test_trajectory_is_oldest_first_and_scoped(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(concept_id=7, label="a", created_at="2026-01-01T00:00:00+00:00"))
        store.add(_event(concept_id=9, label="other", created_at="2026-01-02T00:00:00+00:00"))
        store.add(_event(concept_id=7, label="b", created_at="2026-02-01T00:00:00+00:00"))
        store.add(_event(concept_id=7, label="c", created_at="2026-03-01T00:00:00+00:00"))
        got = store.trajectory(7)
        self.assertEqual([e.label for e in got], ["a", "b", "c"])

    def test_trajectory_limit_keeps_the_oldest_rows(self) -> None:
        """A trajectory is read from its start, so truncation drops the
        recent end -- the inverse of ``list``'s newest-first paging."""
        store = ConceptEventStore(_db())
        for i in range(5):
            store.add(
                _event(
                    concept_id=7,
                    label=f"e{i}",
                    created_at=f"2026-01-0{i + 1}T00:00:00+00:00",
                )
            )
        got = store.trajectory(7, limit=2)
        self.assertEqual([e.label for e in got], ["e0", "e1"])

    def test_trajectory_of_unknown_concept_is_empty(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(concept_id=1))
        self.assertEqual(store.trajectory(404), [])

    def test_trajectory_carries_confidence_and_reason(self) -> None:
        store = ConceptEventStore(_db())
        store.add(
            _event(
                concept_id=7,
                event_type="confidence_sample",
                confidence=0.41,
                reason="Confidence slid to 0.41 from 0.62, no status change.",
            )
        )
        point = store.trajectory(7)[0]
        self.assertEqual(point.confidence, 0.41)
        self.assertEqual(point.event_type, "confidence_sample")
        self.assertIn("0.62", point.reason)


class LatestConfidenceTests(unittest.TestCase):
    def test_returns_newest_row_per_concept(self) -> None:
        store = ConceptEventStore(_db())
        store.add(_event(concept_id=1, confidence=0.5))
        store.add(_event(concept_id=1, confidence=0.7))
        store.add(_event(concept_id=2, confidence=0.3))
        self.assertEqual(
            store.latest_confidence([1, 2]), {1: 0.7, 2: 0.3}
        )

    def test_unknown_ids_are_absent_not_zero(self) -> None:
        """Callers distinguish 'never recorded' from 'recorded at 0.0'."""
        store = ConceptEventStore(_db())
        store.add(_event(concept_id=1, confidence=0.0))
        got = store.latest_confidence([1, 42])
        self.assertEqual(got, {1: 0.0})

    def test_empty_input_skips_the_query(self) -> None:
        self.assertEqual(ConceptEventStore(_db()).latest_confidence([]), {})


class BackfillMigrationTests(unittest.TestCase):
    """The v21->v22 upgrade seeds one discovered event per existing
    concept. We simulate an upgrade by writing a v21 concept row, forcing
    the schema_version back to 21, and re-opening the db."""

    def _make_v21_db_with_concept(self) -> Path:
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "test.db"
        db = ChatDatabase(path)
        store = ConceptStore(db)
        store.add(
            Concept(
                label="Jacob bribes Aiko with cookies",
                kind="identity",
                subject="user",
                embedding=np.zeros(0, dtype=np.float32),
                status="candidate",
                confidence=0.78,
                evidence_count=3,
                distinct_source_count=3,
                first_evidence_at="2026-07-03T21:18:00+00:00",
                created_at="2026-07-03T21:18:00+00:00",
            )
        )
        # Pretend the timeline never existed: clear events + roll version
        # back so the next open runs the v22 migration.
        conn = db._get_conn()  # type: ignore[attr-defined]
        conn.execute("DELETE FROM concept_events")
        conn.execute("UPDATE schema_version SET version = 21")
        conn.commit()
        return path

    def test_backfill_seeds_one_event_per_concept(self) -> None:
        path = self._make_v21_db_with_concept()
        db = ChatDatabase(path)  # re-open -> migration runs
        events = ConceptEventStore(db).list(limit=10)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.event_type, "discovered")
        self.assertEqual(e.subject, "user")
        self.assertIn("cookies", e.label)
        self.assertEqual(e.confidence, 0.78)
        self.assertEqual(e.reason, "backfilled from existing concept")
        self.assertEqual(e.created_at, "2026-07-03T21:18:00+00:00")

    def test_backfill_is_idempotent(self) -> None:
        path = self._make_v21_db_with_concept()
        ChatDatabase(path)  # first upgrade: backfills
        # Roll version back WITHOUT clearing events, re-open: guard on the
        # non-empty table means no second insert.
        db2 = ChatDatabase(path)
        conn = db2._get_conn()  # type: ignore[attr-defined]
        conn.execute("UPDATE schema_version SET version = 21")
        conn.commit()
        db3 = ChatDatabase(path)
        self.assertEqual(ConceptEventStore(db3).count(), 1)

    def test_deleting_concept_keeps_its_event(self) -> None:
        path = self._make_v21_db_with_concept()
        db = ChatDatabase(path)
        store = ConceptStore(db)
        store.load_all()
        cid = store.all()[0].concept_id
        store.delete(cid)
        self.assertEqual(store.count(), 0)
        # The discovery event survives -- the whole point of the timeline.
        self.assertEqual(ConceptEventStore(db).count(), 1)


if __name__ == "__main__":
    unittest.main()
