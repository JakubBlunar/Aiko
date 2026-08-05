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
  are forwarded from ``LlmProvider.extra_headers``.
"""

from __future__ import annotations

import json
import logging
import random
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

# Private keys used by the neutral multi-round tool history. The complete
# Responses output is authoritative; the reasoning-only key remains as a
# compatibility fallback for histories/tests created by the first fix.
# Both are illegal on /v1/chat/completions and are stripped there.
_RESPONSES_OUTPUT_KEY: str = "_responses_output"
_RESPONSES_REASONING_KEY: str = "_responses_reasoning"

# Transient HTTP statuses worth a retry on the non-streaming POST paths.
# 429 = rate limit; 5xx = server-side hiccups. OpenAI's own 500 body says
# "You can retry your request" — the Responses tool pass hit exactly this
# and, unretried, silently dropped an already-executed tool result.
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
# Bounded so a genuinely-down provider still fails fast (attempts = 1 + this).
_MAX_TRANSIENT_RETRIES: int = 2
# Exponential backoff with full jitter, capped so a retry can't blow the
# turn's latency budget.
_RETRY_BASE_DELAY_S: float = 0.5
_RETRY_MAX_DELAY_S: float = 6.0
# Only a *fast* failure is worth retrying. A 5xx that arrives in tens of ms
# is a load-balancer / cold-node blip; a 5xx that arrives after the model
# already reasoned for 20+ s is expensive server work that failed, and a
# retry would just pay that cost again (and again). Above this wall-time we
# fail through immediately rather than multiply the latency — which is what
# keeps a slow tool-pass failure at ~one attempt instead of three.
_RETRY_MAX_ATTEMPT_MS: float = 6000.0


def _strip_gemini_prefix(model: str) -> str:
    """Normalise a model id to lowercase, minus Gemini's ``models/``.

    Gemini's compat layer reports ids both ways (``gemini-3.6-flash``
    from the docs, ``models/gemini-3.6-flash`` from ``/models``), and
    every prefix test below expects the bare form.
    """
    name = (model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    return name


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
    # ── Gemini 3.x family. 1 M native, capped at 128 k. Covers
    # gemini-3-flash, gemini-3.1-pro, gemini-3.1-flash-lite,
    # gemini-3.5-flash-lite, gemini-3.6-flash, … ───────────────────
    ("gemini-3", 131_072),
    # ── Gemini 2.5 family. 1-2 M native, capped at 128 k. ───────────
    ("gemini-2.5-pro", 131_072),
    ("gemini-2.5-flash-lite", 131_072),
    ("gemini-2.5-flash", 131_072),
    ("gemini-2.5", 131_072),
    # Catch-all for Gemini generations that don't exist yet. Every
    # Gemini since 1.5 has shipped with at least a 1 M window, so the
    # 128 k cap is a far better guess for an unknown tag than the
    # 8192 last-resort default the controller would otherwise apply.
    # Must stay below the version-specific rules above.
    ("gemini-", 131_072),
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
    # ── xAI Grok. grok-4.5 is ~256 k-500 k native; capped at 128 k
    # here for the same reasons as the others (real chat rarely
    # exceeds ~50 k, keeps compaction honest). ``x-ai/`` is the
    # OpenRouter slug prefix; ``grok`` alone catches bare ids.
    ("grok-4", 131_072),
    ("grok-3", 131_072),
    ("grok", 131_072),
    ("x-ai/grok", 131_072),
)


def _lookup_context_window(model: str) -> int | None:
    """Match a model id against ``_CONTEXT_WINDOW_TABLE``.

    Strips the ``models/`` prefix Gemini sometimes emits before
    matching, lowercases for case-insensitive matching, and returns
    ``None`` when no prefix matches (the controller falls back to
    the explicit override or the hardcoded 8192 last-resort default).
    """
    name = _strip_gemini_prefix(model)
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
    return _strip_gemini_prefix(model).startswith("gemini-")


# Gemini generations that predate thinking. ``reasoning_effort`` has no
# meaning for them and the compat layer rejects it, so they keep the
# plain chat-completions payload.
_GEMINI_NO_THINKING_PREFIXES: tuple[str, ...] = (
    "gemini-1.0",
    "gemini-1.5",
    "gemini-2.0",
)


def _gemini_supports_thinking(model: str) -> bool:
    """True when ``model`` accepts a ``reasoning_effort``.

    Deny-list rather than allow-list: every Gemini from 2.5 onwards is a
    thinking model, so an unrecognised future tag is far more likely to
    want the parameter than not. Guessing wrong in this direction fails
    loudly on the first call (a ``400`` whose body we log) and is fixed
    by setting the route's effort to ``omit``; guessing wrong the other
    way silently bills every turn at the model's default thinking level,
    which is the exact cost we're here to avoid.
    """
    name = _strip_gemini_prefix(model)
    return not name.startswith(_GEMINI_NO_THINKING_PREFIXES)


def _gemini_thinking_can_be_disabled(model: str) -> bool:
    """True when ``model`` accepts ``reasoning_effort="none"``.

    Google's compat layer maps ``reasoning_effort`` onto Gemini's
    ``thinking_level`` / ``thinking_budget``, but switching thinking
    **off** is only supported on the 2.5 line, and not on 2.5 Pro.
    Every Gemini 3 model reasons unconditionally and answers ``none``
    with ``400 Invalid reasoning_effort``.
    """
    name = _strip_gemini_prefix(model)
    return name.startswith("gemini-2.5") and not name.startswith("gemini-2.5-pro")


def _gemini_min_effort(model: str) -> str:
    """Cheapest reasoning effort ``model`` is guaranteed to accept.

    Used for the tool-decision pass, which only has to emit a function
    name and a few JSON args — a reasoning trace there is pure latency
    and pure output-token spend on every single turn. ``minimal`` is
    deliberately not used even though the 3.x Flash tags accept it:
    3.1 Pro doesn't, and Google's own mapping table collapses it to
    ``low`` there anyway, so ``low`` is the universal floor.
    """
    return "none" if _gemini_thinking_can_be_disabled(model) else "low"


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
            # Responses-API opaque stashes are illegal on chat/completions.
            new_msg.pop(_RESPONSES_OUTPUT_KEY, None)
            new_msg.pop(_RESPONSES_REASONING_KEY, None)
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


# Versioned GPT-5.x line (gpt-5.1, gpt-5.4-mini, …) that 400s on
# ``Function tools with reasoning_effort are not supported … in
# /v1/chat/completions. Please use /v1/responses instead.`` The
# *original* GPT-5 models (gpt-5, gpt-5-mini, gpt-5-nano, gpt-5-pro)
# have NO dotted minor version and DO accept tools + reasoning_effort
# together, so we only match the decimal-versioned siblings. ``openai/``
# (OpenRouter) prefixes are tolerated.
_GPT5_DOTTED_VERSION_RE = re.compile(r"(?:^|/)gpt-5\.\d", re.IGNORECASE)


def _tools_with_reasoning_unsupported(model: str) -> bool:
    """True when sending ``tools`` + ``reasoning_effort`` together on
    ``/v1/chat/completions`` is rejected for ``model``.

    Only the dotted GPT-5.x line (gpt-5.1+, e.g. gpt-5.4-mini) moved
    reasoning behind the Responses API; the tool-decision pass for
    these models must therefore omit ``reasoning_effort`` (it provides
    no behavioural benefit on that pass anyway — see
    ``turn_runner._maybe_run_tool_pass``). This is the fallback guard
    for when such a model is *forced* onto chat-completions; the normal
    path routes it to ``/v1/responses`` (see :func:`_use_responses_api`)."""
    if not isinstance(model, str):
        return False
    return bool(_GPT5_DOTTED_VERSION_RE.search(model.strip()))


# On the Responses API, ``max_output_tokens`` caps reasoning tokens
# AND visible output tokens *combined*. If the budget is too small the
# reasoning trace can consume all of it, leaving status=incomplete with
# no message / no tool call. To preserve the caller's intended *visible*
# budget (``num_predict``) we add a per-effort reasoning reserve on top
# of it. Values are conservative ceilings — the model only spends what
# it actually needs, so a generous reserve costs nothing on easy turns
# but prevents silent truncation on hard ones.
_RESPONSES_REASONING_RESERVE: dict[str, int] = {
    "none": 0,
    "minimal": 256,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}
_RESPONSES_DEFAULT_RESERVE = 1024


def _use_responses_api(model: str) -> bool:
    """True when ``model`` should be driven via ``POST /v1/responses``.

    The dotted GPT-5.x line (gpt-5.1, gpt-5.4-mini, …) moved reasoning
    behind the Responses API: on ``/v1/chat/completions`` it 400s when
    ``tools`` and ``reasoning_effort`` are sent together. Routing the
    whole model through ``/v1/responses`` lets the tool-decision pass
    AND the reply pass both honour the configured reasoning effort.

    The *original* GPT-5 models (gpt-5, gpt-5-mini, gpt-5-nano,
    gpt-5-pro) and the o-series keep using ``/v1/chat/completions``
    where their tool path is already tuned — only the decimal-versioned
    siblings qualify. ``openai/`` (OpenRouter) prefixes are tolerated by
    the shared :data:`_GPT5_DOTTED_VERSION_RE`."""
    if not isinstance(model, str):
        return False
    return bool(_GPT5_DOTTED_VERSION_RE.search(model.strip()))


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
        "completion_tokens=%d (hit max_tokens cap; raise the route's"
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
        reasoning_effort: str | None = None,
        api_style: str | None = None,
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
        # Reasoning-effort hint for Responses-API-family models. Empty /
        # None = "auto" -> the built-in ``minimal`` default. A non-empty
        # value is sent verbatim (providers disagree on the vocabulary).
        self._reasoning_effort = (reasoning_effort or "").strip().lower()
        # Which OpenAI-compatible surface to speak. ``"auto"`` keeps the
        # per-model-name routing (OpenAI GPT-5.x / o-series -> Responses
        # API); ``"responses"`` / ``"chat_completions"`` force one surface
        # (xAI Grok needs ``"responses"``). See ``_should_use_responses``.
        style = (api_style or "auto").strip().lower()
        self._api_style = (
            style if style in ("auto", "responses", "chat_completions") else "auto"
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def _should_use_responses(self, model: str) -> bool:
        """Decide whether ``model`` is driven via ``POST /v1/responses``.

        ``api_style`` wins when it forces a surface; ``"auto"`` falls back
        to the historical per-model-name detection (:func:`_use_responses_api`,
        which only matches OpenAI's dotted GPT-5.x line). This is the seam
        that lets a provider like xAI Grok — whose reasoning + prompt
        caching live on the Responses surface — opt in without a
        model-name hack in the shared regex.
        """
        if self._api_style == "responses":
            return True
        if self._api_style == "chat_completions":
            return False
        return _use_responses_api(model)

    def tool_pass_round_limit(self, model: str) -> int:
        """Maximum pre-stream tool-selection rounds for ``model``.

        Decimal-versioned GPT-5 reasoning models use one tool batch. Their
        first call works and can emit multiple parallel functions, but a
        second forced ``tool_choice="required"`` request after the outputs
        intermittently stalls for 20–45 seconds and returns an OpenAI 500.
        It is also redundant in Aiko's two-pass architecture: the normal
        streaming reply runs immediately afterward with the successful tool
        outputs in context.

        Other providers retain the historical two-round allowance so Grok,
        Gemini, Ollama-compatible routes, and older OpenAI models keep
        sequential tool chaining.
        """
        return 1 if _use_responses_api(model) else 2

    def tool_pass_tool_choice(
        self,
        model: str,
        requested: "str | dict[str, Any]",
    ) -> "str | dict[str, Any]":
        """Relax the legacy forced-pick policy for modern GPT models.

        ``required`` plus a synthetic ``respond_directly`` escape was added
        for older chatty models that narrated tool intent instead of
        emitting a call. Decimal-versioned GPT-5 models call tools reliably
        on ``auto``; forcing them encourages unnecessary calls and makes
        every gated pass choose *something*. Other providers keep the
        caller's historical policy.
        """
        return "auto" if _use_responses_api(model) else requested

    def set_reasoning_effort(self, value: str | None) -> None:
        """Update the Responses-API reasoning-effort hint at runtime.

        Called when a provider / route edit changes the effort without
        rebuilding the client. Empty / None resets to "auto" (``minimal``
        default)."""
        self._reasoning_effort = (value or "").strip().lower()

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
            # the route's ``max_tokens=512``) every token can go to
            # reasoning, leaving Aiko's visible reply empty — so we
            # default to ``minimal``.
            #
            # The value is configurable per provider / route because
            # the vocabulary is NOT universal: gpt-5-mini accepts
            # ``minimal`` but gpt-5.4-mini rejects it (HTTP 400) and
            # wants one of ``none`` / ``low`` / ``medium`` / ``high`` /
            # ``xhigh``. A per-call override (``options["reasoning_effort"]``)
            # wins over the client default; an explicit ``"omit"`` /
            # ``"default"`` skips the param entirely so the provider
            # applies its own default.
            effort = self._reasoning_effort
            if options and isinstance(options, dict):
                override = options.get("reasoning_effort")
                if override is not None:
                    effort = str(override).strip().lower()
            if not effort:
                # "" / unset = auto -> the safe ``minimal`` default that
                # keeps the visible-token budget from being eaten.
                effort = "minimal"
            # The dotted GPT-5.x line (gpt-5.4-mini, …) 400s when tools
            # AND reasoning_effort are sent together on
            # /v1/chat/completions ("use /v1/responses instead"). The
            # tool-decision pass doesn't benefit from reasoning anyway,
            # so drop the param when tools are present for those models.
            # Older GPT-5 / o-series keep it (avoids the ~8s latency of
            # their default ``medium`` effort on the tool pass).
            if tools and _tools_with_reasoning_unsupported(model):
                effort = "omit"
            if effort != "omit":
                payload["reasoning_effort"] = effort
        elif _is_gemini_model(model) and _gemini_supports_thinking(model):
            # Gemini reasons by default too, and its compat layer takes
            # the same ``reasoning_effort`` key (mapped onto
            # ``thinking_level`` / ``thinking_budget`` server-side). The
            # defaults are expensive: 3.6 Flash thinks at ``medium`` and
            # 3.1 Pro at ``high`` unless told otherwise, and Google bills
            # thinking tokens at the *output* rate. Leaving the param off
            # therefore costs both latency and money on every turn, so we
            # send an explicit effort exactly like the OpenAI branch —
            # only the vocabulary differs.
            effort = self._resolve_gemini_effort(options, model=model)
            if tools and effort != "omit":
                # Tool-decision pass: a function name plus a few JSON
                # args. Same reasoning as the Responses branch above.
                effort = _gemini_min_effort(model)
            if effort != "omit":
                payload["reasoning_effort"] = effort
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
            # Handled explicitly above for the Responses-API family and
            # for Gemini; drop it here so it never leaks onto a provider
            # that doesn't understand it (or double-writes the key we
            # just resolved) via the generic pass-through below.
            opts.pop("reasoning_effort", None)
            # ``prompt_cache_key`` is a Responses-surface caching hint
            # (see ``_build_responses_payload``). On /v1/chat/completions
            # it's provider-specific (xAI uses an ``x-grok-conv-id``
            # header instead), and a stray unknown body param 400s on
            # strict providers — so drop it from the chat-completions body.
            opts.pop("prompt_cache_key", None)
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

    # ── /v1/responses request builder + transport ───────────────────

    def _resolve_reasoning_effort(
        self, options: dict[str, object] | None,
    ) -> str:
        """Resolve the effective reasoning effort for a Responses call.

        Precedence: per-call ``options["reasoning_effort"]`` overrides
        the instance default (``self._reasoning_effort``, itself fed
        from the route-else-provider setting). Empty resolves to
        ``"minimal"``. ``"omit"`` is a sentinel that suppresses the
        ``reasoning`` block entirely.
        """
        effort = self._reasoning_effort
        if options and isinstance(options, dict):
            override = options.get("reasoning_effort")
            if override is not None:
                effort = str(override).strip().lower()
        if effort:
            return effort
        # Unset. On the OpenAI ``auto`` path the safe default is
        # ``minimal`` (keeps a tight visible-token budget from being eaten
        # by reasoning). But a provider *forced* onto the Responses surface
        # (api_style="responses", e.g. xAI Grok) may reject ``minimal``
        # entirely (Grok wants low/medium/high) — so there we ``omit`` the
        # reasoning block and let the provider apply its own default rather
        # than 400 on an unsupported value.
        if self._api_style == "responses":
            return "omit"
        return "minimal"

    def _resolve_gemini_effort(
        self, options: dict[str, object] | None, *, model: str,
    ) -> str:
        """Resolve ``reasoning_effort`` for a Gemini chat-completions call.

        Same precedence as :meth:`_resolve_reasoning_effort` (per-call
        override beats the instance default), with two Gemini-specific
        rules:

        * **Unset resolves to ``low``**, not ``minimal``. ``minimal`` is
          accepted by the 3.x Flash tags but rejected by 3.1 Pro, and
          leaving the key off entirely hands the turn to the model's own
          default — ``medium`` on 3.6 Flash, ``high`` on 3.1 Pro — which
          is the latency and output-token bill we're trying to avoid.
        * **``none`` is clamped on models that can't disable thinking.**
          Only the 2.5 line (minus Pro) accepts it; Gemini 3 answers
          ``400 Invalid reasoning_effort``. Clamping rather than passing
          it through means a route configured for one Gemini generation
          keeps working when it's pointed at another.
        """
        effort = self._reasoning_effort
        if options and isinstance(options, dict):
            override = options.get("reasoning_effort")
            if override is not None:
                effort = str(override).strip().lower()
        if not effort:
            return "low"
        if effort in ("omit", "default"):
            return "omit"
        if effort == "none" and not _gemini_thinking_can_be_disabled(model):
            log.debug(
                "gemini reasoning_effort=none unsupported on %s; using low",
                model,
            )
            return "low"
        return effort

    def _build_responses_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        options: dict[str, object] | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        format_json: bool,
        tool_choice: "str | dict[str, Any] | None",
    ) -> dict[str, Any]:
        """Assemble a ``POST /v1/responses`` body.

        Mirrors :meth:`_build_payload` but speaks the Responses shape:
        ``input`` (not ``messages``), ``reasoning.effort`` (not a flat
        ``reasoning_effort``), ``max_output_tokens`` (not
        ``max_completion_tokens``), flat function tools, and
        ``text.format`` for JSON mode. Temperature is intentionally
        omitted — the dotted GPT-5.x reasoning family locks it to the
        default and 400s on any other value.
        """
        payload: dict[str, Any] = {
            "model": model,
            "input": _messages_to_responses_input(messages),
            "stream": stream,
        }
        effort = self._resolve_reasoning_effort(options)
        # Tool-decision pass optimisation. When ``tools`` are present this
        # is the forced tool-pick pass (the reply pass streams WITHOUT
        # tools), which gains nothing from a reasoning trace — it just
        # emits a function name + tiny JSON args. On a reasoning-forced
        # provider (``api_style="responses"``, e.g. xAI Grok, whose
        # *default* effort is otherwise "high") that trace is pure latency
        # paid on every single turn. Grok accepts ``"none"`` to switch
        # reasoning off, so we do that here; the streaming reply pass still
        # honours the configured effort. Left untouched on the OpenAI
        # ``auto`` path (its reasoning vocabulary differs per model and
        # this pass is already cheap there).
        if tools and self._api_style == "responses":
            effort = "none"
        if effort != "omit":
            payload["reasoning"] = {"effort": effort}
        if options and isinstance(options, dict):
            num_predict = options.get("num_predict")
            if num_predict is not None:
                try:
                    visible = int(num_predict)
                except (TypeError, ValueError):
                    visible = None
                if visible is not None:
                    # Reserve headroom for the reasoning trace so it
                    # can't starve the visible answer / tool call.
                    reserve = (
                        0
                        if effort == "omit"
                        else _RESPONSES_REASONING_RESERVE.get(
                            effort, _RESPONSES_DEFAULT_RESERVE,
                        )
                    )
                    payload["max_output_tokens"] = visible + reserve
        if tools:
            payload["tools"] = _tools_to_responses(tools)
            if tool_choice is not None:
                payload["tool_choice"] = _tool_choice_to_responses(tool_choice)
        if format_json:
            payload["text"] = {"format": {"type": "json_object"}}
        # Prompt caching: a stable per-conversation key routes a
        # conversation's requests to the same server so the shared prefix
        # actually hits the cache (OpenAI + xAI both honour it on the
        # Responses surface). Neutral ``options`` key set by the caller
        # (the turn's session id); absent -> no key, provider falls back
        # to best-effort prefix caching.
        if options and isinstance(options, dict):
            cache_key = options.get("prompt_cache_key")
            if cache_key:
                payload["prompt_cache_key"] = str(cache_key)
        return payload

    @staticmethod
    def _retry_delay(attempt: int, response: "requests.Response | None") -> float:
        """Backoff before retry ``attempt`` (0-based), honouring ``Retry-After``.

        Full-jitter exponential backoff (``base * 2**attempt``, randomised
        into ``[0, window]``) capped at :data:`_RETRY_MAX_DELAY_S`. A
        provider-supplied ``Retry-After`` header (seconds) wins when present
        and larger, since the server knows its own cooldown.
        """
        window = min(_RETRY_BASE_DELAY_S * (2 ** attempt), _RETRY_MAX_DELAY_S)
        delay = random.uniform(0.0, window)
        if response is not None:
            hdr = response.headers.get("Retry-After")
            if hdr:
                try:
                    delay = max(delay, min(float(hdr), _RETRY_MAX_DELAY_S))
                except (TypeError, ValueError):
                    pass
        return delay

    def _post_json_with_retry(
        self,
        *,
        path: str,
        payload: dict[str, Any],
        timeout: float,
        surface: str,
        kind: str,
    ) -> tuple["requests.Response", float]:
        """POST JSON with bounded retry on transient failures.

        Retries transport errors (``requests.RequestException``) and the
        transient HTTP statuses in :data:`_RETRYABLE_STATUS` (429 / 5xx) up
        to :data:`_MAX_TRANSIENT_RETRIES` times with jittered backoff. Non-
        retryable responses (2xx and hard 4xx alike) and the final failure
        are returned/raised for the caller to handle exactly as before, so
        logging and error surfacing are unchanged. Returns the response plus
        the elapsed time of the *final* attempt (so usage timing excludes
        backoff sleeps). Only safe for the non-streaming paths — a stream
        can't be replayed once bytes are yielded.
        """
        url = f"{self._base_url}/{path}"
        attempts = _MAX_TRANSIENT_RETRIES + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            t0 = time.monotonic()
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=timeout,
                    headers=self._request_headers(),
                )
            except requests.RequestException as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                # A slow transport failure (e.g. a read timeout) already
                # burned the budget — don't pay it again.
                if last or elapsed_ms > _RETRY_MAX_ATTEMPT_MS:
                    raise
                delay = self._retry_delay(attempt, None)
                log.warning(
                    "openai-compat %s transient transport error in %.0fms, "
                    "retrying in %.1fs (attempt %d/%d) surface=%s exc=%r",
                    kind, elapsed_ms, delay, attempt + 1, attempts,
                    surface, exc,
                )
                time.sleep(delay)
                continue
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            retryable = (
                response.status_code in _RETRYABLE_STATUS
                and not last
                and elapsed_ms <= _RETRY_MAX_ATTEMPT_MS
            )
            if retryable:
                delay = self._retry_delay(attempt, response)
                log.warning(
                    "openai-compat %s transient status=%d in %.0fms, "
                    "retrying in %.1fs (attempt %d/%d) surface=%s",
                    kind, response.status_code, elapsed_ms, delay,
                    attempt + 1, attempts, surface,
                )
                try:
                    response.close()
                except Exception:
                    pass
                time.sleep(delay)
                continue
            return response, elapsed_ms
        # Unreachable: the final attempt always returns or raises above.
        raise RuntimeError("retry loop exited without a response")

    def _responses_complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        options: dict[str, object] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: "str | dict[str, Any] | None",
        format_json: bool,
        timeout: float,
        surface: str,
        think: bool,
    ) -> tuple[str, list[ChatToolCall], ChatUsage, list[dict[str, Any]]]:
        """Non-streaming ``POST /v1/responses``.

        Returns ``(content, calls, usage, response_output_items)``. When
        calls are present, the trailing list is the complete
        ``response.output`` array the documented follow-up flow must append
        verbatim before its ``function_call_output`` items."""
        payload = self._build_responses_payload(
            messages=messages,
            model=model,
            options=options,
            tools=tools,
            stream=False,
            format_json=format_json,
            tool_choice=tool_choice,
        )
        t0 = time.monotonic()
        try:
            response, elapsed_ms = self._post_json_with_retry(
                path="responses",
                payload=payload,
                timeout=timeout,
                surface=surface,
                kind="responses",
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "openai-compat responses transport error: model=%s "
                "surface=%s msgs=%d tools=%d elapsed_ms=%.0f exc=%r",
                model, surface, len(messages), len(tools or []),
                elapsed_ms, exc,
            )
            raise
        if not response.ok:
            self._log_http_error(
                "responses", response, elapsed_ms=elapsed_ms,
            )
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
        content, tool_calls, done_reason = _parse_responses_output(body)
        # Only worth carrying when there are calls to pair them with; a
        # plain text reply has no follow-up round that needs these items.
        response_output_items = (
            _response_output_items(body) if tool_calls else []
        )
        had_thinking = False
        if not think:
            content, had_thinking = _strip_thinking_blocks_with_signal(content)
        usage = _responses_usage(
            body.get("usage") if isinstance(body, dict) else None,
            total_ms=elapsed_ms,
            done_reason=done_reason,
        )
        _warn_if_truncated(
            usage,
            model=model,
            surface=surface,
            benign=had_thinking and _content_looks_complete(content),
        )
        self._announce_connection(model)
        log.debug(
            "openai-compat responses: model=%s surface=%s msgs=%d tools=%d "
            "elapsed_ms=%.0f prompt_tokens=%d completion_tokens=%d "
            "tool_calls=%d",
            model, surface, len(messages), len(tools or []), elapsed_ms,
            usage.prompt_tokens, usage.completion_tokens, len(tool_calls),
        )
        return content, tool_calls, usage, response_output_items

    def _responses_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        options: dict[str, object] | None,
        format_json: bool,
        stop_event: threading.Event | None,
        surface: str,
    ) -> Generator[str, None, None]:
        """Streaming ``POST /v1/responses`` -> visible text token deltas.

        Responses SSE carries the event type inside each ``data`` JSON
        object (``chunk["type"]``), so the shared
        :func:`_iter_sse_data_lines` reader is reused unchanged and we
        switch on the type: ``response.output_text.delta`` yields a
        token; ``response.completed`` / ``response.incomplete`` carry
        the final ``usage`` + status; ``response.failed`` / ``error``
        raise.
        """
        payload = self._build_responses_payload(
            messages=messages,
            model=model,
            options=options,
            tools=None,
            stream=True,
            format_json=format_json,
            tool_choice=None,
        )
        usage = ChatUsage()
        t0 = time.monotonic()
        first_token_ms: float | None = None
        try:
            with requests.post(
                f"{self._base_url}/responses",
                json=payload,
                stream=True,
                timeout=self._timeout_seconds,
                headers=self._request_headers(),
            ) as response:
                if not response.ok:
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    self._log_http_error(
                        "responses_stream", response, elapsed_ms=elapsed_ms,
                    )
                response.raise_for_status()
                done_reason: str | None = None
                for data in _iter_sse_data_lines(
                    response, stop_event=stop_event,
                ):
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    ctype = chunk.get("type")
                    if ctype == "response.output_text.delta":
                        token = chunk.get("delta")
                        if isinstance(token, str) and token:
                            if first_token_ms is None:
                                first_token_ms = (
                                    time.monotonic() - t0
                                ) * 1000.0
                            yield token
                    elif ctype in (
                        "response.completed", "response.incomplete",
                    ):
                        resp_obj = chunk.get("response")
                        if isinstance(resp_obj, dict):
                            u = resp_obj.get("usage")
                            if isinstance(u, dict):
                                usage.prompt_tokens = int(
                                    u.get("input_tokens", 0) or 0,
                                )
                                usage.completion_tokens = int(
                                    u.get("output_tokens", 0) or 0,
                                )
                                details = u.get("input_tokens_details")
                                if isinstance(details, dict):
                                    usage.cached_tokens = int(
                                        details.get("cached_tokens", 0) or 0,
                                    )
                            status = resp_obj.get("status")
                            if status == "incomplete":
                                inc = resp_obj.get("incomplete_details") or {}
                                if (
                                    isinstance(inc, dict)
                                    and inc.get("reason") == "max_output_tokens"
                                ):
                                    done_reason = "length"
                            elif status == "completed":
                                done_reason = "stop"
                    elif ctype in ("response.failed", "error"):
                        err_msg = _extract_responses_stream_error(chunk)
                        log.error(
                            "openai-compat responses_stream error: model=%s "
                            "surface=%s msg=%s",
                            model, surface, err_msg,
                        )
                        raise RuntimeError(
                            f"responses stream error: {err_msg}",
                        )
                usage.done_reason = done_reason
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "openai-compat responses_stream transport error: model=%s "
                "elapsed_ms=%.0f exc=%r",
                model, elapsed_ms, exc,
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        usage.total_duration_ms = elapsed_ms
        _fill_wall_clock_eval_duration(usage, first_token_ms, elapsed_ms)
        self.last_usage = usage
        _warn_if_truncated(usage, model=model, surface=surface)
        self._announce_connection(model)
        log.debug(
            "openai-compat responses_stream done: model=%s msgs=%d "
            "elapsed_ms=%.0f first_token_ms=%s prompt_tokens=%d "
            "completion_tokens=%d stopped=%s",
            model, len(messages), elapsed_ms,
            f"{first_token_ms:.0f}" if first_token_ms is not None else "-",
            usage.prompt_tokens, usage.completion_tokens,
            "1" if (stop_event is not None and stop_event.is_set()) else "0",
        )

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
        tool_choice: "str | dict[str, Any] | None" = None,
        model: str | None = None,
        think: bool = False,
        keep_alive: str | None = None,  # accepted for protocol parity
        surface: str = "chat_with_tools",
    ) -> ChatResponse:
        del keep_alive  # Ollama-only knob; see __init__ docstring
        use_model = (model or "").strip() or self._default_model
        if self._should_use_responses(use_model):
            content, tool_calls, usage, response_output_items = (
                self._responses_complete(
                    messages=messages,
                    model=use_model,
                    options=options,
                    tools=tools,
                    tool_choice=tool_choice,
                    format_json=False,
                    timeout=self._timeout_seconds,
                    surface=surface,
                    think=think,
                )
            )
            self.last_usage = usage
            return ChatResponse(
                content=content,
                tool_calls=tool_calls,
                reasoning_items=[
                    dict(item)
                    for item in response_output_items
                    if item.get("type") == "reasoning"
                ],
                response_output_items=response_output_items,
            )
        payload = self._build_payload(
            messages=messages,
            model=use_model,
            options=options,
            tools=tools,
            stream=False,
            format_json=False,
        )
        # ``tool_choice`` only makes sense alongside ``tools``. Forcing
        # ``"required"`` (paired with a synthetic escape tool on the
        # caller side) is how we stop chatty models from narrating
        # their intent instead of emitting the call.
        if tools and tool_choice is not None:
            payload["tool_choice"] = tool_choice
        t0 = time.monotonic()
        try:
            response, elapsed_ms = self._post_json_with_retry(
                path="chat/completions",
                payload=payload,
                timeout=self._timeout_seconds,
                surface=surface,
                kind="chat",
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "openai-compat chat transport error: model=%s msgs=%d "
                "tools=%d elapsed_ms=%.0f exc=%r",
                use_model, len(messages), len(tools or []), elapsed_ms, exc,
            )
            raise
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
        if self._should_use_responses(use_model):
            yield from self._responses_stream(
                messages=messages,
                model=use_model,
                options=options,
                format_json=format_json,
                stop_event=stop_event,
                surface=surface,
            )
            return
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
        _fill_wall_clock_eval_duration(usage, first_token_ms, elapsed_ms)
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
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._timeout_seconds
        )
        if self._should_use_responses(use_model):
            content, _tool_calls, usage, _output = self._responses_complete(
                messages=messages,
                model=use_model,
                options=merged_options,
                tools=None,
                tool_choice=None,
                format_json=format_json,
                timeout=effective_timeout,
                surface=surface,
                think=think,
            )
            return content, usage
        payload = self._build_payload(
            messages=messages,
            model=use_model,
            options=merged_options,
            tools=None,
            stream=False,
            format_json=format_json,
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
        falls back to the route's ``context_window`` or the hardcoded
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


# ── /v1/responses (Responses API) converters + parsers ───────────────
#
# The Responses API speaks a different request/response shape than
# /v1/chat/completions. These pure helpers translate between the
# codebase's neutral chat-message vocabulary and the Responses shape so
# the public client methods can branch on one routing predicate
# (:func:`_use_responses_api`) and reuse the same ChatResponse /
# ChatToolCall / ChatUsage dataclasses downstream.


def _messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate neutral chat messages into a Responses ``input`` array.

    Plain text turns become ``{"role": ..., "content": <text>}`` items.
    Assistant tool calls become ``{"type": "function_call", "call_id",
    "name", "arguments"(json-string)}`` items (one per call, preceded by
    the assistant's text when present). Tool results become
    ``{"type": "function_call_output", "call_id", "output"}`` items —
    the Responses analogue of a ``role=tool`` message.

    The caller's list is never mutated; a fresh list is returned.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and isinstance(msg.get("tool_calls"), list):
            # The Responses contract is to append ``response.output``
            # verbatim, not to recreate only its function calls. Keeping
            # the original items preserves provider-generated ids/status
            # as well as reasoning items. If present, this authoritative
            # stash replaces both the assistant-content conversion and
            # the reconstructed-call fallback below.
            stashed_output = msg.get(_RESPONSES_OUTPUT_KEY)
            if isinstance(stashed_output, list) and stashed_output:
                out.extend(
                    dict(item) for item in stashed_output
                    if isinstance(item, dict)
                )
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                out.append({"role": "assistant", "content": content})
            # Compatibility fallback for histories generated by the first
            # reasoning-only bridge.
            stashed_reasoning = msg.get(_RESPONSES_REASONING_KEY)
            if isinstance(stashed_reasoning, list):
                for item in stashed_reasoning:
                    if isinstance(item, dict):
                        out.append(dict(item))
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
                call_id = str(call.get("id", "") or "").strip() or f"call_{idx}"
                out.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": str(fn.get("name", "") or ""),
                    "arguments": args_str,
                })
        elif role == "tool":
            call_id = str(
                msg.get("tool_call_id", "") or msg.get("id", "") or "",
            ).strip()
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            out.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": content,
            })
        else:
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            resolved_role = (
                role
                if role in ("system", "developer", "user", "assistant")
                else "user"
            )
            out.append({"role": resolved_role, "content": content})
    return out


def _tools_to_responses(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Flatten chat-completions function tools to the Responses shape.

    chat-completions: ``{"type":"function","function":{name,description,
    parameters}}``. Responses: ``{"type":"function",name,description,
    parameters}`` (the nested ``function`` object is hoisted). Tools
    already in the flat shape pass through unchanged.
    """
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if tool.get("type") == "function" and isinstance(fn, dict):
            out.append({
                "type": "function",
                "name": str(fn.get("name", "") or ""),
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters")
                or {"type": "object", "properties": {}},
            })
        else:
            # Already flat (or a non-function tool, e.g. web_search) —
            # pass through so future Responses-native tool types work.
            out.append(dict(tool))
    return out


def _tool_choice_to_responses(
    tool_choice: "str | dict[str, Any]",
) -> "str | dict[str, Any]":
    """Translate a chat-completions ``tool_choice`` to the Responses form.

    Strings (``"auto"`` / ``"required"`` / ``"none"``) are identical on
    both APIs. The forced-function dict differs: chat-completions nests
    the name under ``function`` (``{"type":"function","function":
    {"name":...}}``) while Responses puts it at the top level
    (``{"type":"function","name":...}``).
    """
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return {"type": "function", "name": str(fn["name"])}
        if tool_choice.get("type") == "function" and tool_choice.get("name"):
            return {"type": "function", "name": str(tool_choice["name"])}
    return tool_choice


def _response_output_items(body: object) -> list[dict[str, Any]]:
    """Copy the complete Responses ``output`` array for a tool follow-up.

    OpenAI's documented flow is ``input += response.output``. Preserving
    every item verbatim is important: reasoning models need their reasoning
    items, while the original function-call item also carries a
    provider-generated ``id`` and ``status`` that a reduced reconstruction
    loses.
    """
    if not isinstance(body, dict):
        return []
    output = body.get("output")
    if not isinstance(output, list):
        return []
    return [dict(item) for item in output if isinstance(item, dict)]


def _reasoning_items_from_responses_output(body: object) -> list[dict[str, Any]]:
    """Pull raw ``reasoning`` items out of a Responses ``output`` array.

    Returned **verbatim** (id + type + summary + any encrypted_content),
    because for GPT-5 / o-series reasoning models the Responses API
    requires the reasoning items that preceded a tool call to be passed
    back with the tool outputs on the follow-up request — dropping them
    is what makes the second tool round 500 (see the function-calling
    guide, "Handling function calls"). We keep the whole item rather than
    cherry-picking fields so the round-trip stays correct if OpenAI adds
    to the shape.
    """
    return [
        dict(item)
        for item in _response_output_items(body)
        if item.get("type") == "reasoning"
    ]


def _parse_responses_output(
    body: object,
) -> tuple[str, list[ChatToolCall], str | None]:
    """Pull visible text, tool calls, and a done_reason from a Responses body.

    Walks ``body.output[]`` collecting ``message`` items' ``output_text``
    parts and ``function_call`` items. ``done_reason`` mirrors the
    chat-completions vocabulary: ``"length"`` when the run stopped on
    ``max_output_tokens``, ``"stop"`` on a clean completion, else the
    raw status string.
    """
    if not isinstance(body, dict):
        return "", [], None
    text_parts: list[str] = []
    tool_calls: list[ChatToolCall] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "output_text"
                        ):
                            text = part.get("text")
                            if isinstance(text, str):
                                text_parts.append(text)
                elif isinstance(content, str):
                    text_parts.append(content)
            elif itype == "function_call":
                name = str(item.get("name", "") or "").strip()
                if not name:
                    continue
                call_id = str(
                    item.get("call_id", "") or item.get("id", "") or "",
                ).strip()
                raw_args = item.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        loaded = json.loads(raw_args)
                    except Exception:
                        loaded = {}
                    args = dict(loaded) if isinstance(loaded, dict) else {}
                elif isinstance(raw_args, dict):
                    args = dict(raw_args)
                else:
                    args = {}
                tool_calls.append(
                    ChatToolCall(name=name, arguments=args, call_id=call_id),
                )
    status = body.get("status")
    done_reason: str | None = None
    if status == "incomplete":
        inc = body.get("incomplete_details") or {}
        if isinstance(inc, dict) and inc.get("reason") == "max_output_tokens":
            done_reason = "length"
        else:
            done_reason = "incomplete"
    elif status == "completed":
        done_reason = "stop"
    elif isinstance(status, str) and status:
        done_reason = status
    return "".join(text_parts), tool_calls, done_reason


