"""Tests for the speculative RAG pre-fetcher (Phase 1b)."""
from __future__ import annotations

import threading
import time
import unittest

from app.core.rag.rag_prefetcher import RagPrefetcher, _prefix_similarity


class PrefixSimilarityTests(unittest.TestCase):
    def test_identical(self) -> None:
        self.assertEqual(_prefix_similarity("hello world", "hello world"), 1.0)

    def test_prefix_proportional(self) -> None:
        # "hello" is a prefix of "hello world", with 5/11 length ratio.
        sim = _prefix_similarity("hello", "hello world")
        self.assertAlmostEqual(sim, 5 / 11, places=3)

    def test_no_overlap(self) -> None:
        self.assertEqual(_prefix_similarity("foo", "bar"), 0.0)

    def test_empty(self) -> None:
        self.assertEqual(_prefix_similarity("", "anything"), 0.0)
        self.assertEqual(_prefix_similarity("anything", ""), 0.0)


class _FakeRetriever:
    """Stand-in for RagRetriever that just records calls."""

    def __init__(self, *, latency: float = 0.0, fail: bool = False) -> None:
        self.latency = latency
        self.fail = fail
        self.calls: list[str] = []
        self.gate = threading.Event()
        self.gate.set()

    def retrieve(
        self,
        query_text: str,
        *,
        recent_turns=None,
        exclude_session_id=None,
    ):
        self.calls.append(query_text)
        if self.latency:
            time.sleep(self.latency)
        self.gate.wait()
        if self.fail:
            raise RuntimeError("simulated retrieval failure")
        # Return any object — the prefetcher only forwards it to format_block.
        return [f"hit-for:{query_text}"]

    @staticmethod
    def format_block(
        hits, *, user_display_name: str = "the user", **_kwargs,
    ) -> str:
        # K7: tolerate the new fade-hedge kwargs the prefetcher now
        # threads through; the stub doesn't care about them.
        if not hits:
            return ""
        return "BLOCK:" + "|".join(str(h) for h in hits)


class RagPrefetcherTests(unittest.TestCase):
    def _wait_completed(self, prefetcher: RagPrefetcher, expected: int, *, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if prefetcher.stats()["completed"] >= expected:
                return
            time.sleep(0.01)
        self.fail(f"prefetcher did not complete {expected} fetch(es) within {timeout}s")

    def test_submit_caches_block(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever, ttl_seconds=10, debounce_ms=0, min_partial_chars=4,
        )
        try:
            self.assertTrue(prefetcher.submit("tell me about my project"))
            self._wait_completed(prefetcher, 1)
            block = prefetcher.lookup("tell me about my project deadline")
            self.assertIsNotNone(block)
            self.assertIn("BLOCK:hit-for:tell me about my project", block or "")
        finally:
            prefetcher.shutdown()

    def test_short_partials_skipped(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever, debounce_ms=0, min_partial_chars=10,
        )
        try:
            self.assertFalse(prefetcher.submit("hi"))
            self.assertEqual(retriever.calls, [])
        finally:
            prefetcher.shutdown()

    def test_debounce_drops_rapid_calls(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever, debounce_ms=200, min_partial_chars=4,
        )
        try:
            self.assertTrue(prefetcher.submit("first long enough partial"))
            # Different text but inside the debounce window -> dropped.
            self.assertFalse(prefetcher.submit("second different text in window"))
            self._wait_completed(prefetcher, 1)
            self.assertEqual(prefetcher.stats()["skipped_debounce"], 1)
        finally:
            prefetcher.shutdown()

    def test_dedupe_same_query(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever, ttl_seconds=10, debounce_ms=0, min_partial_chars=4,
        )
        try:
            self.assertTrue(prefetcher.submit("identical query text"))
            self._wait_completed(prefetcher, 1)
            self.assertFalse(prefetcher.submit("identical query text"))
            self.assertEqual(retriever.calls, ["identical query text"])
            self.assertEqual(prefetcher.stats()["skipped_dup"], 1)
        finally:
            prefetcher.shutdown()

    def test_lookup_misses_when_below_threshold(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever,
            ttl_seconds=10,
            debounce_ms=0,
            min_partial_chars=4,
            similarity_threshold=0.9,
        )
        try:
            prefetcher.submit("a query about astronomy")
            self._wait_completed(prefetcher, 1)
            # Different topic entirely.
            self.assertIsNone(prefetcher.lookup("a question about cooking"))
            self.assertGreaterEqual(prefetcher.stats()["lookup_miss"], 1)
        finally:
            prefetcher.shutdown()

    def test_ttl_expiry(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever, ttl_seconds=0.1, debounce_ms=0, min_partial_chars=4,
        )
        try:
            prefetcher.submit("expiring query content")
            self._wait_completed(prefetcher, 1)
            time.sleep(0.2)
            self.assertIsNone(prefetcher.lookup("expiring query content"))
        finally:
            prefetcher.shutdown()

    def test_failure_does_not_poison_cache(self) -> None:
        retriever = _FakeRetriever(fail=True)
        prefetcher = RagPrefetcher(
            retriever, ttl_seconds=10, debounce_ms=0, min_partial_chars=4,
        )
        try:
            prefetcher.submit("query that fails")
            # Wait for the failure to be recorded.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if prefetcher.stats()["failed"] >= 1:
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(prefetcher.stats()["failed"], 1)
            self.assertIsNone(prefetcher.lookup("query that fails"))
        finally:
            prefetcher.shutdown()

    def test_lookup_can_wait_for_pending(self) -> None:
        retriever = _FakeRetriever(latency=0.15)
        prefetcher = RagPrefetcher(
            retriever, ttl_seconds=10, debounce_ms=0, min_partial_chars=4,
        )
        try:
            prefetcher.submit("waiting partial query")
            # Lookup is allowed to wait briefly for the in-flight fetch.
            block = prefetcher.lookup(
                "waiting partial query and more",
                wait_pending_seconds=0.5,
            )
            self.assertIsNotNone(block)
        finally:
            prefetcher.shutdown()

    def test_shutdown_clears_cache(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever, ttl_seconds=10, debounce_ms=0, min_partial_chars=4,
        )
        prefetcher.submit("any partial text here")
        self._wait_completed(prefetcher, 1)
        prefetcher.shutdown()
        # Subsequent lookups always miss.
        self.assertIsNone(prefetcher.lookup("any partial text here"))


if __name__ == "__main__":
    unittest.main()
