"""F2.1 personality backlog tests for the idle gap resolver.

Covers:
- Resolves when a matching ``preference`` / ``fact`` memory exists.
- Does not resolve when only ``self_tagged`` or ``knowledge_gap``
  candidates pass the cosine threshold (excluded answer kinds).
- Does not resolve below the threshold.
- ``mark_resolved`` audit metadata (``resolved_by``,
  ``resolved_by_memory_id``, ``resolved_similarity``).
- Already-resolved gaps stay closed (no double-resolve).
- Per-tick cap bounds the work the worker does.
- Disabled / cancelled / no-open paths short-circuit cleanly.
- ``is_ready`` respects the interval gate and the "no open gaps" gate.

The deterministic embedder is shared with
``tests/test_knowledge_gap_extractor.py`` -- token slots collide on
hash so two strings with overlapping content cosine-match.
"""
from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.idle_gap_resolver import IdleGapResolver
from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore
from app.core.memory.memory_store import MemoryStore
from app.core.infra.settings import AgentSettings


class _DeterministicEmbedder:
    """Stable token-slot embedder. Uses md5 instead of ``hash()`` so the
    same string maps to the same slot across Python runs (PYTHONHASHSEED
    is randomized by default, which made similar-content tests flaky in
    the F2 suite). The same pattern is mirrored across every
    ``_DeterministicEmbedder`` / ``_StubEmbedder`` / ``_TokenEmbedder``
    helper in the test suite.
    """

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


def _make_resolver(
    *,
    threshold: float = 0.55,
    per_tick: int = 5,
    enabled: bool = True,
    interval: int = 600,
) -> tuple[Path, MemoryStore, KnowledgeGapStore, IdleGapResolver]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    gap_store = KnowledgeGapStore(
        memory_store=memory_store,
        embedder=embedder,
    )
    agent_settings = AgentSettings()
    agent_settings.gap_resolver_enabled = enabled
    agent_settings.gap_resolver_threshold = threshold
    agent_settings.gap_resolver_per_tick = per_tick
    agent_settings.gap_resolver_interval_seconds = interval
    resolver = IdleGapResolver(
        memory_store=memory_store,
        gap_store=gap_store,
        agent_settings=agent_settings,
    )
    return path, memory_store, gap_store, resolver


def _add_memory(
    memory_store: MemoryStore,
    embedder: _DeterministicEmbedder,
    *,
    content: str,
    kind: str,
    tier: str = "long_term",
):
    return memory_store.add(
        content=content,
        kind=kind,
        embedding=embedder.embed(content),
        salience=0.7,
        tier=tier,
    )


class ResolverHappyPathTests(unittest.TestCase):
    def test_resolves_when_matching_preference_exists(self) -> None:
        _, mem, gaps, resolver = _make_resolver()
        embedder = _DeterministicEmbedder()
        # Pre-existing answer memory written by the post-summary
        # extractor on a prior turn.
        answer = _add_memory(
            mem,
            embedder,
            content=(
                "Jacob listens to metal music and anime soundtracks "
                "with guitars while watching anime"
            ),
            kind="preference",
        )
        gap = gaps.add_gap(
            topic="music",
            question="does Jacob listen to specific genres while watching anime",
        )
        self.assertIsNotNone(gap)
        self.assertIsNotNone(answer)

        result = resolver.run()

        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["open_remaining"], 0)
        # Gap is now closed.
        self.assertEqual(gaps.list_open(), [])
        fresh = mem.get(int(gap.id))
        self.assertIsNotNone(fresh)
        meta = fresh.metadata or {}
        self.assertIsNotNone(meta.get("resolved_at"))
        self.assertEqual(meta.get("resolved_by"), "memory_match")
        self.assertEqual(
            int(meta.get("resolved_by_memory_id")), int(answer.id)
        )
        self.assertIsInstance(meta.get("resolved_similarity"), float)
        self.assertGreaterEqual(
            float(meta["resolved_similarity"]), 0.55
        )

    def test_resolves_curiosity_finding_too(self) -> None:
        # G3-style finding row is also accepted as an "answer."
        _, mem, gaps, resolver = _make_resolver()
        embedder = _DeterministicEmbedder()
        answer = _add_memory(
            mem,
            embedder,
            content="Jacob plays piano and violin since teenage years",
            kind="curiosity_finding",
        )
        gap = gaps.add_gap(
            topic="music",
            question="does Jacob play piano and violin",
        )
        self.assertIsNotNone(gap)
        self.assertIsNotNone(answer)

        result = resolver.run()

        self.assertEqual(result["resolved"], 1)


class ResolverFilteringTests(unittest.TestCase):
    def test_rejects_self_tagged_as_answer(self) -> None:
        # Self-* memories describe Aiko, not Jacob. They must never
        # close a user-facing gap.
        _, mem, gaps, resolver = _make_resolver()
        embedder = _DeterministicEmbedder()
        _add_memory(
            mem,
            embedder,
            content="Aiko listens to anime soundtracks while coding",
            kind="self_tagged",
        )
        gap = gaps.add_gap(
            topic="music",
            question="does Jacob listen to anime soundtracks",
        )
        self.assertIsNotNone(gap)

        result = resolver.run()

        self.assertEqual(result["resolved"], 0)
        self.assertEqual(len(gaps.list_open()), 1)

    def test_rejects_other_knowledge_gap_as_answer(self) -> None:
        # Two unrelated gaps. Neither should resolve the other even
        # if their embeddings happen to be cosine-close: the
        # ``_ANSWER_KINDS`` filter excludes ``knowledge_gap``.
        _, mem, gaps, resolver = _make_resolver()
        gap_a = gaps.add_gap(
            topic="music",
            question="does Jacob listen to anime soundtracks",
        )
        gap_b = gaps.add_gap(
            topic="weather",
            question="does Jacob prefer rain or sunshine",
        )
        self.assertIsNotNone(gap_a)
        self.assertIsNotNone(gap_b)
        self.assertEqual(len(gaps.list_open()), 2)

        result = resolver.run()

        self.assertEqual(result["resolved"], 0)
        self.assertEqual(len(gaps.list_open()), 2)

    def test_below_threshold_does_not_resolve(self) -> None:
        _, mem, gaps, resolver = _make_resolver(threshold=0.99)
        embedder = _DeterministicEmbedder()
        _add_memory(
            mem,
            embedder,
            content="Jacob enjoys metal music with guitars",
            kind="preference",
        )
        gaps.add_gap(
            topic="weather",
            question="does Jacob prefer rainy or sunny days",
        )

        result = resolver.run()

        self.assertEqual(result["resolved"], 0)


