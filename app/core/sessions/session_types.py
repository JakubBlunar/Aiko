from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import Protocol


@dataclass(slots=True)
class SessionTurnSignals:
    wants_screen_context: bool = False
    wants_evidence: bool = False
    wants_continue: bool = False


@dataclass(slots=True)
class SessionToolPolicy:
    native_tool_calls_enabled: bool = False
    allowed_tool_prefixes: tuple[str, ...] = ()
    pre_execution_narration_default: bool = True


@dataclass(slots=True)
class SessionNativeToolFlowContext:
    trace: Callable[[str, str], None]
    chat_with_tools: Callable[..., object] | None = None
    on_token: Callable[[str], None] | None = None
    stop_requested: Callable[[], bool] | None = None
    narration_enabled: bool = True
    speak_text: Callable[[str], bool] | None = None
    build_pre_execution_summary: Callable[[list[Any]], str] | None = None
    invoke_tool: Callable[..., object] | None = None
    tool_result_to_message_content: Callable[[str, object], str] | None = None
    sanitize_text: Callable[[str], str] | None = None


@dataclass(slots=True)
class SessionNativeToolFlowResult:
    handled: bool = False
    response: str = ""
    llm_ms: float = 0.0
    tool_calls_executed: bool = False
    pre_execution_narration_emitted: bool = False


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
    active_goal: str = ""
    narration_level: str = "summary"
    available_tools: Callable[[], list[str]] | None = None
    plan_agentic_step: Callable[[str, str | None, list[dict[str, Any]], int], dict[str, Any]] | None = None
    narrate: Callable[[str], None] | None = None
    execute_action_plan: Callable[[Any], Any] | None = None


class SessionHandler(Protocol):
    session_type: str

    def detect_turn_signals(self, user_text: str) -> SessionTurnSignals:
        ...

    def tool_policy(self) -> SessionToolPolicy:
        ...

    def run_native_tool_flow(
        self,
        *,
        messages: list[dict[str, Any]],
        generation_options: dict[str, Any],
        tools: list[dict[str, Any]],
        flow_context: SessionNativeToolFlowContext,
    ) -> SessionNativeToolFlowResult:
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