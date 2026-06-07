"""OpenAI-compatible chat client.

Speaks ``/v1/chat/completions`` (OpenAI, Google Gemini, Groq,
OpenRouter, DeepSeek, Mistral, xAI Grok, etc.). Implements the
structural :class:`app.llm.chat_client.ChatClient` protocol so
``SessionController`` can swap it in for ``OllamaClient`` without the
rest of the code knowing the difference.

Why hand-rolled? Two reasons:

1. We already depend on ``requests`` (the Ollama client uses it), and a
   single-file implementation is ~300 lines. Pulling in
   ``langchain-openai`` would add 30+ MB of transitive deps and a
   global ``ChatOpenAI`` class hierarchy we don't want to inherit.
2. Some providers (Gemini's OpenAI-compat layer in particular) have
   small but real quirks that are easier to handle inline than to push
   into a vendor SDK's settings dict.

Quirks handled here:

- Gemini doesn't accept ``system`` role in OpenAI-compat mode for all
  models — when the configured model name starts with ``gemini-`` /
  ``models/gemini-`` we collapse system messages into the first user
  turn before sending.
- ``finish_reason="length"`` is mapped onto Ollama's
  ``done_reason="length"`` so the existing truncation WARN log fires
  on remote providers identically to local Ollama (see
  :func:`_warn_if_truncated`).
- ``response_format={"type":"json_object"}`` is set when
  ``format_json=True`` so background workers (summary, extractor) get
  JSON-shaped output on providers that respect it. Providers that
  don't (looking at you, Groq with some models) will just return
  text and the existing parsers tolerate that.
- Extra headers (``HTTP-Referer`` / ``X-Title`` for OpenRouter, etc.)
  are forwarded from ``chat_llm.extra_headers``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Generator
from typing import Any

import requests

from app.core.infra.settings import OllamaSettings
from app.llm.chat_client import (
    ChatResponse,
    ChatToolCall,
    ChatUsage,
    content_looks_complete as _content_looks_complete,
    strip_thinking_blocks_with_signal as _strip_thinking_blocks_with_signal,
)


log = logging.getLogger("app.llm.openai_compatible_client")

# One-shot per-base-url connection notices (INFO at most once per process).
_announced_base_urls: set[str] = set()

# Surfaces where ``finish_reason="length"`` is harmless by design.
# Mirrors the Ollama client's list; the rationale is identical (the
# pre-streaming tool-selection pass caps response tokens deliberately).
_BENIGN_TRUNCATION_SURFACES: frozenset[str] = frozenset({"tool_pass"})


# Conservative context-window caps keyed by model-id prefix.
#
# First match wins (longer prefixes must come before shorter ones so
# e.g. ``gpt-4.1-mini`` doesn't fall through to the ``gpt-4`` rule).
# Values are intentionally below the model's true maximum; see
# ``OpenAICompatibleClient.get_context_length`` for the rationale.
_CONTEXT_WINDOW_TABLE: tuple[tuple[str, int], ...] = (
    # ── GPT-5 family (Aug 2025+). 400 k native, capped at 128 k. ───
    # Covers gpt-5, gpt-5-mini, gpt-5-nano, gpt-5-pro, gpt-5.1,
    # gpt-5.2, gpt-5.4-*, gpt-5.5-*, gpt-5.5-pro, …
    ("gpt-5", 131_072),
    # ── GPT-4.1 family. 1 M native, capped at 128 k. ────────────────
    ("gpt-4.1", 131_072),
    # ── GPT-4o family. Native 128 k. ────────────────────────────────
    ("gpt-4o", 131_072),
    ("gpt-4-turbo", 131_072),
    # ── Older GPT-4 / 3.5. Native windows are smaller. ──────────────
    ("gpt-4", 8_192),
    ("gpt-3.5-turbo", 16_385),
    # ── Reasoning models (o-series). 200 k native. ──────────────────
    ("o4-mini", 200_000),
    ("o3", 200_000),
    ("o1", 200_000),
    # ── Gemini 2.5 family. 1-2 M native, capped at 128 k. ───────────
    ("gemini-2.5-pro", 131_072),
    ("gemini-2.5-flash-lite", 131_072),
    ("gemini-2.5-flash", 131_072),
    ("gemini-2.5", 131_072),
    # ── Groq llama-3.x family. 128 k native. ────────────────────────
    ("llama-3.3", 131_072),
    ("llama-3.1", 131_072),
    # ── Anthropic via OpenRouter / openai-compat. 200 k native. ─────
    ("claude-3.5", 200_000),
    ("claude-3-", 200_000),
    ("claude-4", 200_000),
    ("anthropic/claude-3.5", 200_000),
    ("anthropic/claude-3", 200_000),
    ("anthropic/claude-4", 200_000),
)


def _lookup_context_window(model: str) -> int | None:
    """Match a model id against ``_CONTEXT_WINDOW_TABLE``.

    Strips the ``models/`` prefix Gemini sometimes emits before
    matching, lowercases for case-insensitive matching, and returns
    ``None`` when no prefix matches (the controller falls back to
    the explicit override or the hardcoded 8192 last-resort default).
    """
    name = (model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    if not name:
        return None
    for prefix, window in _CONTEXT_WINDOW_TABLE:
        if name.startswith(prefix):
            return window
    return None


def _is_gemini_model(model: str) -> bool:
    """True when the configured model is a Gemini variant.

    Gemini's OpenAI-compat endpoint reports model ids like
    ``gemini-2.5-flash-lite`` or ``models/gemini-2.5-pro``; both
    forms are recognised. Returning True opts into the system-role
    collapse + temperature clamp paths below.
    """
    name = (model or "").strip().lower()
    return name.startswith("gemini-") or name.startswith("models/gemini-")


def _collapse_system_for_gemini(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold every ``system`` message into the first ``user`` message.

    Gemini's OpenAI-compat endpoint accepts ``system`` for most models
    but rejects it intermittently — collapsing avoids the failure mode.
    System content is concatenated (preserving order) and prepended to
    the first user message with a blank line as a separator. The
    function is a no-op when the message list has no system entries.

    We never mutate the caller's list; a fresh list is returned so the
    caller's audit trail / retry logic keeps working.
    """
    has_system = any(
        isinstance(m, dict) and (m.get("role") == "system") for m in messages
    )
    if not has_system:
        return list(messages)
    system_parts: list[str] = []
    other: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            other.append(msg)  # type: ignore[arg-type]
            continue
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue
        other.append(msg)
    if not system_parts:
        return list(messages)
    prefix = "\n\n".join(system_parts)
    # Find first user turn; if none exists (rare — agent-only prompts)
    # synthesise one carrying just the system prefix.
    out: list[dict[str, Any]] = []
    injected = False
    for msg in other:
        if (
            not injected
            and isinstance(msg, dict)
            and msg.get("role") == "user"
        ):
            user_content = msg.get("content", "")
            if not isinstance(user_content, str):
                user_content = "" if user_content is None else str(user_content)
            merged = f"{prefix}\n\n{user_content}".strip()
            out.append({**msg, "content": merged})
            injected = True
        else:
            out.append(msg)
    if not injected:
        out.insert(0, {"role": "user", "content": prefix})
    return out


