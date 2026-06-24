"""F10l — cluster management facade (rename / pin all / forget topic).

Exercises the three :class:`MemoryFacadeMixin` methods against a real
:class:`MemoryStore` + a real persistent :class:`TopicGraph`, through a
minimal host that mixes in the facade (no full SessionController).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.topic_cluster_store import TopicClusterStore
from app.core.conversation.topic_graph import TopicGraph
from app.core.memory.memory_store import MemoryStore
from app.core.session.memory_facade_mixin import MemoryFacadeMixin


class _TokenEmbedder:
    DIM = 64

    @staticmethod
    def _slot(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % _TokenEmbedder.DIM

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            vec[self._slot(token)] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


class _Host(MemoryFacadeMixin):
    def __init__(self, store, graph, chat_db, embedder) -> None:
        self._memory_store = store
        self._topic_graph = graph
        self._chat_db = chat_db
        self._embedder = embedder
        self._memory_listeners = []
        self._memory_updated_listeners = []


def _build() -> tuple[_Host, MemoryStore, TopicGraph, ChatDatabase]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "mem.db"
    # ChatDatabase first: it runs the migrations that create the
    # ``memories`` / ``kv_meta`` / topic-cluster tables MemoryStore +
    # TopicClusterStore then read/write on the same file.
    db = ChatDatabase(path)
    store = MemoryStore(path)
    embedder = _TokenEmbedder()
    # Two clear topics.
    seeds = [
        "cats nap warm sunny windowsill",
        "cats warm sunny purr nap",
        "warm cats curled sunny nap",
        "basil rosemary herbs clay pots",
        "rosemary herbs clay pots water",
        "herbs basil pots water rosemary",
    ]
    for text in seeds:
        store.add(text, "fact", embedder.embed(text), salience=0.6, skip_dedupe=True)

    graph = TopicGraph(
        store,
        similarity=0.40,
        min_cluster_size=2,
        filter_threshold=0.50,
        cluster_store=TopicClusterStore(db),
    )
    graph.rebuild()
    host = _Host(store, graph, db, embedder)
    return host, store, graph, db


class RenameTopicClusterTests(unittest.TestCase):
    def test_rename_sets_label_and_pins_in_cache(self) -> None:
        host, _store, graph, db = _build()
        cluster = graph.topic_clusters()[0]
        cid = cluster.cluster_id
        rep = cluster.representative_id

        result = host.rename_topic_cluster(cid, "  favourite cats  ")
        self.assertIsNotNone(result)
        self.assertEqual(result["summary"], "favourite cats")
        # Live label updated.
        summary = next(
            c.summary for c in graph.topic_clusters() if c.cluster_id == cid
        )
        self.assertEqual(summary, "favourite cats")
        # Cache pinned by representative.
        raw = db.kv_get("aiko.topic_label." + str(rep))
        self.assertIsNotNone(raw)
        cached = json.loads(raw)
        self.assertEqual(cached["label"], "favourite cats")
        self.assertTrue(cached["user_pinned"])

    def test_blank_label_rejected(self) -> None:
        host, _s, graph, _db = _build()
        cid = graph.topic_clusters()[0].cluster_id
        self.assertIsNone(host.rename_topic_cluster(cid, "   "))

    def test_unknown_cluster_returns_none(self) -> None:
        host, *_ = _build()
        self.assertIsNone(host.rename_topic_cluster(99999, "nope"))


class PinTopicClusterTests(unittest.TestCase):
    def test_pin_all_members(self) -> None:
        host, store, graph, _db = _build()
        cluster = graph.topic_clusters()[0]
        result = host.set_topic_cluster_pinned(cluster.cluster_id, True)
        self.assertIsNotNone(result)
        self.assertEqual(result["affected"], cluster.size)
        for mid in cluster.member_ids:
            self.assertTrue(store.get(int(mid)).pinned)
        # Unpin round-trips.
        host.set_topic_cluster_pinned(cluster.cluster_id, False)
        for mid in cluster.member_ids:
            self.assertFalse(store.get(int(mid)).pinned)

    def test_unknown_cluster_returns_none(self) -> None:
        host, *_ = _build()
        self.assertIsNone(host.set_topic_cluster_pinned(99999, True))


class ForgetTopicClusterTests(unittest.TestCase):
    def test_forget_archives_non_pinned_members(self) -> None:
        host, store, graph, _db = _build()
        cluster = graph.topic_clusters()[0]
        members = list(cluster.member_ids)
        # Pin one member -- it must survive the forget.
        kept = int(members[0])
        host.set_memory_pinned(kept, True)

        result = host.forget_topic_cluster(cluster.cluster_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["skipped_pinned"], 1)
        self.assertEqual(result["archived"], len(members) - 1)
        self.assertEqual(store.get(kept).tier, "long_term")  # pinned -> long_term
        for mid in members:
            if int(mid) == kept:
                continue
            self.assertEqual(store.get(int(mid)).tier, "archive")

    def test_unknown_cluster_returns_none(self) -> None:
        host, *_ = _build()
        self.assertIsNone(host.forget_topic_cluster(99999))


if __name__ == "__main__":
    unittest.main()
