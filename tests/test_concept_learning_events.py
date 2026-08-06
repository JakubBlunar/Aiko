"""L17c: the durable learning-event spine and identity continuity."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.concepts.concept_drift import DriftFinding
from app.core.concepts.concept_event_store import ConceptEvent, ConceptEventStore
from app.core.concepts.concept_learning_event_store import (
    ConceptAlias,
    ConceptLearningEventStore,
    LearningEvent,
)
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _build():
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, ConceptLearningEventStore(db), path


def _finding(**kw) -> DriftFinding:
    base = dict(
        shape="succession",
        concept_id=200,
        prior_concept_id=100,
        old_label="likes detailed answers",
        new_label="prefers depth calibrated to the topic",
        salience=0.72,
        plasticity=0.3,
        kind="identity",
        subject="user",
        confidence_delta=0.4,
        because="what looked like A turned out to be B",
        resolution="now held as B",
        decisive_event_id=1003,
        trigger_event_ids=(1001, 1003),
        evidence_refs=(("memory", "1"), ("cluster", "7")),
        detected_at=_iso(1),
        cosine=0.72,
    )
    base.update(kw)
    return DriftFinding(**base)  # type: ignore[arg-type]


class SchemaTests(unittest.TestCase):
    def test_fresh_db_is_v31_with_learning_tables(self) -> None:
        db, _store, _path = _build()
        conn = db._get_conn()
        version = conn.execute("SELECT version FROM schema_version").fetchone()
        self.assertEqual(int(version[0]), _SCHEMA_VERSION)
        self.assertGreaterEqual(_SCHEMA_VERSION, 31)
        for table in ("concept_learning_events", "concept_aliases"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            self.assertIsNotNone(row, f"missing table {table}")

    def test_reopening_an_existing_db_is_a_noop(self) -> None:
        db, store, path = _build()
        store.add(LearningEvent(fingerprint="abc", shape="loss"))
        db.close() if hasattr(db, "close") else None
        again = ChatDatabase(path)
        self.assertEqual(ConceptLearningEventStore(again).count(), 1)


class WriteAndReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.store, _path = _build()

    def test_round_trips_every_field(self) -> None:
        event = LearningEvent.from_finding(
            _finding(), evidence_labels=["a memory", "  ", "a cluster"]
        )
        self.assertGreater(self.store.add(event), 0)
        [back] = self.store.list()
        self.assertEqual(back.shape, "succession")
        self.assertEqual(back.concept_id, 200)
        self.assertEqual(back.prior_concept_id, 100)
        self.assertEqual(back.old_label, "likes detailed answers")
        self.assertEqual(back.trigger_event_ids, (1001, 1003))
        self.assertEqual(
            back.evidence_refs, (("memory", "1"), ("cluster", "7"))
        )
        # Blank evidence labels are dropped at capture time.
        self.assertEqual(back.evidence_labels, ("a memory", "a cluster"))
        self.assertAlmostEqual(back.salience, 0.72, places=4)
        self.assertAlmostEqual(back.cosine or 0.0, 0.72, places=4)

    def test_duplicate_fingerprint_is_absorbed(self) -> None:
        first = LearningEvent.from_finding(_finding())
        second = LearningEvent.from_finding(_finding())
        self.assertGreater(self.store.add(first), 0)
        self.assertEqual(self.store.add(second), 0)
        self.assertEqual(self.store.count(), 1)

    def test_add_many_reports_only_new_rows(self) -> None:
        events = [
            LearningEvent.from_finding(_finding()),
            LearningEvent.from_finding(_finding()),
            LearningEvent.from_finding(_finding(concept_id=201)),
        ]
        self.assertEqual(self.store.add_many(events), 2)

    def test_event_without_fingerprint_is_refused(self) -> None:
        self.assertEqual(self.store.add(LearningEvent(fingerprint="")), 0)
        self.assertEqual(self.store.count(), 0)

    def test_has_fingerprint(self) -> None:
        event = LearningEvent.from_finding(_finding())
        self.store.add(event)
        self.assertTrue(self.store.has_fingerprint(event.fingerprint))
        self.assertFalse(self.store.has_fingerprint("nope"))
        self.assertFalse(self.store.has_fingerprint(""))

    def test_filters(self) -> None:
        self.store.add(
            LearningEvent.from_finding(
                _finding(concept_id=1, subject="user", shape="succession",
                         salience=0.9)
            )
        )
        self.store.add(
            LearningEvent.from_finding(
                _finding(concept_id=2, subject="aiko", shape="loss",
                         salience=0.4, decisive_event_id=2)
            )
        )
        self.assertEqual(len(self.store.list(subject="aiko")), 1)
        self.assertEqual(len(self.store.list(shape="loss")), 1)
        self.assertEqual(len(self.store.list(min_salience=0.8)), 1)
        self.assertEqual(len(self.store.list()), 2)

    def test_concept_filter_matches_either_endpoint(self) -> None:
        self.store.add(
            LearningEvent.from_finding(
                _finding(concept_id=200, prior_concept_id=100)
            )
        )
        self.assertEqual(len(self.store.list(concept_id=200)), 1)
        self.assertEqual(len(self.store.list(concept_id=100)), 1)
        self.assertEqual(len(self.store.list(concept_id=999)), 0)

    def test_paging_backwards(self) -> None:
        for i in range(5):
            self.store.add(
                LearningEvent.from_finding(
                    _finding(concept_id=i, decisive_event_id=i,
                             detected_at=_iso(10 - i))
                )
            )
        page = self.store.list(limit=2)
        self.assertEqual(len(page), 2)
        older = self.store.list(limit=2, before_id=page[-1].event_id)
        self.assertEqual(len(older), 2)
        self.assertTrue(
            all(e.event_id < page[-1].event_id for e in older)
        )

    def test_history_is_oldest_first(self) -> None:
        for i, days in enumerate((30, 20, 10)):
            self.store.add(
                LearningEvent.from_finding(
                    _finding(concept_id=7, decisive_event_id=i,
                             detected_at=_iso(days))
                )
            )
        history = self.store.history_for(7)
        self.assertEqual(len(history), 3)
        self.assertEqual(
            [e.created_at for e in history],
            sorted(e.created_at for e in history),
        )

    def test_counts_by_shape_and_latest_id(self) -> None:
        self.store.add(LearningEvent.from_finding(_finding(shape="loss")))
        self.store.add(
            LearningEvent.from_finding(
                _finding(shape="loss", concept_id=9, decisive_event_id=9)
            )
        )
        self.store.add(
            LearningEvent.from_finding(
                _finding(shape="revival", concept_id=8, decisive_event_id=8)
            )
        )
        self.assertEqual(
            self.store.counts_by_shape(), {"loss": 2, "revival": 1}
        )
        self.assertEqual(self.store.latest_id(), 3)

    def test_as_dict_is_json_safe(self) -> None:
        event = LearningEvent.from_finding(
            _finding(), evidence_labels=["a memory"]
        )
        self.store.add(event)
        payload = self.store.list()[0].as_dict()
        self.assertEqual(payload["shape"], "succession")
        self.assertEqual(payload["evidence_refs"], [["memory", "1"],
                                                    ["cluster", "7"]])
        self.assertEqual(payload["trigger_event_ids"], [1001, 1003])

    def test_null_cosine_survives_the_round_trip(self) -> None:
        self.store.add(
            LearningEvent.from_finding(_finding(shape="loss", cosine=None))
        )
        self.assertIsNone(self.store.list()[0].cosine)
        self.assertIsNone(self.store.list()[0].as_dict()["cosine"])

    def test_snapshot_survives_the_concepts_it_describes(self) -> None:
        # The whole point of capturing labels at detection time: nothing
        # here reads the concepts table, so a deleted concept leaves the
        # history perfectly readable.
        self.store.add(
            LearningEvent.from_finding(
                _finding(), evidence_labels=["the evening he explained it"]
            )
        )
        [back] = self.store.list()
        self.assertEqual(back.old_label, "likes detailed answers")
        self.assertEqual(
            back.evidence_labels, ("the evening he explained it",)
        )


class AliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.store, _path = _build()

    def test_unknown_id_resolves_to_itself(self) -> None:
        self.assertEqual(self.store.resolve_alias(42), 42)

    def test_single_hop(self) -> None:
        self.store.record_alias(
            ConceptAlias(absorbed_id=1, canonical_id=2, absorbed_label="old")
        )
        self.assertEqual(self.store.resolve_alias(1), 2)

    def test_chain_is_followed_transitively(self) -> None:
        self.store.record_alias(ConceptAlias(absorbed_id=1, canonical_id=2))
        self.store.record_alias(ConceptAlias(absorbed_id=2, canonical_id=3))
        self.store.record_alias(ConceptAlias(absorbed_id=3, canonical_id=4))
        self.assertEqual(self.store.resolve_alias(1), 4)

    def test_cycle_degrades_to_a_stop(self) -> None:
        self.store.record_alias(ConceptAlias(absorbed_id=1, canonical_id=2))
        self.store.record_alias(ConceptAlias(absorbed_id=2, canonical_id=1))
        self.assertIn(self.store.resolve_alias(1), (1, 2))

    def test_self_alias_is_refused(self) -> None:
        self.assertFalse(
            self.store.record_alias(ConceptAlias(absorbed_id=5, canonical_id=5))
        )
        self.assertFalse(
            self.store.record_alias(ConceptAlias(absorbed_id=0, canonical_id=5))
        )

    def test_absorbed_into_lists_the_survivors_side(self) -> None:
        self.store.record_alias(
            ConceptAlias(absorbed_id=1, canonical_id=9, absorbed_label="a",
                         merged_at=_iso(3))
        )
        self.store.record_alias(
            ConceptAlias(absorbed_id=2, canonical_id=9, absorbed_label="b",
                         merged_at=_iso(1))
        )
        rows = self.store.absorbed_into(9)
        self.assertEqual([r.absorbed_id for r in rows], [1, 2])
        self.assertEqual([r.absorbed_label for r in rows], ["a", "b"])

    def test_history_follows_the_alias_chain(self) -> None:
        # A belief recorded under id 100, later merged into 300.
        self.store.add(
            LearningEvent.from_finding(
                _finding(concept_id=100, prior_concept_id=None,
                         shape="emergence")
            )
        )
        self.store.add(
            LearningEvent.from_finding(
                _finding(concept_id=300, prior_concept_id=None,
                         shape="relabel", decisive_event_id=77)
            )
        )
        self.store.record_alias(
            ConceptAlias(absorbed_id=100, canonical_id=300)
        )
        history = self.store.history_for(100)
        self.assertEqual(len(history), 2)

    def test_alias_chain_reports_both_ids(self) -> None:
        self.store.record_alias(ConceptAlias(absorbed_id=1, canonical_id=2))
        self.assertEqual(self.store.alias_chain(1), [1, 2])
        self.assertEqual(self.store.alias_chain(2), [2])


class MergeIntegrationTests(unittest.TestCase):
    """The store hook that makes a merge survivable."""

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.db = ChatDatabase(Path(tmp) / "test.db")
        self.learning = ConceptLearningEventStore(self.db)
        self.concepts = ConceptStore(self.db)
        self.concepts.set_alias_sink(
            lambda payload: self.learning.record_alias(
                ConceptAlias(**payload)  # type: ignore[arg-type]
            )
        )

    def _add(self, label: str) -> int:
        return self.concepts.add(
            Concept(
                label=label,
                kind="identity",
                subject="user",
                embedding=np.array([1.0, 0.0], dtype=np.float32),
            )
        )

    def test_merge_records_the_absorption(self) -> None:
        canonical = self._add("keeps things simple")
        absorbed = self._add("prefers simplicity")
        self.assertTrue(
            self.concepts.merge_into(
                canonical_id=canonical, absorbed_id=absorbed
            )
        )
        self.assertIsNone(self.concepts.get(absorbed))
        self.assertEqual(self.learning.resolve_alias(absorbed), canonical)
        [alias] = self.learning.absorbed_into(canonical)
        self.assertEqual(alias.absorbed_label, "prefers simplicity")
        self.assertEqual(alias.kind, "identity")

    def test_refused_merge_records_nothing(self) -> None:
        canonical = self._add("a")
        other = self.concepts.add(
            Concept(
                label="b",
                kind="value",
                subject="user",
                embedding=np.array([1.0, 0.0], dtype=np.float32),
            )
        )
        self.assertFalse(
            self.concepts.merge_into(canonical_id=canonical, absorbed_id=other)
        )
        self.assertEqual(self.learning.absorbed_into(canonical), [])

    def test_merge_still_works_without_a_sink(self) -> None:
        store = ConceptStore(self.db)
        canonical = store.add(
            Concept(label="x", embedding=np.array([1.0], dtype=np.float32))
        )
        absorbed = store.add(
            Concept(label="y", embedding=np.array([1.0], dtype=np.float32))
        )
        self.assertTrue(
            store.merge_into(canonical_id=canonical, absorbed_id=absorbed)
        )

    def test_a_failing_sink_never_breaks_the_merge(self) -> None:
        def boom(_payload: dict) -> None:
            raise RuntimeError("nope")

        self.concepts.set_alias_sink(boom)
        canonical = self._add("a")
        absorbed = self._add("b")
        self.assertTrue(
            self.concepts.merge_into(
                canonical_id=canonical, absorbed_id=absorbed
            )
        )


class DriftWindowTests(unittest.TestCase):
    """The two-ended trajectory read L17b needs."""

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.db = ChatDatabase(Path(tmp) / "test.db")
        self.events = ConceptEventStore(self.db)

    def _add(self, event_type: str, days_ago: float, cid: int = 1) -> int:
        return self.events.add(
            ConceptEvent(
                concept_id=cid,
                event_type=event_type,
                label="a belief",
                created_at=_iso(days_ago),
            )
        )

    def test_recent_structural_events_survive_a_wall_of_samples(self) -> None:
        self._add("discovered", 400)
        self._add("promoted", 390)
        for i in range(200):
            self._add("confidence_sample", 380 - i)
        self._add("retired", 1)

        # The plain oldest-first read is swamped by the samples.
        plain = self.events.trajectory(1, limit=100)
        self.assertNotIn("retired", [e.event_type for e in plain])

        window = self.events.drift_window(1, anchor=10, recent=20)
        types = [e.event_type for e in window]
        self.assertIn("discovered", types)
        self.assertIn("retired", types)

    def test_window_is_chronological_and_deduplicated(self) -> None:
        for i in range(5):
            self._add("confidence_sample", 10 - i)
        window = self.events.drift_window(1, anchor=10, recent=10)
        self.assertEqual(len(window), 5)
        self.assertEqual(
            [e.created_at for e in window],
            sorted(e.created_at for e in window),
        )

    def test_empty_and_unknown_concepts_are_safe(self) -> None:
        self.assertEqual(self.events.drift_window(999), [])

    def test_max_event_id_and_dirty_set(self) -> None:
        self.assertEqual(self.events.max_event_id(), 0)
        first = self._add("discovered", 10, cid=1)
        second = self._add("promoted", 5, cid=2)
        self.assertEqual(self.events.max_event_id(), second)
        self.assertEqual(
            self.events.concepts_with_events_after(first - 1), [1, 2]
        )
        self.assertEqual(self.events.concepts_with_events_after(first), [2])
        self.assertEqual(self.events.concepts_with_events_after(second), [])


if __name__ == "__main__":
    unittest.main()
