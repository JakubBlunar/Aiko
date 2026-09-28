"""Tests for the SummaryWorker synchronous compact_now path."""
from __future__ import annotations

import tempfile
import unittest
import logging
from pathlib import Path
from unittest.mock import MagicMock

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.summary_worker import SummaryWorker
from app.core.proactive.worker_prompt_budget import pack_worker_prompt
from app.llm.ollama_client import OllamaUsage


class _FakeOllama:
    """Minimal stand-in for OllamaClient.chat_json used by SummaryWorker."""

    def __init__(self, content: str = "• summary line one") -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def chat_json(  # noqa: D401  (matches OllamaClient.chat_json signature)
        self,
        messages,
        *,
        model,
        timeout_seconds,
        options,
        format_json,
        **kwargs,
    ):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "options": options,
                "format_json": format_json,
            }
        )
        return self.content, OllamaUsage(prompt_tokens=200, completion_tokens=40)


class CompactNowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = ChatDatabase(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        # SQLite holds the file open via thread-local connection; close it so
        # Windows can release the temp dir.
        conn = getattr(self._db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _seed(self, count: int = 3) -> None:
        for i in range(count):
            self._db.add_message(
                session_id="s1",
                role="user" if i % 2 == 0 else "assistant",
                content=f"line {i}",
                token_count=4,
            )

    def test_compact_now_runs_below_normal_threshold(self) -> None:
        """compact_now must succeed even when fewer messages are available
        than ``min_unsummarized_messages`` requires.
        """
        self._seed(count=3)  # below the worker's normal threshold of 6
        ollama = _FakeOllama(content="• summary bullet")
        worker = SummaryWorker(
            self._db,
            ollama,  # type: ignore[arg-type]
            model="dummy",
            is_busy=lambda: False,
            min_unsummarized_messages=6,
            target_tokens=300,
        )
        wrote = worker.compact_now("s1")
        self.assertTrue(wrote)
        self.assertEqual(worker.compactions_total(), 1)
        latest = self._db.get_latest_summary("s1")
        self.assertIsNotNone(latest)
        assert latest is not None  # for type-checker
        self.assertIn("summary", latest.summary.lower())
        # Honours target_tokens via num_predict.
        self.assertEqual(ollama.calls[0]["options"]["num_predict"], 300)

    def test_shared_budget_reserves_output_and_keeps_worker_order(self) -> None:
        items = ["a" * 800, "b" * 800, "c" * 800]
        def render(selected):
            return [
                {"role": "system", "content": "instructions"},
                {"role": "user", "content": "\n".join(selected)},
            ]
        packed = pack_worker_prompt(
            items, render, context_window=700, output_tokens=300,
            surface="test_summary", log=logging.getLogger(__name__),
        )
        self.assertIsNotNone(packed)
        assert packed is not None
        self.assertEqual(packed.item_count, 1)
        self.assertEqual(packed.messages[1]["content"], items[0])
        self.assertLessEqual(packed.input_tokens + 300, 700)

    def test_compact_now_does_nothing_when_no_messages(self) -> None:
        ollama = _FakeOllama()
        worker = SummaryWorker(
            self._db,
            ollama,  # type: ignore[arg-type]
            model="dummy",
            is_busy=lambda: False,
        )
        self.assertFalse(worker.compact_now("empty-session"))
        self.assertEqual(worker.compactions_total(), 0)

    def test_budgeted_summary_keeps_the_oldest_unread_messages(self) -> None:
        for index in range(8):
            self._db.add_message(
                session_id="s1", role="user",
                content=f"unique {index}: " + "detail " * 80,
                token_count=160,
            )
        ollama = _FakeOllama(content="summary of oldest turns")
        worker = SummaryWorker(
            self._db, ollama,  # type: ignore[arg-type]
            model="dummy", is_busy=lambda: False, target_tokens=300,
            context_window=lambda: 900,
        )
        self.assertTrue(worker.compact_now("s1"))
        first = self._db.get_latest_summary("s1")
        assert first is not None
        self.assertGreater(first.messages_summarized, 0)
        self.assertLess(first.messages_summarized, 8)
        transcript = ollama.calls[0]["messages"][1]["content"]
        self.assertIn("unique 0:", transcript)
        self.assertNotIn("unique 7:", transcript)
        self.assertTrue(worker.compact_now("s1"))
        second = self._db.get_latest_summary("s1")
        assert second is not None
        self.assertGreater(second.messages_summarized, first.messages_summarized)
        second_prompt = ollama.calls[1]["messages"][1]["content"]
        self.assertIn(f"unique {first.messages_summarized}:", second_prompt)
        while second.messages_summarized < 8:
            previous_count = second.messages_summarized
            self.assertTrue(worker._maybe_summarize("s1"))
            second = self._db.get_latest_summary("s1")
            assert second is not None
            self.assertGreater(second.messages_summarized, previous_count)
        self.assertEqual(self._db.kv_get("summary.budget_backlog:s1"), "")

    def test_compact_now_failure_is_swallowed(self) -> None:
        self._seed(count=4)
        ollama = MagicMock()
        ollama.chat_json.side_effect = RuntimeError("boom")
        worker = SummaryWorker(
            self._db,
            ollama,  # type: ignore[arg-type]
            model="dummy",
            is_busy=lambda: False,
        )
        # Should not raise, just return False.
        self.assertFalse(worker.compact_now("s1"))
        self.assertEqual(worker.compactions_total(), 0)


if __name__ == "__main__":
    unittest.main()
