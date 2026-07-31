"""Tests for ``scripts.concept_sweep_unreinforced``.

The L22 one-off sweep parks the bootstrap-era never-reinforced concepts as
``dormant``. It reaches around the L3 single writer, so the things worth
locking in are its *aim* and its *restraint*: which rows it selects, that a
dry run cannot write, and that every demotion leaves a timeline row.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from scripts import concept_sweep_unreinforced as sweep

_CUTOFF = datetime(2026, 7, 13, tzinfo=timezone.utc)
_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _iso(day: int) -> str:
    return datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc).isoformat()


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "test.db"
        ChatDatabase(self.path)  # create the schema
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()

    def _add(
        self,
        label: str,
        *,
        status: str = "active",
        promoted: str | None = _iso(5),
        reinforced: str | None = None,
        confidence: float = 0.8,
        kind: str = "identity",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO concepts ("
            "  label, kind, subject, status, confidence, evidence_count,"
            "  distinct_source_count, created_at, updated_at,"
            "  last_reinforced_at, promoted_at"
            ") VALUES (?, ?, 'user', ?, ?, 3, 2, ?, ?, ?, ?)",
            (
                label, kind, status, confidence, _iso(1), _iso(1),
                reinforced, promoted,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _status(self, cid: int) -> str:
        row = self.conn.execute(
            "SELECT status FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        return str(row["status"])

    def _events(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM concept_events"))

    # ── selection ────────────────────────────────────────────────────

    def test_selects_only_the_bootstrap_cohort(self) -> None:
        stale = self._add("bootstrap belief")
        recent = self._add("promoted after the fix", promoted=_iso(20))
        reinforced = self._add(
            "earned it", promoted=_iso(5), reinforced=_iso(8)
        )
        dormant = self._add("already quiet", status="dormant")
        candidate = self._add("never promoted", status="candidate")

        picked = {int(r["id"]) for r in sweep.select(self.conn, cutoff=_CUTOFF)}
        self.assertEqual(picked, {stale})
        for other in (recent, reinforced, dormant, candidate):
            self.assertNotIn(other, picked)

    def test_reinforcement_at_or_before_promotion_does_not_count(self) -> None:
        """L22 signal C: the reinforcement has to come *after* the
        promotion. A row stamped at the same moment is the promotion
        itself, not evidence the belief was re-observed."""
        same = self._add("same instant", promoted=_iso(5), reinforced=_iso(5))
        before = self._add("stale stamp", promoted=_iso(5), reinforced=_iso(2))
        after = self._add("genuine", promoted=_iso(5), reinforced=_iso(6))

        picked = {int(r["id"]) for r in sweep.select(self.conn, cutoff=_CUTOFF)}
        self.assertEqual(picked, {same, before})
        self.assertNotIn(after, picked)

    def test_oldest_promotions_come_first(self) -> None:
        newer = self._add("newer", promoted=_iso(9))
        older = self._add("older", promoted=_iso(2))
        picked = [int(r["id"]) for r in sweep.select(self.conn, cutoff=_CUTOFF)]
        self.assertEqual(picked, [older, newer])

    # ── the write ────────────────────────────────────────────────────

    def test_apply_demotes_and_records_a_timeline_row(self) -> None:
        cid = self._add("bootstrap belief")
        targets = sweep.select(self.conn, cutoff=_CUTOFF)
        self.assertEqual(sweep.apply_sweep(self.conn, targets, now=_NOW), 1)

        self.assertEqual(self._status(cid), "dormant")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "dormant")
        self.assertEqual(events[0]["concept_id"], cid)
        self.assertEqual(events[0]["created_at"], _NOW.isoformat())
        self.assertIn("never reinforced", events[0]["reason"])

    def test_confidence_and_evidence_are_left_alone(self) -> None:
        """Dormant, not retired, and not devalued -- a genuine
        reinforcement has to be able to bring the belief straight back."""
        cid = self._add("bootstrap belief", confidence=0.83)
        sweep.apply_sweep(
            self.conn, sweep.select(self.conn, cutoff=_CUTOFF), now=_NOW
        )
        row = self.conn.execute(
            "SELECT * FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        self.assertAlmostEqual(float(row["confidence"]), 0.83)
        self.assertEqual(int(row["distinct_source_count"]), 2)
        self.assertEqual(int(row["evidence_count"]), 3)

    def test_untargeted_concepts_are_untouched(self) -> None:
        keep = self._add("earned it", promoted=_iso(5), reinforced=_iso(8))
        sweep.apply_sweep(
            self.conn, sweep.select(self.conn, cutoff=_CUTOFF), now=_NOW
        )
        self.assertEqual(self._status(keep), "active")
        self.assertEqual(self._events(), [])

    # ── the dry run ──────────────────────────────────────────────────

    def test_dry_run_opens_the_database_read_only(self) -> None:
        """The safety property that matters: without ``--apply`` the
        connection physically cannot write, so a bug in the reporting path
        can't turn into a mutation."""
        self._add("bootstrap belief")
        conn = sweep._connect(self.path, write=False)
        try:
            targets = sweep.select(conn, cutoff=_CUTOFF)
            self.assertEqual(len(targets), 1)
            with self.assertRaises(sqlite3.OperationalError):
                sweep.apply_sweep(conn, targets, now=_NOW)
        finally:
            conn.close()
        self.assertEqual(self._events(), [])

    def test_summary_counts_and_samples(self) -> None:
        self._add("one")
        self._add("two", kind="value", confidence=0.6)
        self._add("earned it", reinforced=_iso(8))
        targets = sweep.select(self.conn, cutoff=_CUTOFF)
        data = sweep.summarise(targets, total_active=3, cutoff=_CUTOFF)

        self.assertEqual(data["targets"], 2)
        self.assertEqual(data["active_total"], 3)
        self.assertEqual(data["targets_pct"], 66.7)
        self.assertEqual(
            {row["kind"] for row in data["by_kind"]}, {"identity", "value"}
        )
        self.assertEqual(len(data["sample"]), 2)

    def test_empty_cohort_renders_without_a_sample(self) -> None:
        data = sweep.summarise([], total_active=0, cutoff=_CUTOFF)
        self.assertEqual(data["targets"], 0)
        self.assertIn("Nothing to do", sweep._render(data, applied=False))


if __name__ == "__main__":
    unittest.main()
