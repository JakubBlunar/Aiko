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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.core.proactive.associative_wander_worker import (
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


_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _cue_store():
    """A real CueStore on a throwaway database."""
    from tempfile import TemporaryDirectory

    from app.core.infra.chat_database import ChatDatabase
    from app.core.proactive.cue_store import CueStore

    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))
    return store, tmp


def _make_worker(graph, kv, *, llm=None, store=None, **kw) -> AssociativeWanderWorker:
    params: dict = {
        "interval_seconds": 5400.0,
        "cooldown_seconds": 0.0,
        "journal_max": 6,
        "min_size": 4,
        "max_pair_cosine": 0.25,
        # These fixtures hand-build two or three clusters and assert on
        # the geometry, so they want every qualifying pair. The shipped
        # 0.10 has its own tests in DistantPairSelectionTests.
        "pair_quantile": 1.0,
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
        pairs = find_distant_pairs(
            clusters, max_cosine=0.25, min_size=4, quantile=1.0,
        )
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
            find_distant_pairs(
                clusters, max_cosine=0.25, min_size=4, quantile=1.0,
            ),
            [],
        )

    def test_a_realistic_embedding_spread_still_yields_pairs(self) -> None:
        """The regression this exists for.

        The gate was ``cos <= 0.25``, which is a number in the abstract
        and not a property of any real embedding space -- sentence
        encoders put genuinely unrelated text around 0.3-0.5, not near
        zero. Over the live topic graph the *minimum* cosine across all
        561 eligible pairs was 0.2648, so nothing could ever qualify and
        the worker reported ``no_pair`` on 107 consecutive runs. Ranking
        within the corpus cannot be put out of reach that way.
        """
        # Every pair here sits in the 0.30-0.75 band a real encoder
        # produces -- i.e. all of them would have failed the old bar.
        clusters = [
            _Cluster(1, "anime series details", 9, _vec(1.00, 0.00, 0.00)),
            _Cluster(2, "finding inner stillness", 9, _vec(0.35, 0.94, 0.00)),
            _Cluster(3, "GPU hardware issues", 9, _vec(0.45, 0.30, 0.84)),
            _Cluster(4, "gardening and walking", 9, _vec(0.60, 0.62, 0.50)),
        ]
        self.assertEqual(
            find_distant_pairs(clusters, max_cosine=0.25, min_size=4), [],
            "fixture must fail the old absolute bar, or it proves nothing",
        )
        pairs = find_distant_pairs(clusters, max_cosine=0.60, min_size=4)
        self.assertTrue(pairs)

    def test_quantile_keeps_the_most_distant_slice(self) -> None:
        clusters = [
            _Cluster(1, "a", 9, _vec(1.00, 0.00, 0.00)),
            _Cluster(2, "b", 9, _vec(0.35, 0.94, 0.00)),
            _Cluster(3, "c", 9, _vec(0.45, 0.30, 0.84)),
            _Cluster(4, "d", 9, _vec(0.60, 0.62, 0.50)),
        ]
        every = find_distant_pairs(
            clusters, max_cosine=1.0, min_size=4, quantile=1.0,
        )
        slice_ = find_distant_pairs(
            clusters, max_cosine=1.0, min_size=4, quantile=0.5,
        )
        self.assertEqual(len(slice_), 3)
        self.assertEqual(len(every), 6)
        # And it is the *far* half, not an arbitrary one.
        self.assertEqual([p.key for p in slice_], [p.key for p in every[:3]])

    def test_a_tiny_graph_still_offers_its_furthest_pair(self) -> None:
        """``ceil`` rather than ``round``: on a two-cluster graph a 10%
        quantile must not floor to zero and reproduce the old silence."""
        clusters = [
            _Cluster(1, "a", 9, _vec(1.0, 0.0, 0.0)),
            _Cluster(2, "b", 9, _vec(0.4, 0.9, 0.0)),
        ]
        pairs = find_distant_pairs(
            clusters, max_cosine=0.60, min_size=4, quantile=0.10,
        )
        self.assertEqual(len(pairs), 1)

    def test_the_ceiling_still_rejects_a_uniform_corpus(self) -> None:
        """Relative selection must not mean "always finds something": a
        corpus where every topic is the same topic has no distant pair,
        and saying otherwise is the failure mode the ceiling prevents."""
        clusters = [
            _Cluster(1, "rust borrow checker", 9, _vec(1.00, 0.02, 0.00)),
            _Cluster(2, "rust lifetimes", 9, _vec(0.99, 0.05, 0.01)),
            _Cluster(3, "rust async", 9, _vec(0.98, 0.08, 0.02)),
        ]
        self.assertEqual(
            find_distant_pairs(clusters, max_cosine=0.60, min_size=4), [],
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


class PoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, tmp = _cue_store()
        self.addCleanup(tmp.cleanup)

    def _worker(self, graph, kv=None, **kw) -> AssociativeWanderWorker:
        return _make_worker(
            graph,
            kv or _KV(),
            cue_store_provider=lambda: self.store,
            **kw,
        )

    def test_run_queues_the_pair_as_one_subject(self) -> None:
        result = self._worker(_two_distant()).run()
        self.assertGreater(result["cue_id"], 0)
        rows = self.store.pending("associative_wander")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject, "hiking trails / rust debugging")
        self.assertIn("both reward patience", rows[0].text)

    def test_an_empty_shelf_reports_full_pressure(self) -> None:
        signal = self._worker(_FakeGraph([])).demand(
            now=_NOW, last_run_at=None,
        )
        self.assertEqual(signal.pressure, 1.0)
        self.assertTrue(signal.needs_llm)

    def test_a_pooled_pair_is_not_re_drafted(self) -> None:
        """Even after the kv cooldown map is wiped."""
        kv = _KV()
        worker = self._worker(_two_distant(), kv, pair_cooldown_hours=0.0)
        worker.run()
        kv.d.clear()
        self.assertTrue(worker.run().get("all_on_cooldown"))

    def test_demand_is_none_without_a_pool(self) -> None:
        worker = _make_worker(_FakeGraph([]), _KV())
        self.assertIsNone(worker.demand(now=_NOW, last_run_at=None))

    def test_disabled_worker_reports_zero_not_none(self) -> None:
        worker = self._worker(_FakeGraph([]), enabled_provider=lambda: False)
        self.assertEqual(
            worker.demand(now=_NOW, last_run_at=None).pressure, 0.0,
        )

    def test_is_ready_still_vetoes_on_the_wall_clock_cooldown(self) -> None:
        """The rate limiter guards an LLM call, so demand may not override."""
        kv = _KV()
        worker = self._worker(_two_distant(), kv, cooldown_seconds=3600.0)
        worker.run()
        self.assertFalse(worker.is_ready(now=_NOW, last_run_at=None))


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
        self.debug_overrides.disarm("associative_wander_force_next")


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
        host.debug_overrides.arm("associative_wander_force_next")
        out = host._render_associative_wander_block("")
        self.assertIn("hiking trails", out)


if __name__ == "__main__":
    unittest.main()
