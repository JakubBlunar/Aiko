"""Shared chat-client protocol and dataclasses.

The chat layer used to be Ollama-only — every worker imported
``OllamaClient`` directly and the rest of the code assumed a single
local-Ollama-shaped API. As of the provider-selector work we route the
main chat path through any :class:`ChatClient` implementation while
background workers keep a (possibly independent) local fallback. This
module is the seam between the two: a tiny structural ``Protocol`` plus
the shared response / usage / tool-call dataclasses.

Why structural? Because the existing :class:`app.llm.ollama_client.OllamaClient`
predates the protocol — duck-typing lets us bring it under the umbrella
without refactoring 24 worker init sites. Callers that want to type-check
the new boundary can ``isinstance(client, ChatClient)`` at runtime.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Shared dataclasses ───────────────────────────────────────────────


@dataclass(slots=True)
class ChatToolCall:
    """A tool the model wants to invoke.

    Mirrors the OpenAI ``tool_calls[].function`` shape. ``arguments`` is
    always a parsed dict here; callers don't need to ``json.loads``.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(slots=True)
class ChatResponse:
    """Non-streaming chat reply.

    ``content`` is the user-visible answer (with ``<think>`` blocks
    stripped unless the caller asked for them). ``tool_calls`` is empty
    when the model didn't request any.
    """

    content: str
    tool_calls: list[ChatToolCall] = field(default_factory=list)


