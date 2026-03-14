from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
import json
from typing import Any

import requests

from app.core.settings import OllamaSettings


@dataclass(slots=True)
class OllamaToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(slots=True)
class OllamaChatResponse:
    content: str
    tool_calls: list[OllamaToolCall] = field(default_factory=list)


class OllamaClient:
    def __init__(self, settings: OllamaSettings, timeout_seconds: int = 90) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def chat(self, messages: list[dict[str, Any]], options: dict[str, object] | None = None) -> str:
        return self.chat_with_tools(messages, options=options).content

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, object] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> OllamaChatResponse:
        merged_options: dict[str, object] = {"temperature": self._settings.temperature}
        if options:
            merged_options.update(options)
        payload: dict[str, Any] = {
            "model": self._settings.chat_model,
            "messages": messages,
            "stream": False,
            "options": merged_options,
        }
        if tools:
            payload["tools"] = tools
        response = requests.post(
            f"{self._settings.base_url}/api/chat",
            json=payload,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        message = body.get("message", {}) if isinstance(body, dict) else {}
        content = str(message.get("content", "") or "")
        return OllamaChatResponse(
            content=content,
            tool_calls=self._parse_tool_calls(message.get("tool_calls", [])),
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
    ) -> Generator[str, None, None]:
        merged_options: dict[str, object] = {"temperature": self._settings.temperature}
        if options:
            merged_options.update(options)
        payload = {
            "model": self._settings.chat_model,
            "messages": messages,
            "stream": True,
            "options": merged_options,
        }
        with requests.post(
            f"{self._settings.base_url}/api/chat",
            json=payload,
            stream=True,
            timeout=self._timeout_seconds,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token

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
            f"{self._settings.base_url}/api/tags",
            timeout=self._timeout_seconds,
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
