"""Tests for :mod:`app.core.tasks.task_events`.

Schema v17 append-only per-task event log. The orchestrator appends
to this store on every emit + lifecycle moment; handlers append via
the :class:`TaskEventEmit` outcome. These tests pin the public
surface one method at a time.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_events import (
    EVENT_COMPLETED,
    EVENT_CUSTOM,
    EVENT_PROGRESS,
    EVENT_STARTED,
    KNOWN_EVENT_TYPES,
    TaskEvent,
    TaskEventStore,
    is_known_event_type,
)


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskEventStore(self.db)

    def close(self) -> None:
        if self.db is not None:
            conn = getattr(self.db._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self.db._local.conn = None
            self.db = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class ConstantsTests(unittest.TestCase):
    def test_known_event_types_includes_all_constants(self) -> None:
        # Sanity: every public EVENT_* constant is in the set.
        self.assertIn(EVENT_STARTED, KNOWN_EVENT_TYPES)
        self.assertIn(EVENT_PROGRESS, KNOWN_EVENT_TYPES)
        self.assertIn(EVENT_COMPLETED, KNOWN_EVENT_TYPES)
        self.assertIn(EVENT_CUSTOM, KNOWN_EVENT_TYPES)

    def test_is_known_event_type_rejects_unknown(self) -> None:
        self.assertFalse(is_known_event_type("not_a_real_type"))
        self.assertTrue(is_known_event_type(EVENT_STARTED))


class AppendTests(unittest.TestCase):
    def test_append_returns_positive_id(self) -> None:
        f = _Fixture()
        try:
            eid = f.store.append(1, type=EVENT_STARTED, data={"x": 1})
            self.assertGreater(eid, 0)
        finally:
            f.close()

    def test_append_round_trips_data(self) -> None:
        f = _Fixture()
        try:
            f.store.append(7, type=EVENT_PROGRESS, data={"progress": 0.5})
            events = f.store.list_for_task(7)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].data, {"progress": 0.5})
        finally:
            f.close()

    def test_append_with_none_data_writes_null(self) -> None:
        f = _Fixture()
        try:
            f.store.append(2, type=EVENT_STARTED, data=None)
            events = f.store.list_for_task(2)
            self.assertEqual(len(events), 1)
            self.assertIsNone(events[0].data)
        finally:
            f.close()

    def test_append_rejects_invalid_task_id(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(0, f.store.append(0, type=EVENT_STARTED))
            self.assertEqual(0, f.store.append(-1, type=EVENT_STARTED))
        finally:
            f.close()

    def test_append_rejects_empty_type(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(0, f.store.append(1, type=""))
            self.assertEqual(0, f.store.append(1, type="   "))
        finally:
            f.close()

    def test_append_accepts_unknown_custom_type(self) -> None:
        # Custom types must still persist (handlers may extend).
        f = _Fixture()
        try:
            eid = f.store.append(3, type="my_handler_event", data={"k": "v"})
            self.assertGreater(eid, 0)
            events = f.store.list_for_task(3)
            self.assertEqual(events[0].type, "my_handler_event")
        finally:
            f.close()


class ListTests(unittest.TestCase):
    def test_list_returns_chronological_order(self) -> None:
        f = _Fixture()
        try:
            f.store.append(5, type=EVENT_STARTED)
            f.store.append(5, type=EVENT_PROGRESS, data={"step": 1})
            f.store.append(5, type=EVENT_COMPLETED)
            events = f.store.list_for_task(5)
            self.assertEqual(
                [e.type for e in events],
                [EVENT_STARTED, EVENT_PROGRESS, EVENT_COMPLETED],
            )
        finally:
            f.close()

    def test_list_descending(self) -> None:
        f = _Fixture()
        try:
            f.store.append(5, type=EVENT_STARTED)
            f.store.append(5, type=EVENT_COMPLETED)
            events = f.store.list_for_task(5, ascending=False)
            self.assertEqual(events[0].type, EVENT_COMPLETED)
        finally:
            f.close()

    def test_list_pagination(self) -> None:
        f = _Fixture()
        try:
            for i in range(10):
                f.store.append(8, type=EVENT_PROGRESS, data={"i": i})
            page = f.store.list_for_task(8, limit=4, offset=4)
            self.assertEqual(len(page), 4)
            self.assertEqual(page[0].data, {"i": 4})
            self.assertEqual(page[-1].data, {"i": 7})
        finally:
            f.close()

    def test_list_isolates_by_task_id(self) -> None:
        f = _Fixture()
        try:
            f.store.append(11, type=EVENT_STARTED)
            f.store.append(12, type=EVENT_STARTED)
            self.assertEqual(len(f.store.list_for_task(11)), 1)
            self.assertEqual(len(f.store.list_for_task(12)), 1)
        finally:
            f.close()

    def test_list_returns_empty_for_unknown_task(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(f.store.list_for_task(999), [])
        finally:
            f.close()

    def test_count_matches_list_length(self) -> None:
        f = _Fixture()
        try:
            for _ in range(3):
                f.store.append(20, type=EVENT_PROGRESS)
            self.assertEqual(f.store.count_for_task(20), 3)
        finally:
            f.close()


class LatestForTaskTests(unittest.TestCase):
    def test_latest_returns_most_recent(self) -> None:
        f = _Fixture()
        try:
            f.store.append(30, type=EVENT_STARTED)
            f.store.append(30, type=EVENT_PROGRESS, data={"x": 1})
            latest = f.store.latest_for_task(30)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.type, EVENT_PROGRESS)
        finally:
            f.close()

    def test_latest_filtered_by_type(self) -> None:
        f = _Fixture()
        try:
            f.store.append(31, type=EVENT_STARTED, data={"v": 1})
            f.store.append(31, type=EVENT_PROGRESS, data={"v": 2})
            f.store.append(31, type=EVENT_STARTED, data={"v": 3})
            latest = f.store.latest_for_task(31, type=EVENT_STARTED)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.data, {"v": 3})
        finally:
            f.close()


class CascadeDeleteTests(unittest.TestCase):
    def test_delete_for_task_removes_only_one_task(self) -> None:
        f = _Fixture()
        try:
            for _ in range(3):
                f.store.append(40, type=EVENT_PROGRESS)
            for _ in range(2):
                f.store.append(41, type=EVENT_PROGRESS)
            deleted = f.store.delete_for_task(40)
            self.assertEqual(deleted, 3)
            self.assertEqual(f.store.count_for_task(40), 0)
            self.assertEqual(f.store.count_for_task(41), 2)
        finally:
            f.close()

    def test_delete_for_task_returns_zero_on_unknown(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(f.store.delete_for_task(999), 0)
        finally:
            f.close()


class DataclassTests(unittest.TestCase):
    def test_taskevent_is_frozen(self) -> None:
        evt = TaskEvent(
            id=1,
            task_id=2,
            type=EVENT_STARTED,
            data={"k": "v"},
            created_at="2026-01-01T00:00:00+00:00",
        )
        with self.assertRaises(Exception):
            evt.type = "tampered"  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
