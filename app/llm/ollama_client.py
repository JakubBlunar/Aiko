from __future__ import annotations

import logging
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
import json
from typing import Any

import requests

from app.core.settings import OllamaSettings


log = logging.getLogger("app.llm.ollama_client")

# One-shot per-base-url connection notices (INFO at most once per process).
_announced_base_urls: set[str] = set()


@dataclass(slots=True)
class OllamaToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(slots=True)
class OllamaChatResponse:
    content: str
    tool_calls: list[OllamaToolCall] = field(default_factory=list)


@dataclass(slots=True)
class OllamaUsage:
    """Token + timing telemetry pulled from the final streaming chunk.

    Mirrors the fields Ollama's /api/chat returns when ``done=True`` is sent.
    All values are 0 when the server didn't include them.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_duration_ms: float = 0.0
    eval_duration_ms: float = 0.0
    prompt_eval_duration_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_per_second(self) -> float:
        if self.eval_duration_ms <= 0 or self.completion_tokens <= 0:
            return 0.0
        return round((self.completion_tokens * 1000.0) / self.eval_duration_ms, 1)

    def merge(self, other: "OllamaUsage") -> "OllamaUsage":
        """Return a new usage that adds another pass on top of this one.

        Used to combine the tool pre-pass and the streaming reply pass into a
        single per-turn telemetry record.
        """
        return OllamaUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_duration_ms=self.total_duration_ms + other.total_duration_ms,
            eval_duration_ms=self.eval_duration_ms + other.eval_duration_ms,
            prompt_eval_duration_ms=self.prompt_eval_duration_ms + other.prompt_eval_duration_ms,
        )


class OllamaClient:
    def __init__(
        self,
        settings: OllamaSettings,
        timeout_seconds: int | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.timeout
        self._base_url = (base_url or "").strip() or settings.base_url
        headers: dict[str, str] = {}
        if extra_headers:
            for key, value in extra_headers.items():
                if key and value:
                    headers[str(key).strip()] = str(value).strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        self._headers: dict[str, str] = headers
        self.last_usage: OllamaUsage = OllamaUsage()

    @property
    def base_url(self) -> str:
        return self._base_url

    def _request_headers(self) -> dict[str, str] | None:
        return dict(self._headers) if self._headers else None

    def _announce_connection(self, model: str) -> None:
        """Log one INFO line the first time we successfully reach this server."""
        key = self._base_url
        if key in _announced_base_urls:
            return
        _announced_base_urls.add(key)
        log.info(
            "ollama connected: base_url=%s default_model=%s",
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
            "ollama %s failed: status=%d reason=%s elapsed_ms=%.0f body=%s",
            endpoint, response.status_code, response.reason, elapsed_ms,
            snippet.replace("\n", " ") or "-",
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        model: str | None = None,
        think: bool = False,
    ) -> str:
        return self.chat_with_tools(
            messages, options=options, model=model, think=think
        ).content

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, object] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        think: bool = False,
    ) -> OllamaChatResponse:
        merged_options: dict[str, object] = {"temperature": self._settings.temperature}
        if options:
            merged_options.update(options)
        use_model = (model or "").strip() or self._settings.chat_model
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "stream": False,
            "options": merged_options,
        }
        if tools:
            payload["tools"] = tools
        if think:
            payload["think"] = True
        t0 = time.monotonic()
        try:
            response = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=self._timeout_seconds,
                headers=self._request_headers(),
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "ollama chat transport error: model=%s msgs=%d tools=%d "
                "elapsed_ms=%.0f exc=%r",
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
        message = body.get("message", {}) if isinstance(body, dict) else {}
        content = str(message.get("content", "") or "")
        self.last_usage = OllamaUsage(
            prompt_tokens=int(body.get("prompt_eval_count", 0) or 0),
            completion_tokens=int(body.get("eval_count", 0) or 0),
            total_duration_ms=float(body.get("total_duration", 0) or 0) / 1e6,
            eval_duration_ms=float(body.get("eval_duration", 0) or 0) / 1e6,
            prompt_eval_duration_ms=float(body.get("prompt_eval_duration", 0) or 0) / 1e6,
        )
        self._announce_connection(use_model)
        tool_calls = self._parse_tool_calls(message.get("tool_calls", []))
        log.debug(
            "ollama chat: model=%s msgs=%d tools=%d stream=0 elapsed_ms=%.0f "
            "prompt_tokens=%d completion_tokens=%d tool_calls=%d",
            use_model, len(messages), len(tools or []), elapsed_ms,
            self.last_usage.prompt_tokens, self.last_usage.completion_tokens,
            len(tool_calls),
        )
        # When think=True, Ollama may also return message.thinking (reasoning trace);
        # we use content (final answer) for the response.
        return OllamaChatResponse(
            content=content,
            tool_calls=tool_calls,
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        *,
        model: str | None = None,
        keep_alive: str | None = "10m",
        stop_event: threading.Event | None = None,
        format_json: bool = False,
        think: bool = False,
    ) -> Generator[str, None, None]:
        """Stream content tokens from Ollama /api/chat.

        After iteration completes (or the caller stops consuming) the last
        chunk's usage telemetry is exposed via :attr:`last_usage`. Pass
        ``stop_event`` to abort streaming cleanly: the underlying socket is
        closed which signals Ollama to cancel generation.

        ``think`` defaults to ``False`` so reasoning models (qwen3.x, deepseek-r1,
        gpt-oss…) skip their internal chain-of-thought and stream the actual
        answer immediately. Pass ``think=True`` if you want the reasoning trace
        in ``message.thinking`` (we still only yield ``message.content`` here).
        """
        merged_options: dict[str, object] = {"temperature": self._settings.temperature}
        if options:
            merged_options.update(options)
        use_model = (model or "").strip() or self._settings.chat_model
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "stream": True,
            "think": bool(think),
            "options": merged_options,
        }
        if keep_alive:
            payload["keep_alive"] = keep_alive
        if format_json:
            payload["format"] = "json"
        usage = OllamaUsage()
        t0 = time.monotonic()
        first_token_ms: float | None = None
        try:
            with requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                stream=True,
                timeout=self._timeout_seconds,
                headers=self._request_headers(),
            ) as response:
                if not response.ok:
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    self._log_http_error("chat_stream", response, elapsed_ms=elapsed_ms)
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if stop_event is not None and stop_event.is_set():
                        response.close()
                        break
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("done"):
                        usage.prompt_tokens = int(chunk.get("prompt_eval_count", 0) or 0)
                        usage.completion_tokens = int(chunk.get("eval_count", 0) or 0)
                        usage.total_duration_ms = float(chunk.get("total_duration", 0) or 0) / 1e6
                        usage.eval_duration_ms = float(chunk.get("eval_duration", 0) or 0) / 1e6
                        usage.prompt_eval_duration_ms = float(chunk.get("prompt_eval_duration", 0) or 0) / 1e6
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        if first_token_ms is None:
                            first_token_ms = (time.monotonic() - t0) * 1000.0
                        yield token
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "ollama chat_stream transport error: model=%s elapsed_ms=%.0f exc=%r",
                use_model, elapsed_ms, exc,
            )
            raise
        self.last_usage = usage
        self._announce_connection(use_model)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        log.debug(
            "ollama chat_stream done: model=%s msgs=%d elapsed_ms=%.0f "
            "first_token_ms=%s prompt_tokens=%d completion_tokens=%d "
            "stopped=%s",
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
    ) -> tuple[str, OllamaUsage]:
        """One-shot non-streaming call (defaults to ``format=json``).

        Used by background workers (summary, learner profile) that need a
        bounded response and don't want to manage a stream. Returns
        ``(raw_content, usage)``. Pass ``format_json=False`` for plain text
        responses (e.g. summarisation). ``think`` is False by default so
        reasoning models don't burn the response budget on chain-of-thought.
        """
        merged_options: dict[str, object] = {"temperature": 0.0}
        if options:
            merged_options.update(options)
        use_model = (model or "").strip() or self._settings.chat_model
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "stream": False,
            "keep_alive": "10m",
            "think": bool(think),
            "options": merged_options,
        }
        if format_json:
            payload["format"] = "json"
        t0 = time.monotonic()
        try:
            response = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=timeout_seconds if timeout_seconds is not None else self._timeout_seconds,
                headers=self._request_headers(),
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.error(
                "ollama chat_json transport error: model=%s elapsed_ms=%.0f exc=%r",
                use_model, elapsed_ms, exc,
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if not response.ok:
            self._log_http_error("chat_json", response, elapsed_ms=elapsed_ms)
        response.raise_for_status()
        body = response.json()
        message = body.get("message", {}) if isinstance(body, dict) else {}
        content = str(message.get("content", "") or "")
        usage = OllamaUsage(
            prompt_tokens=int(body.get("prompt_eval_count", 0) or 0),
            completion_tokens=int(body.get("eval_count", 0) or 0),
            total_duration_ms=float(body.get("total_duration", 0) or 0) / 1e6,
            eval_duration_ms=float(body.get("eval_duration", 0) or 0) / 1e6,
            prompt_eval_duration_ms=float(body.get("prompt_eval_duration", 0) or 0) / 1e6,
        )
        self._announce_connection(use_model)
        log.debug(
            "ollama chat_json: model=%s msgs=%d elapsed_ms=%.0f "
            "prompt_tokens=%d completion_tokens=%d format_json=%s",
            use_model, len(messages), elapsed_ms,
            usage.prompt_tokens, usage.completion_tokens,
            "1" if format_json else "0",
        )
        return content, usage

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: object) -> list[OllamaToolCall]:
        if not isinstance(raw_tool_calls, list):
            return []
        parsed: list[OllamaToolCall] = []
        for item in raw_tool_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function", {})
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
            parsed.append(OllamaToolCall(name=name, arguments=args, call_id=call_id))
        return parsed

    def list_models(self) -> list[str]:
        response = requests.get(
            f"{self._base_url}/api/tags",
            timeout=self._timeout_seconds,
            headers=self._request_headers(),
        )
        response.raise_for_status()
        body = response.json()
        models = body.get("models", [])
        output: list[str] = []
        for item in models:
            name = str(item.get("name", "")).strip()
            if name:
                output.append(name)
        return output

    # ── Model metadata ───────────────────────────────────────────────

    _show_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def show(self, model: str, *, refresh: bool = False) -> dict[str, Any]:
        """Fetch model metadata from /api/show.

        Cached per ``(base_url, model)`` for the process lifetime — Ollama's
        model metadata is static once a model is pulled, and we call this on
        every model switch. Returns ``{}`` on failure (network, 404, parse).
        """
        key = (self._base_url, model)
        if not refresh and key in self._show_cache:
            return self._show_cache[key]
        try:
            response = requests.post(
                f"{self._base_url}/api/show",
                json={"model": model, "verbose": False},
                timeout=min(5.0, float(self._timeout_seconds)),
                headers=self._request_headers(),
            )
            response.raise_for_status()
            body = response.json()
            data = body if isinstance(body, dict) else {}
        except Exception:
            data = {}
        self._show_cache[key] = data
        return data

    def get_context_length(self, model: str) -> int | None:
        """Return the model's max context length in tokens, or ``None``.

        Walks ``model_info`` for any key ending in ``.context_length`` (Qwen,
        Llama, Mistral, etc. all expose it under their architecture prefix,
        e.g. ``qwen2.context_length``, ``llama.context_length``).
        """
        info = self.show(model)
        model_info = info.get("model_info") if isinstance(info, dict) else None
        if not isinstance(model_info, dict):
            return None
        for key, value in model_info.items():
            if not isinstance(key, str):
                continue
            if key.endswith(".context_length"):
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
        return None
