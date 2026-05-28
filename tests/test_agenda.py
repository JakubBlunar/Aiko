"""Tests for the agenda module (Phase 4a)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.agenda import (
    AgendaItem,
    AgendaStore,
    AgendaWorker,
    GroomDiff,
    _parse_groom_diff,
    extract_inline_tags,
)
from app.core.chat_database import ChatDatabase


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = AgendaStore(self.db)

    def close(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class _FakeOllama:
    def __init__(self, response: str = "{}") -> None:
        self.response = response
        self.calls: list[dict] = []
        self.fail = False

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return self.response


class ExtractInlineTagsTests(unittest.TestCase):
    def test_basic_tag(self):
        out = extract_inline_tags("[[agenda:plan the trip]]")
        self.assertEqual(out, [("plan the trip", 0.5)])

    def test_explicit_importance(self):
        out = extract_inline_tags("[[agenda:0.8:ship phase 4]]")
        self.assertEqual(out, [("ship phase 4", 0.8)])

    def test_dedupes(self):
        out = extract_inline_tags(
            "[[agenda:write tests]] more text [[agenda:write tests]]"
        )
        self.assertEqual(len(out), 1)

    def test_no_tags(self):
        self.assertEqual(extract_inline_tags("plain prose with no tags"), [])

    def test_multiple_distinct(self):
        out = extract_inline_tags(
            "[[agenda:0.6:fix the build]] [[agenda:read paper]]"
        )
        self.assertEqual(len(out), 2)


class AgendaStoreTests(unittest.TestCase):
    def test_add_open_item(self):
        f = _Fixture()
        try:
            item = f.store.add("u1", goal="learn rust", importance=0.7)
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item.goal, "learn rust")
            self.assertEqual(item.status, "open")
            self.assertEqual(f.store.list_open("u1")[0].goal, "learn rust")
        finally:
            f.close()

    def test_add_dedupes_same_goal(self):
        f = _Fixture()
        try:
            f.store.add("u2", goal="ship the demo", importance=0.5)
            f.store.add("u2", goal="ship the demo", importance=0.8)
            items = f.store.list_open("u2")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].importance, 0.8)
        finally:
            f.close()

    def test_mark_done_excludes_from_open(self):
        f = _Fixture()
        try:
            item = f.store.add("u3", goal="write the README", importance=0.5)
            assert item is not None
            self.assertTrue(f.store.mark_done(item.id))
            self.assertEqual(f.store.list_open("u3"), [])
            all_items = f.store.list_all("u3")
            self.assertEqual(all_items[0].status, "done")
        finally:
            f.close()

    def test_render_block_skips_when_empty(self):
        f = _Fixture()
        try:
            self.assertEqual(f.store.render_block("u4"), "")
        finally:
            f.close()

    def test_render_block_lists_items(self):
        f = _Fixture()
        try:
            f.store.add("u5", goal="thing one", importance=0.6)
            f.store.add("u5", goal="thing two", importance=0.4)
            block = f.store.render_block("u5")
            self.assertIn("thing one", block)
            self.assertIn("thing two", block)
        finally:
            f.close()

    def test_update_importance_clipped(self):
        f = _Fixture()
        try:
            item = f.store.add("u6", goal="some goal", importance=0.5)
            assert item is not None
            f.store.update(item.id, importance=2.5)
            self.assertEqual(f.store.get(item.id).importance, 1.0)
        finally:
            f.close()


class ParseGroomDiffTests(unittest.TestCase):
    def test_full_diff(self):
        raw = (
            '{"complete":[1,2],"drop":[3],'
            '"promote":[{"id":4,"importance":0.8}],'
            '"add":[{"goal":"new thing","importance":0.6}]}'
        )
        diff = _parse_groom_diff(raw)
        self.assertEqual(diff.complete, [1, 2])
        self.assertEqual(diff.drop, [3])
        self.assertEqual(diff.promote, [(4, 0.8)])
        self.assertEqual(diff.add, [("new thing", 0.6)])

    def test_partial_diff(self):
        raw = '{"complete":[5]}'
        diff = _parse_groom_diff(raw)
        self.assertEqual(diff.complete, [5])
        self.assertEqual(diff.drop, [])

    def test_garbage_returns_empty(self):
        diff = _parse_groom_diff("nonsense")
        self.assertEqual(diff.complete, [])
        self.assertEqual(diff.add, [])

    def test_drops_short_add(self):
        raw = '{"add":[{"goal":"x"},{"goal":"good item here","importance":0.4}]}'
        diff = _parse_groom_diff(raw)
        self.assertEqual(len(diff.add), 1)
        self.assertEqual(diff.add[0][0], "good item here")


class AgendaWorkerTests(unittest.TestCase):
    def _make(self, response: str = "{}", **overrides):
        f = _Fixture()
        ollama = _FakeOllama(response)
        kwargs = {
            "ollama": ollama,
            "store": f.store,
            "model": "m",
            "every_n_turns": 2,
        }
        kwargs.update(overrides)
        worker = AgendaWorker(**kwargs)
        return f, ollama, worker

    def test_should_not_run_when_no_open_items(self):
        f, _ollama, worker = self._make()
        try:
            for _ in range(2):
                worker.notify_user_turn()
            self.assertFalse(worker.should_run("u1"))
        finally:
            f.close()

    def test_should_run_after_min_turns_and_open_items(self):
        f, _ollama, worker = self._make()
        try:
            f.store.add("u2", goal="exists", importance=0.5)
            for _ in range(2):
                worker.notify_user_turn()
            self.assertTrue(worker.should_run("u2"))
        finally:
            f.close()

    def test_throttled_below_min_turns(self):
        f, ollama, worker = self._make()
        try:
            f.store.add("u3", goal="seed item", importance=0.5)
            worker.notify_user_turn()  # only 1
            result = worker.maybe_run(
                "u3", history_provider=lambda: [("user", "hi")],
            )
            self.assertIsNone(result)
            self.assertEqual(ollama.calls, [])
        finally:
            f.close()

    def test_runs_and_applies_diff(self):
        f, ollama, worker = self._make(response='{"complete":[],"add":[{"goal":"new from llm","importance":0.7}]}')
        try:
            f.store.add("u4", goal="seed", importance=0.5)
            for _ in range(2):
                worker.notify_user_turn()
            result = worker.maybe_run(
                "u4",
                history_provider=lambda: [
                    ("user", "let's track another goal"),
                    ("assistant", "sure"),
                ],
            )
            self.assertIsNotNone(result)
            assert result is not None
            opens = [i.goal for i in f.store.list_open("u4")]
            self.assertIn("new from llm", opens)
            self.assertGreaterEqual(worker.stats()["adds"], 1)
        finally:
            f.close()

    def test_runs_completes_existing(self):
        f, ollama, worker = self._make(response="placeholder")
        try:
            item = f.store.add("u5", goal="finish thing", importance=0.5)
            assert item is not None
            ollama.response = (
                f'{{"complete":[{item.id}],"drop":[],"promote":[],"add":[]}}'
            )
            for _ in range(2):
                worker.notify_user_turn()
            worker.maybe_run(
                "u5",
                history_provider=lambda: [
                    ("user", "I finished the thing"),
                    ("assistant", "great work!"),
                ],
            )
            self.assertEqual(f.store.get(item.id).status, "done")
            self.assertEqual(worker.stats()["completes"], 1)
        finally:
            f.close()

    def test_failure_does_not_crash(self):
        f, ollama, worker = self._make()
        try:
            f.store.add("u6", goal="seed goal", importance=0.5)
            ollama.fail = True
            for _ in range(2):
                worker.notify_user_turn()
            result = worker.maybe_run(
                "u6", history_provider=lambda: [("user", "hi")],
            )
            self.assertIsNone(result)
            self.assertEqual(worker.stats()["failed"], 1)
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
