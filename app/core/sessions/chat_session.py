from __future__ import annotations

from collections.abc import Callable

from app.core.sessions.session_types import (
    SessionNativeToolFlowContext,
    SessionNativeToolFlowResult,
    SessionRuntimeContext,
    SessionToolPolicy,
    SessionTurnSignals,
)


class ChatSession:
    session_type = "chat"

    def __init__(self, policy: SessionToolPolicy | None = None) -> None:
        self._policy = policy or SessionToolPolicy(
            native_tool_calls_enabled=False,
            allowed_tool_prefixes=(),
            pre_execution_narration_default=False,
        )

    def detect_turn_signals(self, user_text: str) -> SessionTurnSignals:
        _ = user_text
        return SessionTurnSignals()

    def tool_policy(self) -> SessionToolPolicy:
        return self._policy

    def run_native_tool_flow(
        self,
        *,
        messages: list[dict[str, object]],
        generation_options: dict[str, object],
        tools: list[dict[str, object]],
        flow_context: SessionNativeToolFlowContext,
    ) -> SessionNativeToolFlowResult:
        _ = messages
        _ = generation_options
        _ = tools
        _ = flow_context
        return SessionNativeToolFlowResult(handled=False)

    def on_screen_text(
        self,
        *,
        user_text: str,
        screen_text: str | None,
        foreground_window_title: str,
        trace: Callable[[str, str], None],
    ) -> None:
        _ = user_text
        _ = screen_text
        _ = foreground_window_title
        _ = trace

    def build_prompt_context(self) -> str:
        return ""

    def build_evidence_block(self, trace: Callable[[str, str], None]) -> str:
        _ = trace
        return ""

    def continue_after_approval(self, context: SessionRuntimeContext) -> str:
        _ = context
        return ""

    def is_active(self) -> bool:
        return True

    def stop(self, trace: Callable[[str, str], None]) -> bool:
        _ = trace
        return False

    def get_status(self) -> dict[str, bool | int | str]:
        return {"active": True, "session_type": self.session_type}