@dataclass(slots=True)
class ChatUsage:
    """Token + timing telemetry pulled from a chat round-trip.

    All fields are 0 / None when the underlying provider didn't include
    them. ``done_reason`` is the truncation signal (``"stop"`` clean,
    ``"length"`` truncated against ``num_predict`` / ``max_tokens``);
    Ollama reports it natively and the OpenAI-compatible client maps
    ``finish_reason`` onto the same vocabulary.

    ``cached_tokens`` is the OpenAI prompt-caching field — number of
    ``prompt_tokens`` that hit the server-side prefix cache and were
    billed at the ~90% discounted "cached input" rate. Ollama doesn't
    expose this signal (leaves it at ``0``), and most OpenAI-compatible
    providers other than OpenAI itself (Gemini, Groq, OpenRouter, …)
    also leave it at ``0`` because they don't return
    ``prompt_tokens_details.cached_tokens`` in the usage payload. See
    ``docs/prompt-caching.md`` for the prefix-stability contract that
    drives this number up.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_duration_ms: float = 0.0
    eval_duration_ms: float = 0.0
    prompt_eval_duration_ms: float = 0.0
    done_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_per_second(self) -> float:
        if self.eval_duration_ms <= 0 or self.completion_tokens <= 0:
            return 0.0
        return round((self.completion_tokens * 1000.0) / self.eval_duration_ms, 1)

    @property
    def cached_tokens_pct(self) -> float:
        """Percentage of ``prompt_tokens`` that hit the provider cache.

        Returns ``0.0`` on cold / unsupported providers. Returns
        ``100.0`` in the unrealistic case where every prompt token
        was cached (used only for log-line formatting).
        """
        if self.prompt_tokens <= 0 or self.cached_tokens <= 0:
            return 0.0
        return round(100.0 * self.cached_tokens / self.prompt_tokens, 1)

    def merge(self, other: "ChatUsage") -> "ChatUsage":
        """Return a new usage that adds another pass on top of this one.

        Used to combine the tool pre-pass and the streaming reply pass
        into a single per-turn telemetry record. Truncation is sticky:
        if either pass got cut off, the merged usage carries that
        signal forward; otherwise the later pass's reason wins (it's
        the one closer to "what the user actually saw").
        """
        if self.done_reason == "length" or other.done_reason == "length":
            merged_reason: str | None = "length"
        else:
            merged_reason = other.done_reason or self.done_reason
        return type(self)(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            total_duration_ms=self.total_duration_ms + other.total_duration_ms,
            eval_duration_ms=self.eval_duration_ms + other.eval_duration_ms,
            prompt_eval_duration_ms=(
                self.prompt_eval_duration_ms + other.prompt_eval_duration_ms
            ),
            done_reason=merged_reason,
        )


# ── Thinking-block stripper (shared by both clients) ──────────────────


# Reasoning models (qwen3.x, deepseek-r1, gpt-oss, gemini thinking, ...)
# sometimes leak their internal chain-of-thought into ``message.content``
# even when we ask for the final answer only. Different fine-tunes use
# different wrapper tokens; we accept the common ones (case-insensitive)
# and strip them before handing content back to callers. We DO NOT strip
# when the caller explicitly asked for the trace.
_THINKING_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<\s*(think|thinking|reasoning|reflection)\s*>.*?"
    r"<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Some fine-tunes open a thinking block but never close it before
# running out of budget (or before the answer starts). The whole
# unclosed tail is reasoning we can drop.
_UNCLOSED_THINKING_RE: re.Pattern[str] = re.compile(
    r"<\s*(think|thinking|reasoning|reflection)\s*>(?:(?!<\s*/\s*\1\s*>).)*\Z",
    re.IGNORECASE | re.DOTALL,
)


def strip_thinking_blocks_with_signal(text: str) -> tuple[str, bool]:
    """Strip thinking blocks and report whether any were removed.

    Returns ``(cleaned, had_thinking)``. ``had_thinking`` lets the
    caller distinguish two flavours of ``done_reason="length"``:

    1. The model wrote a thinking trace and a complete answer, but the
       *trace* tipped the response over ``num_predict``. The visible
       reply is fine; the warning would be a false positive.
    2. The model didn't think (or barely did), so the cap actually
       chopped the answer.

    Combined with :func:`content_looks_complete` we can downgrade the
    first case to debug noise instead of surfacing it as a WARNING.
    """
    if not text or "<" not in text:
        return text, False
    cleaned = _THINKING_BLOCK_RE.sub("", text)
    cleaned = _UNCLOSED_THINKING_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    return cleaned, cleaned != text


def strip_thinking_blocks(text: str) -> str:
    """Remove ``<think>...</think>``-style blocks from LLM content.

    Returns the text unchanged when there are no thinking markers, so
    the common case is essentially free. Both balanced blocks and a
    final unclosed block are stripped, then surrounding whitespace is
    collapsed so the cleaned content starts and ends where the actual
    answer does.
    """
    cleaned, _ = strip_thinking_blocks_with_signal(text)
    return cleaned


# Closing punctuation that signals "the answer made it to a natural
# stop". Non-exhaustive on purpose -- the goal is to suppress only the
# cases we're confident about and warn on anything else.
_TERMINAL_PUNCTUATION: tuple[str, ...] = (
    ".", "!", "?", "…", '"', "'", "`", ")", "]", "}", ">",
)


def content_looks_complete(text: str) -> bool:
    """Heuristic: did the visible answer reach a natural stop?

    True when ``text`` is non-empty and ends with closing punctuation
    after stripping trailing whitespace. False for empty content (cap
    chopped everything) or content that ends mid-word/mid-clause.
    """
    if not text:
        return False
    return text.rstrip().endswith(_TERMINAL_PUNCTUATION)


# ── Protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class ChatClient(Protocol):
    """Structural contract every chat-LLM client must satisfy.

    Implementations live alongside this module: :class:`OllamaClient`
    speaks Ollama's ``/api/chat`` natively, and
    :class:`OpenAICompatibleClient` speaks ``/v1/chat/completions``
    (OpenAI, Gemini, Groq, OpenRouter, …). Both share the dataclasses
    defined above so downstream code (TurnRunner, every worker) never
    has to branch on the concrete type.

    Why ``runtime_checkable`` instead of an ABC? Because the existing
    ``OllamaClient`` predates this protocol. Marking it runtime-
    checkable lets us assert conformance from tests without forcing
    inheritance.
    """

    # Public state every client exposes.
    base_url: str
    last_usage: ChatUsage

    def chat(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        model: str | None = None,
        think: bool = False,
        *,
        surface: str = "chat",
    ) -> str:
        """One-shot non-streaming convenience wrapper.

        Returns just the response content. Equivalent to calling
        :meth:`chat_with_tools` with ``tools=None`` and discarding the
        (empty) tool-call list.
        """

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, object] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        think: bool = False,
        keep_alive: str | None = None,
        surface: str = "chat_with_tools",
    ) -> ChatResponse:
        """Non-streaming call that returns content + any tool calls.

        ``tools`` follows the OpenAI ``function`` shape; the Ollama
        client passes it through unchanged because Ollama adopts the
        same schema. ``keep_alive`` is honoured natively by Ollama and
        silently ignored by remote providers.
        """

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        *,
        model: str | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        format_json: bool = False,
        think: bool = False,
        surface: str = "chat_stream",
    ) -> Generator[str, None, None]:
        """Stream content tokens as they arrive.

        After the generator drains, the last-chunk telemetry lands in
        :attr:`last_usage`. ``stop_event`` lets the caller abort
        mid-stream cleanly (the underlying socket is closed).
        """

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        options: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
        format_json: bool = True,
        think: bool = False,
        keep_alive: str | None = None,
        surface: str = "chat_json",
    ) -> tuple[str, ChatUsage]:
        """One-shot non-streaming call, defaults to JSON-format output.

        Used by background workers that need a bounded response and
        don't want to manage a stream. Returns ``(raw_content, usage)``.
        Set ``format_json=False`` for plain text (e.g. summarisation).
        """

    def list_models(self) -> list[str]:
        """Return the model identifiers this client can dispatch to.

        Best-effort: a transport failure should return ``[]`` rather
        than raise, so the UI's model dropdown can fall back to
        free-text.
        """

    def get_context_length(self, model: str) -> int | None:
        """Return the model's max input-token capacity, or ``None``.

        Ollama exposes this via ``/api/show``; OpenAI-compatible
        providers usually don't, in which case the controller falls
        back to ``chat_llm.context_window`` or a hardcoded default.
        """


__all__ = [
    "ChatClient",
    "ChatResponse",
    "ChatToolCall",
    "ChatUsage",
    "strip_thinking_blocks",
    "strip_thinking_blocks_with_signal",
    "content_looks_complete",
]
