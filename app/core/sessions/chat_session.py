from __future__ import annotations

from collections.abc import Callable

from app.core.sessions.session_types import SessionRuntimeContext, SessionTurnSignals


class ChatSession:
    session_type = "chat"

    def detect_turn_signals(self, user_text: str) -> SessionTurnSignals:
        _ = user_text
        return SessionTurnSignals()

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