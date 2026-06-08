"""Tests for :class:`app.llm.embedder.Embedder` (P1 perf backlog).

The interesting surface here is the per-turn budget hooks:
``begin_turn`` / ``end_turn`` / ``peek_turn_stats``. We stub
``_call_ollama`` so the tests don't touch the network and so we
can pin the timing contract (one HTTP call = one increment, cache
hits = no increment, thread-isolated, etc.).
"""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

import numpy as np

from app.llm.embedder import Embedder


# ── stub helpers ────────────────────────────────────────────────────


def _settings() -> SimpleNamespace:
    """Minimal stand-in for :class:`OllamaSettings`."""
    return SimpleNamespace(
        embedding_model="qwen3-embedding:0.6b",
        embedding_base_url="",
        base_url="http://localhost:11434",
    )


def _build(call_delay_s: float = 0.0) -> tuple[Embedder, list[str]]:
    """Return an ``Embedder`` with ``_call_ollama`` stubbed to a fixed
    delay and a list capturing each call's ``text`` argument."""
    emb = Embedder(_settings())
    seen: list[str] = []
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def _stub_call(text: str) -> np.ndarray:
        seen.append(text)
        if call_delay_s > 0:
            time.sleep(call_delay_s)
        return vec

    emb._call_ollama = _stub_call  # type: ignore[assignment]
    return emb, seen


# ── tests ───────────────────────────────────────────────────────────


class BeginEndTurnTests(unittest.TestCase):
    def test_no_active_turn_returns_zero(self) -> None:
        emb, _seen = _build()
        # Without ``begin_turn`` the embedder is in pass-through mode;
        # ``end_turn`` must return clean zeros, not raise.
        calls, ms = emb.end_turn()
        self.assertEqual(calls, 0)
        self.assertEqual(ms, 0.0)

    def test_begin_then_end_counts_real_calls(self) -> None:
        emb, _seen = _build(call_delay_s=0.005)
        emb.begin_turn()
        emb.embed("alpha alpha alpha")
        emb.embed("beta beta beta")
        emb.embed("gamma gamma gamma")
        calls, ms = emb.end_turn()
        self.assertEqual(calls, 3)
        # Three calls × ≥5ms sleep should clear 5ms in aggregate.
        self.assertGreaterEqual(ms, 5.0)

    def test_cache_hits_dont_increment_call_counter(self) -> None:
        emb, seen = _build(call_delay_s=0.001)
        emb.begin_turn()
        emb.embed("same string")
        emb.embed("same string")  # LRU hit, no HTTP
        emb.embed("same string")  # LRU hit, no HTTP
        calls, ms = emb.end_turn()
        # Only one real HTTP call even though embed() was hit thrice.
        self.assertEqual(calls, 1)
        self.assertEqual(len(seen), 1)
        # Timing should reflect just the single round-trip.
        self.assertGreaterEqual(ms, 0.5)

    def test_peek_does_not_reset(self) -> None:
        emb, _seen = _build(call_delay_s=0.001)
        emb.begin_turn()
        emb.embed("alpha")
        peek_calls, peek_ms = emb.peek_turn_stats()
        self.assertEqual(peek_calls, 1)
        self.assertGreaterEqual(peek_ms, 0.5)
        # After peek, end_turn should still report the same call count
        # (peek is non-destructive).
        end_calls, _ = emb.end_turn()
        self.assertEqual(end_calls, 1)

    def test_end_turn_resets_state_for_next_turn(self) -> None:
        emb, _seen = _build(call_delay_s=0.001)
        emb.begin_turn()
        emb.embed("alpha")
        emb.end_turn()
        # Second turn starts cold even though the same thread is reused.
        emb.begin_turn()
        emb.embed("beta")
        calls, _ = emb.end_turn()
        self.assertEqual(calls, 1)

    def test_double_begin_resets_counters(self) -> None:
        # ``begin_turn`` is idempotent within a thread: a second call
        # resets to zero so a previous turn that forgot to call
        # ``end_turn`` doesn't leak its counters into the new turn.
        emb, _seen = _build(call_delay_s=0.001)
        emb.begin_turn()
        emb.embed("alpha")
        emb.embed("beta")
        emb.begin_turn()  # forced reset
        emb.embed("gamma")
        calls, _ = emb.end_turn()
        self.assertEqual(calls, 1)


