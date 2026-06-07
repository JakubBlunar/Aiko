"""Tests for the local-worker + embedder pre-warm path on session boot.

Regression for the symptom observed in production: when chat is a
remote provider (e.g. ``openai_compatible`` -> gpt-5-mini) and
``workers_use_local=true`` (the default), ``prewarm_runtime`` used to
report ``"Using remote model: ... (no local warmup)"`` and exit
*without warming the local worker model or the embedder*. The first
real turn then paid the full cold-load cost: ~22s on the embedder
plus tens of seconds if the worker model was something big like
``qwen3-coder:30b``.

The fix split the warmup into three independent passes:

* chat-model warmup (existing) — only fires for local-Ollama chat
* worker-model warmup (new) — fires when worker client is a separate
  local Ollama instance
* embedder warmup (new) — always fires when an embedder exists

These tests exercise the two new helpers directly on a stubbed
``self``-shape so we don't have to build a full ``SessionController``
(heavy + side-effect-laden).
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from app.core.session.session_controller import SessionController
from app.llm.ollama_client import OllamaClient


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


class _StubEmbedder:
    """Bare-minimum embedder surface the warmup helper reads."""

    def __init__(
        self,
        *,
        model: str = "nomic-embed-text",
        raises: Exception | None = None,
    ) -> None:
        self.model = model
        self._raises = raises
        self.calls: list[str] = []

    def embed(self, text: str) -> Any:  # pragma: no cover - simple shim
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        return [0.0, 0.0]


class _StubOllamaSettings:
    """Minimal ``OllamaSettings`` surface the warmup helper reads."""

    def __init__(self, context_window: int | None = 32768) -> None:
        self.context_window = context_window


class _StubAppSettings:
    """``SessionController._settings`` carries an ``.ollama`` block —
    the worker warmup uses ``self._settings.ollama.context_window`` to
    size ``num_ctx`` on the very first call (the kv-cache-size fix).
    """

    def __init__(self, context_window: int | None = 32768) -> None:
        self.ollama = _StubOllamaSettings(context_window)


def _make_stub_session(
    *,
    worker_client: Any,
    chat_client: Any,
    effective_worker_model: str = "qwen3-coder:30b",
    embedder: Any = None,
    context_window: int | None = 32768,
) -> Any:
    """Return an object with just the attributes the helpers read."""

    class _Stub:
        pass

    stub = _Stub()
    stub._worker_client = worker_client
    stub._chat_client = chat_client
    stub._effective_worker_model = effective_worker_model
    stub._embedder = embedder
    stub._settings = _StubAppSettings(context_window)
    return stub


# ── _prewarm_local_worker_model ──────────────────────────────────────────


class PrewarmLocalWorkerModelTests(unittest.TestCase):
    def test_skipped_when_worker_is_chat_client(self) -> None:
        # Pure-Ollama mode: worker and chat are the same client; the
        # chat warmup at the top of ``prewarm_runtime`` already loaded
        # this model. Touching it again would be wasted work.
        shared = MagicMock(spec=OllamaClient)
        stub = _make_stub_session(
            worker_client=shared, chat_client=shared,
        )
        report = _Recorder()
        SessionController._prewarm_local_worker_model(stub, report)
        shared.chat.assert_not_called()
        self.assertEqual(report.messages, [])

    def test_skipped_when_worker_is_not_ollama(self) -> None:
        # ``workers_use_local=False`` keeps workers on the remote chat
        # client; nothing local to warm.
        chat_client = object()  # any non-OllamaClient sentinel
        worker_client = chat_client  # both point at remote
        stub = _make_stub_session(
            worker_client=worker_client, chat_client=chat_client,
        )
        report = _Recorder()
        SessionController._prewarm_local_worker_model(stub, report)
        self.assertEqual(report.messages, [])

    def test_skipped_when_worker_model_is_empty(self) -> None:
        worker = MagicMock(spec=OllamaClient)
        chat = object()
        stub = _make_stub_session(
            worker_client=worker, chat_client=chat,
            effective_worker_model="   ",
        )
        report = _Recorder()
        SessionController._prewarm_local_worker_model(stub, report)
        worker.chat.assert_not_called()

    def test_cloud_worker_model_skips_chat_ping(self) -> None:
        # Ollama Cloud loads server-side; a local ping is wasted.
        # The helper should still emit a status line so the boot log
        # shows we *recognised* the cloud model rather than silently
        # skipping it.
        worker = MagicMock(spec=OllamaClient)
        chat = object()
        for cloud_model in ("qwen3-coder:cloud", "llama3.1-8b-cloud"):
            stub = _make_stub_session(
                worker_client=worker, chat_client=chat,
                effective_worker_model=cloud_model,
            )
            report = _Recorder()
            SessionController._prewarm_local_worker_model(stub, report)
            worker.chat.assert_not_called()
            self.assertTrue(
                any("Ollama Cloud worker model" in m for m in report.messages),
                f"expected cloud-model status line for {cloud_model}, "
                f"got {report.messages!r}",
            )

    def test_happy_path_warms_local_worker_model(self) -> None:
        # The actual regression: chat=remote, worker=local Ollama,
        # worker model is a real local model. The helper must call
        # ``chat`` on the worker client with the right model and
        # surface tag so future ``surface=model_warmup`` log filters
        # work.
        worker = MagicMock(spec=OllamaClient)
        chat = object()  # remote chat client (any non-Ollama sentinel)
        stub = _make_stub_session(
            worker_client=worker, chat_client=chat,
            effective_worker_model="qwen3-coder:30b",
            context_window=32768,
        )
        report = _Recorder()
        SessionController._prewarm_local_worker_model(stub, report)
        worker.chat.assert_called_once()
        call_kwargs = worker.chat.call_args.kwargs
        self.assertEqual(call_kwargs.get("model"), "qwen3-coder:30b")
        self.assertEqual(call_kwargs.get("surface"), "model_warmup")
        # The kv-cache-size fix: the warmup MUST pass num_ctx so
        # Ollama's first load reserves the right size. Without this
        # qwen3-coder:30b would load at its built-in 256k default and
        # spill from VRAM to RAM.
        self.assertEqual(
            call_kwargs.get("options"), {"num_ctx": 32768},
        )
        self.assertTrue(
            any("Warming worker model: qwen3-coder:30b" in m
                for m in report.messages),
        )

    def test_happy_path_omits_options_when_context_window_unset(self) -> None:
        # When the user hasn't configured a context window (the
        # documented ``None`` "auto-detect" sentinel), the warmup
        # must NOT pass an empty ``options`` dict either — the
        # OllamaClient's own default-injection layer will handle the
        # fallback. Explicit ``None`` keeps the call site behaviour
        # symmetric with the legacy code path.
        worker = MagicMock(spec=OllamaClient)
        chat = object()
        stub = _make_stub_session(
            worker_client=worker, chat_client=chat,
            effective_worker_model="qwen3-coder:30b",
            context_window=None,
        )
        report = _Recorder()
        SessionController._prewarm_local_worker_model(stub, report)
        worker.chat.assert_called_once()
        self.assertIsNone(worker.chat.call_args.kwargs.get("options"))

    def test_warmup_exception_swallowed(self) -> None:
        # A cold-start failure here must not block the rest of boot.
        # The exception is logged at WARNING; we just verify the
        # helper returns cleanly.
        worker = MagicMock(spec=OllamaClient)
        worker.chat.side_effect = RuntimeError("connection refused")
        chat = object()
        stub = _make_stub_session(
            worker_client=worker, chat_client=chat,
        )
        report = _Recorder()
        SessionController._prewarm_local_worker_model(stub, report)
        worker.chat.assert_called_once()


# ── _prewarm_embedder ────────────────────────────────────────────────────


class PrewarmEmbedderTests(unittest.TestCase):
    def test_skipped_when_no_embedder(self) -> None:
        stub = _make_stub_session(
            worker_client=None, chat_client=None, embedder=None,
        )
        report = _Recorder()
        SessionController._prewarm_embedder(stub, report)
        self.assertEqual(report.messages, [])

    def test_skipped_when_model_blank(self) -> None:
        embedder = _StubEmbedder(model="   ")
        stub = _make_stub_session(
            worker_client=None, chat_client=None, embedder=embedder,
        )
        report = _Recorder()
        SessionController._prewarm_embedder(stub, report)
        self.assertEqual(embedder.calls, [])

    def test_happy_path_calls_embed_with_minimal_text(self) -> None:
        # The cheapest possible ``/embeddings`` round-trip:
        # single-character prompt. The point isn't the embedding
        # vector — it's that Ollama loads the model into its
        # loaded-models slot so the first turn's RAG retrieval
        # doesn't pay 20+ seconds of cold-load latency.
        embedder = _StubEmbedder(model="nomic-embed-text")
        stub = _make_stub_session(
            worker_client=None, chat_client=None, embedder=embedder,
        )
        report = _Recorder()
        SessionController._prewarm_embedder(stub, report)
        self.assertEqual(embedder.calls, ["."])
        self.assertTrue(
            any("Warming embedder: nomic-embed-text" in m
                for m in report.messages),
        )

    def test_embed_exception_swallowed(self) -> None:
        # Embedder cold-start failure must not block boot. A cold
        # embedder is slow but not fatal: RAG retrieval silently
        # degrades when ``Embedder.embed`` raises, so a warmup miss
        # is recoverable.
        embedder = _StubEmbedder(
            model="nomic-embed-text",
            raises=RuntimeError("read timeout"),
        )
        stub = _make_stub_session(
            worker_client=None, chat_client=None, embedder=embedder,
        )
        report = _Recorder()
        SessionController._prewarm_embedder(stub, report)
        # Helper still attempted exactly one embed call.
        self.assertEqual(embedder.calls, ["."])


if __name__ == "__main__":
    unittest.main()
