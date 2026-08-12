"""Tests for the F6 privacy-preserving query reformulation helper."""
from __future__ import annotations

import unittest

from app.core.memory.query_reformulation import (
    _REFORMULATE_CONTEXT_SYSTEM,
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


class ContextTests(unittest.TestCase):
    """The claim span alone is usually not enough to write a query from.

    Spans arrive as sub-sentence fragments — "Back Camp", "2026" — with no
    predicate, so a rewrite of the span in isolation can only pad it with
    a guess. The enclosing sentence says what is being asserted, which is
    what a search has to be able to check.
    """

    def test_the_context_reaches_the_reformulator(self) -> None:
        seen: list[tuple[str, str]] = []

        def _fn(claim: str, context: str = "") -> str:
            seen.append((claim, context))
            return "Fullmetal Alchemist Brotherhood episode count"

        reformulate_query_for_search(
            "Fullmetal Alchemist",
            reformulate_fn=_fn,
            context="Jacob said Fullmetal Alchemist Brotherhood has 64 episodes",
            user_names=["Jacob"],
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "Fullmetal Alchemist")
        self.assertIn("64 episodes", seen[0][1])

    def test_the_context_itself_is_never_the_query(self) -> None:
        # The context is private transcript text. It shapes the rewrite and
        # stops there; whatever comes back still faces the scrubber.
        out = reformulate_query_for_search(
            "Back Camp",
            reformulate_fn=lambda _c, _x="": "NONE",
            context="Jacob is going to Back Camp with me in 2026",
            user_names=["Jacob"],
        )
        self.assertIsNotNone(out)
        self.assertNotIn("jacob", (out or "").lower())
        self.assertNotIn("2026", out or "")

    def test_a_leak_in_the_rewrite_is_still_caught(self) -> None:
        # Feeding the model private context gives it more to echo back, so
        # the post-filter matters more here, not less.
        out = reformulate_query_for_search(
            "violin",
            reformulate_fn=lambda _c, _x="": "Jacob violin practice technique",
            context="Jacob has practised violin since 2010",
            user_names=["Jacob"],
        )
        self.assertIsNotNone(out)
        self.assertNotIn("jacob", (out or "").lower())
        self.assertIn("violin", (out or "").lower())

    def test_a_single_argument_callable_still_works(self) -> None:
        # Older callables and test doubles take only the claim.
        calls: list[str] = []

        def _fn(claim: str) -> str:
            calls.append(claim)
            return "history of jazz"

        out = reformulate_query_for_search(
            "jazz",
            reformulate_fn=_fn,
            context="Jacob thinks jazz started in New Orleans",
            user_names=["Jacob"],
        )
        self.assertEqual(out, "history of jazz")
        self.assertEqual(calls, ["jazz"])

    def test_a_raising_two_arg_callable_falls_back(self) -> None:
        def _fn(_claim: str, _context: str = "") -> str:
            raise RuntimeError("model down")

        out = reformulate_query_for_search(
            "the violin practice routine",
            reformulate_fn=_fn,
            context="some sentence",
        )
        self.assertEqual(out, "the violin practice routine")

    def test_blank_context_uses_the_plain_path(self) -> None:
        seen: list[int] = []

        def _fn(*args: str) -> str:
            seen.append(len(args))
            return "topic query"

        reformulate_query_for_search(
            "topic", reformulate_fn=_fn, context="   ",
        )
        self.assertEqual(seen, [1])


class AlreadyNeutralTests(unittest.TestCase):
    """F9's planner already writes impersonal queries.

    28 of 34 reformulations of planner output were byte-identical no-ops —
    a whole extra generation per search that changed nothing. Skipping the
    rewrite must not skip the leak guard.
    """

    def test_the_llm_pass_is_skipped(self) -> None:
        called: list[str] = []

        out = reformulate_query_for_search(
            "history of the Kalman filter",
            reformulate_fn=lambda t, *_a: called.append(t) or "something else",
            already_neutral=True,
        )
        self.assertEqual(out, "history of the Kalman filter")
        self.assertEqual(called, [])

    def test_the_deterministic_guard_still_runs(self) -> None:
        out = reformulate_query_for_search(
            "Jacob favourite shoegaze bands",
            reformulate_fn=lambda t, *_a: t,
            already_neutral=True,
            user_names=["Jacob"],
        )
        self.assertIsNotNone(out)
        self.assertNotIn("jacob", (out or "").lower())

    def test_an_unsafe_neutral_query_is_still_refused(self) -> None:
        out = reformulate_query_for_search(
            "mail jacob@example.com",
            reformulate_fn=lambda t, *_a: t,
            already_neutral=True,
        )
        self.assertIsNone(out)

    def test_blank_still_returns_none(self) -> None:
        out = reformulate_query_for_search(
            "  ",
            reformulate_fn=lambda t, *_a: t,
            already_neutral=True,
        )
        self.assertIsNone(out)


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

    def test_context_switches_the_prompt(self) -> None:
        ollama = _FakeOllama("episode count of Brotherhood")
        fn = make_reformulator(ollama=ollama, chat_model="m")
        fn("Fullmetal Alchemist", "Jacob said Brotherhood has 64 episodes")
        messages = ollama.last_kwargs["messages"]
        self.assertEqual(messages[0]["content"], _REFORMULATE_CONTEXT_SYSTEM)
        self.assertIn("CLAIM: Fullmetal Alchemist", messages[1]["content"])
        self.assertIn("CONTEXT: Jacob said", messages[1]["content"])

    def test_no_context_keeps_the_plain_prompt(self) -> None:
        ollama = _FakeOllama("history of jazz")
        fn = make_reformulator(ollama=ollama, chat_model="m")
        fn("tell me about jazz")
        messages = ollama.last_kwargs["messages"]
        self.assertNotEqual(messages[0]["content"], _REFORMULATE_CONTEXT_SYSTEM)
        self.assertEqual(messages[1]["content"], "tell me about jazz")

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
