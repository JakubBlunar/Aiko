from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class SessionTurnSignals:
    wants_screen_context: bool = False
    wants_evidence: bool = False
    wants_continue: bool = False


@dataclass(slots=True)
class SessionRuntimeContext:
    actions_enabled: bool
    screen_enabled: bool
    foreground_window_title: str
    get_require_confirmation: Callable[[], bool]
    set_require_confirmation: Callable[[bool], None]
    invoke_tool: Callable[..., object]
    capture_screen_text: Callable[..., str | None]
    trace: Callable[[str, str], None]


class SessionHandler(Protocol):
    session_type: str

    def detect_turn_signals(self, user_text: str) -> SessionTurnSignals:
        ...

    def on_screen_text(
        self,
        *,
        user_text: str,
        screen_text: str | None,
        foreground_window_title: str,
        trace: Callable[[str, str], None],
    ) -> None:
        ...

    def build_prompt_context(self) -> str:
        ...

    def build_evidence_block(self, trace: Callable[[str, str], None]) -> str:
        ...

    def continue_after_approval(self, context: SessionRuntimeContext) -> str:
        ...

    def is_active(self) -> bool:
        ...

    def stop(self, trace: Callable[[str, str], None]) -> bool:
        ...

    def get_status(self) -> dict[str, bool | int | str]:
        ...