def _normalize_tool_messages_for_openai(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reshape tool-call traffic from neutral form to strict OpenAI shape.

    The codebase emits a single neutral message format that Ollama and
    OpenAI-compatible providers both consume (see ``TurnRunner``):

    - assistant tool_calls carry ``id`` + ``type=function`` +
      ``function: {name, arguments(dict)}``.
    - tool result messages carry ``tool_call_id`` + ``name`` +
      ``content``.

    Ollama is permissive — it accepts dict ``arguments`` and ignores any
    extras. OpenAI's ``/v1/chat/completions`` is strict and 400s if:

    - ``tool_calls[i].type`` is missing,
    - ``tool_calls[i].id`` is missing,
    - ``tool_calls[i].function.arguments`` is not a JSON string,
    - ``role=tool`` lacks ``tool_call_id``.

    This pass walks ``messages`` and normalises just those four points.
    Anything already in the right shape passes through. The caller's
    list is never mutated — a fresh list is returned so retry buffers
    keep working.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)  # type: ignore[arg-type]
            continue
        role = msg.get("role")
        if role == "assistant" and isinstance(msg.get("tool_calls"), list):
            new_calls: list[dict[str, Any]] = []
            for idx, call in enumerate(msg["tool_calls"]):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict):
                    fn = {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    args_str = args
                elif args is None:
                    args_str = "{}"
                else:
                    try:
                        args_str = json.dumps(
                            args, ensure_ascii=False, default=str,
                        )
                    except (TypeError, ValueError):
                        args_str = "{}"
                call_id = str(call.get("id", "") or "").strip()
                if not call_id:
                    call_id = f"call_{idx}"
                new_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(fn.get("name", "") or ""),
                        "arguments": args_str,
                    },
                })
            new_msg = dict(msg)
            new_msg["tool_calls"] = new_calls
            # OpenAI rejects ``content: null`` only sometimes; an empty
            # string is universally accepted.
            if new_msg.get("content") is None:
                new_msg["content"] = ""
            out.append(new_msg)
        elif role == "tool":
            new_msg = dict(msg)
            tool_call_id = new_msg.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                # Fall back to ``id`` if a caller still uses the old name.
                fallback = str(new_msg.get("id", "") or "").strip()
                if fallback:
                    new_msg["tool_call_id"] = fallback
            # OpenAI doesn't read ``name`` on tool messages and some
            # routes warn about unknown keys — keep it for Ollama
            # compatibility; both providers ignore-or-tolerate it.
            content = new_msg.get("content", "")
            if not isinstance(content, str):
                new_msg["content"] = "" if content is None else str(content)
            out.append(new_msg)
        else:
            out.append(msg)
    return out


