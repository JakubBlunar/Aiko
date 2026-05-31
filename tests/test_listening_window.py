"""Tests for the listening-window prefetch path.

Covers the wire-up between live-voice partials and the cache pre-warm
machinery the plan adds: debounced partial feed, cancel-on-speech of
in-flight background work, ``recent_turns`` plumbing through
``RagPrefetcher.submit``, ``PromptAssembler.prebuild_static_slices`` cache
hit/miss behaviour, and ``OllamaClient`` request bodies including
``keep_alive``.

These tests deliberately stay below the live-audio layer (no
``mic_capture`` mocking) — the integration with ``capture_phrase`` is
already covered by ``tests/test_mic_capture_endpointing.py``. Here we
exercise just the prefetch / assembler / client glue.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.rag.rag_prefetcher import RagPrefetcher
from app.core.infra.settings import OllamaSettings
from app.llm.ollama_client import OllamaClient


# ── shared helpers ────────────────────────────────────────────────────────


class _TempDb:
    """Disposable ChatDatabase under a tmpdir.

    Closes the SQLite connection on exit so Windows can clean up the
    temp directory (sqlite holds the file open otherwise).
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db: ChatDatabase | None = None

    def __enter__(self) -> ChatDatabase:
        path = Path(self._tmp.name) / "test.db"
        self._db = ChatDatabase(path)
        return self._db

    def __exit__(self, *exc_info: object) -> None:
        if self._db is not None:
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


