"""Tests for ``scripts.fact_check_backfill``.

The backfill reaches around the live enqueue hook to queue claims already
sitting in memory, so the things worth locking in are its *restraint*: that it
re-runs the same privacy gates the turn path uses rather than trusting the
kind, that a dry run cannot write, and that re-running it does not duplicate
work already in the queue.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_queue import FactCheckQueue
from scripts import fact_check_backfill as backfill

_KINDS = ("knowledge", "curiosity_finding", "topic_digest")


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "test.db"
        ChatDatabase(self.path)  # create the schema
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()

    def _add(
        self,
        content: str,
        *,
        kind: str = "knowledge",
        metadata: dict | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories "
            "(content, kind, embedding, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                content,
                kind,
                b"",  # the backfill never reads vectors
                "2026-08-01T12:00:00+00:00",
                json.dumps(metadata or {}),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _collect(self, **kwargs):
        params = {
            "kinds": _KINDS,
            "user_names": ["Jacob"],
            "assistant_name": "Aiko",
            "limit": 100,
        }
        params.update(kwargs)
        return backfill.collect(self.conn, **params)

    # ── selection ─────────────────────────────────────────────────────

    def test_an_impersonal_claim_is_planned(self) -> None:
        self._add("Trine 2 was developed by Frozen Byte and published in 2011.")
        data = self._collect()
        self.assertEqual(data["planned_memories"], 1)
        self.assertGreater(data["planned_claims"], 0)

    def test_a_row_naming_the_user_is_refused_by_the_privacy_gate(self) -> None:
        self._add("Jacob bought a new GPU in 2024.")
        data = self._collect()
        self.assertEqual(data["planned_memories"], 0)
        self.assertIn("privacy:user_name", data["skipped"])

    def test_a_row_with_no_extractable_span_is_skipped(self) -> None:
        self._add("Pausing before speaking fosters deeper connection.")
        data = self._collect()
        self.assertEqual(data["planned_memories"], 0)
        self.assertEqual(data["skipped"].get("no_extractable_claim"), 1)

    def test_an_already_adjudicated_row_is_left_alone(self) -> None:
        self._add(
            "Trine 2 was developed by Frozen Byte in 2011.",
            metadata={"last_verified_at": "2026-07-01T00:00:00+00:00"},
        )
        data = self._collect()
        self.assertEqual(data["planned_memories"], 0)
        self.assertEqual(data["skipped"].get("already_adjudicated"), 1)

    def test_kinds_outside_the_list_are_never_scanned(self) -> None:
        self._add("Trine 2 was developed by Frozen Byte in 2011.", kind="diary")
        data = self._collect()
        self.assertEqual(data["scanned"], 0)
        self.assertEqual(data["planned_memories"], 0)

    def test_the_limit_caps_planned_memories(self) -> None:
        for n in range(5):
            self._add(f"Trine {n} was developed by Frozen Byte in 201{n}.")
        data = self._collect(limit=2)
        self.assertEqual(data["planned_memories"], 2)

    # ── writing ───────────────────────────────────────────────────────

    def test_collect_writes_nothing(self) -> None:
        self._add("Trine 2 was developed by Frozen Byte in 2011.")
        self._collect()
        queue = FactCheckQueue(ChatDatabase(self.path))
        self.assertEqual(len(queue.peek_all()), 0)

    def test_apply_enqueues_the_plan(self) -> None:
        self._add("Trine 2 was developed by Frozen Byte in 2011.")
        data = self._collect()
        appended = backfill.apply(self.path, data["plan"])
        self.assertEqual(appended, data["planned_claims"])
        queue = FactCheckQueue(ChatDatabase(self.path))
        self.assertEqual(len(queue.peek_all()), data["planned_claims"])

    def test_re_applying_does_not_duplicate(self) -> None:
        self._add("Trine 2 was developed by Frozen Byte in 2011.")
        data = self._collect()
        backfill.apply(self.path, data["plan"])
        second = backfill.apply(self.path, data["plan"])
        self.assertEqual(second, 0)
        queue = FactCheckQueue(ChatDatabase(self.path))
        self.assertEqual(len(queue.peek_all()), data["planned_claims"])


if __name__ == "__main__":
    unittest.main()