# Ollama exposes a wide ``options`` dict (``num_ctx``, ``num_keep``,
# ``mirostat``, ``num_thread``, …) on top of the shared knobs
# (``temperature``, ``top_p``, ``seed``, …). The rest of the codebase
# speaks Ollama, so worker call sites send dicts like
# ``{"temperature": 0.2, "num_predict": 512, "num_ctx": 32768}``.
# OpenAI's ``/chat/completions`` strict-rejects unknown params with
# HTTP 400 (``Unknown parameter: 'num_ctx'``), so this client drops
# any Ollama-only key from the outbound payload before posting.
#
# Keep this list narrow: it only covers keys Ollama owns exclusively
# (model-host knobs + sampling extensions that no major
# OpenAI-compatible remote provider speaks). Overlapping keys —
# ``temperature``, ``top_p``, ``top_k``, ``min_p``, ``repeat_penalty``,
# ``seed``, ``frequency_penalty``, ``presence_penalty``, ``stop``,
# ``logit_bias`` — fall through unchanged so Gemini's
# OpenAI-compatible layer (which accepts ``top_k`` etc.) keeps
# working. ``num_predict`` is translated separately to ``max_tokens``.
_OLLAMA_ONLY_OPTION_KEYS: frozenset[str] = frozenset({
    "num_ctx",
    "num_keep",
    "num_batch",
    "num_gpu",
    "main_gpu",
    "num_thread",
    "low_vram",
    "f16_kv",
    "vocab_only",
    "use_mmap",
    "use_mlock",
    "numa",
    "mirostat",
    "mirostat_tau",
    "mirostat_eta",
    "tfs_z",
    "typical_p",
    "repeat_last_n",
    "penalize_newline",
})


def _is_responses_api_family(model: str) -> bool:
    """Return True if ``model`` belongs to OpenAI's newer
    Responses-API parameter family (GPT-5 + o-series reasoning).

    Two parameter-shape quirks distinguish this family from older
    OpenAI models (and from all non-OpenAI compat providers):

    * ``max_tokens`` is replaced by ``max_completion_tokens`` (legacy
      field hard-400s with ``Unsupported parameter: 'max_tokens'``).
    * The classic sampling knobs are LOCKED to their default value:
      ``temperature`` must be ``1`` (or omitted), and ``top_p``,
      ``presence_penalty``, ``frequency_penalty``, ``logprobs``,
      ``top_logprobs``, ``logit_bias`` are not supported at all.
      Sending any of them with a non-default value 400s with
      ``Unsupported value: 'temperature' does not support 0.6 with
      this model. Only the default (1) value is supported.`` or
      ``Unsupported parameter: '<key>'``.

    Older OpenAI models (``gpt-4o*``, ``gpt-4.1*``, ``gpt-4-turbo*``)
    and every non-OpenAI compat provider (Gemini, Groq, OpenRouter,
    llama.cpp …) accept the legacy shape, so we leave them alone for
    cross-provider portability.
    """
    if not isinstance(model, str):
        return False
    name = model.strip().lower()
    if not name:
        return False
    # GPT-5 family (gpt-5, gpt-5-mini, gpt-5-nano, gpt-5-pro, …).
    if name.startswith("gpt-5"):
        return True
    # o-series reasoning models: o1, o1-mini, o1-preview, o3, o3-mini,
    # o4, o4-mini, … Match the ``o<digit>`` prefix so future siblings
    # auto-qualify.
    if len(name) >= 2 and name[0] == "o" and name[1].isdigit():
        return True
    return False


# Sampling knobs the Responses-API family (GPT-5 + o-series) does
# NOT support. Dropping them entirely is preferred over forcing them
# to "default" — omission lets the server pick its actual default
# and avoids tripping the strict 400 gate on borderline values.
_RESPONSES_API_UNSUPPORTED_OPTION_KEYS: frozenset[str] = frozenset({
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "logit_bias",
})