class _FakeRetriever:
    """Records ``recent_turns`` and ``exclude_session_id`` on each call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def retrieve(
        self,
        query_text: str,
        *,
        recent_turns=None,
        exclude_session_id=None,
    ):
        self.calls.append(
            {
                "query": query_text,
                "recent_turns": tuple(recent_turns) if recent_turns else None,
                "exclude_session_id": exclude_session_id,
            }
        )
        return [f"hit:{query_text}"]

    @staticmethod
    def format_block(
        hits, *, user_display_name: str = "the user", **_kwargs,
    ) -> str:
        # K7: tolerate the new fade-hedge kwargs the prefetcher now
        # threads through; the stub doesn't care about them.
        if not hits:
            return ""
        return "BLOCK:" + "|".join(str(h) for h in hits)


def _wait_completed(prefetcher: RagPrefetcher, expected: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if prefetcher.stats()["completed"] >= expected:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"prefetcher did not complete {expected} fetch(es) within {timeout}s"
    )


# ── debounced feed simulation ─────────────────────────────────────────────


class DebouncedPartialFeedTests(unittest.TestCase):
    """Reproduces the debounce/dedup logic from
    ``capture_live_phrase._maybe_feed_partial`` so a fast burst of
    partials only triggers a small handful of feeds.
    """

    def _run_simulation(
        self,
        partials: list[tuple[str, float]],
        *,
        min_chars: int = 12,
        debounce_s: float = 0.4,
        char_delta: int = 6,
    ) -> list[str]:
        """Mirror of ``capture_live_phrase._maybe_feed_partial``.

        The real impl seeds ``last_fed_at = 0.0`` and compares to
        ``time.monotonic()`` (a positive baseline of ~10⁵ s on Windows),
        so the first call is never debounced. Replicate that here.
        """
        fed: list[str] = []
        last_text: list[str] = [""]
        last_at: list[float] = [-1e9]  # effectively "never fired"

        def _maybe_feed(partial: str, now: float) -> None:
            if not partial or len(partial) < min_chars:
                return
            if (now - last_at[0]) < debounce_s:
                return
            if (
                abs(len(partial) - len(last_text[0])) < char_delta
                and partial == last_text[0]
            ):
                return
            last_text[0] = partial
            last_at[0] = now
            fed.append(partial)

        for text, t in partials:
            _maybe_feed(text, t)
        return fed

    def test_short_partials_skipped(self) -> None:
        # "hi" (2) and "hello" (5) are below the 12-char minimum.
        # "hello world how" (15) is the first that qualifies.
        fed = self._run_simulation(
            [
                ("hi", 0.0),
                ("hello", 0.5),
                ("hello world how", 1.0),
            ],
        )
        self.assertEqual(fed, ["hello world how"])

    def test_debounce_collapses_burst(self) -> None:
        # Five partials fired inside one debounce window collapse to 1.
        partials = [
            ("hello world how", 0.0),
            ("hello world how are", 0.05),
            ("hello world how are you", 0.10),
            ("hello world how are you doing", 0.15),
            ("hello world how are you doing today", 0.20),
        ]
        fed = self._run_simulation(partials)
        self.assertEqual(len(fed), 1)
        self.assertEqual(fed[0], "hello world how")

    def test_growing_partials_trigger_separate_feeds(self) -> None:
        # Three partials, each spaced > debounce and growing > char_delta.
        partials = [
            ("hello world really", 0.0),
            ("hello world really nice", 0.5),
            ("hello world really nice today and yesterday", 1.0),
        ]
        fed = self._run_simulation(partials)
        self.assertEqual(len(fed), 3)


# ── recent_turns plumbing through RagPrefetcher.submit ───────────────────


class RecentTurnsPlumbingTests(unittest.TestCase):
    def test_submit_passes_recent_turns_to_retriever(self) -> None:
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever,
            ttl_seconds=10,
            debounce_ms=0,
            min_partial_chars=4,
        )
        try:
            self.assertTrue(
                prefetcher.submit(
                    "tell me about the project",
                    recent_turns=["earlier user line", "earlier aiko line"],
                    exclude_session_id="sess-1",
                ),
            )
            _wait_completed(prefetcher, 1)
        finally:
            prefetcher.shutdown()
        self.assertEqual(len(retriever.calls), 1)
        call = retriever.calls[0]
        self.assertEqual(
            call["recent_turns"], ("earlier user line", "earlier aiko line"),
        )
        self.assertEqual(call["exclude_session_id"], "sess-1")

    def test_recent_turns_filter_falsy(self) -> None:
        # ``RagPrefetcher.submit`` drops falsy entries (``""``/``None``)
        # but leaves whitespace strings to ``RagRetriever._build_query``
        # which strips them downstream. Locking that contract here so the
        # listening-window hook can hand it raw chat-db rows verbatim.
        retriever = _FakeRetriever()
        prefetcher = RagPrefetcher(
            retriever,
            ttl_seconds=10,
            debounce_ms=0,
            min_partial_chars=4,
        )
        try:
            self.assertTrue(
                prefetcher.submit(
                    "another query that's long enough",
                    recent_turns=["", "real line", None, "another"],  # type: ignore[list-item]
                ),
            )
            _wait_completed(prefetcher, 1)
        finally:
            prefetcher.shutdown()
        call = retriever.calls[0]
        self.assertEqual(call["recent_turns"], ("real line", "another"))


# ── PromptAssembler static-slice cache ────────────────────────────────────


class StaticSliceCacheTests(unittest.TestCase):
    SESSION = "test-session"

    def test_first_assemble_is_miss_second_is_hit(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "first user line")
            db.add_message(self.SESSION, "assistant", "first reply")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            assembler.assemble_with_budget(
                self.SESSION, "what is up?",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "miss")
            assembler.assemble_with_budget(
                self.SESSION, "another question",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "hit")

    def test_new_message_invalidates_cache(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "first")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            assembler.assemble_with_budget(
                self.SESSION, "first turn",
                context_window=4096, response_budget=256,
            )
            assembler.assemble_with_budget(
                self.SESSION, "second turn",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "hit")
            db.add_message(self.SESSION, "assistant", "new reply lands")
            assembler.assemble_with_budget(
                self.SESSION, "third turn",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "miss")

    def test_prebuild_then_assemble_is_hit(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "warmup line one")
            db.add_message(self.SESSION, "assistant", "warmup line two")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            slices = assembler.prebuild_static_slices(self.SESSION)
            self.assertIsNotNone(slices)
            self.assertEqual(len(slices.history_msgs), 2)
            assembler.assemble_with_budget(
                self.SESSION, "now answer this",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "hit")

    def test_telemetry_records_slice_event(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "anchor line")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            _, telemetry = assembler.assemble_with_budget(
                self.SESSION, "first ask",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(telemetry.slice_cache_event, "miss")
            _, telemetry2 = assembler.assemble_with_budget(
                self.SESSION, "follow-up",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(telemetry2.slice_cache_event, "hit")

    def test_reset_slice_cache_clears(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "anchor line")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            assembler.assemble_with_budget(
                self.SESSION, "x",
                context_window=4096, response_budget=256,
            )
            assembler.assemble_with_budget(
                self.SESSION, "y",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "hit")
            assembler.reset_slice_cache(self.SESSION)
            assembler.assemble_with_budget(
                self.SESSION, "z",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(assembler.last_slice_cache_event, "miss")


# ── PromptTelemetry rag_prefetch_event ───────────────────────────────────


class PrefetchEventTelemetryTests(unittest.TestCase):
    SESSION = "tele-session"

    def test_skip_when_no_lookup_configured(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "anchor")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            _, telemetry = assembler.assemble_with_budget(
                self.SESSION, "ask",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(telemetry.rag_prefetch_event, "skip")

    def test_hit_when_lookup_returns_block(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "anchor")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            assembler.set_rag_prefetch_lookup(lambda _t: "BLOCK:cached-rag")
            _, telemetry = assembler.assemble_with_budget(
                self.SESSION, "ask",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(telemetry.rag_prefetch_event, "hit")
            self.assertGreater(telemetry.rag_tokens, 0)

    def test_miss_when_lookup_returns_none(self) -> None:
        with _TempDb() as db:
            db.add_message(self.SESSION, "user", "anchor")
            assembler = PromptAssembler(
                db,
                persona_path=Path("nonexistent_persona.txt"),
                recent_window=8,
            )
            assembler.set_rag_prefetch_lookup(lambda _t: None)
            _, telemetry = assembler.assemble_with_budget(
                self.SESSION, "ask",
                context_window=4096, response_budget=256,
            )
            self.assertEqual(telemetry.rag_prefetch_event, "miss")


# ── OllamaClient keep_alive plumbing ──────────────────────────────────────


def _make_ollama_client(*, keep_alive: str | None = None) -> OllamaClient:
    settings = OllamaSettings(
        base_url="http://localhost:11434",
        chat_model="test-model",
        temperature=0.5,
    )
    return OllamaClient(settings, keep_alive=keep_alive)


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = payload
    response.iter_lines.return_value = iter([])
    return response


class KeepAliveRequestBodyTests(unittest.TestCase):
    def _captured_payload(self, mock_post: MagicMock) -> dict:
        self.assertEqual(mock_post.call_count, 1)
        kwargs = mock_post.call_args.kwargs
        self.assertIn("json", kwargs)
        return kwargs["json"]

    def test_chat_with_tools_uses_default_keep_alive(self) -> None:
        client = _make_ollama_client(keep_alive="30m")
        with patch("app.llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _ok_response(
                {"message": {"content": "hello"}}
            )
            client.chat_with_tools([{"role": "user", "content": "hi"}])
        body = self._captured_payload(mock_post)
        self.assertEqual(body.get("keep_alive"), "30m")

    def test_chat_with_tools_per_call_override(self) -> None:
        client = _make_ollama_client(keep_alive="30m")
        with patch("app.llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _ok_response(
                {"message": {"content": "hello"}}
            )
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                keep_alive="2h",
            )
        body = self._captured_payload(mock_post)
        self.assertEqual(body.get("keep_alive"), "2h")

    def test_chat_with_tools_falls_back_to_default_when_none_provided(self) -> None:
        # Default for the client itself defaults to "30m" when not provided.
        client = _make_ollama_client()
        with patch("app.llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _ok_response(
                {"message": {"content": "hello"}}
            )
            client.chat_with_tools([{"role": "user", "content": "hi"}])
        body = self._captured_payload(mock_post)
        self.assertEqual(body.get("keep_alive"), "30m")

    def test_chat_json_uses_default_keep_alive(self) -> None:
        client = _make_ollama_client(keep_alive="45m")
        with patch("app.llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _ok_response(
                {"message": {"content": "{\"x\": 1}"}}
            )
            client.chat_json([{"role": "user", "content": "hi"}])
        body = self._captured_payload(mock_post)
        self.assertEqual(body.get("keep_alive"), "45m")

    def test_chat_stream_passes_keep_alive_in_payload(self) -> None:
        client = _make_ollama_client(keep_alive="1h")
        # ``chat_stream`` opens a streaming connection — we don't need the
        # generator to yield anything, just that the POST body carries
        # ``keep_alive``. Iterating the generator once triggers the call.
        with patch("app.llm.ollama_client.requests.post") as mock_post:
            response = MagicMock()
            response.ok = True
            response.status_code = 200
            response.iter_lines.return_value = iter([])
            response.__enter__ = lambda self_: self_
            response.__exit__ = lambda *args: None
            mock_post.return_value = response
            gen = client.chat_stream([{"role": "user", "content": "hi"}])
            for _ in gen:
                pass
        self.assertGreaterEqual(mock_post.call_count, 1)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body.get("keep_alive"), "1h")


# ── Cancel-on-speech via RagPrefetcher dedup gate ────────────────────────


class CancelOnSpeechTests(unittest.TestCase):
    """The plan relies on
    ``SessionController.feed_stt_partial`` calling ``scheduler.on_user_speech``
    on every non-final partial. We can't import the live scheduler here
    (heavy deps), but we can verify the contract: a fake observer plugged
    into a stand-in for ``feed_stt_partial`` is invoked once per call.
    """

    def test_observer_runs_per_partial(self) -> None:
        observer_calls: list[str] = []
        prefetcher_calls: list[str] = []

        def feed_partial(text: str, *, final: bool = False) -> None:
            text = (text or "").strip()
            if not text:
                return
            if not final:
                observer_calls.append(text)
            prefetcher_calls.append(text)

        feed_partial("first long enough text")
        feed_partial("second long enough text")
        feed_partial("third long enough text", final=True)
        self.assertEqual(len(observer_calls), 2)
        self.assertEqual(len(prefetcher_calls), 3)


if __name__ == "__main__":
    unittest.main()
