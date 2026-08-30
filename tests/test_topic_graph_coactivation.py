"""Tests for the L4 cluster co-activation signal (``cluster_coactivation``).

Exercise co-firing detection across conversation sessions, the
support / Jaccard thresholds, connected-component mode grouping, the
session bucket strategy (members without a session are unbucketable),
the non-persistent no-op, and the coarse result cache. Uses the same
persistent-mode harness shape as ``test_topic_graph_persistent`` with
``source_session`` added to the stub memory.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.topic_cluster_store import TopicClusterStore
from app.core.conversation.topic_graph import (
    TopicGraph,
    _normalise,
    temporal_prime_reps,
    format_coactivation_hint_lines,
    CoactivationMode,
)
from app.core.affect.circadian import coarse_coactivation_period
from app.core.infra import timephrase


@dataclass
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    kind: str = "fact"
    salience: float = 0.5
    use_count: int = 0
    source_session: str | None = None
    source_message_id: int | None = None
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "long_term"


class _StubMemoryStore:
    def __init__(self) -> None:
        self._mirror: dict[int, _StubMemory] = {}
        self._lock = threading.Lock()

    def add(self, mem: _StubMemory) -> None:
        with self._lock:
            self._mirror[mem.id] = mem

    def get(self, memory_id: int) -> _StubMemory | None:
        with self._lock:
            return self._mirror.get(int(memory_id))


# Four well-separated cluster directions in R^4.
_DIRS = {
    "A": [1.0, 0.0, 0.0, 0.0],
    "B": [0.0, 1.0, 0.0, 0.0],
    "C": [0.0, 0.0, 1.0, 0.0],
    "D": [0.0, 0.0, 0.0, 1.0],
}


def _vec(family: str, jitter: float) -> np.ndarray:
    base = np.asarray(_DIRS[family], dtype=np.float32).copy()
    # Small in-family jitter on a second axis so members aren't identical
    # but stay far from the other families.
    idx = (list(_DIRS).index(family) + 1) % 4
    base[idx] += jitter
    return _normalise(base)


def _cluster_store() -> tuple[ChatDatabase, TopicClusterStore]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    return db, TopicClusterStore(db)


def _graph(mem_store, cluster_store) -> TopicGraph:
    return TopicGraph(
        mem_store,
        similarity=0.5,
        min_cluster_size=2,
        filter_threshold=0.6,
        cluster_store=cluster_store,
    )


def _rep_to_family(graph: TopicGraph, mem: _StubMemoryStore) -> dict[int, str]:
    """Map each cluster's representative_id to its family letter by looking
    at the family of any member (all members of a cluster share a family)."""
    out: dict[int, str] = {}
    for cluster in graph.topic_clusters():
        first = next(iter(cluster.member_ids))
        content = mem.get(int(first)).content  # type: ignore[union-attr]
        out[int(cluster.representative_id)] = content.split(":")[0]
    return out


class CoactivationDetectionTests(unittest.TestCase):
    def _two_mode_store(self) -> _StubMemoryStore:
        """A/B co-fire in sessions s1+s2; C/D co-fire in s3+s4."""
        mem = _StubMemoryStore()
        rows = [
            # id, family, session
            (1, "A", "s1"), (2, "A", "s2"),
            (3, "B", "s1"), (4, "B", "s2"),
            (5, "C", "s3"), (6, "C", "s4"),
            (7, "D", "s3"), (8, "D", "s4"),
        ]
        for i, (mid, fam, sess) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid,
                    f"{fam}:memory {mid}",
                    _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                )
            )
        return mem

    def test_detects_two_modes(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(len(g.topic_clusters()), 4)
        modes = g.cluster_coactivation(
            min_pair_support=2, min_strength=0.5, max_modes=4,
        )
        self.assertEqual(len(modes), 2)
        rep_family = _rep_to_family(g, mem)
        mode_families = {
            frozenset(rep_family[r] for r in mode.reps) for mode in modes
        }
        self.assertEqual(
            mode_families, {frozenset({"A", "B"}), frozenset({"C", "D"})}
        )
        for mode in modes:
            self.assertEqual(mode.bucket_by, "session")
            self.assertAlmostEqual(mode.strength, 1.0, places=3)
            self.assertEqual(len(mode.labels), len(mode.reps))

    def test_min_pair_support_threshold(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        # Every pair only co-occurs in 2 sessions; support 3 => nothing.
        modes = g.cluster_coactivation(min_pair_support=3, min_strength=0.1)
        self.assertEqual(modes, [])

    def test_min_strength_threshold(self) -> None:
        # A fires alone in many sessions but co-fires with B only twice ->
        # low Jaccard, filtered out by a high min_strength.
        mem = _StubMemoryStore()
        rows = [
            (1, "A", "s1"), (2, "A", "s2"),
            (3, "A", "s3"), (4, "A", "s4"),  # A also fires solo
            (5, "B", "s1"), (6, "B", "s2"),  # B only ever with A in s1,s2
        ]
        for i, (mid, fam, sess) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid, f"{fam}:m{mid}", _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                )
            )
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        # inter=2, |A buckets|=4, |B buckets|=2 -> Jaccard 2/4 = 0.5.
        self.assertEqual(
            g.cluster_coactivation(min_pair_support=2, min_strength=0.6), []
        )
        kept = g.cluster_coactivation(min_pair_support=2, min_strength=0.4)
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0].strength, 0.5, places=3)

    def test_connected_component_grouping(self) -> None:
        # A-B co-fire (s1,s2), B-C co-fire (s3,s4) -> one mode {A,B,C}.
        mem = _StubMemoryStore()
        rows = [
            (1, "A", "s1"), (2, "A", "s2"),
            (3, "B", "s1"), (4, "B", "s2"),
            (5, "B", "s3"), (6, "B", "s4"),
            (7, "C", "s3"), (8, "C", "s4"),
        ]
        for i, (mid, fam, sess) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid, f"{fam}:m{mid}", _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                )
            )
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        modes = g.cluster_coactivation(
            min_pair_support=2, min_strength=0.4, max_reps_per_mode=4,
        )
        self.assertEqual(len(modes), 1)
        rep_family = _rep_to_family(g, mem)
        fams = {rep_family[r] for r in modes[0].reps}
        self.assertEqual(fams, {"A", "B", "C"})

    def test_members_without_session_are_ignored(self) -> None:
        # Same embedding families but no session ids -> unbucketable -> no
        # co-firing at all.
        mem = _StubMemoryStore()
        rows = [
            (1, "A"), (2, "A"), (3, "B"), (4, "B"),
        ]
        for i, (mid, fam) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid, f"{fam}:m{mid}", _vec(fam, 0.05 * (i + 1)),
                    source_session=None,
                )
            )
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(len(g.topic_clusters()), 2)
        self.assertEqual(
            g.cluster_coactivation(min_pair_support=1, min_strength=0.0), []
        )

    def test_non_persistent_is_empty(self) -> None:
        mem = self._two_mode_store()
        g = TopicGraph(mem, similarity=0.5, min_cluster_size=2)
        self.assertFalse(g.persistent)
        self.assertEqual(g.cluster_coactivation(), [])

    def test_unknown_bucket_by_is_empty(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(g.cluster_coactivation(bucket_by="nope"), [])

    def test_result_is_cached(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        first = g.cluster_coactivation(min_pair_support=2, min_strength=0.5)
        self.assertTrue(g._coact_cache)  # cache populated
        second = g.cluster_coactivation(min_pair_support=2, min_strength=0.5)
        self.assertEqual(
            [m.reps for m in first], [m.reps for m in second]
        )


class ExtraBucketTests(unittest.TestCase):
    """day / circadian / weekday axes + partitioner + temporal priming."""

    def setUp(self) -> None:
        timephrase.set_now_provider(
            lambda: datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        )

    def tearDown(self) -> None:
        timephrase.set_now_provider(None)

    def _mem(self, rows: list[tuple]) -> _StubMemoryStore:
        """rows: (id, family, created_at [, session])."""
        mem = _StubMemoryStore()
        for i, row in enumerate(rows):
            mid, fam, created = row[0], row[1], row[2]
            sess = row[3] if len(row) > 3 else f"s{mid}"
            mem.add(
                _StubMemory(
                    mid,
                    f"{fam}:memory {mid}",
                    _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                    created_at=created,
                )
            )
        return mem

    def test_day_detects_same_calendar_day_pairings(self) -> None:
        mem = self._mem([
            (1, "A", "2026-03-01T12:00:00+00:00"),
            (2, "A", "2026-03-02T12:00:00+00:00"),
            (3, "B", "2026-03-01T18:00:00+00:00"),
            (4, "B", "2026-03-02T18:00:00+00:00"),
            (5, "C", "2026-03-10T12:00:00+00:00"),
            (6, "C", "2026-03-11T12:00:00+00:00"),
            (7, "D", "2026-03-10T18:00:00+00:00"),
            (8, "D", "2026-03-11T18:00:00+00:00"),
        ])
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        modes = g.cluster_coactivation(
            bucket_by="day", min_pair_support=2, min_strength=0.5,
        )
        self.assertEqual(len(modes), 2)
        rep_family = _rep_to_family(g, mem)
        mode_families = {
            frozenset(rep_family[r] for r in mode.reps) for mode in modes
        }
        self.assertEqual(
            mode_families, {frozenset({"A", "B"}), frozenset({"C", "D"})}
        )
        for mode in modes:
            self.assertEqual(mode.bucket_by, "day")
            self.assertEqual(mode.partition, "")

    def test_circadian_night_and_morning_do_not_merge(self) -> None:
        # A+B co-fire two nights; B+C co-fire two mornings. Without a
        # partitioner, B would bridge them into one {A,B,C} mode.
        mem = self._mem([
            (1, "A", "2026-03-01T23:00:00+00:00"),
            (2, "A", "2026-03-02T23:00:00+00:00"),
            (3, "B", "2026-03-01T23:30:00+00:00"),
            (4, "B", "2026-03-02T23:30:00+00:00"),
            (5, "B", "2026-03-03T09:00:00+00:00"),
            (6, "B", "2026-03-04T09:00:00+00:00"),
            (7, "C", "2026-03-03T09:30:00+00:00"),
            (8, "C", "2026-03-04T09:30:00+00:00"),
        ])
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        modes = g.cluster_coactivation(
            bucket_by="circadian", min_pair_support=2, min_strength=0.4,
            max_modes=4, max_reps_per_mode=4,
        )
        self.assertEqual(len(modes), 2)
        parts = {mode.partition for mode in modes}
        self.assertEqual(parts, {"night", "morning"})
        rep_family = _rep_to_family(g, mem)
        by_part = {
            mode.partition: frozenset(rep_family[r] for r in mode.reps)
            for mode in modes
        }
        self.assertEqual(by_part["night"], frozenset({"A", "B"}))
        self.assertEqual(by_part["morning"], frozenset({"B", "C"}))

    def test_weekday_monday_and_friday_do_not_merge(self) -> None:
        mem = self._mem([
            (1, "A", "2026-03-02T12:00:00+00:00"),  # Monday
            (2, "A", "2026-03-09T12:00:00+00:00"),  # Monday
            (3, "B", "2026-03-02T15:00:00+00:00"),
            (4, "B", "2026-03-09T15:00:00+00:00"),
            (5, "B", "2026-03-06T12:00:00+00:00"),  # Friday
            (6, "B", "2026-03-13T12:00:00+00:00"),  # Friday
            (7, "C", "2026-03-06T15:00:00+00:00"),
            (8, "C", "2026-03-13T15:00:00+00:00"),
        ])
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        modes = g.cluster_coactivation(
            bucket_by="weekday", min_pair_support=2, min_strength=0.4,
            max_modes=4, max_reps_per_mode=4,
        )
        self.assertEqual(len(modes), 2)
        parts = {mode.partition for mode in modes}
        self.assertEqual(parts, {"monday", "friday"})
        rep_family = _rep_to_family(g, mem)
        by_part = {
            mode.partition: frozenset(rep_family[r] for r in mode.reps)
            for mode in modes
        }
        self.assertEqual(by_part["monday"], frozenset({"A", "B"}))
        self.assertEqual(by_part["friday"], frozenset({"B", "C"}))

    def test_missing_created_at_is_unbucketable(self) -> None:
        mem = self._mem([
            (1, "A", ""),
            (2, "A", ""),
            (3, "B", ""),
            (4, "B", ""),
        ])
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(len(g.topic_clusters()), 2)
        self.assertEqual(
            g.cluster_coactivation(
                bucket_by="day", min_pair_support=1, min_strength=0.0,
            ),
            [],
        )
        self.assertEqual(
            g.cluster_coactivation(
                bucket_by="circadian", min_pair_support=1, min_strength=0.0,
            ),
            [],
        )

    def test_cluster_coactivations_matches_single_axis_calls(self) -> None:
        mem = self._two_mode_store_days()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        multi = g.cluster_coactivations(
            axes=("session", "day"),
            min_pair_support=2, min_strength=0.5,
        )
        for axis in ("session", "day"):
            single = g.cluster_coactivation(
                bucket_by=axis, min_pair_support=2, min_strength=0.5,
            )
            self.assertEqual(
                [m.reps for m in multi[axis]],
                [m.reps for m in single],
                axis,
            )

    def _two_mode_store_days(self) -> _StubMemoryStore:
        return self._mem([
            (1, "A", "2026-03-01T12:00:00+00:00", "s1"),
            (2, "A", "2026-03-02T12:00:00+00:00", "s2"),
            (3, "B", "2026-03-01T12:00:00+00:00", "s1"),
            (4, "B", "2026-03-02T12:00:00+00:00", "s2"),
            (5, "C", "2026-03-10T12:00:00+00:00", "s3"),
            (6, "C", "2026-03-11T12:00:00+00:00", "s4"),
            (7, "D", "2026-03-10T12:00:00+00:00", "s3"),
            (8, "D", "2026-03-11T12:00:00+00:00", "s4"),
        ])

    def test_session_modes_have_empty_partition(self) -> None:
        mem = CoactivationDetectionTests()._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        for mode in g.cluster_coactivation(min_pair_support=2, min_strength=0.5):
            self.assertEqual(mode.partition, "")


class CoarsePeriodAndPrimingTests(unittest.TestCase):
    def test_coarse_period_collapses_seven_bins(self) -> None:
        self.assertEqual(coarse_coactivation_period(2), "night")
        self.assertEqual(coarse_coactivation_period(23), "night")
        self.assertEqual(coarse_coactivation_period(7), "morning")
        self.assertEqual(coarse_coactivation_period(13), "morning")
        self.assertEqual(coarse_coactivation_period(16), "afternoon")
        self.assertEqual(coarse_coactivation_period(20), "evening")

    def test_temporal_prime_reps_follows_clock(self) -> None:
        night = CoactivationMode(
            reps=(10, 11), labels=("a", "b"), strength=0.9,
            bucket_by="circadian", partition="night",
        )
        morning = CoactivationMode(
            reps=(20, 21), labels=("c", "d"), strength=0.8,
            bucket_by="circadian", partition="morning",
        )
        monday = CoactivationMode(
            reps=(30, 31), labels=("e", "f"), strength=0.7,
            bucket_by="weekday", partition="monday",
        )
        session = CoactivationMode(
            reps=(1, 2), labels=("x", "y"), strength=1.0,
            bucket_by="session",
        )
        modes = [night, morning, monday, session]
        self.assertEqual(
            temporal_prime_reps(modes, period="night", weekday="tuesday"),
            [10, 11],
        )
        self.assertEqual(
            temporal_prime_reps(modes, period="morning", weekday="monday"),
            [20, 21, 30, 31],
        )
        self.assertEqual(
            temporal_prime_reps(modes, period="afternoon", weekday="friday"),
            [],
        )

    def test_hint_lines_tag_the_axis(self) -> None:
        mode = CoactivationMode(
            reps=(10, 11), labels=("a", "b"), strength=0.9,
            bucket_by="circadian", partition="night",
        )
        lines = format_coactivation_hint_lines([mode], {10, 11})
        self.assertEqual(lines, ["- [10, 11] (at night)"])


if __name__ == "__main__":
    unittest.main()
