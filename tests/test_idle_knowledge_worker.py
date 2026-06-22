"""Tests for the F9 interest-driven knowledge enrichment worker.

The worker reads the K9 topic graph, picks the densest
under-researched interest cluster, scrubs its summary into a safe
search query, web-searches it, distils one or two impersonal
``knowledge`` facts with the LLM, and writes them silently.

We exercise the flow against a real :class:`MemoryStore` +
:class:`ChatDatabase` (so writes and the kv cooldown map land in real
rows) and patch :func:`build_topic_graph_snapshot` so the cluster
fixture is controlled without standing up a real clustering pass.
Only the LLM and the web-search tool are stubbed.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from unittest import mock

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.memory.memory_store import MemoryStore
from app.core.proactive.idle_knowledge_worker import (
    IdleKnowledgeWorker,
    KnowledgeFact,
)


# ── tiny stubs ─────────────────────────────────────────────────────────


class _DeterministicEmbedder:
    """Token-slot embedder using md5 so results are PYTHONHASHSEED-stable."""

    DIM = 16

    @staticmethod
    def _slot(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % _DeterministicEmbedder.DIM

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            vec[self._slot(token)] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


@dataclass
class _StubWebSearch:
    payload: dict[str, Any] = field(
        default_factory=lambda: {
            "results": [
                {
                    "title": "Italian roast coffee",
                    "url": "https://en.example.org/italian-roast",
                    "snippet": (
                        "Italian roast is a very dark roast level, roasted "
                        "until the beans are nearly black and oily."
                    ),
                },
            ],
        }
    )
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_call: bool = False

    def run(self, args: dict[str, Any]) -> str:
        self.calls.append(dict(args))
        if self.raise_on_call:
            raise RuntimeError("simulated search outage")
        return json.dumps(self.payload)


@dataclass
class _StubOllamaClient:
    facts_json: dict[str, Any] = field(
        default_factory=lambda: {
            "facts": [
                {
                    "text": (
                        "Italian roast is one of the darkest common roast "
                        "levels, producing a bittersweet, smoky flavour."
                    ),
                    "confidence": 0.8,
                },
            ],
        }
    )
    raise_on_call: bool = False
    chat_calls: list[dict[str, Any]] = field(default_factory=list)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        *,
        model: str | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        format_json: bool = False,
        think: bool = False,
        **kwargs: Any,
    ) -> Iterable[str]:
        self.chat_calls.append(
            {
                "messages": messages,
                "model": model,
                "format_json": format_json,
            }
        )
        if self.raise_on_call:
            raise RuntimeError("simulated ollama outage")
        if stop_event is not None and stop_event.is_set():
            return
        yield json.dumps(self.facts_json)


@dataclass
class _StubAgent:
    knowledge_enrichment_enabled: bool = True
    knowledge_enrichment_per_hour_cap: int = 5
    knowledge_enrichment_per_day_cap: int = 20


@dataclass
class _StubMemorySettings:
    knowledge_enrichment_interval_seconds: int = 3600
    knowledge_cluster_cooldown_hours: int = 72
    knowledge_enrichment_max_per_cluster: int = 3


# ── snapshot fixture ───────────────────────────────────────────────────


def _snapshot(*clusters: dict[str, Any]) -> dict[str, Any]:
    return {"enabled": True, "clusters": list(clusters)}


def _cluster(
    *,
    cluster_id: int = 1,
    summary: str = "italian dark roast coffee and espresso",
    size: int = 5,
    knowledge_count: int = 0,
) -> dict[str, Any]:
    kind_counts: dict[str, int] = {"preference": size}
    if knowledge_count:
        kind_counts["knowledge"] = knowledge_count
    return {
        "cluster_id": cluster_id,
        "summary": summary,
        "size": size,
        "kind_counts": kind_counts,
    }


# ── shared fixture ─────────────────────────────────────────────────────


def _build_world(
    *,
    enabled: bool = True,
    facts_json: dict[str, Any] | None = None,
    user_names: list[str] | None = None,
    raise_search: bool = False,
) -> dict[str, Any]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    rate_limiter = FactCheckRateLimiter(
        chat_db,
        per_hour_cap=5,
        per_day_cap=20,
        state_key="idle_knowledge.rate_state",
    )
    web_search = _StubWebSearch(raise_on_call=raise_search)
    ollama = _StubOllamaClient()
    if facts_json is not None:
        ollama.facts_json = facts_json
    cancel_event = threading.Event()
    added_calls: list[dict[str, Any]] = []

    worker = IdleKnowledgeWorker(
        memory_store=memory_store,
        embedder=embedder,
        ollama=ollama,
        chat_model="stub-model",
        web_search_tool=web_search,
        rate_limiter=rate_limiter,
        cancel_event=cancel_event,
        agent_settings=_StubAgent(knowledge_enrichment_enabled=enabled),
        memory_settings=_StubMemorySettings(),
        topic_graph_provider=lambda: object(),
        kv_get=chat_db.kv_get,
        kv_set=chat_db.kv_set,
        user_names_provider=(lambda: user_names or []),
        assistant_name_provider=(lambda: None),
        notify_memory_added=lambda d: added_calls.append(d),
    )
    return {
        "chat_db": chat_db,
        "memory_store": memory_store,
        "embedder": embedder,
        "rate_limiter": rate_limiter,
        "web_search": web_search,
        "ollama": ollama,
        "cancel_event": cancel_event,
        "worker": worker,
        "added_calls": added_calls,
    }


@contextmanager
def _patched_snapshot(snapshot: dict[str, Any]):
    with mock.patch(
        "app.core.conversation.topic_graph.build_topic_graph_snapshot",
        return_value=snapshot,
    ):
        yield


# ── _parse_facts ───────────────────────────────────────────────────────


class TestParseFacts(unittest.TestCase):
    def test_parses_clean_json(self) -> None:
        facts = IdleKnowledgeWorker._parse_facts(
            json.dumps(
                {"facts": [{"text": "A concrete fact.", "confidence": 0.8}]}
            )
        )
        assert facts is not None
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].text, "A concrete fact.")
        self.assertAlmostEqual(facts[0].confidence, 0.8)

    def test_parses_json_in_prose(self) -> None:
        raw = (
            "Here you go:\n"
            '{"facts": [{"text": "X is a thing.", "confidence": 0.7}]}\n'
            "done"
        )
        facts = IdleKnowledgeWorker._parse_facts(raw)
        assert facts is not None
        self.assertEqual(facts[0].text, "X is a thing.")

    def test_filters_low_confidence(self) -> None:
        facts = IdleKnowledgeWorker._parse_facts(
            json.dumps(
                {
                    "facts": [
                        {"text": "low", "confidence": 0.3},
                        {"text": "high", "confidence": 0.9},
                    ]
                }
            )
        )
        assert facts is not None
        self.assertEqual([f.text for f in facts], ["high"])

    def test_caps_to_two_facts(self) -> None:
        facts = IdleKnowledgeWorker._parse_facts(
            json.dumps(
                {
                    "facts": [
                        {"text": "one", "confidence": 0.9},
                        {"text": "two", "confidence": 0.9},
                        {"text": "three", "confidence": 0.9},
                    ]
                }
            )
        )
        assert facts is not None
        self.assertEqual(len(facts), 2)

    def test_empty_list_for_off_topic(self) -> None:
        facts = IdleKnowledgeWorker._parse_facts(json.dumps({"facts": []}))
        self.assertEqual(facts, [])

    def test_returns_none_on_garbage(self) -> None:
        self.assertIsNone(IdleKnowledgeWorker._parse_facts("not json"))


# ── cluster selection ──────────────────────────────────────────────────


class TestClusterSelection(unittest.TestCase):
    def test_picks_densest_under_researched(self) -> None:
        world = _build_world()
        snap = _snapshot(
            _cluster(cluster_id=1, summary="big topic about woodworking", size=8),
            _cluster(cluster_id=2, summary="smaller topic on gardening", size=3),
        )
        with _patched_snapshot(snap):
            pick = world["worker"]._pick_cluster(now=datetime.now(timezone.utc))
        self.assertIsNotNone(pick)
        assert pick is not None
        self.assertEqual(pick.cluster_id, 1)
        self.assertEqual(pick.size, 8)

    def test_skips_already_researched(self) -> None:
        world = _build_world()
        snap = _snapshot(
            _cluster(
                cluster_id=1,
                summary="topic already covered enough here",
                size=8,
                knowledge_count=3,
            ),
            _cluster(cluster_id=2, summary="fresh topic still open here", size=3),
        )
        with _patched_snapshot(snap):
            pick = world["worker"]._pick_cluster(now=datetime.now(timezone.utc))
        assert pick is not None
        self.assertEqual(pick.cluster_id, 2)

    def test_honors_cooldown(self) -> None:
        world = _build_world()
        now = datetime.now(timezone.utc)
        snap = _snapshot(
            _cluster(cluster_id=1, summary="only topic in the graph here", size=8),
        )
        # Stamp the only cluster's cooldown so it drops out.
        with _patched_snapshot(snap):
            pick = world["worker"]._pick_cluster(now=now)
            assert pick is not None
            world["worker"]._stamp_cooldown(pick.cluster_key, now=now)
            again = world["worker"]._pick_cluster(now=now)
        self.assertIsNone(again)

    def test_picks_after_cooldown_expires(self) -> None:
        world = _build_world()
        now = datetime.now(timezone.utc)
        snap = _snapshot(
            _cluster(cluster_id=1, summary="only topic in the graph here", size=8),
        )
        with _patched_snapshot(snap):
            pick = world["worker"]._pick_cluster(now=now)
            assert pick is not None
            world["worker"]._stamp_cooldown(
                pick.cluster_key, now=now - timedelta(hours=100),
            )
            again = world["worker"]._pick_cluster(now=now)
        self.assertIsNotNone(again)

    def test_skips_too_short_summary(self) -> None:
        world = _build_world()
        snap = _snapshot(_cluster(cluster_id=1, summary="hi", size=8))
        with _patched_snapshot(snap):
            pick = world["worker"]._pick_cluster(now=datetime.now(timezone.utc))
        self.assertIsNone(pick)


# ── end-to-end ─────────────────────────────────────────────────────────


class TestRunSuccessPath(unittest.TestCase):
    def test_writes_knowledge_and_stamps_cooldown(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        snap = _snapshot(_cluster())
        with _patched_snapshot(snap):
            result = world["worker"].run()
        self.assertEqual(result.get("outcome"), "wrote")
        self.assertGreaterEqual(result.get("wrote", 0), 1)

        rows = store.iter_by_kind("knowledge")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.kind, "knowledge")
        self.assertEqual(row.tier, "long_term")
        self.assertLessEqual(row.confidence, 0.9)
        self.assertEqual(row.metadata.get("source_url"), "https://en.example.org/italian-roast")
        self.assertIn("cluster_key", row.metadata)
        # Listener fired for the new knowledge row.
        self.assertEqual(len(world["added_calls"]), 1)
        # Web search ran exactly once.
        self.assertEqual(len(world["web_search"].calls), 1)
        # Cooldown was stamped for the researched cluster.
        cooldowns = world["worker"]._load_cooldowns()
        self.assertEqual(len(cooldowns), 1)

    def test_consumes_one_rate_token(self) -> None:
        world = _build_world()
        snap = _snapshot(_cluster())
        before = world["rate_limiter"].snapshot()
        self.assertEqual(before["hour_used"], 0)
        with _patched_snapshot(snap):
            world["worker"].run()
        after = world["rate_limiter"].snapshot()
        self.assertEqual(after["hour_used"], 1)


class TestRunNonWritePaths(unittest.TestCase):
    def test_no_search_results(self) -> None:
        world = _build_world()
        world["web_search"].payload = {"results": []}
        snap = _snapshot(_cluster())
        with _patched_snapshot(snap):
            result = world["worker"].run()
        self.assertEqual(result.get("outcome"), "no_results")
        self.assertEqual(len(world["ollama"].chat_calls), 0)

    def test_inconclusive_distil(self) -> None:
        world = _build_world(facts_json={"facts": []})
        snap = _snapshot(_cluster())
        with _patched_snapshot(snap):
            result = world["worker"].run()
        self.assertEqual(result.get("outcome"), "inconclusive")
        self.assertEqual(world["memory_store"].iter_by_kind("knowledge"), [])

    def test_privacy_gate_skips_search(self) -> None:
        world = _build_world()
        snap = _snapshot(
            _cluster(summary="what jacob@example.com keeps emailing about"),
        )
        with _patched_snapshot(snap):
            result = world["worker"].run()
        self.assertEqual(result.get("reason"), "privacy_gate")
        self.assertEqual(world["web_search"].calls, [])


class TestRunGuards(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        world = _build_world(enabled=False)
        snap = _snapshot(_cluster())
        with _patched_snapshot(snap):
            result = world["worker"].run()
        self.assertEqual(result.get("reason"), "disabled")
        self.assertEqual(world["web_search"].calls, [])

    def test_no_cluster_returns_skipped(self) -> None:
        world = _build_world()
        with _patched_snapshot(_snapshot()):
            result = world["worker"].run()
        self.assertEqual(result.get("reason"), "no_cluster")

    def test_pre_set_cancel_aborts(self) -> None:
        world = _build_world()
        world["cancel_event"].set()
        snap = _snapshot(_cluster())
        with _patched_snapshot(snap):
            result = world["worker"].run()
        self.assertEqual(result.get("reason"), "cancelled_before_start")


class TestIsReady(unittest.TestCase):
    def test_disabled_not_ready(self) -> None:
        world = _build_world(enabled=False)
        self.assertFalse(
            world["worker"].is_ready(
                now=datetime.now(timezone.utc), last_run_at=None,
            )
        )

    def test_no_cluster_not_ready(self) -> None:
        world = _build_world()
        with _patched_snapshot(_snapshot()):
            ready = world["worker"].is_ready(
                now=datetime.now(timezone.utc), last_run_at=None,
            )
        self.assertFalse(ready)

    def test_ready_when_cluster_pending(self) -> None:
        world = _build_world()
        with _patched_snapshot(_snapshot(_cluster())):
            ready = world["worker"].is_ready(
                now=datetime.now(timezone.utc), last_run_at=None,
            )
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()
