"""Tests for :mod:`app.core.tasks.task_store`.

The store is the source of truth for every task row. The orchestrator
trusts it to round-trip JSON columns (args, state, input_request,
result, metadata), to enforce non-empty user_id / handler_name on
insert, to surface only active rows from ``list_running``, and to
gate ``mark_cancelled`` / ``mark_interrupted`` against already-
terminal rows.

These tests pin each contract one method at a time.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_handler import (
    INITIATED_BY_AIKO,
    INITIATED_BY_BACKGROUND,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PAUSED,
    STATUS_RUNNING,
)
from app.core.tasks.task_store import TaskRow, TaskStore


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)

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
            tid = f.store.create(
                user_id="jacob",
                handler_name="file_search",
                title="search 'notes'",
                args={"q": "notes"},
                state={"phase": "init"},
            )
            self.assertGreater(tid, 0)
        finally:
            f.close()

    def test_create_round_trips_args_and_state(self) -> None:
        f = _Fixture()
        try:
            args = {"q": "notes", "limit": 10, "nested": {"a": [1, 2, 3]}}
            state = {"phase": "scanning", "visited": ["a.md", "b.md"]}
            tid = f.store.create(
                user_id="jacob",
                handler_name="file_search",
                title="t",
                args=args,
                state=state,
            )
            row = f.store.get(tid)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.args, args)
            self.assertEqual(row.state, state)
        finally:
            f.close()

    def test_create_defaults_status_running(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_RUNNING)
            self.assertTrue(row.notify_aiko)
            self.assertTrue(row.visible_to_user)
            self.assertEqual(row.initiated_by, INITIATED_BY_AIKO)
        finally:
            f.close()

    def test_create_visibility_flags_persist(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                notify_aiko=False, visible_to_user=False,
                initiated_by=INITIATED_BY_BACKGROUND,
                args={}, state={},
            )
            row = f.store.get(tid)
            assert row is not None
            self.assertFalse(row.notify_aiko)
            self.assertFalse(row.visible_to_user)
            self.assertEqual(row.initiated_by, INITIATED_BY_BACKGROUND)
        finally:
            f.close()

    def test_create_rejects_empty_user_id(self) -> None:
        f = _Fixture()
        try:
            with self.assertRaises(ValueError):
                f.store.create(
                    user_id="   ",
                    handler_name="h",
                    title="t",
                    args={},
                    state={},
                )
        finally:
            f.close()

    def test_create_rejects_empty_handler_name(self) -> None:
        f = _Fixture()
        try:
            with self.assertRaises(ValueError):
                f.store.create(
                    user_id="u",
                    handler_name="",
                    title="t",
                    args={},
                    state={},
                )
        finally:
            f.close()

    def test_create_downgrades_unknown_initiated_by(self) -> None:
        """An unknown ``initiated_by`` value silently falls back to
        the AIKO default rather than raising — a defensive cushion
        for older callers that hand-roll the string."""
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                initiated_by="nonsense",
                args={}, state={},
            )
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.initiated_by, INITIATED_BY_AIKO)
        finally:
            f.close()

    def test_create_persists_metadata(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
                metadata={"trace_id": "abc", "tags": ["alpha"]},
            )
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.metadata, {"trace_id": "abc", "tags": ["alpha"]})
        finally:
            f.close()


class UpdateTests(unittest.TestCase):
    def test_update_state_round_trip(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={"phase": "init"},
            )
            ok = f.store.update_state(tid, {"phase": "halfway", "n": 42})
            self.assertTrue(ok)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.state, {"phase": "halfway", "n": 42})
        finally:
            f.close()

    def test_update_state_unknown_id_returns_false(self) -> None:
        f = _Fixture()
        try:
            self.assertFalse(f.store.update_state(999999, {"a": 1}))
        finally:
            f.close()

    def test_update_progress_only_patches_supplied_fields(self) -> None:
        """If a caller passes ``progress=0.5`` but no ``message``,
        the existing ``last_message`` must not get cleared."""
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.update_progress(tid, progress=0.2, message="scanning")
            f.store.update_progress(tid, progress=0.6)
            row = f.store.get(tid)
            assert row is not None
            self.assertAlmostEqual(row.progress or 0.0, 0.6)
            self.assertEqual(row.last_message, "scanning")
        finally:
            f.close()

    def test_update_progress_message_only(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.update_progress(tid, progress=0.5)
            f.store.update_progress(tid, message="now indexing")
            row = f.store.get(tid)
            assert row is not None
            self.assertAlmostEqual(row.progress or 0.0, 0.5)
            self.assertEqual(row.last_message, "now indexing")
        finally:
            f.close()


class AwaitingInputTests(unittest.TestCase):
    def test_mark_awaiting_input_sets_status_and_payload(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            ok = f.store.mark_awaiting_input(
                tid, prompt="which one?", options=["a", "b"]
            )
            self.assertTrue(ok)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_AWAITING_INPUT)
            self.assertEqual(row.input_request, {"prompt": "which one?", "options": ["a", "b"]})
        finally:
            f.close()

    def test_mark_awaiting_input_without_options_leaves_none(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_awaiting_input(tid, prompt="free text?")
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.input_request, {"prompt": "free text?"})
        finally:
            f.close()

    def test_clear_awaiting_input_returns_to_running(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_awaiting_input(tid, prompt="?", options=["a"])
            f.store.clear_awaiting_input(tid)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_RUNNING)
            self.assertIsNone(row.input_request)
        finally:
            f.close()


class TerminalTransitionTests(unittest.TestCase):
    def test_mark_done_clears_error_sets_result(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_failed(tid, error="oops")  # corner case: ressurect
            ok = f.store.mark_done(tid, result={"found": 3})
            self.assertTrue(ok)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_DONE)
            self.assertEqual(row.result, {"found": 3})
            self.assertIsNone(row.error)
            self.assertIsNotNone(row.completed_at)
            # Progress is clamped to 1.0 if the handler forgot.
            self.assertAlmostEqual(row.progress or 0.0, 1.0)
        finally:
            f.close()

    def test_mark_done_preserves_explicit_progress(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.update_progress(tid, progress=0.85)
            f.store.mark_done(tid, result={})
            row = f.store.get(tid)
            assert row is not None
            self.assertAlmostEqual(row.progress or 0.0, 0.85)
        finally:
            f.close()

    def test_mark_failed_persists_error(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_done(tid, result={"x": 1})
            f.store.mark_failed(tid, error="db connection died")
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_FAILED)
            self.assertEqual(row.error, "db connection died")
            self.assertIsNone(row.result, "result should be NULLed on failure")
        finally:
            f.close()

    def test_mark_cancelled_only_from_active_status(self) -> None:
        """``mark_cancelled`` must not move a row that's already
        terminal — the orchestrator uses this gate to win the race
        against a late completion."""
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_done(tid, result={})
            ok = f.store.mark_cancelled(tid)
            self.assertFalse(ok)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_DONE)
        finally:
            f.close()

    def test_mark_cancelled_from_running(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            ok = f.store.mark_cancelled(tid)
            self.assertTrue(ok)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_CANCELLED)
            self.assertIsNotNone(row.completed_at)
        finally:
            f.close()

    def test_mark_cancelled_from_awaiting_input(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_awaiting_input(tid, prompt="?")
            self.assertTrue(f.store.mark_cancelled(tid))
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_CANCELLED)
        finally:
            f.close()

    def test_mark_interrupted_only_from_active(self) -> None:
        f = _Fixture()
        try:
            tid_run = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            tid_done = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            f.store.mark_done(tid_done, result={})
            self.assertTrue(f.store.mark_interrupted(tid_run))
            self.assertFalse(f.store.mark_interrupted(tid_done))
            self.assertEqual(f.store.get(tid_run).status, STATUS_INTERRUPTED)
            self.assertEqual(f.store.get(tid_done).status, STATUS_DONE)
        finally:
            f.close()


class ListingTests(unittest.TestCase):
    def _seed(self, store: TaskStore) -> dict[str, int]:
        ids = {
            "jacob_run": store.create(user_id="jacob", handler_name="file_search", title="r1", args={}, state={}),
            "jacob_await": store.create(user_id="jacob", handler_name="file_read", title="r2", args={}, state={}),
            "jacob_done": store.create(user_id="jacob", handler_name="file_search", title="r3", args={}, state={}),
            "alice_run": store.create(user_id="alice", handler_name="file_search", title="a1", args={}, state={}),
        }
        store.mark_awaiting_input(ids["jacob_await"], prompt="?")
        store.mark_done(ids["jacob_done"], result={})
        return ids

    def test_list_running_default_excludes_terminal(self) -> None:
        f = _Fixture()
        try:
            ids = self._seed(f.store)
            rows = f.store.list_running()
            row_ids = {r.id for r in rows}
            self.assertIn(ids["jacob_run"], row_ids)
            self.assertIn(ids["jacob_await"], row_ids)
            self.assertIn(ids["alice_run"], row_ids)
            self.assertNotIn(ids["jacob_done"], row_ids)
        finally:
            f.close()

    def test_list_running_filters_by_user(self) -> None:
        f = _Fixture()
        try:
            ids = self._seed(f.store)
            rows = f.store.list_running(user_id="alice")
            self.assertEqual({r.id for r in rows}, {ids["alice_run"]})
        finally:
            f.close()

    def test_count_active_for_user(self) -> None:
        f = _Fixture()
        try:
            self._seed(f.store)
            self.assertEqual(f.store.count_active_for_user("jacob"), 2)
            self.assertEqual(f.store.count_active_for_user("alice"), 1)
            self.assertEqual(f.store.count_active_for_user("nobody"), 0)
        finally:
            f.close()

    def test_list_for_user_newest_first(self) -> None:
        f = _Fixture()
        try:
            ids = self._seed(f.store)
            rows = f.store.list_for_user("jacob")
            # newest -> oldest
            row_ids = [r.id for r in rows]
            self.assertEqual(row_ids, sorted(row_ids, reverse=True))
            self.assertEqual(set(row_ids), {ids["jacob_run"], ids["jacob_await"], ids["jacob_done"]})
        finally:
            f.close()

    def test_list_for_user_status_filter(self) -> None:
        f = _Fixture()
        try:
            ids = self._seed(f.store)
            rows = f.store.list_for_user("jacob", status=STATUS_DONE)
            self.assertEqual({r.id for r in rows}, {ids["jacob_done"]})
        finally:
            f.close()

    def test_list_for_user_pagination(self) -> None:
        f = _Fixture()
        try:
            self._seed(f.store)
            # Three jacob rows; page 1 size 2.
            page1 = f.store.list_for_user("jacob", limit=2, offset=0)
            page2 = f.store.list_for_user("jacob", limit=2, offset=2)
            self.assertEqual(len(page1), 2)
            self.assertEqual(len(page2), 1)
            self.assertEqual(
                {*[r.id for r in page1], *[r.id for r in page2]},
                set(r.id for r in f.store.list_for_user("jacob", limit=100)),
            )
        finally:
            f.close()

    def test_list_for_user_roots_only_excludes_children(self) -> None:
        f = _Fixture()
        try:
            ids = self._seed(f.store)
            # Spawn a child under the running parent.
            child = f.store.create(
                user_id="jacob",
                handler_name="file_write",
                title="child step",
                args={},
                state={},
                parent_task_id=ids["jacob_run"],
            )
            all_rows = {r.id for r in f.store.list_for_user("jacob", limit=100)}
            self.assertIn(child, all_rows)
            roots = {
                r.id
                for r in f.store.list_for_user(
                    "jacob", limit=100, roots_only=True
                )
            }
            self.assertNotIn(child, roots)
            self.assertEqual(
                roots,
                {ids["jacob_run"], ids["jacob_await"], ids["jacob_done"]},
            )
            # Count mirrors the filter so the pager stays consistent.
            self.assertEqual(f.store.count_for_user("jacob"), 4)
            self.assertEqual(
                f.store.count_for_user("jacob", roots_only=True), 3
            )
        finally:
            f.close()

    def test_list_non_terminal_returns_active(self) -> None:
        f = _Fixture()
        try:
            ids = self._seed(f.store)
            rows = f.store.list_non_terminal()
            ids_seen = {r.id for r in rows}
            self.assertIn(ids["jacob_run"], ids_seen)
            self.assertIn(ids["jacob_await"], ids_seen)
            self.assertIn(ids["alice_run"], ids_seen)
            self.assertNotIn(ids["jacob_done"], ids_seen)
        finally:
            f.close()


class TaskRowShapeTests(unittest.TestCase):
    def test_taskrow_is_dataclass_with_expected_fields(self) -> None:
        """The 19-field shape is what callers (REST serialiser, MCP
        debug, frontend) lean on. Pin it here so any DDL drift trips
        the test at the dataclass layer too."""
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            row = f.store.get(tid)
            assert row is not None
            self.assertIsInstance(row, TaskRow)
            self.assertEqual(row.id, tid)
            self.assertEqual(row.user_id, "u")
            self.assertEqual(row.handler_name, "h")
            self.assertEqual(row.title, "t")
            self.assertEqual(row.args, {})
            self.assertEqual(row.state, {})
            self.assertEqual(row.status, STATUS_RUNNING)
            self.assertIsNone(row.progress)
            self.assertIsNone(row.last_message)
            self.assertIsNone(row.input_request)
            self.assertIsNone(row.result)
            self.assertIsNone(row.error)
            self.assertTrue(row.notify_aiko)
            self.assertTrue(row.visible_to_user)
            self.assertEqual(row.initiated_by, INITIATED_BY_AIKO)
            self.assertTrue(row.created_at)
            self.assertTrue(row.updated_at)
            self.assertIsNone(row.completed_at)
            self.assertIsNone(row.metadata)
        finally:
            f.close()


class DeleteTests(unittest.TestCase):
    def test_delete_removes_row(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t",
                args={}, state={},
            )
            self.assertTrue(f.store.delete(tid))
            self.assertIsNone(f.store.get(tid))
        finally:
            f.close()

    def test_delete_unknown_returns_false(self) -> None:
        f = _Fixture()
        try:
            self.assertFalse(f.store.delete(999999))
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
