"""Tests for :class:`app.core.tasks.file_snapshot.FileSnapshotStore`.

Covers the pure diff, the kv-backed read-modify-write, first-run
baseline semantics, and concurrent-write serialization.
"""
from __future__ import annotations

import threading
import unittest

from app.core.tasks.file_snapshot import FileSnapshotStore, SnapshotDiff


class _FakeKv:
    """In-memory stand-in for ChatDatabase's kv_get/kv_set."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}
        self.set_calls = 0

    def kv_get(self, key: str) -> str | None:
        return self._d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.set_calls += 1
        self._d[key] = value


class DiffPureTests(unittest.TestCase):
    def test_no_prior_is_baseline(self) -> None:
        d = FileSnapshotStore.diff(None, {"a.md": {"mtime": 1.0, "size": 10}})
        self.assertIsInstance(d, SnapshotDiff)
        self.assertTrue(d.baseline_established)
        self.assertEqual(d.new, [])
        self.assertEqual(d.modified, [])
        self.assertTrue(d.is_empty)

    def test_new_file_detected(self) -> None:
        prior = {"a.md": {"mtime": 1.0, "size": 10}}
        cur = {"a.md": {"mtime": 1.0, "size": 10}, "b.md": {"mtime": 2.0, "size": 20}}
        d = FileSnapshotStore.diff(prior, cur)
        self.assertFalse(d.baseline_established)
        self.assertEqual(d.new, ["b.md"])
        self.assertEqual(d.modified, [])

    def test_modified_by_mtime(self) -> None:
        prior = {"a.md": {"mtime": 1.0, "size": 10}}
        cur = {"a.md": {"mtime": 5.0, "size": 10}}
        d = FileSnapshotStore.diff(prior, cur)
        self.assertEqual(d.modified, ["a.md"])
        self.assertEqual(d.new, [])

    def test_modified_by_size(self) -> None:
        prior = {"a.md": {"mtime": 1.0, "size": 10}}
        cur = {"a.md": {"mtime": 1.0, "size": 99}}
        d = FileSnapshotStore.diff(prior, cur)
        self.assertEqual(d.modified, ["a.md"])

    def test_unchanged_is_empty(self) -> None:
        prior = {"a.md": {"mtime": 1.0, "size": 10}}
        cur = {"a.md": {"mtime": 1.0, "size": 10}}
        d = FileSnapshotStore.diff(prior, cur)
        self.assertTrue(d.is_empty)
        self.assertFalse(d.baseline_established)

    def test_changed_property_unions(self) -> None:
        prior = {"a.md": {"mtime": 1.0, "size": 10}}
        cur = {
            "a.md": {"mtime": 9.0, "size": 10},  # modified
            "b.md": {"mtime": 2.0, "size": 20},  # new
        }
        d = FileSnapshotStore.diff(prior, cur)
        self.assertEqual(d.changed, {"a.md", "b.md"})

    def test_removed_files_not_reported(self) -> None:
        # A file that disappeared is neither new nor modified.
        prior = {"a.md": {"mtime": 1.0, "size": 10}, "gone.md": {"mtime": 1.0, "size": 5}}
        cur = {"a.md": {"mtime": 1.0, "size": 10}}
        d = FileSnapshotStore.diff(prior, cur)
        self.assertTrue(d.is_empty)


class DiffAndUpdateTests(unittest.TestCase):
    def test_first_run_baseline_then_new(self) -> None:
        kv = _FakeKv()
        store = FileSnapshotStore(kv)
        # First scan -> baseline, zero new.
        d1 = store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        self.assertTrue(d1.baseline_established)
        self.assertEqual(d1.new, [])
        # Second scan with a new file -> only the new one.
        d2 = store.diff_and_update(
            "Docs",
            {"a.md": {"mtime": 1.0, "size": 10}, "b.md": {"mtime": 2.0, "size": 5}},
        )
        self.assertFalse(d2.baseline_established)
        self.assertEqual(d2.new, ["b.md"])

    def test_snapshot_advances_each_call(self) -> None:
        kv = _FakeKv()
        store = FileSnapshotStore(kv)
        store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}, "b.md": {"mtime": 2.0, "size": 5}})
        # Third scan: b.md already recorded, nothing new.
        d3 = store.diff_and_update(
            "Docs",
            {"a.md": {"mtime": 1.0, "size": 10}, "b.md": {"mtime": 2.0, "size": 5}},
        )
        self.assertTrue(d3.is_empty)

    def test_per_label_isolation(self) -> None:
        kv = _FakeKv()
        store = FileSnapshotStore(kv)
        store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        # Different label -> independent baseline.
        d = store.diff_and_update("Notes", {"a.md": {"mtime": 1.0, "size": 10}})
        self.assertTrue(d.baseline_established)

    def test_corrupt_blob_treated_as_baseline(self) -> None:
        kv = _FakeKv()
        kv.kv_set("tasks.file_snapshot.Docs", "{not json")
        store = FileSnapshotStore(kv)
        d = store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        self.assertTrue(d.baseline_established)

    def test_empty_blob_treated_as_baseline(self) -> None:
        kv = _FakeKv()
        kv.kv_set("tasks.file_snapshot.Docs", "")
        store = FileSnapshotStore(kv)
        d = store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        self.assertTrue(d.baseline_established)

    def test_reset_rebaselines(self) -> None:
        kv = _FakeKv()
        store = FileSnapshotStore(kv)
        store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        store.reset("Docs")
        d = store.diff_and_update("Docs", {"a.md": {"mtime": 1.0, "size": 10}})
        self.assertTrue(d.baseline_established)

    def test_concurrent_writes_serialized(self) -> None:
        # Many threads diff_and_update the same label concurrently; the
        # lock must keep every write consistent (no lost updates / crash).
        kv = _FakeKv()
        store = FileSnapshotStore(kv)
        store.diff_and_update("Docs", {})  # baseline empty
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for _ in range(20):
                    store.diff_and_update(
                        "Docs", {f"f{n}.md": {"mtime": float(n), "size": n}}
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # The store is still loadable and well-formed.
        final = store.load("Docs")
        self.assertIsInstance(final, dict)


if __name__ == "__main__":
    unittest.main()
