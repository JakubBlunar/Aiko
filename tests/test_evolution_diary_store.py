"""L17f: the evolution diary store and the forward learning-event cursor."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.concepts.concept_learning_event_store import (
    ConceptLearningEventStore,
    LearningEvent,
)
from app.core.concepts.evolution_diary_store import (
    DiaryEntry,
    EvolutionDiaryStore,
)
from app.core.infra.chat_database import _SCHEMA_VERSION, ChatDatabase


def _db() -> ChatDatabase:
    return ChatDatabase(Path(tempfile.mkdtemp()) / "test.db")


def _entry(**kw) -> DiaryEntry:
    base = dict(
        entry="This week I stopped hedging about what you actually want.",
        period_start="2026-08-01T00:00:00+00:00",
        period_end="2026-08-07T00:00:00+00:00",
        event_watermark=12,
        learning_event_ids=(3, 7, 12),
        concept_ids=(41, 55),
        shape_counts={"emergence": 2, "loss": 1},
        salience_max=0.62,
    )
    base.update(kw)
    return DiaryEntry(**base)  # type: ignore[arg-type]


class SchemaTests(unittest.TestCase):
    def test_fresh_db_carries_the_diary_table(self) -> None:
        self.assertGreaterEqual(_SCHEMA_VERSION, 32)
        conn = _db()._get_conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("evolution_diary",),
        ).fetchone()
        self.assertIsNotNone(row)
        version = conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        self.assertEqual(int(version[0]), _SCHEMA_VERSION)

    def test_upgrading_from_v31_adds_the_table(self) -> None:
        path = Path(tempfile.mkdtemp()) / "legacy.db"
        db = ChatDatabase(path)
        conn = db._get_conn()
        conn.execute("DROP TABLE evolution_diary")
        conn.execute("UPDATE schema_version SET version = 31")
        conn.commit()

        again = ChatDatabase(path)
        store = EvolutionDiaryStore(again)
        self.assertGreater(store.add(_entry()), 0)
        version = again._get_conn().execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        self.assertEqual(int(version), _SCHEMA_VERSION)


class DiaryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EvolutionDiaryStore(_db())

    def test_round_trips_every_field(self) -> None:
        eid = self.store.add(_entry())
        self.assertGreater(eid, 0)
        [got] = self.store.list()
        self.assertEqual(got.entry_id, eid)
        self.assertEqual(got.learning_event_ids, (3, 7, 12))
        self.assertEqual(got.concept_ids, (41, 55))
        self.assertEqual(got.shape_counts, {"emergence": 2, "loss": 1})
        self.assertAlmostEqual(got.salience_max, 0.62, places=4)
        self.assertEqual(got.event_watermark, 12)

    def test_a_blank_entry_is_refused(self) -> None:
        # A placeholder row would read as "a period she had nothing to say
        # about", which is the filler the skip rule exists to prevent.
        self.assertEqual(self.store.add(_entry(entry="   ")), 0)
        self.assertEqual(self.store.count(), 0)

    def test_entries_come_back_newest_first(self) -> None:
        self.store.add(_entry(entry="the older one", event_watermark=4))
        self.store.add(_entry(entry="the newer one", event_watermark=9))
        self.assertEqual(
            [e.entry for e in self.store.list()],
            ["the newer one", "the older one"],
        )

    def test_before_id_pages_backwards(self) -> None:
        first = self.store.add(_entry(entry="one"))
        self.store.add(_entry(entry="two"))
        [older] = self.store.list(before_id=first + 1)
        self.assertEqual(older.entry, "one")

    def test_watermark_is_the_high_water_mark(self) -> None:
        self.assertEqual(self.store.latest_watermark(), 0)
        self.store.add(_entry(event_watermark=30))
        # An out-of-order entry must not rewind the resume point.
        self.store.add(_entry(event_watermark=11))
        self.assertEqual(self.store.latest_watermark(), 30)

    def test_latest_returns_the_newest_entry(self) -> None:
        self.assertIsNone(self.store.latest())
        self.store.add(_entry(entry="first"))
        self.store.add(_entry(entry="second"))
        latest = self.store.latest()
        assert latest is not None
        self.assertEqual(latest.entry, "second")

    def test_cited_concept_ids_are_deduplicated(self) -> None:
        self.store.add(_entry(concept_ids=(1, 2)))
        self.store.add(_entry(concept_ids=(2, 3)))
        self.assertEqual(sorted(self.store.cited_concept_ids()), [1, 2, 3])

    def test_entries_since_filters_by_watermark(self) -> None:
        self.store.add(_entry(entry="old", event_watermark=5))
        self.store.add(_entry(entry="new", event_watermark=25))
        self.assertEqual(
            [e.entry for e in self.store.entries_since(10)], ["new"]
        )

    def test_as_dict_carries_the_provenance(self) -> None:
        self.store.add(_entry())
        payload = self.store.list()[0].as_dict()
        self.assertEqual(payload["learning_event_ids"], [3, 7, 12])
        self.assertEqual(payload["concept_ids"], [41, 55])
        self.assertEqual(payload["shape_counts"], {"emergence": 2, "loss": 1})


class ForwardCursorTests(unittest.TestCase):
    """``after_id`` is what every periodic consumer resumes from."""

    def setUp(self) -> None:
        self.store = ConceptLearningEventStore(_db())
        self.ids = [
            self.store.add(
                LearningEvent(
                    shape="emergence",
                    concept_id=i,
                    new_label=f"belief {i}",
                    fingerprint=f"fp-{i}",
                    salience=0.5,
                    created_at=f"2026-08-0{i + 1}T00:00:00+00:00",
                )
            )
            for i in range(3)
        ]

    def test_after_id_returns_only_the_unseen(self) -> None:
        rows = self.store.list(after_id=self.ids[0])
        self.assertEqual(
            sorted(e.event_id for e in rows), sorted(self.ids[1:])
        )

    def test_the_newest_id_leaves_nothing_pending(self) -> None:
        self.assertEqual(self.store.list(after_id=self.ids[-1]), [])

    def test_after_id_composes_with_the_other_filters(self) -> None:
        self.store.add(
            LearningEvent(
                shape="loss",
                concept_id=99,
                subject="aiko",
                new_label="a faded belief",
                fingerprint="fp-aiko",
                salience=0.7,
                created_at="2026-08-09T00:00:00+00:00",
            )
        )
        rows = self.store.list(
            after_id=self.ids[0], subject="aiko", min_salience=0.6
        )
        self.assertEqual([e.concept_id for e in rows], [99])

    def test_omitting_the_cursor_still_returns_everything(self) -> None:
        self.assertEqual(len(self.store.list()), 3)

    def test_page_since_walks_forwards_oldest_first(self) -> None:
        # The ordering guarantee a watermarked consumer depends on: the
        # page it reads is the page its watermark advances past.
        page = self.store.page_since(0, limit=2)
        self.assertEqual([e.event_id for e in page], self.ids[:2])
        nxt = self.store.page_since(page[-1].event_id, limit=2)
        self.assertEqual([e.event_id for e in nxt], self.ids[2:])

    def test_page_since_honours_the_salience_floor(self) -> None:
        self.store.add(
            LearningEvent(
                shape="loss",
                concept_id=42,
                new_label="a faint change",
                fingerprint="fp-faint",
                salience=0.1,
                created_at="2026-08-09T00:00:00+00:00",
            )
        )
        page = self.store.page_since(0, limit=10, min_salience=0.4)
        self.assertEqual([e.event_id for e in page], self.ids)

    def test_page_since_can_filter_by_subject(self) -> None:
        aiko = self.store.add(
            LearningEvent(
                shape="loss",
                concept_id=7,
                subject="aiko",
                new_label="something about me",
                fingerprint="fp-aiko",
                salience=0.6,
                created_at="2026-08-09T00:00:00+00:00",
            )
        )
        page = self.store.page_since(0, limit=10, subject="aiko")
        self.assertEqual([e.event_id for e in page], [aiko])

    def test_count_since_matches_the_page(self) -> None:
        self.assertEqual(self.store.count_since(0), 3)
        self.assertEqual(self.store.count_since(self.ids[0]), 2)
        self.assertEqual(self.store.count_since(0, min_salience=0.9), 0)


if __name__ == "__main__":
    unittest.main()
