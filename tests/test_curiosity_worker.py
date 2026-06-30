"""Tests for the Phase 4c CuriosityWorker."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from app.core.proactive.curiosity_worker import (
    CuriosityWorker,
    _clean_curiosity_output,
    _looks_like_question,
    _word_count,
)


@dataclass
class _FakeMemory:
    id: int
    content: str
    kind: str
    embedding: list[float]
    salience: float
    source_session: str | None = None
    source_message_id: int | None = None


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.adds: list[_FakeMemory] = []
        self._next_id = 1

    def add(
        self,
        *,
        content: str,
        kind: str,
        embedding: list[float],
        salience: float,
        source_session: str | None = None,
        source_message_id: int | None = None,
    ) -> _FakeMemory | None:
        mem = _FakeMemory(
            id=self._next_id,
            content=content,
            kind=kind,
            embedding=list(embedding),
            salience=salience,
            source_session=source_session,
            source_message_id=source_message_id,
        )
        self._next_id += 1
        self.adds.append(mem)
        return mem


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [float(len(text))]


@dataclass
class _FakeInterest:
    """Mimics topic_graph.InterestActivity (has .label / .days_since)."""

    label: str
    days_since: float | None = None
    size: int = 5


class _FakeOllama:
    def __init__(self, output: str = "") -> None:
        self.calls = 0
        self.output = output
        self.last_messages: list[dict[str, str]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        self.calls += 1
        self.last_messages = messages
        return self.output


def _make(
    *,
    ollama_output: str = (
        "Maybe ask Jacob what he meant by 'weird week' — sounds layered."
    ),
    min_turns_between: int = 1,
    min_seconds_between: float = 0.0,
    max_user_word_count: int = 8,
    has_components: bool = True,
    interest_rows: list | None = None,
    cluster_anchor_enabled: bool = True,
    quiet_min_days: float = 7.0,
) -> tuple[CuriosityWorker, _FakeOllama, _FakeMemoryStore]:
    ollama = _FakeOllama(ollama_output)
    memory = _FakeMemoryStore()
    embedder = _FakeEmbedder()
    worker = CuriosityWorker(
        ollama=ollama if has_components else None,
        memory_store=memory if has_components else None,
        embedder=embedder if has_components else None,
        model="qwen3:4b",
        min_turns_between=min_turns_between,
        min_seconds_between=min_seconds_between,
        max_user_word_count=max_user_word_count,
        user_display_name_provider=lambda: "Jacob",
        interest_provider=(
            (lambda: interest_rows) if interest_rows is not None else None
        ),
        cluster_anchor_enabled=cluster_anchor_enabled,
        quiet_min_days=quiet_min_days,
    )
    return worker, ollama, memory


class WordCountTests(unittest.TestCase):
    def test_word_count_basic(self) -> None:
        self.assertEqual(_word_count(""), 0)
        self.assertEqual(_word_count("hi there"), 2)
        self.assertEqual(_word_count("don't worry, friend"), 3)


class LooksLikeQuestionTests(unittest.TestCase):
    def test_question_mark(self) -> None:
        self.assertTrue(_looks_like_question("how are you doing?"))

    def test_starts_with_what(self) -> None:
        self.assertTrue(_looks_like_question("What about that"))

    def test_plain_statement(self) -> None:
        self.assertFalse(_looks_like_question("yeah just chillin"))


class CleanCuriosityOutputTests(unittest.TestCase):
    def test_passes_through_well_formed(self) -> None:
        text = "Maybe ask Jacob what he meant by chess."
        self.assertEqual(_clean_curiosity_output(text), text)

    def test_strips_quotes(self) -> None:
        text = '"Maybe ask Jacob about today."'
        self.assertEqual(
            _clean_curiosity_output(text),
            "Maybe ask Jacob about today.",
        )

    def test_rejects_no_prefix(self) -> None:
        self.assertEqual(
            _clean_curiosity_output("Just ask him something."),
            "",
        )

    def test_salvages_almost_prefix(self) -> None:
        out = _clean_curiosity_output(
            "  ask jacob how he's holding up.",
        )
        self.assertTrue(out.lower().startswith("maybe ask jacob"))


class CuriosityWorkerTests(unittest.TestCase):
    def test_writes_open_question_on_shallow_short_turn(self) -> None:
        worker, ollama, memory = _make()
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah, sounds nice",
            assistant_text="Glad you like it.",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(result)
        self.assertEqual(ollama.calls, 1)
        self.assertEqual(len(memory.adds), 1)
        self.assertEqual(memory.adds[0].kind, "open_question")
        self.assertTrue(memory.adds[0].content.startswith("Maybe ask Jacob"))

    def test_skips_when_user_already_asked(self) -> None:
        worker, ollama, memory = _make()
        result = worker.maybe_run(
            session_key="s1",
            user_text="how was your day?",
            assistant_text="Oh, fine!",
            arc_label="casual_check_in",
        )
        self.assertIsNone(result)
        self.assertEqual(ollama.calls, 0)
        self.assertEqual(memory.adds, [])

    def test_skips_when_user_too_long(self) -> None:
        worker, ollama, _memory = _make(max_user_word_count=4)
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah I think so probably maybe",
            assistant_text="Cool.",
            arc_label="casual_check_in",
        )
        self.assertIsNone(result)
        self.assertEqual(ollama.calls, 0)

    def test_skips_when_arc_not_shallow(self) -> None:
        worker, ollama, _memory = _make()
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah",
            assistant_text="...",
            arc_label="support",
        )
        self.assertIsNone(result)
        self.assertEqual(ollama.calls, 0)

    def test_throttles_by_turns(self) -> None:
        worker, ollama, _memory = _make(min_turns_between=3)
        # First fires.
        first = worker.maybe_run(
            session_key="s1",
            user_text="ok",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(first)
        # Next two are throttled.
        for _ in range(2):
            r = worker.maybe_run(
                session_key="s1",
                user_text="ok",
                assistant_text="...",
                arc_label="casual_check_in",
            )
            self.assertIsNone(r)
        # Fourth turn passes the gate.
        again = worker.maybe_run(
            session_key="s1",
            user_text="ok",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(again)

    def test_skips_when_components_disabled(self) -> None:
        worker, ollama, _memory = _make(has_components=False)
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNone(result)

    def test_skips_when_llm_returns_garbage(self) -> None:
        worker, ollama, memory = _make(ollama_output="lol nope")
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNone(result)
        self.assertEqual(memory.adds, [])
        # The throttle counter advances even on a refusal so a flaky
        # model can't make us spin on every turn.
        self.assertEqual(worker.stats()["scheduled"], 1)


class ClusterAnchorTests(unittest.TestCase):
    """K65c: anchor the follow-up on a known-but-quiet interest."""

    def _sys_prompt(self, ollama: _FakeOllama) -> str:
        return ollama.last_messages[0]["content"]

    def test_anchors_on_quiet_interest(self) -> None:
        worker, ollama, _ = _make(
            ollama_output="Maybe ask Jacob if he's still rock climbing lately.",
            interest_rows=[_FakeInterest("rock climbing", days_since=30.0)],
        )
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah, sounds nice",
            assistant_text="Glad you like it.",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(result)
        self.assertIn("rock climbing", self._sys_prompt(ollama))
        self.assertEqual(worker.stats()["anchored_on_interest"], 1)

    def test_picks_quietest_interest(self) -> None:
        worker, ollama, _ = _make(
            ollama_output="Maybe ask Jacob about the old band stuff.",
            interest_rows=[
                _FakeInterest("rust", days_since=8.0),
                _FakeInterest("the band", days_since=45.0),
                _FakeInterest("cooking", days_since=12.0),
            ],
        )
        worker.maybe_run(
            session_key="s1",
            user_text="ok cool",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        # Largest days_since wins.
        self.assertIn("the band", self._sys_prompt(ollama))

    def test_recent_interests_fall_back_to_legacy(self) -> None:
        worker, ollama, _ = _make(
            interest_rows=[_FakeInterest("rust", days_since=2.0)],
            quiet_min_days=7.0,
        )
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(result)
        # No quiet interest cleared the threshold -> legacy prompt, no anchor.
        self.assertNotIn("rust", self._sys_prompt(ollama))
        self.assertEqual(worker.stats()["anchored_on_interest"], 0)

    def test_unknown_days_since_treated_as_dormant(self) -> None:
        worker, ollama, _ = _make(
            ollama_output="Maybe ask Jacob about gardening again sometime.",
            interest_rows=[_FakeInterest("gardening", days_since=None)],
        )
        worker.maybe_run(
            session_key="s1",
            user_text="ok",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIn("gardening", self._sys_prompt(ollama))

    def test_switch_off_disables_anchor(self) -> None:
        worker, ollama, _ = _make(
            interest_rows=[_FakeInterest("rock climbing", days_since=30.0)],
            cluster_anchor_enabled=False,
        )
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(result)
        self.assertNotIn("rock climbing", self._sys_prompt(ollama))
        self.assertEqual(worker.stats()["anchored_on_interest"], 0)

    def test_no_provider_is_legacy(self) -> None:
        worker, ollama, _ = _make()  # interest_rows=None -> no provider
        result = worker.maybe_run(
            session_key="s1",
            user_text="yeah",
            assistant_text="...",
            arc_label="casual_check_in",
        )
        self.assertIsNotNone(result)
        self.assertEqual(worker.stats()["anchored_on_interest"], 0)


if __name__ == "__main__":
    unittest.main()
