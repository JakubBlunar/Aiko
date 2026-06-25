"""Tests for K64a — the associative-wandering worker + surfacing provider.

Covers the cue producer
(:class:`~app.core.proactive.associative_wander_worker.AssociativeWanderWorker`),
its pure helpers (``pair_key`` / ``find_distant_pairs`` / ``wander_relevant``),
and the inner-life consumer
(:meth:`InnerLifePart2Mixin._render_associative_wander_block`).
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.proactive.associative_wander_worker import (
    ASSOCIATIVE_WANDER_JOURNAL_KEY,
    AssociativeWanderWorker,
    _KV_PAIR_COOLDOWNS,
    append_wander,
    find_distant_pairs,
    load_wanders,
    pair_key,
    wander_relevant,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _Cluster:
    cluster_id: int
    summary: str
    size: int
    centroid: np.ndarray
    member_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass
class _Mem:
    content: str


class _FakeGraph:
    def __init__(self, clusters: list[_Cluster]) -> None:
        self._clusters = clusters

    def topic_clusters(self) -> list[_Cluster]:
        return list(self._clusters)

    def cluster_member_ids(self, cluster_id: int) -> list[int]:
        for c in self._clusters:
            if c.cluster_id == cluster_id:
                return list(c.member_ids)
        return []


class _FakeStore:
    def __init__(self, mems: dict[int, _Mem]) -> None:
        self._mems = mems

    def get(self, memory_id: int) -> _Mem | None:
        return self._mems.get(memory_id)


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


class _FakeLLM:
    """Returns a canned JSON connection (or a no-connection verdict)."""

    def __init__(self, *, connects: bool = True, connection: str = "both reward patience") -> None:
        self._connects = connects
        self._connection = connection
        self.calls = 0

    def chat_json(self, messages, *, model, options, format_json, surface):
        self.calls += 1
        return (
            json.dumps(
                {"connects": self._connects, "connection": self._connection}
            ),
            None,
        )


def _vec(*xs: float) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


def _make_worker(graph, kv, *, llm=None, store=None, **kw) -> AssociativeWanderWorker:
    params: dict = {
        "interval_seconds": 5400.0,
        "cooldown_seconds": 0.0,
        "daily_cap": 5,
        "journal_max": 6,
        "min_size": 4,
        "max_pair_cosine": 0.25,
        "pair_cooldown_hours": 168.0,
        "member_samples": 0,
    }
    params.update(kw)
    return AssociativeWanderWorker(
        topic_graph_provider=lambda: graph,
        memory_store=store or _FakeStore({}),
        kv_get=kv.kv_get,
        kv_set=kv.kv_set,
        ollama=llm if llm is not None else _FakeLLM(),
        model="worker-model",
        **params,
    )


# ── pure helpers ─────────────────────────────────────────────────────────


class HelperTests(unittest.TestCase):
    def test_pair_key_order_independent_and_normalised(self) -> None:
        self.assertEqual(
            pair_key("Hiking  Trails", "rust debugging"),
            pair_key("rust debugging", "hiking trails"),
        )
        self.assertNotEqual(pair_key("a", "b"), pair_key("a", "c"))

    def test_find_distant_pairs_selects_far_excludes_near(self) -> None:
        clusters = [
            _Cluster(1, "hiking", 5, _vec(1, 0, 0)),
            _Cluster(2, "rust debugging", 6, _vec(0, 1, 0)),  # cos 0 vs c1
            _Cluster(3, "trail running", 5, _vec(0.98, 0.2, 0)),  # near c1
        ]
        pairs = find_distant_pairs(clusters, max_cosine=0.25, min_size=4)
        keys = {(p.cluster_id_a, p.cluster_id_b) for p in pairs}
        # (1,2) distant, (2,3) distant; (1,3) is a near neighbour → excluded.
        self.assertIn((1, 2), keys)
        self.assertIn((2, 3), keys)
        self.assertNotIn((1, 3), keys)
        # Sorted most-distant first.
        self.assertLessEqual(pairs[0].cosine, pairs[-1].cosine)

    def test_find_distant_pairs_filters_small_and_unlabelled(self) -> None:
        clusters = [
            _Cluster(1, "hiking", 2, _vec(1, 0, 0)),  # too small
            _Cluster(2, "", 9, _vec(0, 1, 0)),  # blank label
            _Cluster(3, "rust", 9, _vec(0, 0, 1)),
        ]
        self.assertEqual(
            find_distant_pairs(clusters, max_cosine=0.25, min_size=4), []
        )

    def test_wander_relevant_either_topic(self) -> None:
        entry = {"topic_a": "hiking trails", "topic_b": "rust debugging"}
        self.assertTrue(wander_relevant(entry, "I went hiking today"))
        self.assertTrue(wander_relevant(entry, "debugging some rust code"))
        self.assertFalse(wander_relevant(entry, "let's talk about wine"))


# ── worker ───────────────────────────────────────────────────────────────


def _two_distant() -> _FakeGraph:
    return _FakeGraph(
        [
            _Cluster(1, "hiking trails", 5, _vec(1, 0, 0), (10, 11)),
            _Cluster(2, "rust debugging", 6, _vec(0, 1, 0), (20, 21)),
        ]
    )


class WorkerTests(unittest.TestCase):
    def test_drafts_connection(self) -> None:
        kv = _KV()
        worker = _make_worker(_two_distant(), kv)
        result = worker.run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["connection"], "both reward patience")
        ring = load_wanders(kv.kv_get)
        self.assertEqual(len(ring), 1)
        self.assertEqual(ring[0]["topic_a"], "hiking trails")
        self.assertEqual(ring[0]["topic_b"], "rust debugging")
        self.assertEqual(
            ring[0]["pair_key"], pair_key("hiking trails", "rust debugging")
        )

    def test_feeds_member_snippets_to_llm(self) -> None:
        kv = _KV()
        store = _FakeStore({10: _Mem("summited a ridge"), 20: _Mem("traced a borrow bug")})
        llm = _FakeLLM()
        worker = _make_worker(_two_distant(), kv, llm=llm, store=store, member_samples=2)
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(llm.calls, 1)

    def test_no_graph(self) -> None:
        kv = _KV()
        worker = AssociativeWanderWorker(
            topic_graph_provider=lambda: None,
            memory_store=_FakeStore({}),
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
        )
        self.assertTrue(worker.run().get("no_graph"))

    def test_no_distant_pair(self) -> None:
        # Two near clusters → nothing distant enough to connect.
        graph = _FakeGraph(
            [
                _Cluster(1, "hiking", 5, _vec(1, 0, 0)),
                _Cluster(2, "trail running", 5, _vec(0.99, 0.14, 0)),
            ]
        )
        self.assertTrue(_make_worker(graph, _KV()).run().get("no_pair"))

    def test_disabled(self) -> None:
        worker = _make_worker(
            _two_distant(), _KV(), enabled_provider=lambda: False
        )
        self.assertTrue(worker.run().get("disabled"))

    def test_no_connection_stamps_pair(self) -> None:
        kv = _KV()
        worker = _make_worker(
            _two_distant(), kv, llm=_FakeLLM(connects=False, connection="")
        )
        result = worker.run()
        self.assertTrue(result.get("no_connection"))
        self.assertEqual(load_wanders(kv.kv_get), [])
        # The pair is stamped on cooldown so it isn't retried every tick.
        cooldowns = json.loads(kv.d[_KV_PAIR_COOLDOWNS])
        self.assertIn(pair_key("hiking trails", "rust debugging"), cooldowns)

    def test_pair_cooldown_blocks_redraft(self) -> None:
        kv = _KV()
        worker = _make_worker(_two_distant(), kv)
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("all_on_cooldown"))

    def test_force_next_bypasses_pair_cooldown(self) -> None:
        kv = _KV()
        worker = _make_worker(_two_distant(), kv)
        worker.run()
        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(len(load_wanders(kv.kv_get)), 2)

    def test_global_cooldown_blocks(self) -> None:
        kv = _KV()
        worker = _make_worker(_two_distant(), kv, cooldown_seconds=3600.0)
        self.assertEqual(worker.run()["drafted"], 1)
        # Different pair would exist, but the global cooldown gate fires.
        self.assertTrue(worker.run().get("skipped_cooldown"))

    def test_daily_cap_blocks(self) -> None:
        kv = _KV()
        worker = _make_worker(
            _two_distant(), kv, daily_cap=1, pair_cooldown_hours=0.0
        )
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("skipped_daily_cap"))

    def test_journal_trims_to_max(self) -> None:
        kv = _KV()
        for i in range(10):
            append_wander(
                kv.kv_get, kv.kv_set,
                {"at": str(i), "topic_a": f"a{i}", "topic_b": f"b{i}",
                 "pair_key": f"k{i}", "connection": "x"},
                max_entries=6,
            )
        self.assertEqual(len(load_wanders(kv.kv_get)), 6)


# ── provider ─────────────────────────────────────────────────────────────


class _Agent:
    associative_wander_enabled = True


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self) -> None:
        self._settings = _Settings()
        self._chat_db = _KV()
        self._associative_wander_force_next = False


class ProviderTests(unittest.TestCase):
    def _seed(self, host: _Host) -> None:
        append_wander(
            host._chat_db.kv_get,
            host._chat_db.kv_set,
            {
                "at": "2026-01-01T00:00:00+00:00",
                "topic_a": "hiking trails",
                "topic_b": "rust debugging",
                "pair_key": pair_key("hiking trails", "rust debugging"),
                "connection": "both reward following a faint trail patiently",
            },
            max_entries=6,
        )

    def test_empty_ring_returns_blank(self) -> None:
        self.assertEqual(
            _Host()._render_associative_wander_block("I went hiking"), ""
        )

    def test_disabled_returns_blank(self) -> None:
        host = _Host()
        host._settings.agent.associative_wander_enabled = False
        self._seed(host)
        self.assertEqual(
            host._render_associative_wander_block("I went hiking"), ""
        )

    def test_surfaces_on_topic_relevant_turn(self) -> None:
        host = _Host()
        self._seed(host)
        out = host._render_associative_wander_block("I went hiking today")
        self.assertIn("hiking trails", out)
        self.assertIn("rust debugging", out)
        self.assertIn("connection", out.lower())

    def test_not_relevant_returns_blank(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertEqual(
            host._render_associative_wander_block("tell me about wine"), ""
        )

    def test_surfaced_once_only(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertTrue(host._render_associative_wander_block("hiking trip"))
        self.assertEqual(host._render_associative_wander_block("hiking trip"), "")

    def test_force_next_bypasses_relevance(self) -> None:
        host = _Host()
        self._seed(host)
        host._associative_wander_force_next = True
        out = host._render_associative_wander_block("")
        self.assertIn("hiking trails", out)


if __name__ == "__main__":
    unittest.main()
