"""Tests for the SummaryWorker synchronous compact_now path."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.core.chat_database import ChatDatabase
from app.core.summary_worker import SummaryWorker
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
