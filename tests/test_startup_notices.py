"""Tests for the boot-notice plumbing (I7).

Covers ``_capture_embedding_swap_notice`` / ``_queue_startup_notice`` /
``consume_startup_notices`` on ``LifecycleMixin`` without standing up a
full ``SessionController`` (the methods only touch ``_startup_notices``
and the passed-in rag-store stub).
"""
from __future__ import annotations

import unittest

from app.core.session.lifecycle_mixin import LifecycleMixin


class _Host(LifecycleMixin):
    """Minimal host exposing only the notice helpers."""

    def __init__(self) -> None:
        self._startup_notices: list[dict] = []


class _FakeRag:
    def __init__(self, swap: dict | None) -> None:
        self.embedding_swap = swap


class ConsumeStartupNoticesTests(unittest.TestCase):
    def test_consume_is_one_shot(self) -> None:
        host = _Host()
        host._queue_startup_notice(kind="info", text="hi")
        first = host.consume_startup_notices()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["text"], "hi")
        # Second read is empty -- later reconnects don't repeat the toast.
        self.assertEqual(host.consume_startup_notices(), [])

    def test_consume_initialises_missing_list(self) -> None:
        host = LifecycleMixin()  # no _startup_notices attr at all
        self.assertEqual(host.consume_startup_notices(), [])


class CaptureEmbeddingSwapTests(unittest.TestCase):
    def test_no_swap_queues_nothing(self) -> None:
        host = _Host()
        host._capture_embedding_swap_notice(_FakeRag(None))
        self.assertEqual(host.consume_startup_notices(), [])

    def test_swap_queues_warning(self) -> None:
        host = _Host()
        host._capture_embedding_swap_notice(
            _FakeRag(
                {
                    "from_model": "nomic-embed-text",
                    "from_dim": 768,
                    "to_model": "mxbai-embed-large",
                    "to_dim": 1024,
                    "at": "2026-01-01T00:00:00Z",
                },
            ),
        )
        notices = host.consume_startup_notices()
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice["kind"], "warning")
        self.assertEqual(notice["code"], "embedding_rebuild")
        self.assertIn("nomic-embed-text", notice["text"])
        self.assertIn("mxbai-embed-large", notice["text"])
        self.assertEqual(notice["detail"]["from_dim"], 768)


if __name__ == "__main__":
    unittest.main()