def _map_finish_reason(reason: object) -> str | None:
    """Translate OpenAI ``finish_reason`` to the Ollama-shaped vocabulary.

    The truncation gate downstream only looks at ``"length"``; mapping
    keeps the warning behaviour symmetric across providers. ``"stop"``
    passes through unchanged; everything else collapses to its lowercase
    string form for telemetry.
    """
    if reason is None:
        return None
    text = str(reason).strip().lower()
    if not text:
        return None
    if text == "length":
        return "length"
    if text == "stop":
        return "stop"
    return text


def _warn_if_truncated(
    usage: ChatUsage, *, model: str, surface: str, benign: bool = False,
) -> None:
    """Emit a single WARNING when ``done_reason == "length"``.

    Mirrors :func:`app.llm.ollama_client._warn_if_truncated` so log
    consumers grepping for ``"response truncated"`` catch both clients
    uniformly. Surfaces in :data:`_BENIGN_TRUNCATION_SURFACES` are
    suppressed; ``benign=True`` downgrades to DEBUG for the
    "thinking-trace tipped the cap" case.
    """
    if usage.done_reason != "length":
        return
    if surface in _BENIGN_TRUNCATION_SURFACES:
        return
    if benign:
        log.debug(
            "openai-compat response capped on thinking trace (answer "
            "looks complete): surface=%s model=%s completion_tokens=%d",
            surface, model, int(usage.completion_tokens),
        )
        return
    log.warning(
        "openai-compat response truncated: surface=%s model=%s "
        "completion_tokens=%d (hit max_tokens cap; raise chat_llm."
        "max_tokens if this is frequent)",
        surface, model, int(usage.completion_tokens),
    )


# Regex used to split an SSE event line on its first colon. The OpenAI
# streaming protocol uses ``data: {...}\n\n``; everything else (heartbeat
# ``:`` comments, ``id:`` / ``event:`` fields) is ignored.
_SSE_SPLIT_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s?(.*)$")


def _iter_sse_data_lines(
    response: requests.Response,
    *,
    stop_event: threading.Event | None = None,
) -> Generator[str, None, None]:
    """Yield each ``data:`` payload (without the prefix) from an SSE stream.

    Skips comments, ``event:``/``id:`` fields, and the terminator
    sentinel ``[DONE]``. Returns when the stream closes or the
    ``stop_event`` fires (the underlying socket is closed by the
    caller's ``with`` block in either case).

    Malformed lines (no colon, unknown field name) are silently
    dropped — providers occasionally emit junk and we'd rather keep
    streaming than raise mid-token.
    """
    for raw_line in response.iter_lines(decode_unicode=True):
        if stop_event is not None and stop_event.is_set():
            return
        if not raw_line:
            continue
        if raw_line.startswith(":"):  # SSE heartbeat comment
            continue
        match = _SSE_SPLIT_RE.match(raw_line)
        if match is None:
            continue
        field_name, value = match.group(1), match.group(2)
        if field_name != "data":
            continue
        value = value.strip()
        if value == "[DONE]":
            return
        if value:
            yield value


