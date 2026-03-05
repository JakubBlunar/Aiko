from __future__ import annotations

import unittest

from app.core.services.startup_runtime_service import StartupRuntimeService


class StartupRuntimeServiceTests(unittest.TestCase):
    def _build_service(self, *, mcp_status: dict[str, object]) -> StartupRuntimeService:
        return StartupRuntimeService(
            persona_snapshot=lambda _max_notes: {},
            history_messages=lambda _limit: [],
            history_summary=lambda _limit, _max_chars: "",
            remember_history=lambda: False,
            startup_context_prewarm_enabled=lambda: False,
            startup_history_limit=lambda: 10,
            history_summary_enabled=lambda: False,
            history_summary_limit=lambda: 20,
            history_summary_max_chars=lambda: 420,
            ollama_list_models=lambda: ["model-a"],
            ollama_chat=lambda _messages: "OK",
            thinking_chat=lambda: (None, None),
            chat_model=lambda: "model-a",
            tts_getter=lambda: object(),
            tts_status=lambda: ("ready", "ok"),
            mcp_runtime_status=lambda: mcp_status,
            trace=lambda _stage, _message: None,
        )

    def test_prewarm_fails_when_mcp_enabled_and_no_servers_running(self) -> None:
        service = self._build_service(
            mcp_status={
                "enabled": True,
                "server_count": 1,
                "connected_count": 0,
            }
        )

        with self.assertRaises(RuntimeError):
            service.prewarm_runtime()

    def test_prewarm_passes_when_mcp_enabled_and_at_least_one_server_running(self) -> None:
        service = self._build_service(
            mcp_status={
                "enabled": True,
                "server_count": 2,
                "connected_count": 1,
            }
        )

        service.prewarm_runtime()

    def test_prewarm_skips_mcp_check_when_disabled(self) -> None:
        service = self._build_service(
            mcp_status={
                "enabled": False,
                "server_count": 0,
                "connected_count": 0,
            }
        )

        service.prewarm_runtime()


if __name__ == "__main__":
    unittest.main()
