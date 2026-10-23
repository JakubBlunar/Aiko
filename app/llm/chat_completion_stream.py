"""Chat Completions streaming: framing, private text and completion integrity."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass

import requests

from app.llm.chat_client import ChatUsage
from app.llm.token_usage import read_reasoning_tokens
from app.llm.turn_outcome import ProviderOutcomeError


_SSE_SPLIT_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s?(.*)$")
_THINKING_NAMES = ("think", "thinking", "reasoning", "reflection")
_THINKING_OPEN = re.compile(r"<\s*(think|thinking|reasoning|reflection)\s*>", re.IGNORECASE)


def map_finish_reason(reason: object) -> str | None:
    """Keep the provider's lowercase reason for truncation/error telemetry."""
    if reason is None:
        return None
    return str(reason).strip().lower() or None


@dataclass
class _SSEState:
    done: bool = False


def iter_sse_data_lines(
    response: requests.Response,
    *,
    stop_event: threading.Event | None = None,
    state: _SSEState | None = None,
) -> Generator[str, None, None]:
    """Yield SSE data, retaining an optional signal for the terminal sentinel.

    SSE is UTF-8 even when a server omits its charset. Requests otherwise
    assumes Latin-1 for a text content type, or returns bytes without encoding.
    Comments, empty lines and non-data fields do not contain model output.
    """
    response.encoding = "utf-8"
    for raw_line in response.iter_lines(decode_unicode=True):
        if stop_event is not None and stop_event.is_set():
            return
        if not raw_line or raw_line.startswith(":"):
            continue
        match = _SSE_SPLIT_RE.match(raw_line)
        if match is None or match.group(1) != "data":
            continue
        value = match.group(2).strip()
        if value == "[DONE]":
            if state is not None:
                state.done = True
            return
        if value:
            yield value


class _ThinkingFilter:
    """Streaming counterpart to chat_client's inline thinking-block stripper.

    Ordinary content streams immediately. Only a possible tag suffix waits;
    private blocks are discarded incrementally rather than kept in memory.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._private_tag = ""

    def _possible_marker(self, suffix: str) -> bool:
        rest = suffix[1:].lstrip().lower()
        if self._private_tag:
            if not rest:
                return True
            if not rest.startswith("/"):
                return False
            rest = rest[1:].lstrip()
        names = (self._private_tag,) if self._private_tag else _THINKING_NAMES
        return any(
            name.startswith(rest) or (rest.startswith(name) and rest[len(name):].isspace())
            for name in names
        )

    def feed(self, text: str, *, final: bool = False) -> str:
        self._pending += text
        visible = []
        while self._pending:
            if self._private_tag:
                close = re.search(
                    rf"<\s*/\s*{self._private_tag}\s*>", self._pending, re.IGNORECASE,
                )
                if close is not None:
                    self._pending = self._pending[close.end():]
                    self._private_tag = ""
                    continue
            else:
                opening = _THINKING_OPEN.search(self._pending)
                if opening is not None:
                    visible.append(self._pending[:opening.start()])
                    self._private_tag = opening.group(1).lower()
                    self._pending = self._pending[opening.end():]
                    continue
            start = self._pending.rfind("<")
            hold = (
                start if not final and start >= 0 and self._possible_marker(self._pending[start:])
                else len(self._pending)
            )
            if not self._private_tag:
                visible.append(self._pending[:hold])
            self._pending = self._pending[hold:]
            break
        return "".join(visible)


def stream_chat_completion(
    response: requests.Response,
    *,
    usage: ChatUsage,
    started_at: float,
    stop_event: threading.Event | None,
) -> Generator[str, None, float | None]:
    """Yield only answer text, and fail before callers commit an invalid turn.

    A terminal finish reason or [DONE] establishes completion. An explicit
    cancellation needs neither. Errors/refusal signals remain provider outcomes;
    their payloads and private reasoning are never returned as character speech.
    Visible length-limited replies retain the existing truncation warning path.
    """
    state = _SSEState()
    thinking = _ThinkingFilter()
    first_token_ms = None
    visible_content = False
    for data in iter_sse_data_lines(response, stop_event=stop_event, state=state):
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error") is not None or chunk.get("type") == "error":
            raise ProviderOutcomeError(
                "Chat completion stream reported a provider error.", outcome="provider_error",
            )
        raw_usage = chunk.get("usage")
        if isinstance(raw_usage, dict):
            usage.prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
            usage.completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
            details = raw_usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                usage.cached_tokens = int(details.get("cached_tokens", 0) or 0)
            read_reasoning_tokens(usage, raw_usage)
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        if choice.get("finish_reason") is not None:
            usage.done_reason = map_finish_reason(choice["finish_reason"])
        delta = choice.get("delta")
        if usage.done_reason in {"content_filter", "refusal", "error"} or (
            isinstance(delta, dict) and delta.get("refusal")
        ):
            raise ProviderOutcomeError(
                "Chat completion was blocked by the provider.", outcome="provider_blocked",
                finish_reason=usage.done_reason,
            )
        if not isinstance(delta, dict):
            continue
        token = delta.get("content")
        if isinstance(token, str) and token:
            answer = thinking.feed(token)
            if answer:
                if first_token_ms is None:
                    first_token_ms = (time.monotonic() - started_at) * 1000.0
                visible_content = visible_content or bool(answer.strip())
                yield answer
    if stop_event is not None and stop_event.is_set():
        return first_token_ms
    if usage.done_reason is None and not state.done:
        raise ProviderOutcomeError(
            "Chat completion stream ended before completion.", outcome="incomplete",
        )
    tail = thinking.feed("", final=True)
    if tail:
        if first_token_ms is None:
            first_token_ms = (time.monotonic() - started_at) * 1000.0
        visible_content = visible_content or bool(tail.strip())
        yield tail
    if not visible_content:
        raise ProviderOutcomeError(
            "Chat completion produced no visible answer; check the reasoning/output budget.",
            outcome="budget_exhausted" if usage.done_reason == "length" else "empty",
            finish_reason=usage.done_reason,
        )
    return first_token_ms