class ThreadIsolationTests(unittest.TestCase):
    def test_background_thread_does_not_pollute_turn_counters(self) -> None:
        # The MessageIndexer runs on a background thread that shares
        # the same Embedder instance. Its async embeds must NOT land
        # on the turn thread's budget. We simulate that contract by
        # running embeds on a fresh thread that doesn't call
        # ``begin_turn``.
        emb, _seen = _build(call_delay_s=0.001)
        emb.begin_turn()
        emb.embed("turn-thread call")

        def _bg_embed() -> None:
            for _ in range(5):
                emb.embed(f"bg-{_}")

        t = threading.Thread(target=_bg_embed, daemon=True)
        t.start()
        t.join(timeout=5.0)
        calls, _ = emb.end_turn()
        # Only the turn-thread's single call counts; the five
        # background-thread calls don't show up.
        self.assertEqual(calls, 1)


class CallTimingTests(unittest.TestCase):
    def test_call_ms_accumulates(self) -> None:
        # Two slow calls + one fast call should accumulate roughly
        # additively. We use a generous lower bound to keep the test
        # robust on slow CI machines.
        emb, _seen = _build(call_delay_s=0.010)
        emb.begin_turn()
        emb.embed("aaa aaa aaa")
        emb.embed("bbb bbb bbb")
        emb.embed("ccc ccc ccc")
        calls, ms = emb.end_turn()
        self.assertEqual(calls, 3)
        self.assertGreaterEqual(ms, 25.0)

    def test_short_text_still_throws(self) -> None:
        # Empty input is rejected before begin_turn accounting kicks
        # in; the counters must stay at zero.
        emb, _seen = _build()
        emb.begin_turn()
        with self.assertRaises(ValueError):
            emb.embed("")
        calls, ms = emb.end_turn()
        self.assertEqual(calls, 0)
        self.assertEqual(ms, 0.0)


class _FakeResponse:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def raise_for_status(self) -> None:  # noqa: D401 - test stub
        return None

    def json(self) -> dict:
        return {"embedding": self._vector}


class RequestOptionsTests(unittest.TestCase):
    """The VRAM levers (``num_gpu`` / ``num_ctx``) reach the HTTP body."""

    def _capture_payload(self, settings) -> dict:
        emb = Embedder(settings)
        captured: dict = {}

        def _fake_post(url, json=None, timeout=None):  # noqa: A002
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse([1.0, 0.0, 0.0])

        emb._session.post = _fake_post  # type: ignore[assignment]
        emb.embed("hello world here")
        return captured["json"]

    def test_no_options_when_unset(self) -> None:
        body = self._capture_payload(_settings())
        self.assertNotIn("options", body)

    def test_num_gpu_zero_forces_cpu(self) -> None:
        settings = _settings()
        settings.embedding_num_gpu = 0
        body = self._capture_payload(settings)
        self.assertEqual(body["options"], {"num_gpu": 0})

    def test_num_ctx_is_passed(self) -> None:
        settings = _settings()
        settings.embedding_num_ctx = 2048
        body = self._capture_payload(settings)
        self.assertEqual(body["options"], {"num_ctx": 2048})

    def test_both_options_combine(self) -> None:
        settings = _settings()
        settings.embedding_num_gpu = 0
        settings.embedding_num_ctx = 2048
        body = self._capture_payload(settings)
        self.assertEqual(body["options"], {"num_gpu": 0, "num_ctx": 2048})


if __name__ == "__main__":
    unittest.main()