class ResolverNoOpTests(unittest.TestCase):
    def test_disabled_returns_skipped(self) -> None:
        _, _, _, resolver = _make_resolver(enabled=False)
        result = resolver.run()
        self.assertEqual(result, {"skipped": True, "reason": "disabled"})

    def test_cancelled_before_start_returns_skipped(self) -> None:
        _, mem, gaps, _ = _make_resolver()
        # Build a fresh resolver with cancel_event set.
        cancel = threading.Event()
        cancel.set()
        agent_settings = AgentSettings()
        resolver = IdleGapResolver(
            memory_store=mem,
            gap_store=gaps,
            agent_settings=agent_settings,
            cancel_event=cancel,
        )
        # Add an open gap to make sure the early-return check is the
        # cancel branch, not "no_open_gaps."
        gaps.add_gap(topic="x", question="something to ask about")
        result = resolver.run()
        self.assertEqual(result["skipped"], True)
        self.assertEqual(result["reason"], "cancelled_before_start")

    def test_no_open_gaps_returns_skipped(self) -> None:
        _, _, _, resolver = _make_resolver()
        result = resolver.run()
        self.assertEqual(
            result, {"skipped": True, "reason": "no_open_gaps"}
        )

    def test_already_resolved_gap_skipped(self) -> None:
        # Mark a gap resolved by hand; it must not show up in scan.
        _, mem, gaps, resolver = _make_resolver()
        gap = gaps.add_gap(
            topic="music",
            question="does Jacob play violin or piano",
        )
        self.assertIsNotNone(gap)
        gaps.mark_resolved(int(gap.id), answer_memory_id=None)

        result = resolver.run()

        self.assertEqual(
            result, {"skipped": True, "reason": "no_open_gaps"}
        )


    def test_per_tick_cap_limits_scan(self) -> None:
        # Three open gaps; cap=2 -> first tick must only scan two.
        # We don't assert how many resolve (depends on the embedder's
        # cosine fidelity for the test corpus); the contract here is
        # "scanned <= per_tick_cap" so the CPU budget is bounded.
        _, mem, gaps, resolver = _make_resolver(per_tick=2)
        gaps.add_gap(topic="music", question="does Jacob play violin")
        gaps.add_gap(topic="food", question="does Jacob like spicy food")
        gaps.add_gap(topic="work", question="what city does Jacob work in")
        self.assertEqual(len(gaps.list_open()), 3)

        first = resolver.run()
        self.assertEqual(first["scanned"], 2)


class ResolverIsReadyTests(unittest.TestCase):
    def test_is_ready_false_when_no_open_gaps(self) -> None:
        _, _, _, resolver = _make_resolver()
        now = datetime.now(timezone.utc)
        self.assertFalse(resolver.is_ready(now=now, last_run_at=None))

    def test_is_ready_true_first_run_with_open_gap(self) -> None:
        _, _, gaps, resolver = _make_resolver()
        gaps.add_gap(topic="x", question="something to ponder")
        now = datetime.now(timezone.utc)
        self.assertTrue(resolver.is_ready(now=now, last_run_at=None))

    def test_is_ready_false_within_interval(self) -> None:
        _, _, gaps, resolver = _make_resolver(interval=600)
        gaps.add_gap(topic="x", question="something to ponder")
        now = datetime.now(timezone.utc)
        recent = now - timedelta(seconds=120)  # 2 min ago, < 10 min
        self.assertFalse(resolver.is_ready(now=now, last_run_at=recent))

    def test_is_ready_false_when_disabled(self) -> None:
        _, _, gaps, resolver = _make_resolver(enabled=False)
        gaps.add_gap(topic="x", question="something to ponder")
        now = datetime.now(timezone.utc)
        self.assertFalse(resolver.is_ready(now=now, last_run_at=None))


class ResolverLogsAuditTrailTests(unittest.TestCase):
    def test_resolution_emits_info_log(self) -> None:
        _, mem, gaps, resolver = _make_resolver()
        embedder = _DeterministicEmbedder()
        _add_memory(
            mem,
            embedder,
            content="Jacob enjoys jazz piano in the evenings",
            kind="preference",
        )
        gaps.add_gap(
            topic="music",
            question="does Jacob enjoy jazz piano in the evenings",
        )
        with self.assertLogs("app.idle_gap_resolver", level="INFO") as cm:
            resolver.run()
        self.assertTrue(
            any(
                "gap_resolver: resolved gap_id=" in line
                and "by memory_id=" in line
                for line in cm.output
            ),
            f"missing audit log line; got {cm.output!r}",
        )


if __name__ == "__main__":
    unittest.main()
