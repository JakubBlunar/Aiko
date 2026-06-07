"""Tests for :mod:`app.core.tasks.task_inputs`.

Schema v17 per-task input/answer history. Pins the public surface:
create / answer / supersede / latest_pending / list, plus the
``answered_at`` bumping invariants.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_inputs import (
    KIND_CHOICE,
    KIND_FREE_TEXT,
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_SUPERSEDED,
    TaskInput,
    TaskInputStore,
)


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskInputStore(self.db)

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


class CreateTests(unittest.TestCase):
    def test_create_returns_positive_id(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(1, prompt="which root?", kind=KIND_CHOICE,
                                 options=["A", "B"])
            self.assertGreater(iid, 0)
        finally:
            f.close()

    def test_create_round_trips_options(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(
                1, prompt="pick", kind=KIND_CHOICE, options=["one", "two"]
            )
            row = f.store.get(iid)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.options, ["one", "two"])
            self.assertEqual(row.kind, KIND_CHOICE)
            self.assertEqual(row.status, STATUS_PENDING)
            self.assertIsNone(row.response)
            self.assertIsNone(row.answered_at)
        finally:
            f.close()

    def test_create_accepts_free_text_kind(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(2, prompt="what?", kind=KIND_FREE_TEXT)
            row = f.store.get(iid)
            assert row is not None
            self.assertEqual(row.kind, KIND_FREE_TEXT)
            self.assertIsNone(row.options)
        finally:
            f.close()

    def test_create_rejects_empty_prompt(self) -> None:
        f = _Fixture()
        try:
            with self.assertRaises(ValueError):
                f.store.create(1, prompt="")
            with self.assertRaises(ValueError):
                f.store.create(1, prompt="   ")
        finally:
            f.close()

    def test_create_rejects_invalid_task_id(self) -> None:
        f = _Fixture()
        try:
            with self.assertRaises(ValueError):
                f.store.create(0, prompt="x")
        finally:
            f.close()


class AnswerTests(unittest.TestCase):
    def test_answer_marks_row_answered(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(10, prompt="?")
            self.assertTrue(f.store.answer(iid, response="yes"))
            row = f.store.get(iid)
            assert row is not None
            self.assertEqual(row.status, STATUS_ANSWERED)
            self.assertEqual(row.response, "yes")
            self.assertIsNotNone(row.answered_at)
        finally:
            f.close()

    def test_answer_is_one_shot(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(11, prompt="?")
            self.assertTrue(f.store.answer(iid, response="a"))
            # Second answer attempt on the same row is a no-op.
            self.assertFalse(f.store.answer(iid, response="b"))
            row = f.store.get(iid)
            assert row is not None
            self.assertEqual(row.response, "a")
        finally:
            f.close()

    def test_answer_returns_false_for_unknown_input(self) -> None:
        f = _Fixture()
        try:
            self.assertFalse(f.store.answer(999, response="anything"))
        finally:
            f.close()

    def test_answer_after_supersede_returns_false(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(20, prompt="first")
            f.store.supersede_pending_for_task(20)
            self.assertFalse(f.store.answer(iid, response="late"))
        finally:
            f.close()


class SupersedeTests(unittest.TestCase):
    def test_supersede_only_affects_pending_rows(self) -> None:
        f = _Fixture()
        try:
            iid1 = f.store.create(30, prompt="q1")
            f.store.answer(iid1, response="r1")
            iid2 = f.store.create(30, prompt="q2")
            superseded = f.store.supersede_pending_for_task(30)
            self.assertEqual(superseded, 1)
            row1 = f.store.get(iid1)
            row2 = f.store.get(iid2)
            assert row1 is not None and row2 is not None
            self.assertEqual(row1.status, STATUS_ANSWERED)
            self.assertEqual(row2.status, STATUS_SUPERSEDED)
        finally:
            f.close()

    def test_supersede_marks_multiple_rows(self) -> None:
        f = _Fixture()
        try:
            for _ in range(3):
                f.store.create(40, prompt="?")
            superseded = f.store.supersede_pending_for_task(40)
            self.assertEqual(superseded, 3)
            self.assertIsNone(f.store.latest_pending(40))
        finally:
            f.close()


class CancelTests(unittest.TestCase):
    def test_cancel_marks_pending_rows(self) -> None:
        f = _Fixture()
        try:
            f.store.create(50, prompt="a")
            f.store.create(50, prompt="b")
            cancelled = f.store.cancel_pending_for_task(50)
            self.assertEqual(cancelled, 2)
            rows = f.store.list_for_task(50)
            self.assertTrue(all(r.status == STATUS_CANCELLED for r in rows))
        finally:
            f.close()

    def test_cancel_leaves_answered_alone(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(51, prompt="?")
            f.store.answer(iid, response="ok")
            f.store.cancel_pending_for_task(51)
            row = f.store.get(iid)
            assert row is not None
            self.assertEqual(row.status, STATUS_ANSWERED)
        finally:
            f.close()


class LatestPendingTests(unittest.TestCase):
    def test_latest_pending_returns_most_recent_pending(self) -> None:
        f = _Fixture()
        try:
            f.store.create(60, prompt="first")
            f.store.supersede_pending_for_task(60)
            iid2 = f.store.create(60, prompt="second")
            row = f.store.latest_pending(60)
            assert row is not None
            self.assertEqual(row.id, iid2)
            self.assertEqual(row.prompt, "second")
        finally:
            f.close()

    def test_latest_pending_returns_none_when_nothing_pending(self) -> None:
        f = _Fixture()
        try:
            iid = f.store.create(61, prompt="?")
            f.store.answer(iid, response="ok")
            self.assertIsNone(f.store.latest_pending(61))
        finally:
            f.close()


class ListAndDeleteTests(unittest.TestCase):
    def test_list_for_task_returns_history(self) -> None:
        f = _Fixture()
        try:
            iid1 = f.store.create(70, prompt="q1")
            f.store.answer(iid1, response="r1")
            f.store.create(70, prompt="q2")
            rows = f.store.list_for_task(70)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].prompt, "q1")
            self.assertEqual(rows[1].prompt, "q2")
        finally:
            f.close()

    def test_delete_for_task_removes_all(self) -> None:
        f = _Fixture()
        try:
            f.store.create(80, prompt="a")
            f.store.create(80, prompt="b")
            deleted = f.store.delete_for_task(80)
            self.assertEqual(deleted, 2)
            self.assertEqual(f.store.list_for_task(80), [])
        finally:
            f.close()


class DataclassTests(unittest.TestCase):
    def test_taskinput_is_frozen(self) -> None:
        inp = TaskInput(
            id=1, task_id=2, prompt="?", kind=None, options=None,
            status=STATUS_PENDING, response=None,
            created_at="2026-01-01T00:00:00+00:00", answered_at=None,
        )
        with self.assertRaises(Exception):
            inp.status = STATUS_ANSWERED  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
