from __future__ import annotations

from collections.abc import Generator
import json

import requests

from app.core.settings import OllamaSettings


class OllamaClient:
    def __init__(self, settings: OllamaSettings, timeout_seconds: int = 90) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def chat(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self._settings.chat_model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self._settings.temperature},
        }
        response = requests.post(
            f"{self._settings.base_url}/api/chat",
            json=payload,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        return body.get("message", {}).get("content", "")

    def chat_stream(self, messages: list[dict[str, str]]) -> Generator[str, None, None]:
        payload = {
            "model": self._settings.chat_model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self._settings.temperature},
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
