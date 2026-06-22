"""Tests for the F6 privacy-preserving query reformulation helper."""
from __future__ import annotations

import unittest

from app.core.memory.query_reformulation import (
    make_reformulator,
    reformulate_query_for_search,
)


class ReformulateQueryTests(unittest.TestCase):
    def test_clean_topic_is_used(self) -> None:
        out = reformulate_query_for_search(
            "Jacob wants more currently-airing anime",
            reformulate_fn=lambda _t: "best currently airing anime summer 2026",
            user_names=["Jacob"],
        )
        self.assertEqual(out, "best currently airing anime summer 2026")

    def test_hallucinated_name_is_post_filtered(self) -> None:
        # The model leaves the user's name in; the deterministic
        # post-filter must strip it before the query is returned.
        out = reformulate_query_for_search(
            "Jacob likes shoegaze bands",
            reformulate_fn=lambda _t: "Jacob favourite shoegaze bands",
            user_names=["Jacob"],
        )
        self.assertIsNotNone(out)
        self.assertNotIn("jacob", (out or "").lower())
        self.assertIn("shoegaze", (out or "").lower())

    def test_none_falls_back_to_deterministic_scrub(self) -> None:
        out = reformulate_query_for_search(
            "the violin practice routine",
            reformulate_fn=lambda _t: "NONE",
        )
        self.assertEqual(out, "the violin practice routine")

    def test_llm_failure_falls_back(self) -> None:
        def _boom(_t: str) -> str:
            raise RuntimeError("model down")

        out = reformulate_query_for_search(
            "the violin practice routine",
            reformulate_fn=_boom,
        )
        self.assertEqual(out, "the violin practice routine")

    def test_blank_input_returns_none(self) -> None:
        out = reformulate_query_for_search(
            "   ",
            reformulate_fn=lambda _t: "anything",
        )
        self.assertIsNone(out)

    def test_strips_quotes_and_query_label(self) -> None:
        out = reformulate_query_for_search(
            "topic about jazz history",
            reformulate_fn=lambda _t: 'Query: "history of jazz music"',
        )
        self.assertEqual(out, "history of jazz music")

    def test_post_filter_reject_falls_back_to_original(self) -> None:
        # Model returns ONLY the name; post-filter rejects it, so we fall
        # back to the deterministic scrub of the original claim.
        out = reformulate_query_for_search(
            "the history of bonsai cultivation",
            reformulate_fn=lambda _t: "Jacob",
            user_names=["Jacob"],
        )
        self.assertEqual(out, "the history of bonsai cultivation")


class _FakeOllama:
    def __init__(self, text: str, *, raises: bool = False) -> None:
        self._text = text
        self._raises = raises
        self.last_kwargs: dict = {}

    def chat_stream(self, messages, **kwargs):
        self.last_kwargs = {"messages": messages, **kwargs}
        if self._raises:
            raise RuntimeError("stream down")
        # Yield in chunks to mimic streaming.
        for piece in self._text.split(" "):
            yield piece + " "


class MakeReformulatorTests(unittest.TestCase):
    def test_streams_and_joins(self) -> None:
        ollama = _FakeOllama("history of jazz")
        fn = make_reformulator(ollama=ollama, chat_model="m")
        out = fn("tell me about jazz")
        self.assertIn("history of jazz", out or "")
        self.assertEqual(ollama.last_kwargs["model"], "m")

    def test_stream_error_returns_none(self) -> None:
        ollama = _FakeOllama("", raises=True)
        fn = make_reformulator(ollama=ollama, chat_model="m")
        self.assertIsNone(fn("anything"))

    def test_end_to_end_with_reformulator(self) -> None:
        ollama = _FakeOllama("best ambient albums")
        fn = make_reformulator(ollama=ollama, chat_model="m")
        out = reformulate_query_for_search(
            "Jacob loves ambient music",
            reformulate_fn=fn,
            user_names=["Jacob"],
        )
        self.assertIn("ambient", (out or "").lower())
        self.assertNotIn("jacob", (out or "").lower())


if __name__ == "__main__":
    unittest.main()