def _extract_responses_stream_error(chunk: dict[str, Any]) -> str:
    """Best-effort human message from a Responses ``error`` / ``failed`` SSE.

    ``error`` events carry ``{"message": ...}`` directly;
    ``response.failed`` nests it under ``response.error.message``.
    Falls back to a compact repr so the raise always carries *some*
    context.
    """
    message = chunk.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    resp_obj = chunk.get("response")
    if isinstance(resp_obj, dict):
        err = resp_obj.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
    err = chunk.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return str(chunk)[:300]


def _responses_usage(
    usage_dict: object, *, total_ms: float, done_reason: str | None,
) -> ChatUsage:
    """Build a ChatUsage from a Responses ``usage`` block.

    Responses names the token fields ``input_tokens`` /
    ``output_tokens`` (vs chat-completions' ``prompt_tokens`` /
    ``completion_tokens``) and reports cache hits under
    ``input_tokens_details.cached_tokens``. ``done_reason`` is already
    mapped by :func:`_parse_responses_output`, so it's stored as-is.
    """
    usage = ChatUsage(total_duration_ms=float(total_ms))
    if isinstance(usage_dict, dict):
        usage.prompt_tokens = int(usage_dict.get("input_tokens", 0) or 0)
        usage.completion_tokens = int(usage_dict.get("output_tokens", 0) or 0)
        details = usage_dict.get("input_tokens_details")
        if isinstance(details, dict):
            usage.cached_tokens = int(details.get("cached_tokens", 0) or 0)
    usage.done_reason = done_reason
    return usage


def _fill_wall_clock_eval_duration(
    usage: ChatUsage,
    first_token_ms: float | None,
    elapsed_ms: float,
) -> None:
    """Derive generation time from the stream when the provider won't say.

    Ollama reports ``eval_duration`` natively; no OpenAI-compatible
    endpoint does. ``ChatUsage.tokens_per_second`` divides by that field,
    so every cloud turn used to render as ``0 tok/s`` in the UI and log a
    flat ``eval_ms=0``.

    The stream itself already knows enough: the span between the first
    content delta and the last byte IS the generation phase, with
    time-to-first-token (queueing plus prompt eval) excluded. It carries
    network jitter that Ollama's server-side number does not, hence the
    ``eval_duration_estimated`` flag rather than pretending the two are
    the same measurement.

    Only fills a field the provider left empty, so a future endpoint that
    starts reporting real timings silently wins.
    """
    if usage.eval_duration_ms > 0 or first_token_ms is None:
        return
    usage.eval_duration_ms = max(0.0, elapsed_ms - first_token_ms)
    usage.eval_duration_estimated = True


__all__ = ["OpenAICompatibleClient"]