class OpenAICompatibleClient:
    """Chat client for OpenAI-shape ``/v1/chat/completions`` endpoints.

    Constructor accepts the same ``OllamaSettings`` instance as
    ``OllamaClient`` so the controller can build either client from the
    same source-of-truth knobs (``timeout``, ``temperature``). Provider-
    specific fields land via the explicit kwargs: ``base_url``,
    ``api_key``, ``model``, ``extra_headers``.
    """

    def __init__(
        self,
        settings: OllamaSettings,
        timeout_seconds: int | None = None,
        *,
        api_key: str | None = None,
        base_url: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
        keep_alive: str | None = None,
    ) -> None:
        if not (base_url or "").strip():
            raise ValueError("OpenAICompatibleClient requires a base_url")
        if not (model or "").strip():
            raise ValueError("OpenAICompatibleClient requires a model")
        self._settings = settings
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.timeout
        )
        self._base_url = base_url.strip().rstrip("/")
        self._default_model = model.strip()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if extra_headers:
            for key, value in extra_headers.items():
                key_s = str(key).strip()
                value_s = str(value).strip()
                if key_s and value_s:
                    headers[key_s] = value_s
        if api_key:
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        self._headers: dict[str, str] = headers
        self.last_usage: ChatUsage = ChatUsage()
        # ``keep_alive`` is Ollama-only. We accept the kwarg so the
        # controller can pass the same value to either client without
        # branching, but it never makes it onto the wire here.
        self._keep_alive_unused = keep_alive

    @property
    def base_url(self) -> str:
        return self._base_url

    def _request_headers(self) -> dict[str, str]:
        return dict(self._headers)

    def _announce_connection(self, model: str) -> None:
        if self._base_url in _announced_base_urls:
            return
        _announced_base_urls.add(self._base_url)
        log.info(
            "openai-compat connected: base_url=%s default_model=%s",
            self._base_url, model,
        )

    def _log_http_error(
        self,
        endpoint: str,
        response: "requests.Response",
        *,
        elapsed_ms: float,
    ) -> None:
        try:
            snippet = response.text or ""
        except Exception:
            snippet = ""
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        log.error(
            "openai-compat %s failed: status=%d reason=%s elapsed_ms=%.0f body=%s",
            endpoint, response.status_code, response.reason, elapsed_ms,
            snippet.replace("\n", " ") or "-",
        )

    # ── Payload helpers ─────────────────────────────────────────────

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        options: dict[str, object] | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        format_json: bool,
    ) -> dict[str, Any]:
        """Assemble the JSON body for ``/v1/chat/completions``.

        ``options`` follows the Ollama dict shape (``temperature``,
        ``num_predict``, ``top_p``, …) which the rest of the codebase
        already speaks. We translate the two we actually use here
        (``temperature``, ``num_predict``) onto the OpenAI param
        names (``temperature``, ``max_tokens``) so callers don't have
        to know which client they're talking to. Unknown keys pass
        through as-is — providers ignore params they don't recognise.
        """
        # First: normalize neutral tool-call traffic into strict OpenAI
        # shape (id + type + JSON-string arguments + tool_call_id).
        # Then: collapse system messages for Gemini's OpenAI-compat layer.
        normalized = _normalize_tool_messages_for_openai(messages)
        merged_messages = (
            _collapse_system_for_gemini(normalized)
            if _is_gemini_model(model)
            else normalized
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": merged_messages,
            "stream": stream,
        }
        if stream:
            # Some providers need to be asked nicely for usage stats
            # mid-stream; OpenAI added this param specifically for the
            # SSE case. Harmless on providers that ignore it.
            payload["stream_options"] = {"include_usage": True}
        responses_api = _is_responses_api_family(model)
        if responses_api:
            # GPT-5 family + o-series consume part of the
            # ``max_completion_tokens`` budget on hidden reasoning
            # tokens before any visible output. With the default
            # ``reasoning_effort="medium"`` and a tight budget (e.g.
            # ``chat_llm.max_tokens=512``) every token can go to
            # reasoning, leaving Aiko's visible reply empty.
            #
            # The right value is *surface-aware* — there are two
            # very different shapes of call hitting this client:
            #
            # * Tool-decision pass (``chat_with_tools`` with ``tools``
            #   set): the visible output is tiny — a function name
            #   and a small JSON args object, ~30-80 tokens. The
            #   bottleneck is *planning*, not budget. With
            #   ``minimal`` we observed gpt-5-mini defer ("I'll list
            #   the folders") instead of emitting a tool call.
            #   Bumping to ``low`` gives it just enough reasoning
            #   budget to commit to a tool selection — empirically
            #   tens of reasoning tokens, well within
            #   ``num_predict=256``.
            # * Narration / streaming reply (``chat_stream``, no
            #   ``tools``) — the visible output IS the user-facing
            #   message and the budget matters most. Keep
            #   ``minimal`` so prose isn't starved.
            # * Plain ``chat`` / ``chat_json`` calls without tools —
            #   no planning needed; keep ``minimal``.
            #
            # The split keys cleanly on ``tools is not None``: tools
            # are passed only on decision passes, never on the
            # streaming narration pass. Users who want deeper
            # reasoning can still raise ``chat_llm.max_tokens`` and
            # the family will spend it.
            payload["reasoning_effort"] = "low" if tools else "minimal"
        if options:
            # Pull out the keys we know how to translate, pass the rest
            # through. The Ollama vocabulary leaks here on purpose — the
            # codebase has hundreds of call sites built around it. We
            # explicitly DROP keys that are Ollama-only (OpenAI strict-
            # rejects unknown params with HTTP 400 — e.g. ``num_ctx``).
            # Keys both engines understand (``top_p``, ``seed``,
            # ``frequency_penalty``, ``presence_penalty``, ``stop``, …)
            # fall through untouched, so new OpenAI params get picked
            # up automatically without churn here. On the
            # Responses-API model family (GPT-5 + o-series), the
            # sampling knobs are locked to defaults so we drop them
            # entirely — see ``_is_responses_api_family``.
            opts = dict(options)
            temp = opts.pop("temperature", None)
            if temp is not None and not responses_api:
                try:
                    payload["temperature"] = float(temp)
                except (TypeError, ValueError):
                    pass
            num_predict = opts.pop("num_predict", None)
            if num_predict is not None:
                try:
                    # GPT-5 family + o-series require
                    # ``max_completion_tokens``; older OpenAI models
                    # and non-OpenAI compat providers (Gemini, Groq,
                    # OpenRouter) still want ``max_tokens``.
                    token_key = (
                        "max_completion_tokens"
                        if responses_api
                        else "max_tokens"
                    )
                    payload[token_key] = int(num_predict)
                except (TypeError, ValueError):
                    pass
            for key in _OLLAMA_ONLY_OPTION_KEYS:
                opts.pop(key, None)
            if responses_api:
                for key in _RESPONSES_API_UNSUPPORTED_OPTION_KEYS:
                    opts.pop(key, None)
            for key, value in opts.items():
                if key not in payload:
                    payload[key] = value
        elif not responses_api:
            payload["temperature"] = float(self._settings.temperature)
        if _is_gemini_model(model):
            # Gemini clamps temperature into [0, 2]; values outside
            # the band silently round in some SDKs and 400 in others.
            # Clamping here makes the behaviour predictable.
            temp = payload.get("temperature")
            if isinstance(temp, (int, float)):
                payload["temperature"] = max(0.0, min(2.0, float(temp)))
        if tools:
            payload["tools"] = tools
        if format_json:
            # response_format is OpenAI-only; Gemini's OpenAI-compat
            # layer accepts it but enforces it weakly. Providers that
            # don't understand it ignore the field.
            payload["response_format"] = {"type": "json_object"}
        return payload

    # ── Public API ──────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        model: str | None = None,
        think: bool = False,
        *,
        surface: str = "chat",
    ) -> str:
        return self.chat_with_tools(
            messages, options=options, model=model, think=think,
            surface=surface,
        ).content

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, object] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        think: bool = False,
        keep_alive: str | None = None,  # accepted for protocol parity
        surface: str = "chat_with_tools",
    ) -> ChatResponse:
        del keep_alive  # Ollama-only knob; see __init__ docstring
        use_model = (model or "").strip() or self._default_model
        payload = self._build_payload(
            messages=messages,
            model=use_model,
            options=options,
            tools=tools,
            stream=False,
            format_json=False,
        )
        t0 = time.monotonic()
        try:
            response = requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                timeout=self._timeout_seconds,
                headers=self._request_headers(),
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "openai-compat chat transport error: model=%s msgs=%d "
                "tools=%d elapsed_ms=%.0f exc=%r",
                use_model, len(messages), len(tools or []), elapsed_ms, exc,
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if not response.ok:
            self._log_http_error("chat", response, elapsed_ms=elapsed_ms)
            try:
                err_body = response.text
                if err_body and len(err_body) > 500:
                    err_body = err_body[:500] + "..."
            except Exception:
                err_body = ""
            msg = f"{response.status_code} {response.reason}"
            if err_body:
                msg += f" — {err_body}"
            raise requests.HTTPError(msg, response=response)
        body = response.json()
        content, tool_calls, finish_reason = self._extract_choice(body)
        had_thinking = False
        if not think:
            content, had_thinking = _strip_thinking_blocks_with_signal(
                content,
            )
        usage_dict = (
            body.get("usage") if isinstance(body, dict) else None
        )
        self.last_usage = self._build_usage(
            usage_dict=usage_dict,
            finish_reason=finish_reason,
            total_ms=elapsed_ms,
        )
        _warn_if_truncated(
            self.last_usage,
            model=use_model,
            surface=surface,
            benign=had_thinking and _content_looks_complete(content),
        )
        self._announce_connection(use_model)
        log.debug(
            "openai-compat chat: model=%s msgs=%d tools=%d stream=0 "
            "elapsed_ms=%.0f prompt_tokens=%d completion_tokens=%d "
            "tool_calls=%d",
            use_model, len(messages), len(tools or []), elapsed_ms,
            self.last_usage.prompt_tokens,
            self.last_usage.completion_tokens,
            len(tool_calls),
        )
        return ChatResponse(content=content, tool_calls=tool_calls)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        *,
        model: str | None = None,
        keep_alive: str | None = None,  # accepted for protocol parity
        stop_event: threading.Event | None = None,
        format_json: bool = False,
        think: bool = False,
        surface: str = "chat_stream",
    ) -> Generator[str, None, None]:
        del keep_alive
        del think  # OpenAI-compat doesn't expose a thinking-trace toggle
        use_model = (model or "").strip() or self._default_model
        payload = self._build_payload(
            messages=messages,
            model=use_model,
            options=options,
            tools=None,
            stream=True,
            format_json=format_json,
        )
        usage = ChatUsage()
        t0 = time.monotonic()
        first_token_ms: float | None = None
        try:
            with requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                stream=True,
                timeout=self._timeout_seconds,
                headers=self._request_headers(),
            ) as response:
                if not response.ok:
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    self._log_http_error(
                        "chat_stream", response, elapsed_ms=elapsed_ms,
                    )
                response.raise_for_status()
                finish_reason: str | None = None
                for data in _iter_sse_data_lines(
                    response, stop_event=stop_event,
                ):
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    # Usage stats arrive in a dedicated terminal chunk
                    # (when stream_options.include_usage was honoured).
                    usage_payload = chunk.get("usage")
                    if isinstance(usage_payload, dict):
                        usage.prompt_tokens = int(
                            usage_payload.get("prompt_tokens", 0) or 0,
                        )
                        usage.completion_tokens = int(
                            usage_payload.get("completion_tokens", 0) or 0,
                        )
                        # OpenAI prompt-caching: see _build_usage above
                        # for the equivalent non-streaming path. Field
                        # is absent on most non-OpenAI providers, so
                        # the default ``0`` is the right outcome there.
                        details = usage_payload.get("prompt_tokens_details")
                        if isinstance(details, dict):
                            usage.cached_tokens = int(
                                details.get("cached_tokens", 0) or 0,
                            )
                    choices = chunk.get("choices") or []
                    if not isinstance(choices, list) or not choices:
                        continue
                    first_choice = choices[0]
                    if not isinstance(first_choice, dict):
                        continue
                    delta = first_choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    token = delta.get("content")
                    if isinstance(token, str) and token:
                        if first_token_ms is None:
                            first_token_ms = (
                                time.monotonic() - t0
                            ) * 1000.0
                        yield token
                    fr = first_choice.get("finish_reason")
                    if fr is not None:
                        finish_reason = str(fr)
                usage.done_reason = _map_finish_reason(finish_reason)
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "openai-compat chat_stream transport error: model=%s "
                "elapsed_ms=%.0f exc=%r",
                use_model, elapsed_ms, exc,
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        usage.total_duration_ms = elapsed_ms
        self.last_usage = usage
        _warn_if_truncated(usage, model=use_model, surface=surface)
        self._announce_connection(use_model)
        log.debug(
            "openai-compat chat_stream done: model=%s msgs=%d "
            "elapsed_ms=%.0f first_token_ms=%s prompt_tokens=%d "
            "completion_tokens=%d stopped=%s",
            use_model, len(messages), elapsed_ms,
            f"{first_token_ms:.0f}" if first_token_ms is not None else "-",
            usage.prompt_tokens, usage.completion_tokens,
            "1" if (stop_event is not None and stop_event.is_set()) else "0",
        )

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
        del keep_alive
        # ``temperature=0.0`` is the per-worker convention from the
        # Ollama client; replicate it so the two paths produce
        # equivalent output for the JSON-shaped workers.
        merged_options: dict[str, object] = {"temperature": 0.0}
        if options:
            merged_options.update(options)
        use_model = (model or "").strip() or self._default_model
        payload = self._build_payload(
            messages=messages,
            model=use_model,
            options=merged_options,
            tools=None,
            stream=False,
            format_json=format_json,
        )
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._timeout_seconds
        )
        t0 = time.monotonic()
        try:
            response = requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                timeout=effective_timeout,
                headers=self._request_headers(),
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "openai-compat chat_json transport error: model=%s "
                "elapsed_ms=%.0f exc=%r",
                use_model, elapsed_ms, exc,
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if not response.ok:
            self._log_http_error(
                "chat_json", response, elapsed_ms=elapsed_ms,
            )
        response.raise_for_status()
        body = response.json()
        content, _tool_calls, finish_reason = self._extract_choice(body)
        had_thinking = False
        if not think:
            content, had_thinking = _strip_thinking_blocks_with_signal(
                content,
            )
        usage = self._build_usage(
            usage_dict=(
                body.get("usage") if isinstance(body, dict) else None
            ),
            finish_reason=finish_reason,
            total_ms=elapsed_ms,
        )
        _warn_if_truncated(
            usage,
            model=use_model,
            surface=surface,
            benign=had_thinking and _content_looks_complete(content),
        )
        self._announce_connection(use_model)
        log.debug(
            "openai-compat chat_json: model=%s msgs=%d elapsed_ms=%.0f "
            "prompt_tokens=%d completion_tokens=%d format_json=%s",
            use_model, len(messages), elapsed_ms,
            usage.prompt_tokens, usage.completion_tokens,
            "1" if format_json else "0",
        )
        return content, usage

    def list_models(self) -> list[str]:
        """Return model ids from ``/v1/models``.

        Returns ``[]`` on any failure — the UI dropdown falls back to
        free-text in that case.
        """
        try:
            response = requests.get(
                f"{self._base_url}/models",
                timeout=min(10.0, float(self._timeout_seconds)),
                headers=self._request_headers(),
            )
            response.raise_for_status()
            body = response.json()
        except Exception:
            return []
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return []
        names: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Gemini reports ``id="models/gemini-2.5-flash-lite"`` and
            # also accepts the same value in subsequent requests; we
            # keep the prefix intact so the round-trip is identity.
            name = str(item.get("id", "")).strip()
            if name:
                names.append(name)
        return names

    def get_context_length(self, model: str) -> int | None:
        """Return a conservative context-window cap for known cloud models.

        OpenAI-compat endpoints (OpenAI, Gemini, Groq, OpenRouter,
        Anthropic via OpenRouter, ...) don't expose context-window
        metadata over ``/v1/models``, so we maintain a static table
        of known model-id prefixes -> conservative caps. Returns
        ``None`` for ids we don't recognise; the controller then
        falls back to ``chat_llm.context_window`` or the hardcoded
        8192 last-resort default in ``_resolve_context_window``.

        Caps are intentionally **conservative**, not the model's
        true maximum: gpt-4.1-mini's 1 M and gemini-2.5-pro's 2 M
        are capped at 128 k here because (a) real conversational
        use rarely exceeds 50 k, (b) larger budgets make prompt
        compaction lazy, and (c) for OpenAI's long-context tier
        pricing, staying under 128 k keeps requests in the cheaper
        short-context billing column.

        First match wins. The ``models/`` prefix Gemini sometimes
        emits is stripped before matching so both ``gemini-2.5-pro``
        and ``models/gemini-2.5-pro`` resolve identically.
        """
        return _lookup_context_window(model)

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_choice(
        body: object,
    ) -> tuple[str, list[ChatToolCall], str | None]:
        """Pull the first choice's content, tool calls, and finish reason.

        OpenAI-shape: ``body.choices[0].message.{content,tool_calls}``
        plus ``body.choices[0].finish_reason``. Defends against partial
        or malformed bodies (some Gemini errors come back 200 with a
        missing ``choices`` key) by returning empty defaults.
        """
        if not isinstance(body, dict):
            return "", [], None
        choices = body.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return "", [], None
        first = choices[0]
        if not isinstance(first, dict):
            return "", [], None
        message = first.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        raw_calls = message.get("tool_calls") or []
        tool_calls = _parse_openai_tool_calls(raw_calls)
        finish_reason = first.get("finish_reason")
        return content, tool_calls, (
            str(finish_reason) if finish_reason is not None else None
        )

    @staticmethod
    def _build_usage(
        *,
        usage_dict: object,
        finish_reason: str | None,
        total_ms: float,
    ) -> ChatUsage:
        usage = ChatUsage(total_duration_ms=float(total_ms))
        if isinstance(usage_dict, dict):
            usage.prompt_tokens = int(usage_dict.get("prompt_tokens", 0) or 0)
            usage.completion_tokens = int(
                usage_dict.get("completion_tokens", 0) or 0,
            )
            # OpenAI prompt-caching: ``prompt_tokens_details.cached_tokens``
            # reports how many input tokens hit the server-side prefix
            # cache (billed at ~10% of the uncached input rate). Field
            # is absent on most non-OpenAI providers — defaults to 0
            # there, which is the right answer. See
            # ``docs/prompt-caching.md``.
            details = usage_dict.get("prompt_tokens_details")
            if isinstance(details, dict):
                usage.cached_tokens = int(
                    details.get("cached_tokens", 0) or 0,
                )
        usage.done_reason = _map_finish_reason(finish_reason)
        return usage


def _parse_openai_tool_calls(raw: object) -> list[ChatToolCall]:
    """Parse OpenAI-shape ``tool_calls[]`` into our neutral dataclass.

    Both OpenAI and Gemini emit the same shape:
    ``{"id": "...", "type": "function", "function": {"name": "...",
    "arguments": "json-encoded-string"}}``. We tolerate ``arguments``
    being a dict (some providers do this) and silently drop entries
    missing a name.
    """
    if not isinstance(raw, list):
        return []
    parsed: list[ChatToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = str(function.get("name", "") or "").strip()
        if not name:
            continue
        call_id = str(item.get("id", "") or "").strip()
        raw_args = function.get("arguments", {})
        args: dict[str, Any]
        if isinstance(raw_args, dict):
            args = dict(raw_args)
        elif isinstance(raw_args, str):
            try:
                loaded = json.loads(raw_args)
            except Exception:
                loaded = {}
            args = dict(loaded) if isinstance(loaded, dict) else {}
        else:
            args = {}
        parsed.append(ChatToolCall(name=name, arguments=args, call_id=call_id))
    return parsed


__all__ = ["OpenAICompatibleClient"]
