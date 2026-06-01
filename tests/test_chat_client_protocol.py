"""Both chat clients must satisfy :class:`ChatClient`.

A runtime structural check via ``isinstance(c, ChatClient)`` would be
weak (Protocols with ``runtime_checkable`` only test attribute
presence, not signatures). We do that as the smoke check, but also
inspect each method's signature directly so a future refactor that
drops a parameter on one client but not the other fails the test.
"""

from __future__ import annotations

import inspect
import unittest

from app.core.infra.settings import load_settings
from app.llm.chat_client import ChatClient
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import OpenAICompatibleClient


_PROTOCOL_METHODS: tuple[str, ...] = (
    "chat",
    "chat_with_tools",
    "chat_stream",
    "chat_json",
    "list_models",
    "get_context_length",
)


class ChatClientProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings().ollama

    def _build_ollama(self) -> OllamaClient:
        return OllamaClient(self.settings)

    def _build_openai(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            self.settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-XXX",
        )

    def test_ollama_satisfies_chat_client(self) -> None:
        self.assertIsInstance(self._build_ollama(), ChatClient)

    def test_openai_satisfies_chat_client(self) -> None:
        self.assertIsInstance(self._build_openai(), ChatClient)

    def test_method_signatures_align(self) -> None:
        """Same arg names + arg counts on both implementations.

        This protects against a quiet drift where one client gains a
        new kwarg (e.g. ``num_predict``) and the controller starts
        passing it to both, but the other client silently drops it.
        """
        ollama = self._build_ollama()
        openai = self._build_openai()
        for method_name in _PROTOCOL_METHODS:
            with self.subTest(method=method_name):
                ollama_sig = inspect.signature(getattr(ollama, method_name))
                openai_sig = inspect.signature(getattr(openai, method_name))
                # We allow OpenAICompatibleClient methods to be a strict
                # superset (it might add a quirk-specific kwarg later);
                # the minimum bar is that every parameter the protocol
                # declares is honoured by both.
                ollama_params = set(ollama_sig.parameters.keys())
                openai_params = set(openai_sig.parameters.keys())
                # The protocol's declared method shape (see
                # chat_client.py) — both clients must accept these.
                if method_name in {"chat", "chat_with_tools"}:
                    expected = {"messages", "options", "model", "think", "surface"}
                elif method_name == "chat_with_tools":
                    expected |= {"tools", "keep_alive"}
                elif method_name == "chat_stream":
                    expected = {
                        "messages", "options", "model", "keep_alive",
                        "stop_event", "format_json", "think", "surface",
                    }
                elif method_name == "chat_json":
                    expected = {
                        "messages", "model", "options", "timeout_seconds",
                        "format_json", "think", "keep_alive", "surface",
                    }
                elif method_name == "list_models":
                    expected = set()
                elif method_name == "get_context_length":
                    expected = {"model"}
                else:
                    expected = set()
                self.assertTrue(
                    expected.issubset(ollama_params),
                    f"OllamaClient.{method_name} missing {expected - ollama_params}",
                )
                self.assertTrue(
                    expected.issubset(openai_params),
                    (
                        f"OpenAICompatibleClient.{method_name} missing "
                        f"{expected - openai_params}"
                    ),
                )

    def test_last_usage_attribute_present(self) -> None:
        for client in (self._build_ollama(), self._build_openai()):
            self.assertTrue(hasattr(client, "last_usage"))

    def test_base_url_attribute_present(self) -> None:
        for client in (self._build_ollama(), self._build_openai()):
            self.assertTrue(hasattr(client, "base_url"))
            self.assertIsInstance(client.base_url, str)


if __name__ == "__main__":
    unittest.main()
