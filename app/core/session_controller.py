from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any

from app.core.tooling.runtime.action_runtime import (
    ActionExecutionResult,
    GuardedActionExecutor,
)
from app.core.tooling.runtime.emergency_stop import EmergencyStopState, GlobalHotkeyListener
from app.audio.mic_capture import MicrophoneCapture
from app.core.planning.action_planner import ActionPlanner
from app.core.planning.autonomy_planner import AutonomyPlanner, GoalInference
from app.core.planning.turn_orchestrator import TurnAutonomyPlan, TurnOrchestrator, TurnOrchestratorPlan
from app.core.services.action_execution_service import ActionExecutionService
from app.core.services.action_response_service import (
    build_post_action_followup,
    normalize_action_narration,
)
from app.core.conversation_memory import ConversationMemoryStore
from app.core.crash_logging import log_event, log_handled_exception
from app.core.sessions.chat_session import ChatSession
from app.core.sessions.agentic_session import AgenticSessionConfig, AgenticSessionManager
from app.core.sessions.agentic_session_adapter import AgenticSessionAdapter
from app.core.sessions.reading_session import ReadingSessionConfig, ReadingSessionManager
from app.core.sessions.reading_session_adapter import ReadingSessionAdapter
from app.core.sessions.session_router import SessionRouter
from app.core.sessions.session_types import SessionHandler, SessionRuntimeContext
from app.core.settings import AppSettings, OllamaSettings
from app.core.services.response_text_service import extract_tts_reaction_tag, strip_action_meta_for_tts
from app.core.services.startup_runtime_service import StartupRuntimeService
from app.core.session_text_utils import (
    drain_tts_stream_chunks,
    extract_json_object,
    infer_tts_reaction,
    prepare_tts_text,
    sanitize_assistant_text,
    sanitize_user_text,
)
from app.core.services.screen_context_service import ScreenContextService
from app.core.tooling.mcp_client import MCPStdioClient
from app.core.tooling.mcp_http_client import MCPHttpClient
from app.core.tooling.mcp_tools import build_mcp_tools
from app.core.tooling import ToolContext, ToolExecutor, ToolRegistry, load_tooling_config
from app.core.tooling.tools import ActionExecutePlanTool, build_default_tools
from app.core.tooling.types import ToolError, ToolResult
from app.core.turn_manager import TurnInput, TurnManager
from app.llm.ollama_client import OllamaClient
from app.stt.prosody_fast import FastProsodyAnalyzer, ProsodyAnalysis
from app.stt.whisper_service import WhisperService
from app.tts.llasa_service import LlasaTtsService
from app.tts.piper_service import PiperTtsService
from app.vision.screen_capture import ScreenCaptureService


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    screen_enabled: bool
    autonomy_mode: str
    session_type: str


class SessionController:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        # Initialize trace buffer first because background startup paths (e.g. MCP stderr)
        # can emit trace events before the controller has fully finished bootstrapping.
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)
        self._turn_manager = TurnManager()
        self._ollama = OllamaClient(settings.ollama)
        self._thinking_model = settings.assistant.thinking_model
        self._thinking_ollama: OllamaClient | None = None
        self._rebuild_thinking_client()
        self._microphone = MicrophoneCapture(settings.audio)
        self._whisper = WhisperService(
            model_name=settings.stt.model,
            language=settings.stt.language,
        )
        self._prosody = FastProsodyAnalyzer(enabled=bool(settings.stt.prosody_enabled))
        self._prosody_include_in_prompt = bool(settings.stt.prosody_include_in_prompt)
        self._screen = ScreenCaptureService(settings.screen)
        self._action_stop_state = EmergencyStopState()
        self._action_hotkey_listener = GlobalHotkeyListener(
            hotkey=settings.actions.emergency_hotkey,
            state=self._action_stop_state,
        )
        self._action_executor = GuardedActionExecutor(settings.actions, self._action_stop_state)
        self._memory = ConversationMemoryStore()

        tooling_default_path = Path(settings.tooling.config_default_path)
        tooling_user_path = Path(settings.tooling.config_user_path)
        workspace_root = Path(__file__).resolve().parents[2]
        if not tooling_default_path.is_absolute():
            tooling_default_path = workspace_root / tooling_default_path
        if not tooling_user_path.is_absolute():
            tooling_user_path = workspace_root / tooling_user_path
        self._tooling_config = load_tooling_config(
            default_path=tooling_default_path,
            user_path=tooling_user_path,
        )
        self._tool_registry = ToolRegistry()
        self._mcp_servers: list[dict[str, Any]] = []
        self._tool_registry.register_many(
            build_default_tools(settings, self._tooling_config, memory_store=self._memory)
        )
        self._tool_registry.register(ActionExecutePlanTool(self._action_executor))
        self._register_mcp_tools()
        self._tool_executor = ToolExecutor(self._tool_registry, self._tooling_config)
        self._tool_context = ToolContext(metadata={"source": "session_controller"})
        history_cfg = self._tooling_config.tool_settings("history")
        self._history_prompt_limit = self._coerce_int(
            history_cfg.get("prompt_limit", history_cfg.get("default_limit", 12)),
            default=12,
            minimum=1,
            maximum=400,
        )
        self._history_summary_enabled = bool(history_cfg.get("summary_enabled", True))
        self._history_summary_limit = self._coerce_int(
            history_cfg.get("summary_limit", max(self._history_prompt_limit * 3, 24)),
            default=max(self._history_prompt_limit * 3, 24),
            minimum=1,
            maximum=400,
        )
        self._history_summary_max_chars = self._coerce_int(
            history_cfg.get("summary_max_chars", 420),
            default=420,
            minimum=80,
            maximum=2000,
        )
        self._history_summary_tail_limit = self._coerce_int(
            history_cfg.get("summary_tail_limit", 8),
            default=8,
            minimum=1,
            maximum=100,
        )
        self._startup_history_limit = self._coerce_int(
            history_cfg.get("startup_history_limit", self._history_prompt_limit),
            default=self._history_prompt_limit,
            minimum=1,
            maximum=400,
        )
        self._startup_context_prewarm_enabled = bool(history_cfg.get("preload_on_startup", True))
        persona_cfg = self._tooling_config.tool_settings("persona")
        self._persona_compact_enabled = bool(persona_cfg.get("compact_notes_enabled", True))
        self._persona_compact_max_notes = self._coerce_int(
            persona_cfg.get("compact_max_notes", 10),
            default=10,
            minimum=1,
            maximum=40,
        )
        self._persona_compact_max_chars = self._coerce_int(
            persona_cfg.get("compact_max_chars", 110),
            default=110,
            minimum=40,
            maximum=400,
        )
        self._persona_filter_enabled = bool(persona_cfg.get("filter_notes_enabled", True))
        self._persona_filter_max_notes = self._coerce_int(
            persona_cfg.get("filter_max_notes", self._persona_compact_max_notes),
            default=self._persona_compact_max_notes,
            minimum=1,
            maximum=60,
        )
        self._persona_filter_min_chars = self._coerce_int(
            persona_cfg.get("filter_min_chars", 12),
            default=12,
            minimum=4,
            maximum=120,
        )
        self._persona_filter_remove_generic_user_said = bool(
            persona_cfg.get("filter_remove_generic_user_said", True)
        )
        self._tts = self._build_tts_service(settings)
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._microphone_device = settings.audio.microphone_device
        self._remember_history = settings.assistant.remember_history
        self._autonomy_mode = str(getattr(settings.autonomy, "mode", "interactive") or "interactive").strip().lower()
        if self._autonomy_mode not in {"manual", "interactive", "automatic"}:
            self._autonomy_mode = "interactive"
        self._active_goal = settings.autonomy.default_goal
        self._active_goal_description: str = ""
        self._last_screen_elements: list[dict] = []
        self._open_windows: list[dict] = []
        self._all_windows: list[dict] = []
        self._foreground_window_title: str = ""
        self._reading_session = ReadingSessionManager(
            ReadingSessionConfig(
                memory_enabled=bool(settings.autonomy.reading_session_memory_enabled),
                max_scroll_steps=max(1, int(settings.autonomy.reading_max_scroll_steps)),
                max_quotes=max(1, int(settings.autonomy.reading_max_quotes)),
                max_quote_chars=max(120, int(settings.autonomy.reading_max_quote_chars)),
                trusted_window_titles=list(settings.autonomy.reading_trusted_window_titles or []),
            )
        )
        self._agentic_session = AgenticSessionManager(
            AgenticSessionConfig(
                enabled=bool(self._settings.autonomy.enabled),
                max_auto_steps=max(1, int(getattr(self._settings.autonomy, "agentic_max_auto_steps", 3))),
            )
        )
        self._session_handlers: dict[str, SessionHandler] = {
            "chat": ChatSession(),
            "reading": ReadingSessionAdapter(self._reading_session),
            "agentic": AgenticSessionAdapter(self._agentic_session),
        }
        self._session_router = SessionRouter(
            supported_session_types=set(self._session_handlers.keys()),
            default_session_type="chat",
        )
        self._active_session_type, _ = self._session_router.resolve(
            inferred_session_type="chat",
            inferred_goal=self._active_goal,
            current_session_type="chat",
        )
        self._active_session = self._session_handlers[self._active_session_type]
        self._turn_orchestrator = TurnOrchestrator(
            planner_chat=lambda messages: (self._thinking_ollama or self._ollama).chat(messages),
            history_messages=lambda limit: self._history_messages(limit=limit),
            extract_json_object=extract_json_object,
            trace=self._trace,
        )
        self._autonomy_planner = AutonomyPlanner(
            planner_chat=lambda messages: (self._thinking_ollama or self._ollama).chat(messages),
            history_messages=lambda limit: self._history_messages(limit=limit),
            extract_json_object=extract_json_object,
            trace=self._trace,
        )
        self._action_planner = ActionPlanner(
            planner_chat=lambda messages: (self._thinking_ollama or self._ollama).chat(messages),
            history_messages=lambda limit: self._history_messages(limit=limit),
            extract_json_object=extract_json_object,
            trace=self._trace,
        )
        self._screen_context = ScreenContextService(
            screen_settings=self._settings.screen,
            planner_chat=lambda messages: (self._thinking_ollama or self._ollama).chat(messages),
            history_messages=lambda limit: self._history_messages(limit=limit),
            invoke_tool=self._invoke_tool,
            trace=self._trace,
            screen_capture_once_with_region=self._screen.capture_once_with_region,
            screen_capture_once=self._screen.capture_once,
        )
        self._action_execution = ActionExecutionService(
            actions_settings=self._settings.actions,
            action_planner=self._action_planner,
            capture_screen_text=self._capture_screen_text,
            invoke_tool=self._invoke_tool,
            trace=self._trace,
            screen_enabled=lambda: bool(self._state.screen_enabled),
            active_goal=lambda: self._active_goal,
            last_screen_elements=lambda: list(self._last_screen_elements),
            all_windows=lambda: list(self._all_windows),
        )
        self._startup_runtime = StartupRuntimeService(
            persona_snapshot=lambda max_notes: self._persona_snapshot(max_notes=max_notes),
            history_messages=lambda limit: self._history_messages(limit=limit),
            history_summary=lambda limit, max_chars: self._history_summary(limit=limit, max_chars=max_chars),
            remember_history=lambda: bool(self._remember_history),
            startup_context_prewarm_enabled=lambda: bool(self._startup_context_prewarm_enabled),
            startup_history_limit=lambda: int(self._startup_history_limit),
            history_summary_enabled=lambda: bool(self._history_summary_enabled),
            history_summary_limit=lambda: int(self._history_summary_limit),
            history_summary_max_chars=lambda: int(self._history_summary_max_chars),
            ollama_list_models=self._ollama.list_models,
            ollama_chat=lambda messages: self._ollama.chat(messages),
            thinking_chat=lambda: (
                (lambda messages: self._thinking_ollama.chat(messages)) if self._thinking_ollama else None,
                self._thinking_model,
            ),
            chat_model=lambda: self.chat_model,
            tts_getter=lambda: self._tts,
            tts_status=self.get_tts_model_status,
            trace=self._trace,
        )
        self._last_metrics: dict[str, float | str] = {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
        }
        self._metrics_history: deque[dict[str, float | str]] = deque(maxlen=10)
        self._live_no_speech_streak = 0

        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            screen_enabled=settings.screen.enable_screen_context,
            autonomy_mode=self._autonomy_mode,
            session_type=self._active_session_type,
        )
        self._apply_persona_runtime_preferences()

    @property
    def state(self) -> SessionState:
        return self._state

    def update_sources(self, *, mic: bool, screen: bool) -> None:
        self._state.mic_enabled = mic
        self._state.screen_enabled = screen

    def list_microphone_devices(self) -> list[tuple[int, str]]:
        return self._microphone.list_input_devices()

    def set_microphone_device(self, device_index: int | None) -> None:
        self._microphone_device = device_index
        self._microphone.set_device(device_index)

    @property
    def microphone_device(self) -> int | None:
        return self._microphone_device

    @property
    def vad_level_threshold(self) -> float:
        return self._vad_level_threshold

    @property
    def vad_silence_seconds(self) -> float:
        return self._vad_silence_seconds

    @property
    def stt_model(self) -> str:
        return str(self._settings.stt.model or "base").strip() or "base"

    @property
    def prosody_enabled(self) -> bool:
        return bool(self._prosody.enabled)

    def set_prosody_enabled(self, value: bool) -> None:
        enabled = bool(value)
        self._settings.stt.prosody_enabled = enabled
        self._prosody.set_enabled(enabled)

    @property
    def prosody_include_in_prompt(self) -> bool:
        return bool(self._prosody_include_in_prompt)

    def set_prosody_include_in_prompt(self, value: bool) -> None:
        include = bool(value)
        self._settings.stt.prosody_include_in_prompt = include
        self._prosody_include_in_prompt = include

    def set_stt_model(self, model_name: str) -> bool:
        normalized = str(model_name or "").strip()
        if not normalized:
            return False
        if normalized == self.stt_model:
            return True

        candidate = WhisperService(
            model_name=normalized,
            language=self._settings.stt.language,
        )
        if not candidate.is_available:
            self._trace("stt.error", f"Failed to load STT model: {normalized}")
            return False

        self._settings.stt.model = normalized
        self._whisper = candidate
        self._trace("stt.model", f"Switched STT model to {normalized}")
        return True

    def set_vad_level_threshold(self, value: float) -> None:
        self._vad_level_threshold = max(0.001, min(value, 0.5))

    def set_vad_silence_seconds(self, value: float) -> None:
        self._vad_silence_seconds = max(0.3, min(value, 6.0))

    @property
    def action_min_interval_seconds(self) -> float:
        return float(self._settings.actions.min_action_interval_seconds)

    def set_action_min_interval_seconds(self, value: float) -> None:
        self._settings.actions.min_action_interval_seconds = max(0.0, float(value))

    @property
    def tts_provider(self) -> str:
        return (self._settings.tts.provider or "piper").strip().lower() or "piper"

    def list_tts_providers(self) -> list[str]:
        return ["piper", "llasa"]

    @property
    def tts_voice(self) -> str:
        return str(self._settings.tts.voice or "").strip()

    def list_tts_voices(self) -> list[str]:
        workspace_root = Path(__file__).resolve().parents[2]
        models_dir = workspace_root / "models"
        voices: list[str] = []

        if models_dir.exists():
            for model_path in sorted(models_dir.glob("*.onnx")):
                try:
                    relative = model_path.relative_to(workspace_root).as_posix()
                except Exception:
                    relative = str(model_path)
                voices.append(relative)

        current = self.tts_voice
        if current and current not in voices:
            voices.insert(0, current)
        return voices

    def set_tts_voice(self, voice: str) -> None:
        normalized = str(voice or "").strip().replace("\\", "/")
        if not normalized:
            return
        if normalized == self.tts_voice:
            return

        self._settings.tts.voice = normalized
        if self.tts_provider == "piper":
            try:
                self._tts.stop()
            except Exception:
                pass
        self._trace("tts.voice", f"Switched TTS voice to {normalized}")

    def get_tts_model_status(self) -> tuple[str, str]:
        get_status = getattr(self._tts, "get_status", None)
        if callable(get_status):
            try:
                state, details = get_status()
                return str(state), str(details)
            except Exception:
                return "error", "Failed to read TTS status"
        return "ready", "TTS runtime available"

    def speak_text(self, text: str) -> bool:
        if not bool(getattr(self._settings.tts, "enabled", True)):
            return False
        message = prepare_tts_text(text)
        if not message:
            return False
        try:
            reaction = infer_tts_reaction(message)
            self._tts.speak_async(message, reaction=reaction)
            return True
        except Exception as exc:
            self._trace("tts.error", f"TTS startup speak failed: {exc}")
            return False

    def build_startup_greeting(self) -> str:
        return self._startup_runtime.build_startup_greeting()

    def prewarm_tts(self) -> None:
        self._startup_runtime.prewarm_tts()

    def prewarm_runtime(self, on_status: Callable[[str], None] | None = None) -> None:
        self._startup_runtime.prewarm_runtime(on_status=on_status)

    def set_tts_provider(self, provider: str) -> None:
        normalized = (provider or "").strip().lower()
        if normalized not in {"piper", "llasa"}:
            normalized = "piper"

        if normalized == self.tts_provider:
            return

        try:
            self._tts.stop()
        except Exception:
            pass

        self._settings.tts.provider = normalized
        self._tts = self._build_tts_service(self._settings)
        self._apply_persona_runtime_preferences()
        self._trace("tts.provider", f"Switched TTS provider to {normalized}")

    @property
    def chat_model(self) -> str:
        return self._settings.ollama.chat_model

    def set_chat_model(self, model_name: str) -> None:
        model_name = (model_name or "").strip()
        if model_name:
            self._settings.ollama.chat_model = model_name
            if not self._thinking_model:
                self._rebuild_thinking_client()

    def list_chat_models(self) -> list[str]:
        try:
            models = self._ollama.list_models()
        except Exception:
            models = []

        current = self.chat_model
        if current and current not in models:
            models.insert(0, current)
        return models

    @property
    def thinking_model(self) -> str | None:
        return self._thinking_model

    def set_thinking_model(self, model_name: str | None) -> None:
        candidate = (model_name or "").strip()
        self._thinking_model = candidate or None
        self._rebuild_thinking_client()

    def list_thinking_models(self) -> list[str]:
        return self.list_chat_models()

    @property
    def remember_history(self) -> bool:
        return self._remember_history

    def set_remember_history(self, value: bool) -> None:
        self._remember_history = bool(value)

    @property
    def autonomy_mode(self) -> str:
        return self._autonomy_mode

    def set_autonomy_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in {"manual", "interactive", "automatic"}:
            return
        if normalized == self._autonomy_mode:
            return
        previous = self._autonomy_mode
        self._autonomy_mode = normalized
        self._state.autonomy_mode = normalized
        if normalized == "automatic":
            self._set_active_session("agentic")
        elif self._active_session_type == "agentic" and normalized in {"manual", "interactive"}:
            self._set_active_session("chat")
        self._trace("autonomy.mode", f"Switched autonomy mode {previous} -> {normalized}")

    def set_active_session_type(self, session_type: str) -> None:
        self._set_active_session(session_type)

    def clear_conversation_memory(self) -> None:
        self._memory.clear()

    @property
    def active_goal(self) -> str:
        return self._active_goal

    @property
    def active_session_type(self) -> str:
        return str(getattr(self, "_active_session_type", "chat") or "chat")

    def get_decision_trace(self, max_entries: int = 300) -> list[dict[str, str]]:
        capped = max(1, max_entries)
        items = list(self._decision_trace)
        if capped >= len(items):
            return items
        return items[-capped:]

    def clear_decision_trace(self) -> None:
        self._decision_trace.clear()

    @property
    def emergency_hotkey(self) -> str:
        return self._settings.actions.emergency_hotkey

    @property
    def emergency_stop_active(self) -> bool:
        return self._action_executor.emergency_stopped

    def reset_emergency_stop(self) -> None:
        self._action_executor.reset_emergency_stop()

    @property
    def has_pending_action(self) -> bool:
        return self._action_executor.has_pending_action

    @property
    def pending_action_description(self) -> str:
        pending = self._action_executor.pending_action
        if pending is None:
            return "none"
        if pending.kind == "click":
            return f"click x={pending.x} y={pending.y} conf={round(pending.confidence, 2)}"
        if pending.kind == "type_text":
            text = (pending.text or "").strip()
            preview = text if len(text) <= 32 else f"{text[:29]}..."
            return (
                "type_text "
                f"chars={len(text)} preview='{preview}' conf={round(pending.confidence, 2)}"
            )
        if pending.kind == "scroll":
            mode = str(pending.text or "down:8").strip() or "down:8"
            if pending.x is not None and pending.y is not None:
                return f"scroll mode={mode} at=({pending.x}, {pending.y}) conf={round(pending.confidence, 2)}"
            return f"scroll mode={mode} conf={round(pending.confidence, 2)}"
        if pending.kind == "window_state":
            state = str(pending.text or "restore").strip() or "restore"
            return f"window_state={state} hwnd={pending.hwnd} conf={round(pending.confidence, 2)}"
        return pending.kind

    def approve_pending_action(self) -> tuple[str, str | None]:
        result = self._action_executor.approve_pending_action()
        self._trace("action.confirmation", result.message)
        followups: list[str] = []
        followup = build_post_action_followup(
            result,
            require_confirmation=self._settings.actions.require_confirmation,
        )
        if followup:
            followups.append(followup)

        if result.executed and self._settings.actions.require_confirmation:
            reading_followup = self._continue_reading_after_approval()
            if reading_followup:
                followups.append(reading_followup)

        combined = "\n\n".join(part.strip() for part in followups if str(part).strip())
        return result.message, (combined if combined else None)

    @property
    def agentic_narration_level(self) -> str:
        level = str(getattr(self._settings.autonomy, "agentic_narration_level", "summary") or "summary").strip().lower()
        return level if level in {"full", "summary", "off"} else "summary"

    def tts_text_for_followup(self, followup: str) -> str:
        level = self.agentic_narration_level
        if level == "off":
            return ""
        if level == "full" and "agentic continuation completed" in str(followup or "").lower():
            return "Agentic continuation completed."
        return self.summarize_followup_for_tts(followup)

    @staticmethod
    def summarize_followup_for_tts(followup: str, *, max_steps: int = 3) -> str:
        source = strip_action_meta_for_tts(str(followup or "")).strip()
        if not source:
            return ""

        progress_match = re.search(r"progress:\s*(\d+)\s*/\s*(\d+)", source, flags=re.IGNORECASE)
        step_matches = re.findall(
            r"^[-*\s]*step\s+(\d+)\s*:\s*(.+)$",
            source,
            flags=re.IGNORECASE | re.MULTILINE,
        )

        chunks: list[str] = []
        if "agentic continuation" in source.lower():
            chunks.append("Agentic continuation update.")
        if progress_match:
            done, total = progress_match.group(1), progress_match.group(2)
            chunks.append(f"Progress {done} of {total} autonomous steps.")

        for index, (step_no, detail) in enumerate(step_matches[: max(1, int(max_steps))], start=1):
            _ = index
            cleaned = " ".join(str(detail).split())
            if len(cleaned) > 90:
                cleaned = cleaned[:87].rstrip() + "..."
            chunks.append(f"Step {step_no}: {cleaned}.")

        if chunks:
            return " ".join(chunks).strip()

        compact = " ".join(source.split())
        if len(compact) > 220:
            compact = compact[:217].rstrip() + "..."
        return compact

    def reject_pending_action(self) -> str:
        result = self._action_executor.reject_pending_action()
        self._trace("action.confirmation", result.message)
        return result.message

    def start_action_hotkey_listener(self) -> bool:
        return self._action_hotkey_listener.start()

    def stop_action_hotkey_listener(self) -> None:
        self._action_hotkey_listener.stop()

    def shutdown(self) -> None:
        self.stop_action_hotkey_listener()
        for entry in self._mcp_servers:
            client = entry.get("client") if isinstance(entry, dict) else None
            if client is None:
                continue
            try:
                client.stop()
            except Exception:
                pass
        self._mcp_servers = []

    def get_last_metrics(self) -> dict[str, float | str]:
        return dict(self._last_metrics)

    def get_average_metrics(self) -> dict[str, float | str]:
        if not self._metrics_history:
            return {
                "window": 0,
                "capture_ms": 0.0,
                "stt_ms": 0.0,
                "llm_ms": 0.0,
                "tts_ms": 0.0,
                "total_ms": 0.0,
            }

        def avg(key: str) -> float:
            values = [float(item.get(key, 0.0)) for item in self._metrics_history]
            return round(sum(values) / max(1, len(values)), 1)

        return {
            "window": len(self._metrics_history),
            "capture_ms": avg("capture_ms"),
            "stt_ms": avg("stt_ms"),
            "llm_ms": avg("llm_ms"),
            "tts_ms": avg("tts_ms"),
            "total_ms": avg("total_ms"),
        }

    def get_reading_status(self) -> dict[str, bool | int | str]:
        reading = self._session_handlers.get("reading")
        if reading is None:
            return {
                "active": False,
                "window": "",
                "chunks": 0,
                "scroll_steps": 0,
                "max_scroll_steps": 0,
            }
        return reading.get_status()

    def get_agentic_status(self) -> dict[str, bool | int | str]:
        agentic = self._session_handlers.get("agentic")
        if agentic is None:
            return {
                "active": False,
                "objective": "",
                "auto_steps": 0,
                "max_auto_steps": 0,
            }
        return agentic.get_status()

    def get_mcp_runtime_status(self) -> dict[str, object]:
        cfg = self._tooling_config.tool_settings("mcp")
        enabled = bool(cfg.get("enabled", False))
        auto_restart = bool(cfg.get("auto_restart", True))
        restart_backoff_seconds = max(1.0, float(cfg.get("restart_backoff_seconds", 4.0)))
        max_restart_attempts = max(1, int(cfg.get("max_restart_attempts", 5)))

        servers: list[dict[str, object]] = []
        for index, entry in enumerate(self._mcp_servers, start=1):
            if not isinstance(entry, dict):
                continue
            client = entry.get("client")
            transport = str(entry.get("transport", "stdio")).strip() or "stdio"
            if (
                client is not None
                and hasattr(client, "get_runtime_status")
                and callable(getattr(client, "get_runtime_status", None))
            ):
                try:
                    client_status = client.get_runtime_status()
                except Exception as exc:
                    client_status = {
                        "connected": False,
                        "server_name": "",
                        "server_version": "",
                        "protocol_version": "",
                        "capability_keys": [],
                        "tool_names": [],
                        "tool_count": 0,
                        "command": str(entry.get("command", "")).strip(),
                        "url": str(entry.get("url", "")).strip(),
                        "args": list(entry.get("args", [])),
                        "framing_mode": str(entry.get("framing_mode", "")),
                        "error": str(exc),
                    }

                connected = bool(client_status.get("connected", False))
                restart_attempts = int(entry.get("restart_attempts", 0) or 0)
                last_restart_ts = float(entry.get("last_restart_ts", 0.0) or 0.0)
                if (
                    auto_restart
                    and (not connected)
                    and restart_attempts < max_restart_attempts
                    and (time.time() - last_restart_ts) >= restart_backoff_seconds
                ):
                    entry["last_restart_ts"] = time.time()
                    entry["restart_attempts"] = restart_attempts + 1
                    try:
                        restarted = bool(client.start())
                    except Exception as exc:
                        restarted = False
                        self._trace(
                            "mcp.error",
                            f"MCP server restart threw exception for '{entry.get('name', 'unknown')}': {exc}",
                        )
                    if restarted:
                        entry["error"] = ""
                        self._trace(
                            "mcp.start",
                            (
                                "Restarted MCP server "
                                f"'{entry.get('name', 'unknown')}' "
                                f"(attempt={entry['restart_attempts']}/{max_restart_attempts})."
                            ),
                        )
                        try:
                            client_status = client.get_runtime_status()
                        except Exception:
                            pass
                    else:
                        entry["error"] = "restart failed"
            else:
                client_status = {
                    "connected": False,
                    "server_name": "",
                    "server_version": "",
                    "protocol_version": "",
                    "capability_keys": [],
                    "tool_names": [],
                    "tool_count": 0,
                    "command": str(entry.get("command", "")).strip(),
                    "url": str(entry.get("url", "")).strip(),
                    "args": list(entry.get("args", [])),
                    "framing_mode": str(entry.get("framing_mode", "")),
                    "error": str(entry.get("error", "")).strip(),
                }

            servers.append(
                {
                    "id": str(entry.get("id", f"mcp-{transport}-{index}")),
                    "transport": transport,
                    "connected": bool(client_status.get("connected", False)),
                    "command": str(client_status.get("command", "")).strip(),
                    "url": str(client_status.get("url", "")).strip(),
                    "args": [str(item) for item in client_status.get("args", []) if str(item).strip()],
                    "framing_mode": str(client_status.get("framing_mode", "")).strip(),
                    "server_name": str(client_status.get("server_name", "")).strip(),
                    "server_version": str(client_status.get("server_version", "")).strip(),
                    "protocol_version": str(client_status.get("protocol_version", "")).strip(),
                    "capabilities": [str(item) for item in client_status.get("capability_keys", []) if str(item).strip()],
                    "tool_count": int(client_status.get("tool_count", 0) or 0),
                    "tool_names": [str(item) for item in client_status.get("tool_names", []) if str(item).strip()],
                    "error": str(client_status.get("error", "")).strip(),
                    "configured_name": str(entry.get("name", "")).strip(),
                    "configured_prefix": str(entry.get("prefix", "")).strip(),
                    "source": str(entry.get("source", "")).strip(),
                    "restart_attempts": int(entry.get("restart_attempts", 0) or 0),
                }
            )

        connected_count = sum(1 for item in servers if bool(item.get("connected", False)))
        return {
            "enabled": enabled,
            "server_count": len(servers),
            "connected_count": connected_count,
            "auto_restart": auto_restart,
            "max_restart_attempts": max_restart_attempts,
            "servers": servers,
        }

    def reset_latency_metrics(self) -> None:
        self._last_metrics = {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
        }
        self._metrics_history.clear()

    def _set_last_metrics(self, metrics: dict[str, float | str]) -> None:
        self._last_metrics = metrics
        self._metrics_history.append(dict(metrics))

    def get_conversation_memory(self, max_entries: int = 200) -> list[dict[str, str]]:
        return self._history_entries(limit=max_entries)

    @staticmethod
    def _coerce_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _preview_tool_value(value: object) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            text = value.strip().replace("\n", " ")
            return repr(text if len(text) <= 80 else f"{text[:77]}...")
        if hasattr(value, "shape"):
            return f"<image shape={getattr(value, 'shape', '?')}>"
        if isinstance(value, dict):
            return f"<dict keys={len(value)}>"
        if isinstance(value, list):
            return f"<list len={len(value)}>"
        return f"<{type(value).__name__}>"

    def _preview_tool_args(self, args: dict[str, object]) -> str:
        if not args:
            return "{}"
        parts: list[str] = []
        for key in sorted(args.keys()):
            if len(parts) >= 6:
                parts.append("...")
                break
            parts.append(f"{key}={self._preview_tool_value(args[key])}")
        return "{" + ", ".join(parts) + "}"

    def _invoke_tool(
        self,
        name: str,
        *,
        args: dict[str, object] | None = None,
        cancel_token: Callable[[], bool] | None = None,
    ):
        safe_args = dict(args or {})
        self._trace("tool.invoke", f"{name} args={self._preview_tool_args(safe_args)}")
        started = time.perf_counter()
        try:
            result = self._tool_executor.invoke(
                name,
                args=safe_args,
                context=self._tool_context,
                cancel_token=cancel_token,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
            log_handled_exception(exc, context=f"session.tool_invoke:{name}")
            self._trace("tool.error", f"{name} invoke crashed ms={duration_ms}: {exc}")
            return ToolResult(
                success=False,
                duration_ms=duration_ms,
                error=ToolError(
                    code="tool_invoke_exception",
                    message=f"Tool invocation crashed for '{name}': {exc}",
                ),
            )
        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if result.success:
            data_keys = list(result.data.keys()) if isinstance(result.data, dict) else []
            keys_preview = ",".join(data_keys[:6]) if data_keys else "none"
            self._trace("tool.result", f"{name} success ms={duration_ms} keys={keys_preview}")
        else:
            if result.requires_confirmation:
                self._trace("tool.confirmation", f"{name} confirmation required")
            message = result.error.message if result.error else "unknown error"
            self._trace("tool.error", f"{name} failed ms={duration_ms}: {message}")
        return result

    def _history_messages(self, *, limit: int, offset: int = 0) -> list[dict[str, str]]:
        result = self._invoke_tool(
            "history.read_messages",
            args={"limit": max(1, int(limit)), "offset": max(0, int(offset))},
        )
        if result.success:
            messages = result.data.get("messages", [])
            if isinstance(messages, list):
                normalized: list[dict[str, str]] = []
                for item in messages:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role", "")).strip().lower()
                    content = str(item.get("content", "")).strip()
                    if role in {"user", "assistant"} and content:
                        normalized.append({"role": role, "content": content})
                return normalized
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        fallback_entries = self._memory.recent_entries(max_entries=safe_limit + safe_offset)
        if safe_offset:
            if len(fallback_entries) <= safe_offset:
                return []
            end = len(fallback_entries) - safe_offset
            start = max(0, end - safe_limit)
            fallback_entries = fallback_entries[start:end]
        else:
            fallback_entries = fallback_entries[-safe_limit:]

        return [
            {"role": item.role, "content": item.content}
            for item in fallback_entries
            if item.role in {"user", "assistant"} and item.content
        ]

    def _history_entries(self, *, limit: int, offset: int = 0) -> list[dict[str, str]]:
        result = self._invoke_tool(
            "history.read_entries",
            args={"limit": max(1, int(limit)), "offset": max(0, int(offset))},
        )
        if result.success:
            entries = result.data.get("entries", [])
            if isinstance(entries, list):
                normalized: list[dict[str, str]] = []
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role", "")).strip().lower()
                    content = str(item.get("content", "")).strip()
                    timestamp = str(item.get("timestamp", "")).strip()
                    if role in {"user", "assistant"} and content:
                        normalized.append(
                            {
                                "role": role,
                                "content": content,
                                "timestamp": timestamp,
                            }
                        )
                return normalized

        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        fallback_entries = self._memory.recent_entries(max_entries=safe_limit + safe_offset)
        if safe_offset:
            if len(fallback_entries) <= safe_offset:
                return []
            end = len(fallback_entries) - safe_offset
            start = max(0, end - safe_limit)
            fallback_entries = fallback_entries[start:end]
        else:
            fallback_entries = fallback_entries[-safe_limit:]

        return [
            {"role": item.role, "content": item.content, "timestamp": item.timestamp}
            for item in fallback_entries
        ]

    def _history_summary(self, *, limit: int, max_chars: int, offset: int = 0) -> str:
        result = self._invoke_tool(
            "history.read_summary",
            args={
                "limit": max(1, int(limit)),
                "offset": max(0, int(offset)),
                "max_chars": max(80, int(max_chars)),
            },
        )
        if result.success:
            summary = str(result.data.get("summary", "")).strip()
            if summary:
                return summary

        entries = self._history_entries(limit=limit, offset=offset)
        if not entries:
            return ""
        parts: list[str] = []
        for item in entries[-3:]:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                trimmed = content if len(content) <= 120 else f"{content[:117].rstrip()}..."
                parts.append(f"{role}: {trimmed}")
        fallback = " | ".join(parts)
        return fallback[: max(80, int(max_chars))].strip()

    def _persona_compact_notes(self) -> None:
        if not self._persona_compact_enabled:
            return
        result = self._invoke_tool(
            "persona.compact_notes",
            args={
                "max_notes": self._persona_compact_max_notes,
                "max_chars_per_note": self._persona_compact_max_chars,
            },
        )
        if result.success:
            removed = int(result.data.get("removed_count", 0) or 0)
            if removed > 0:
                self._trace("persona.compact", f"Compacted persona notes: removed={removed}")

    def _persona_filter_notes(self, *, focus_text: str) -> None:
        if not self._persona_filter_enabled:
            return
        result = self._invoke_tool(
            "persona.filter_notes",
            args={
                "max_notes": self._persona_filter_max_notes,
                "min_chars": self._persona_filter_min_chars,
                "remove_generic_user_said": self._persona_filter_remove_generic_user_said,
                "focus_text": focus_text,
            },
        )
        if result.success:
            removed = int(result.data.get("removed_count", 0) or 0)
            if removed > 0:
                self._trace("persona.filter", f"Filtered persona notes: removed={removed}")

    def chat_once(self, user_text: str) -> str:
        return self.chat_once_streaming(user_text=user_text, mode="typed")

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        user_vocal_tone: str | None = None,
        on_token: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
        mode: str = "typed",
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
    ) -> str:
        turn_start = time.perf_counter()
        self._trace("pipeline.turn.start", f"mode={mode}")
        self._tool_executor.reset_turn_budget()
        raw_user_text = str(user_text or "")
        user_text = sanitize_user_text(raw_user_text)
        if raw_user_text.strip() and not user_text:
            self._trace("pipeline.input.drop", "Input became empty after sanitization")
            self._set_last_metrics(
                {
                    "mode": mode,
                    "capture_ms": round(capture_ms, 1),
                    "stt_ms": round(stt_ms, 1),
                    "llm_ms": 0.0,
                    "tts_ms": 0.0,
                    "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
                }
            )
            return "I could not parse that clearly. Please say it again in plain words."

        if raw_user_text != user_text:
            self._trace("stt.clean", f"Sanitized user text ({len(raw_user_text)} -> {len(user_text)} chars).")

        steering_response = self._handle_steering_command(user_text)
        if steering_response is not None:
            self._set_last_metrics(
                {
                    "mode": mode,
                    "capture_ms": round(capture_ms, 1),
                    "stt_ms": round(stt_ms, 1),
                    "llm_ms": 0.0,
                    "tts_ms": 0.0,
                    "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
                }
            )
            return steering_response

        if self._stop_agentic_for_emergency(source="turn_start"):
            self._set_last_metrics(
                {
                    "mode": mode,
                    "capture_ms": round(capture_ms, 1),
                    "stt_ms": round(stt_ms, 1),
                    "llm_ms": 0.0,
                    "tts_ms": 0.0,
                    "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
                }
            )
            return (
                "Emergency stop is active, so I stopped agentic mode and switched to chat. "
                "Reset emergency stop before asking for automatic continuation again."
            )

        try:
            persona_changed = self._persona_update_from_user_text(user_text)
            if persona_changed:
                self._persona_filter_notes(focus_text=user_text)
                self._persona_compact_notes()
                self._apply_persona_runtime_preferences()
        except Exception as exc:
            log_handled_exception(exc, context="session.persona_update")
            self._trace("persona.error", f"Persona update failed: {exc}")

        persona_snapshot = self._persona_snapshot(max_notes=6)

        try:
            self._update_goal_from_conversation(user_text=user_text)
        except Exception as exc:
            log_handled_exception(exc, context="session.goal_update")
            self._trace("autonomy.goal.error", f"Goal update failed: {exc}")

        try:
            autonomy_plan = self._plan_turn_autonomy(user_text=user_text)
        except Exception as exc:
            log_handled_exception(exc, context="session.autonomy_plan")
            self._trace("autonomy.plan.error", f"Plan generation failed: {exc}")
            autonomy_plan = TurnAutonomyPlan(
                strategy="Respond naturally, concise first, ask one focused follow-up when useful.",
                should_use_screen=False,
                should_plan_action=False,
                ask_followup=True,
                confidence=0.4,
                action_intent="",
            )
        self._route_session_for_turn(user_text=user_text)
        session_signals = self._active_session.detect_turn_signals(user_text)
        screen_intent = self._is_screen_intent(user_text)
        reading_intent = bool(session_signals.wants_screen_context)
        continue_reading = bool(session_signals.wants_continue)
        orchestration_plan = self._plan_turn_orchestration(
            user_text=user_text,
            autonomy_plan=autonomy_plan,
            screen_intent=screen_intent,
            reading_intent=reading_intent,
            continue_reading=continue_reading,
        )
        autonomy_plan.strategy = orchestration_plan.strategy

        screen_text = None
        should_capture = False
        decision_source = "none"
        if orchestration_plan.should_capture_screen and self._state.screen_enabled:
            should_capture = True
            decision_source = "orchestrator"
        elif not orchestration_plan.should_capture_screen:
            should_capture, decision_source = self._should_capture_screen(user_text=user_text)

        if orchestration_plan.has_operation("continue_session") and self._active_session.is_active():
            should_capture = True
            decision_source = "reading_session"

        if should_capture:
            screen_text = self._capture_screen_text(decision_source=decision_source)
            self._active_session.on_screen_text(
                user_text=user_text,
                screen_text=screen_text,
                foreground_window_title=self._foreground_window_title,
                trace=self._trace,
            )
        elif decision_source == "disabled":
            self._trace(
                "screen.capture",
                "Screen context is disabled. Enable 'Screen Context' to allow OCR capture.",
            )
        elif decision_source != "none":
            self._trace(
                "screen.capture",
                f"Skipped screen capture (reason={decision_source}).",
            )

        if screen_intent and not self._state.screen_enabled:
            self._set_last_metrics(
                {
                    "mode": mode,
                    "capture_ms": round(capture_ms, 1),
                    "stt_ms": round(stt_ms, 1),
                    "llm_ms": 0.0,
                    "tts_ms": 0.0,
                    "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
                }
            )
            return (
                "I can’t read your screen right now because Screen Context is OFF. "
                "Enable 'Screen Context' and click 'Apply Sources', then ask again."
            )

        if screen_intent and should_capture and not screen_text:
            self._set_last_metrics(
                {
                    "mode": mode,
                    "capture_ms": round(capture_ms, 1),
                    "stt_ms": round(stt_ms, 1),
                    "llm_ms": 0.0,
                    "tts_ms": 0.0,
                    "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
                }
            )
            return (
                "I tried to read the screen, but OCR did not return readable text. "
                "Try putting clear/high-contrast text in the active monitor and run Test OCR."
            )

        # Build structured screen context: give the main LLM real element coordinates
        # so it never has to invent positions.
        if screen_text and self._last_screen_elements:
            el_lines = [
                f'- "{e["text"]}" at ({e["cx"]}, {e["cy"]})'
                for e in self._last_screen_elements[:60]
                if str(e.get("text", "")).strip()
            ]
            if el_lines:
                screen_context_for_llm = (
                    "Detected UI elements (label → click coordinates):\n"
                    + "\n".join(el_lines)
                    + "\n\nFull text: " + screen_text
                )
            else:
                screen_context_for_llm = screen_text
        else:
            screen_context_for_llm = screen_text

        # Prepend open-windows / foreground-window context
        win_header_parts: list[str] = []
        if self._foreground_window_title:
            win_header_parts.append(f"Foreground window: {self._foreground_window_title}")
        if self._open_windows:
            win_lines = []
            for w in self._open_windows[:15]:
                prefix = "[active] " if w.get("is_foreground") else ""
                win_lines.append(f"  - {prefix}{w['title']}")
            win_header_parts.append("Open windows:\n" + "\n".join(win_lines))
        if win_header_parts:
            screen_context_for_llm = "\n".join(win_header_parts) + "\n\n" + (screen_context_for_llm or "")

        reading_context = self._active_session.build_prompt_context()
        if reading_context:
            screen_context_for_llm = ((screen_context_for_llm or "").strip() + "\n\n" + reading_context).strip()

        # Build capability list so the model knows what it can do this turn.
        caps = [f"tool:{name}" for name in self._tool_executor.list_available_tools()]
        if self._settings.actions.enabled:
            caps.extend(["click (pyautogui)", "type_text (pyautogui)"])

        messages = self._turn_manager.build_chat_messages(
            TurnInput(
                user_text=user_text,
                session_type=self._active_session_type,
                user_vocal_tone=user_vocal_tone,
                screen_text=screen_context_for_llm,
                persona_background=str(persona_snapshot.get("assistant_background", "")),
                persona_user_notes=list(persona_snapshot.get("user_notes", [])),
                persona_response_style=str(persona_snapshot.get("response_style", "balanced")),
                memory_messages=(
                    self._history_messages(
                        limit=(self._history_summary_tail_limit if self._history_summary_enabled else self._history_prompt_limit)
                    )
                    if self._remember_history
                    else None
                ),
                memory_summary=(
                    self._history_summary(
                        limit=self._history_summary_limit,
                        max_chars=self._history_summary_max_chars,
                    )
                    if (self._remember_history and self._history_summary_enabled)
                    else None
                ),
                assistant_strategy=autonomy_plan.strategy,
                active_goal=self._active_goal,
                goal_description=self._active_goal_description,
                available_capabilities=caps or None,
            )
        )

        generation_options: dict[str, object] = {
            "num_predict": self._response_num_predict(),
        }

        stream_tts_used = False
        stream_tts_enqueued_chunks = 0

        try:
            if on_generation_status:
                on_generation_status("AI is generating response...")
            self._trace("pipeline.llm.start", f"mode={mode} model={self.chat_model}")
            llm_started = time.perf_counter()
            stream_tts_buffer = ""
            enqueue_tts = getattr(self._tts, "enqueue_async", None)
            # Reliability guard: keep token streaming in UI, but use full-response TTS playback.
            can_stream_tts = False
            if on_token is None:
                response = sanitize_assistant_text(
                    self._ollama.chat(messages, options=generation_options)
                )
            else:
                pieces: list[str] = []
                for token in self._ollama.chat_stream(messages, options=generation_options):
                    if stop_requested and stop_requested():
                        break
                    safe_token = sanitize_assistant_text(
                        token,
                        preserve_newlines=False,
                        trim=False,
                    )
                    if not safe_token:
                        continue
                    pieces.append(safe_token)
                    on_token(safe_token)
                    if can_stream_tts:
                        stream_tts_buffer += safe_token
                        ready_chunks, stream_tts_buffer = drain_tts_stream_chunks(
                            stream_tts_buffer,
                            flush=False,
                        )
                        for chunk in ready_chunks:
                            tts_chunk = prepare_tts_text(chunk)
                            if tts_chunk and callable(enqueue_tts):
                                queued_ok = bool(enqueue_tts(tts_chunk))
                                if queued_ok:
                                    stream_tts_used = True
                                    stream_tts_enqueued_chunks += 1
                                else:
                                    self._trace("pipeline.tts.enqueue", "chunk enqueue failed")
                response = "".join(pieces).strip()
                if can_stream_tts and stream_tts_buffer.strip():
                    ready_chunks, stream_tts_buffer = drain_tts_stream_chunks(
                        stream_tts_buffer,
                        flush=True,
                    )
                    for chunk in ready_chunks:
                        tts_chunk = prepare_tts_text(chunk)
                        if tts_chunk and callable(enqueue_tts):
                            queued_ok = bool(enqueue_tts(tts_chunk))
                            if queued_ok:
                                stream_tts_used = True
                                stream_tts_enqueued_chunks += 1
                            else:
                                self._trace("pipeline.tts.enqueue", "flush chunk enqueue failed")
            llm_ms = (time.perf_counter() - llm_started) * 1000.0
            self._trace(
                "pipeline.llm.done",
                f"mode={mode} chars={len(response)} llm_ms={round(llm_ms, 1)}",
            )
        except Exception as exc:
            self._trace("pipeline.llm.error", str(exc))
            self._set_last_metrics({
                "mode": mode,
                "capture_ms": round(capture_ms, 1),
                "stt_ms": round(stt_ms, 1),
                "llm_ms": 0.0,
                "tts_ms": 0.0,
                "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
            })
            return (
                "I could not reach Ollama. Please make sure it is running and the model is available. "
                f"Details: {exc}"
            )

        explicit_reaction, response = extract_tts_reaction_tag(response)
        if explicit_reaction:
            self._trace("pipeline.tts.reaction", f"explicit={explicit_reaction}")

        if stop_requested and stop_requested():
            self._trace("pipeline.turn.stopped", f"mode={mode} stop_requested=true")
            return response

        self._trace("pipeline.action.start", f"mode={mode}")
        try:
            if orchestration_plan.should_plan_action:
                if not self._should_allow_action_execution(
                    session_type=self._active_session_type,
                    user_text=user_text,
                ):
                    self._trace(
                        "pipeline.action.skip",
                        "Chat turn has no explicit action intent; skipping action execution.",
                    )
                    action_result = None
                else:
                    action_result = self._maybe_execute_action(
                        user_text=user_text,
                        assistant_reply=response,
                        screen_text=screen_text,
                        allow_planning_override=True,
                        action_intent=orchestration_plan.action_intent,
                        on_token=on_token,
                    )
            else:
                self._trace("pipeline.action.skip", "orchestrator decided no action")
                action_result = None
        except Exception as exc:
            log_handled_exception(exc, context="session.action_execution")
            self._trace("pipeline.action.error", str(exc))
            action_result = ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=True,
                requires_confirmation=False,
                message="Action pipeline failed unexpectedly. See logs for details.",
            )
        self._trace(
            "pipeline.action.done",
            "executed" if action_result is not None else "skipped",
        )

        if action_result is not None and self._settings.actions.require_confirmation:
            response = normalize_action_narration(response, action_result)

        # Keep the pure conversational text for TTS — action status lines are UI-only.
        llm_response_for_tts = strip_action_meta_for_tts(response)

        if action_result is not None and action_result.message:
            prefix = "[Action]" if action_result.executed or action_result.requires_confirmation else "[Note]"
            action_suffix = f"\n\n{prefix} {action_result.message}"
            if on_token:
                on_token(action_suffix)
            response = f"{response}{action_suffix}".strip()

            post_action_followup = build_post_action_followup(
                action_result,
                require_confirmation=self._settings.actions.require_confirmation,
            )
            if post_action_followup:
                followup_suffix = f"\n\n{post_action_followup}"
                if on_token:
                    on_token(followup_suffix)
                response = f"{response}{followup_suffix}".strip()

        if orchestration_plan.has_operation("include_session_evidence") or session_signals.wants_evidence:
            evidence_suffix = self._active_session.build_evidence_block(self._trace)
            if evidence_suffix:
                response = f"{response}\n\n{evidence_suffix}".strip()
                if on_token:
                    on_token(f"\n\n{evidence_suffix}")

        response = sanitize_assistant_text(response)

        if self._remember_history:
            try:
                self._memory.add(role="user", content=user_text)
                self._memory.add(role="assistant", content=response)
            except Exception as exc:
                log_handled_exception(exc, context="session.memory_write")
                self._trace("memory.error", f"Failed to write conversation memory: {exc}")

        tts_started = time.perf_counter()
        tts_ms = 0.0
        should_speak_full_response = bool(response and not stream_tts_used)
        if stream_tts_used and stream_tts_enqueued_chunks < 1:
            self._trace("pipeline.tts.enqueue", "stream mode used but no chunks enqueued; fallback full TTS")
            should_speak_full_response = bool(response)
        self._trace(
            "pipeline.tts.plan",
            (
                f"stream_used={stream_tts_used} "
                f"queued_chunks={stream_tts_enqueued_chunks} "
                f"full_response={'yes' if should_speak_full_response else 'no'}"
            ),
        )
        if response and stream_tts_used:
            has_pending_audio = getattr(self._tts, "has_pending_audio", None)
            if callable(has_pending_audio):
                try:
                    should_speak_full_response = not bool(has_pending_audio())
                except Exception:
                    should_speak_full_response = True

        if should_speak_full_response:
            try:
                tts_text = prepare_tts_text(strip_action_meta_for_tts(llm_response_for_tts))
                if tts_text:
                    reaction = explicit_reaction or infer_tts_reaction(llm_response_for_tts)
                    self._tts.speak_async(tts_text, reaction=reaction)
                    self._trace("pipeline.tts.reaction", f"reaction={reaction}")
                    self._trace("pipeline.tts.speak", f"chars={len(tts_text)}")
            except Exception as exc:
                self._trace("tts.error", f"TTS speak failed: {exc}")
                self._trace("pipeline.tts.error", str(exc))
            tts_ms = (time.perf_counter() - tts_started) * 1000.0
        else:
            self._trace("pipeline.tts.speak", "stream queue active")

        self._set_last_metrics({
            "mode": mode,
            "capture_ms": round(capture_ms, 1),
            "stt_ms": round(stt_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "tts_ms": round(tts_ms, 1),
            "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
        })
        self._trace("pipeline.turn.done", f"mode={mode} total_ms={round((time.perf_counter() - turn_start) * 1000.0, 1)}")
        return response

    def _apply_persona_runtime_preferences(self) -> None:
        snapshot = self._persona_snapshot(max_notes=1)
        length_scale = float(snapshot.get("tts_length_scale", 1.0) or 1.0)
        set_length_scale = getattr(self._tts, "set_length_scale", None)
        if callable(set_length_scale):
            try:
                set_length_scale(length_scale)
            except Exception:
                pass

    def _response_num_predict(self) -> int:
        style = str(self._persona_snapshot(max_notes=1).get("response_style", "balanced"))
        if style == "concise":
            return 96
        if style == "detailed":
            return 220
        return 140

    def _persona_update_from_user_text(self, user_text: str) -> bool:
        result = self._invoke_tool(
            "persona.update_from_user_text",
            args={"user_text": user_text},
        )
        if not result.success:
            return False
        return bool(result.data.get("changed", False))

    def _persona_snapshot(self, *, max_notes: int) -> dict:
        result = self._invoke_tool(
            "persona.read_snapshot",
            args={"max_notes": max_notes},
        )
        if not result.success:
            return {
                "assistant_background": self._settings.assistant.background,
                "user_notes": [],
                "response_style": "balanced",
                "tts_length_scale": 1.0,
            }
        return result.data

    def _plan_turn_autonomy(self, *, user_text: str) -> TurnAutonomyPlan:
        if not self._settings.autonomy.enabled:
            return TurnAutonomyPlan(
                strategy="Respond naturally, concise first, ask one focused follow-up when useful.",
                should_use_screen=False,
                should_plan_action=False,
                ask_followup=True,
                confidence=0.4,
                action_intent="",
            )

        if self._autonomy_mode == "manual":
            return TurnAutonomyPlan(
                strategy="Respond naturally and avoid autonomous actions unless explicitly requested.",
                should_use_screen=False,
                should_plan_action=False,
                ask_followup=True,
                confidence=0.8,
                action_intent="",
            )

        plan = self._autonomy_planner.plan_turn_autonomy(
            user_text=user_text,
            active_goal=self._active_goal,
            active_goal_description=self._active_goal_description,
            max_strategy_chars=int(self._settings.autonomy.max_strategy_chars),
            proactive_conversation=bool(self._settings.autonomy.proactive_conversation),
            allow_action_suggestions=bool(self._settings.autonomy.allow_action_suggestions),
            allow_proactive_actions=bool(self._settings.autonomy.allow_proactive_actions),
            actions_enabled=bool(self._settings.actions.enabled),
            available_tool_names=self._tool_executor.list_available_tools(),
        )

        if self._autonomy_mode == "automatic":
            plan.ask_followup = False
            plan.should_use_screen = bool(self._state.screen_enabled)
            plan.should_plan_action = bool(self._settings.actions.enabled)
            if plan.should_plan_action and not plan.action_intent:
                plan.action_intent = "Execute the next best UI step for the active objective."
            self._trace("autonomy.mode", "Automatic mode forced action planning and disabled follow-up prompt.")
        return plan

    def _plan_turn_orchestration(
        self,
        *,
        user_text: str,
        autonomy_plan: TurnAutonomyPlan,
        screen_intent: bool,
        reading_intent: bool,
        continue_reading: bool,
    ) -> TurnOrchestratorPlan:
        return self._turn_orchestrator.plan_turn(
            user_text=user_text,
            active_goal=self._active_goal,
            autonomy_plan=autonomy_plan,
            screen_intent=screen_intent,
            reading_intent=reading_intent,
            continue_reading=continue_reading,
        )

    def _update_goal_from_conversation(self, *, user_text: str) -> None:
        if not self._settings.autonomy.enabled or not self._settings.autonomy.auto_goal_switch:
            return

        inferred = self._infer_goal(user_text=user_text)
        min_conf = float(self._settings.autonomy.goal_switch_min_confidence)
        if inferred.confidence < min_conf:
            self._trace(
                "autonomy.goal",
                (
                    f"Kept goal='{self._active_goal}' "
                    f"(low confidence {round(inferred.confidence, 2)} < {round(min_conf, 2)})."
                ),
            )
            return

        resolved_session_type, session_reason = self._session_router.resolve(
            inferred_session_type=inferred.session_type,
            inferred_goal=inferred.goal,
            current_session_type=self._active_session_type,
        )
        previous_goal = self._active_goal
        previous_session_type = self._active_session_type
        goal_changed = inferred.goal != self._active_goal
        session_changed = resolved_session_type != self._active_session_type
        if not goal_changed and not session_changed:
            return

        self._active_goal = inferred.goal
        self._active_goal_description = inferred.description
        if session_changed:
            self._set_active_session(resolved_session_type)

        self._trace(
            "autonomy.goal",
            (
                f"Switched goal {previous_goal} -> {self._active_goal} "
                f"(confidence={round(inferred.confidence, 2)}; reason={inferred.reason or 'n/a'}; "
                f"desc={inferred.description or 'n/a'}; "
                f"session={previous_session_type}->{self._active_session_type} via {session_reason})"
            ),
        )

    def _infer_goal(self, *, user_text: str) -> GoalInference:
        return self._autonomy_planner.infer_goal(user_text=user_text, active_goal=self._active_goal)

    def _set_active_session(self, session_type: str) -> None:
        normalized = str(session_type or "").strip().lower()
        if normalized not in self._session_handlers:
            normalized = "chat"
        self._active_session_type = normalized
        self._active_session = self._session_handlers[normalized]
        self._state.session_type = normalized

    def _route_session_for_turn(self, *, user_text: str) -> None:
        current_signals = self._active_session.detect_turn_signals(user_text)
        if current_signals.wants_continue or current_signals.wants_screen_context or current_signals.wants_evidence:
            return

        for session_type, handler in self._session_handlers.items():
            if session_type == self._active_session_type:
                continue
            signals = handler.detect_turn_signals(user_text)
            if signals.wants_continue or signals.wants_screen_context or signals.wants_evidence:
                previous = self._active_session_type
                self._set_active_session(session_type)
                self._trace(
                    "session.route",
                    f"Switched active session {previous} -> {self._active_session_type} (intent route).",
                )
                return

    def _should_capture_screen(self, *, user_text: str) -> tuple[bool, str]:
        return self._screen_context.should_capture_screen(
            user_text=user_text,
            screen_enabled=bool(self._state.screen_enabled),
        )

    @staticmethod
    def _is_screen_intent(user_text: str) -> bool:
        return ScreenContextService.is_screen_intent(user_text)

    @staticmethod
    def _should_allow_action_execution(*, session_type: str, user_text: str) -> bool:
        if str(session_type or "").strip().lower() != "chat":
            return True
        return ActionExecutionService.has_action_intent(user_text)

    def _capture_screen_text(self, *, decision_source: str) -> str | None:
        result = self._screen_context.capture_screen_text(decision_source=decision_source)
        self._last_screen_elements = list(result.elements)
        self._foreground_window_title = str(result.foreground_window_title or "")
        self._open_windows = list(result.open_windows)
        self._all_windows = list(result.all_windows)
        return result.text

    def run_screen_ocr_diagnostic(self) -> dict[str, object]:
        return self._screen_context.run_ocr_diagnostic()

    def run_stt_diagnostic(
        self,
        *,
        seconds: float = 5.0,
        vad_filter: bool = True,
        initial_prompt: str = "",
    ) -> dict[str, object]:
        if not self._state.mic_enabled:
            return {
                "ok": False,
                "reason": "mic-disabled",
                "message": "Microphone source is disabled.",
            }

        if not self._whisper.is_available:
            return {
                "ok": False,
                "reason": "stt-unavailable",
                "message": "Whisper model is unavailable.",
            }

        duration = max(1.0, min(float(seconds), 30.0))
        capture_started = time.perf_counter()
        wav_path = self._microphone.capture_to_wav(seconds=duration)
        capture_ms = (time.perf_counter() - capture_started) * 1000.0

        prosody: ProsodyAnalysis | None = None
        try:
            stt_started = time.perf_counter()
            text = self._whisper.transcribe(
                str(wav_path),
                vad_filter=bool(vad_filter),
                initial_prompt=(str(initial_prompt).strip() or None),
            )
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
            prosody = self._analyze_prosody(wav_path=str(wav_path), text=text or "")
        finally:
            self._safe_unlink(wav_path)

        sanitized = sanitize_user_text(text or "")
        preview = " ".join((sanitized or "").split())
        if len(preview) > 180:
            preview = f"{preview[:177]}..."
        self._trace(
            "stt.mic",
            (
                f"diagnostic capture_ms={round(capture_ms, 1)} stt_ms={round(stt_ms, 1)} "
                f"chars={len(sanitized)} vad_filter={bool(vad_filter)} "
                f"text={preview or '[empty]'}"
            ),
        )

        return {
            "ok": True,
            "reason": "ok",
            "seconds": duration,
            "capture_ms": round(capture_ms, 1),
            "stt_ms": round(stt_ms, 1),
            "chars": len(sanitized),
            "vad_filter": bool(vad_filter),
            "stt_model": self.stt_model,
            "prosody_enabled": bool(self._prosody.enabled),
            "prosody": (
                {
                    "emotion": prosody.emotion,
                    "question_likely": bool(prosody.question_likely),
                    "confidence": round(float(prosody.confidence), 3),
                    "rms": round(float(prosody.rms), 5),
                    "zcr": round(float(prosody.zcr), 5),
                    "pitch_start_hz": (
                        round(float(prosody.pitch_start_hz), 1)
                        if prosody.pitch_start_hz is not None
                        else None
                    ),
                    "pitch_end_hz": (
                        round(float(prosody.pitch_end_hz), 1)
                        if prosody.pitch_end_hz is not None
                        else None
                    ),
                    "analysis_ms": round(float(prosody.analysis_ms), 2),
                }
                if prosody is not None
                else None
            ),
            "text": sanitized,
        }

    def _trace(self, stage: str, message: str) -> None:
        stage_text = str(stage)
        message_text = str(message)
        trace_buffer = getattr(self, "_decision_trace", None)
        if not isinstance(trace_buffer, deque):
            trace_buffer = deque(maxlen=500)
            self._decision_trace = trace_buffer

        trace_buffer.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "stage": stage_text,
                "message": message_text,
            }
        )
        if "error" in stage_text:
            try:
                log_event(stage_text, message_text)
            except Exception:
                pass

    def _rebuild_thinking_client(self) -> None:
        if not self._thinking_model:
            self._thinking_ollama = None
            return

        self._thinking_ollama = OllamaClient(
            OllamaSettings(
                base_url=self._settings.ollama.base_url,
                chat_model=self._thinking_model,
                temperature=0.1,
            )
        )

    @staticmethod
    def _build_tts_service(settings: AppSettings):
        provider = (settings.tts.provider or "piper").strip().lower()
        if provider == "llasa":
            return LlasaTtsService(settings.tts)
        return PiperTtsService(settings.tts)

    def _maybe_execute_action(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        allow_planning_override: bool = False,
        action_intent: str = "",
        on_token: Callable[[str], None] | None = None,
    ) -> ActionExecutionResult | None:
        return self._action_execution.maybe_execute_action(
            user_text=user_text,
            assistant_reply=assistant_reply,
            screen_text=screen_text,
            action_intent=action_intent,
            allow_planning_override=allow_planning_override,
            on_token=on_token,
        )

    def record_and_chat(
        self,
        seconds: float = 5.0,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")

        if not self._whisper.is_available:
            raise RuntimeError(
                "Whisper is not installed. Install AI extras with: pip install -e .[ai]"
            )

        capture_started = time.perf_counter()
        wav_path = self._microphone.capture_to_wav(seconds=seconds)
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        prosody: ProsodyAnalysis | None = None
        try:
            stt_started = time.perf_counter()
            text = self._whisper.transcribe(str(wav_path))
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
            prosody = self._analyze_prosody(wav_path=str(wav_path), text=text or "")
        finally:
            self._safe_unlink(wav_path)

        if not text:
            raise RuntimeError("No speech was detected from microphone audio.")

        text = sanitize_user_text(text)
        if not text:
            raise RuntimeError("No clear speech was detected from microphone audio.")

        preview = " ".join(text.strip().split())
        if len(preview) > 180:
            preview = f"{preview[:177]}..."
        self._trace("stt.mic", f"record transcribe ({len(text)} chars): {preview}")

        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            on_generation_status=on_generation_status,
            mode="record",
            capture_ms=capture_ms,
            stt_ms=stt_ms,
            user_vocal_tone=self._prosody_prompt_hint(prosody),
        )
        return text, response

    def listen_once_and_chat(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_listen_seconds: float = 18.0,
        on_token: Callable[[str], None] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str] | None:
        captured = self.capture_live_phrase(
            stop_requested=stop_requested,
            max_listen_seconds=max_listen_seconds,
            on_audio_level=on_audio_level,
            on_generation_status=on_generation_status,
        )
        if captured is None:
            return None

        wav_path, capture_ms = captured
        return self.process_live_capture(
            wav_path=wav_path,
            capture_ms=capture_ms,
            stop_requested=stop_requested,
            on_token=on_token,
            on_generation_status=on_generation_status,
        )

    def capture_live_phrase(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_listen_seconds: float = 18.0,
        on_audio_level: Callable[[float], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[Path, float] | None:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")

        if not self._whisper.is_available:
            raise RuntimeError(
                "Whisper is not installed. Install AI extras with: pip install -e .[ai]"
            )

        live_level_threshold = max(0.004, float(self._vad_level_threshold) * 0.4)
        if self._live_no_speech_streak > 0:
            relax_factor = min(0.7, 0.18 * float(self._live_no_speech_streak))
            live_level_threshold = max(0.002, live_level_threshold * (1.0 - relax_factor))
        live_end_level_threshold = max(0.004, float(self._vad_level_threshold) * 0.4)
        live_pause_base = max(0.3, float(self._vad_silence_seconds))
        live_silence_seconds = min(6.0, max(1.5, live_pause_base + 0.4))
        live_min_speech_before_stop = 1.5
        live_start_grace_seconds = 0.8
        live_max_speech_seconds = 18.0
        live_wait_for_start_seconds = 12.0
        live_use_webrtc_vad = self._live_no_speech_streak < 3

        self._trace(
            "pipeline.listen.start",
            (
                f"threshold={live_level_threshold:.4f} silence={live_silence_seconds:.2f}s "
                f"streak={self._live_no_speech_streak} webrtcvad={live_use_webrtc_vad}"
            ),
        )

        # Track the peak mic level seen during capture for diagnostic logging.
        _peak_level: list[float] = [0.0]
        _orig_on_audio_level = on_audio_level
        def _tracking_level(lvl: float) -> None:
            if lvl > _peak_level[0]:
                _peak_level[0] = lvl
            if _orig_on_audio_level:
                _orig_on_audio_level(lvl)

        capture_started = time.perf_counter()
        if on_generation_status:
            on_generation_status("listening")
        wav_path = self._microphone.capture_phrase_to_wav(
            max_seconds=max_listen_seconds,
            max_wait_for_speech_start_seconds=live_wait_for_start_seconds,
            use_webrtc_vad=live_use_webrtc_vad,
            silence_seconds_to_stop=live_silence_seconds,
            level_threshold=live_level_threshold,
            end_level_threshold=live_end_level_threshold,
            min_speech_seconds_before_stop=live_min_speech_before_stop,
            speech_start_grace_seconds=live_start_grace_seconds,
            max_seconds_after_speech_start=live_max_speech_seconds,
            stop_requested=stop_requested,
            on_speech_start=None,
            on_audio_level=_tracking_level,
        )
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        if wav_path is None:
            self._live_no_speech_streak += 1
            self._trace(
                "pipeline.listen.no_speech",
                (
                    f"capture_ms={round(capture_ms, 1)} "
                    f"peak_level={_peak_level[0]:.4f} "
                    f"threshold={live_level_threshold:.4f} "
                    f"streak={self._live_no_speech_streak}"
                ),
            )
            if on_generation_status:
                on_generation_status(f"listening (retry {self._live_no_speech_streak})")
            return None

        return wav_path, capture_ms

    def process_live_capture(
        self,
        *,
        wav_path: Path,
        capture_ms: float,
        stop_requested: Callable[[], bool] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str] | None:
        if not self._whisper.is_available:
            return None

        prosody: ProsodyAnalysis | None = None
        try:
            if on_generation_status:
                on_generation_status("transcribing")
            self._trace("pipeline.stt.start", f"capture_ms={round(capture_ms, 1)}")
            stt_started = time.perf_counter()
            text = self._whisper.transcribe(
                str(wav_path),
                vad_filter=False,
                initial_prompt=(
                    "The speaker may say technical words such as pipeline, debugging, "
                    "streaming, latency, microphone, transcription."
                ),
            )
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
            prosody = self._analyze_prosody(wav_path=str(wav_path), text=text or "")
            self._trace("pipeline.stt.done", f"stt_ms={round(stt_ms, 1)} chars={len(text or '')}")
        finally:
            self._safe_unlink(wav_path)

        if not text:
            self._live_no_speech_streak += 1
            self._trace(
                "pipeline.stt.empty",
                f"streak={self._live_no_speech_streak}",
            )
            if on_generation_status:
                on_generation_status("did not catch that, listening")
            return None

        text = sanitize_user_text(text)
        if not text:
            self._live_no_speech_streak += 1
            self._trace(
                "pipeline.stt.drop",
                f"empty after sanitize streak={self._live_no_speech_streak}",
            )
            if on_generation_status:
                on_generation_status("did not catch that, listening")
            return None

        self._live_no_speech_streak = 0

        preview = " ".join(text.strip().split())
        if len(preview) > 180:
            preview = f"{preview[:177]}..."
        self._trace("stt.mic", f"live transcribe ({len(text)} chars): {preview}")

        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            stop_requested=stop_requested,
            on_generation_status=on_generation_status,
            mode="live",
            capture_ms=capture_ms,
            stt_ms=stt_ms,
            user_vocal_tone=self._prosody_prompt_hint(prosody),
        )

        return text, response

    def _analyze_prosody(self, *, wav_path: str, text: str) -> ProsodyAnalysis | None:
        if not self._prosody.enabled:
            return None
        analysis = self._prosody.analyze_wav(wav_path, text=text)
        if analysis is None:
            return None
        self._trace(
            "stt.prosody",
            (
                f"emotion={analysis.emotion} question={analysis.question_likely} "
                f"conf={round(analysis.confidence, 2)} rms={round(analysis.rms, 4)} "
                f"zcr={round(analysis.zcr, 4)} ms={round(analysis.analysis_ms, 1)}"
            ),
        )
        return analysis

    def _prosody_prompt_hint(self, analysis: ProsodyAnalysis | None) -> str | None:
        if analysis is None or not self._prosody_include_in_prompt:
            return None
        parts = [f"emotion={analysis.emotion}"]
        if analysis.question_likely:
            parts.append("question_tone=true")
        parts.append(f"confidence={round(analysis.confidence, 2)}")
        return ", ".join(parts)

    def _continue_session_after_approval(self) -> str:
        if self._stop_agentic_for_emergency(source="continue_after_approval"):
            return (
                "Emergency stop is active, so I stopped agentic mode and switched to chat. "
                "Reset emergency stop before resuming automation."
            )

        actions_enabled = bool(getattr(self._settings.actions, "enabled", False))
        state = getattr(self, "_state", None)
        screen_enabled = bool(getattr(state, "screen_enabled", False))
        require_confirmation = lambda: bool(getattr(self._settings.actions, "require_confirmation", True))
        available_tools = (
            self._tool_executor.list_available_tools
            if hasattr(self, "_tool_executor") and self._tool_executor is not None
            else (lambda: [])
        )
        autonomy_settings = getattr(self._settings, "autonomy", None)
        narration_level = str(getattr(autonomy_settings, "agentic_narration_level", "summary") or "summary").strip().lower()
        if narration_level not in {"full", "summary", "off"}:
            narration_level = "summary"
        narrate_callback = (
            self._narrate_agentic_step
            if narration_level == "full"
            else None
        )
        runtime = SessionRuntimeContext(
            actions_enabled=actions_enabled,
            screen_enabled=screen_enabled,
            foreground_window_title=str(self._foreground_window_title or ""),
            get_require_confirmation=require_confirmation,
            set_require_confirmation=lambda value: setattr(
                self._settings.actions,
                "require_confirmation",
                bool(value),
            ),
            invoke_tool=self._invoke_tool,
            capture_screen_text=self._capture_screen_text,
            trace=self._trace,
            active_goal=str(getattr(self, "_active_goal", "") or ""),
            narration_level=narration_level,
            available_tools=available_tools,
            plan_agentic_step=self._plan_agentic_step,
            narrate=narrate_callback,
        )
        return self._active_session.continue_after_approval(runtime)

    def _stop_agentic_for_emergency(self, *, source: str) -> bool:
        if not bool(getattr(self, "emergency_stop_active", False)):
            return False
        if str(getattr(self, "_active_session_type", "chat") or "chat") != "agentic":
            return False

        stopped = False
        try:
            active_session = getattr(self, "_active_session", None)
            if active_session is not None and hasattr(active_session, "stop"):
                stopped = bool(active_session.stop(self._trace))
        except Exception as exc:
            self._trace("autonomy.safety.error", f"Failed to stop agentic session cleanly: {exc}")

        handlers = getattr(self, "_session_handlers", {})
        if isinstance(handlers, dict) and "chat" in handlers:
            self._set_active_session("chat")
        else:
            self._active_session_type = "chat"

        mode = str(getattr(self, "_autonomy_mode", "interactive") or "interactive").strip().lower()
        if mode == "automatic":
            self._autonomy_mode = "interactive"
            state = getattr(self, "_state", None)
            if state is not None:
                setattr(state, "autonomy_mode", "interactive")

        self._trace(
            "autonomy.safety",
            (
                "Emergency stop is active; agentic session halted "
                f"(source={source}, stopped={stopped})."
            ),
        )
        return True

    def _narrate_agentic_step(self, text: str) -> None:
        spoken = " ".join(str(text or "").split()).strip()
        if not spoken:
            return
        if len(spoken) > 220:
            spoken = spoken[:217].rstrip() + "..."
        self.speak_text(spoken)

    def _plan_agentic_step(
        self,
        objective: str,
        screen_text: str | None,
        recent_events: list[dict[str, Any]],
        remaining_steps: int,
    ) -> dict[str, Any]:
        available_tools = self._tool_executor.list_available_tools()
        objective_text = str(objective or "").strip() or str(self._active_goal or "").strip() or "general_assistance"
        compact_events = recent_events[-4:]
        screen_preview = str(screen_text or "")[:1800]
        self._trace(
            "agentic.plan.input",
            (
                f"objective={objective_text!r} remaining={max(1, int(remaining_steps))} "
                f"tools={len(available_tools)} events={len(compact_events)} screen_chars={len(screen_preview)}"
            ),
        )

        if not available_tools:
            self._trace("agentic.plan.output", "done=true reason=no_tools")
            return {
                "done": True,
                "progress_note": "No tools available, stopping autonomous continuation.",
                "next_tool": "",
                "next_args": {},
            }

        try:
            raw = (self._thinking_ollama or self._ollama).chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an agentic continuation planner. "
                            "Choose exactly one best next tool call to advance the objective, "
                            "or mark done when objective appears complete. "
                            "Return exactly one JSON object with keys: "
                            "done (bool), progress_note (str), next_tool (str), next_args (object). "
                            "Rules: use only tools listed in AVAILABLE_TOOLS; do not invent tool names; "
                            "if uncertain, prefer observational tools before mutating actions."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"OBJECTIVE:\n{objective_text}\n\n"
                            f"ACTIVE_GOAL:\n{self._active_goal}\n\n"
                            f"REMAINING_STEPS:{max(1, int(remaining_steps))}\n\n"
                            f"AVAILABLE_TOOLS:\n" + "\n".join(f"- {name}" for name in available_tools[:120]) + "\n\n"
                            f"RECENT_EVENTS_JSON:\n{compact_events}\n\n"
                            f"SCREEN_CONTEXT_PREVIEW:\n{screen_preview or '[none]'}"
                        ),
                    },
                ]
            )
            payload = extract_json_object(raw)
        except Exception as exc:
            self._trace("agentic.plan.error", f"planner unavailable: {exc}")
            payload = {}

        if not isinstance(payload, dict):
            payload = {}

        done = bool(payload.get("done", False))
        progress_note = str(payload.get("progress_note", "")).strip()
        next_tool = str(payload.get("next_tool", "")).strip()
        next_args = payload.get("next_args", {})
        if not isinstance(next_args, dict):
            next_args = {}

        if not done and next_tool and next_tool not in available_tools:
            self._trace("agentic.plan.error", f"planner chose unavailable tool: {next_tool}")
            return {
                "done": True,
                "progress_note": (
                    "Planner selected unavailable tool; stopping to avoid invalid execution."
                ),
                "next_tool": "",
                "next_args": {},
            }

        if not done and not next_tool:
            # Fallback: prefer non-mutating context refresh if available.
            for candidate in ("mcp.windows.Snapshot", "ocr.extract_details", "ocr.extract_elements"):
                if candidate in available_tools:
                    self._trace("agentic.plan.output", f"done=false fallback_tool={candidate}")
                    return {
                        "done": False,
                        "progress_note": progress_note or "Refreshing context before next action.",
                        "next_tool": candidate,
                        "next_args": {},
                    }
            self._trace("agentic.plan.output", "done=true reason=no_fallback")
            return {
                "done": True,
                "progress_note": progress_note or "No safe fallback tool available.",
                "next_tool": "",
                "next_args": {},
            }

        self._trace(
            "agentic.plan.output",
            f"done={done} next_tool={next_tool or '[none]'} note={progress_note or '[none]'}",
        )

        return {
            "done": done,
            "progress_note": progress_note,
            "next_tool": next_tool,
            "next_args": next_args,
        }

    def _continue_reading_after_approval(self) -> str:
        return self._continue_session_after_approval()

    def _handle_steering_command(self, user_text: str) -> str | None:
        text = str(user_text or "").strip()
        if not text.startswith("@"):
            return None

        lowered = text.lower()
        if lowered.startswith("@mode"):
            parts = lowered.split(maxsplit=1)
            if len(parts) < 2:
                return "Mode command requires a value: @mode manual|interactive|automatic"
            target = parts[1].strip()
            if target not in {"manual", "interactive", "automatic"}:
                return "Unknown mode. Use: @mode manual|interactive|automatic"
            self.set_autonomy_mode(target)
            return f"Autonomy mode set to {self._autonomy_mode}. Active session: {self._active_session_type}."

        if lowered.startswith("@session"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                return "Session command requires a value: @session chat|reading|agentic"
            raw_target = str(parts[1]).strip()
            session_parts = raw_target.split(maxsplit=1)
            target = session_parts[0].strip()
            objective = session_parts[1].strip() if len(session_parts) > 1 else ""
            if target not in self._session_handlers:
                return "Unknown session. Use: @session chat|reading|agentic"
            self._set_active_session(target)
            if target == "agentic" and self._autonomy_mode != "automatic":
                self.set_autonomy_mode("automatic")
            if target == "agentic" and objective:
                self._agentic_session.set_objective(objective=objective, trace=self._trace)
            return f"Session switched to {self._active_session_type}. Mode: {self._autonomy_mode}."

        if lowered.startswith("@stop session"):
            was_active = self._active_session.stop(self._trace)
            self._set_active_session("chat")
            return "Stopped active session and returned to chat." if was_active else "No active session to stop."

        return None

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return

    @staticmethod
    def _slugify_mcp_server_name(name: str) -> str:
        text = re.sub(r"[^a-zA-Z0-9]+", "_", str(name or "").strip().lower()).strip("_")
        return text or "server"

    @staticmethod
    def _normalize_mcp_framing_mode(value: object, *, fallback: str = "content-length") -> str:
        text = str(value or "").strip().lower()
        if text in {"content-length", "newline-json"}:
            return text
        return str(fallback or "content-length").strip().lower() or "content-length"

    @staticmethod
    def _parse_mcp_servers_payload(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        servers_raw = payload.get("mcpServers", {})
        if not isinstance(servers_raw, dict):
            return []

        parsed: list[dict[str, Any]] = []
        for name, raw in servers_raw.items():
            server_name = str(name or "").strip()
            if not server_name or not isinstance(raw, dict):
                continue
            transport = str(raw.get("transport", "stdio")).strip().lower() or "stdio"
            if transport not in {"stdio", "http", "streamable-http"}:
                continue

            command = str(raw.get("command", "")).strip()
            url = str(raw.get("url", "")).strip()
            if transport == "stdio" and not command:
                continue
            if transport in {"http", "streamable-http"} and not url:
                continue

            args = raw.get("args", [])
            env = raw.get("env", {})
            headers = raw.get("headers", {})
            parsed.append(
                {
                    "name": server_name,
                    "transport": transport,
                    "command": command,
                    "url": url,
                    "args": [str(item).strip() for item in args if str(item).strip()] if isinstance(args, list) else [],
                    "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
                    "headers": {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
                    "framing_mode": str(raw.get("framing_mode", "")).strip().lower(),
                }
            )
        return parsed

    def _load_mcp_servers_from_json(self, *, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        path_raw = str(cfg.get("servers_json_path", "")).strip()
        if not path_raw:
            return []

        path = Path(path_raw)
        if not path.is_absolute():
            workspace_root = Path(__file__).resolve().parents[2]
            path = workspace_root / path
        if not path.exists():
            self._trace("mcp.error", f"Configured MCP servers JSON file not found: {path}")
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._trace("mcp.error", f"Failed to parse MCP servers JSON ({path}): {exc}")
            return []

        parsed = self._parse_mcp_servers_payload(payload)
        if not parsed:
            self._trace("mcp.error", f"No valid mcpServers entries found in: {path}")
            return []

        for item in parsed:
            item["source"] = str(path)
        return parsed

    def _load_mcp_servers_from_user_json(self, *, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        path_raw = str(cfg.get("servers_user_json_path", "")).strip()
        if not path_raw:
            return []

        path = Path(path_raw)
        if not path.is_absolute():
            workspace_root = Path(__file__).resolve().parents[2]
            path = workspace_root / path
        if not path.exists():
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._trace("mcp.error", f"Failed to parse MCP servers user JSON ({path}): {exc}")
            return []

        parsed = self._parse_mcp_servers_payload(payload)
        if not parsed:
            self._trace("mcp.error", f"No valid mcpServers entries found in user JSON: {path}")
            return []

        for item in parsed:
            item["source"] = str(path)
        return parsed

    def _register_mcp_tools(self) -> None:
        cfg = self._tooling_config.tool_settings("mcp")
        self._mcp_servers = []
        if not bool(cfg.get("enabled", False)):
            self._trace("mcp.start", "MCP integration disabled by config.")
            return

        timeout_ms = int(cfg.get("timeout_ms", self._tooling_config.policies.default_timeout_ms))
        startup_timeout_seconds = float(cfg.get("startup_timeout_seconds", 12.0))
        default_framing_mode = self._normalize_mcp_framing_mode(cfg.get("framing_mode", "content-length"))
        # Keep namespace stable in code; no user-facing prefix config needed.
        base_prefix = "mcp.windows"

        allowed_tools = set()
        blocked_tools = set()
        mutating_tools = set()
        for item in cfg.get("allow_tools", []):
            text = str(item).strip()
            if text:
                allowed_tools.add(text)
        for item in cfg.get("block_tools", []):
            text = str(item).strip()
            if text:
                blocked_tools.add(text)
        for item in cfg.get("mutating_tools", []):
            text = str(item).strip()
            if text:
                mutating_tools.add(text)

        server_configs = self._load_mcp_servers_from_user_json(cfg=cfg)
        if not server_configs:
            server_configs = self._load_mcp_servers_from_json(cfg=cfg)
        if not server_configs:
            self._trace(
                "mcp.error",
                "MCP is enabled but no valid user JSON or fallback JSON mcpServers config was loaded. Skipping MCP startup.",
            )
            return

        total_registered = 0
        multiple_servers = len(server_configs) > 1
        for index, server_cfg in enumerate(server_configs, start=1):
            server_name = str(server_cfg.get("name", f"server-{index}")).strip() or f"server-{index}"
            server_transport = str(server_cfg.get("transport", "stdio")).strip().lower() or "stdio"
            server_command = str(server_cfg.get("command", "")).strip()
            server_url = str(server_cfg.get("url", "")).strip()
            server_args = [str(item).strip() for item in server_cfg.get("args", []) if str(item).strip()]
            server_env = {str(k): str(v) for k, v in server_cfg.get("env", {}).items()} if isinstance(server_cfg.get("env", {}), dict) else {}
            server_headers = {str(k): str(v) for k, v in server_cfg.get("headers", {}).items()} if isinstance(server_cfg.get("headers", {}), dict) else {}
            server_source = str(server_cfg.get("source", "tools.mcp")).strip() or "tools.mcp"
            server_framing_mode = self._normalize_mcp_framing_mode(
                server_cfg.get("framing_mode", ""),
                fallback=default_framing_mode,
            )

            if server_transport == "stdio" and not server_command:
                self._trace("mcp.error", f"Skipping MCP stdio server '{server_name}' (missing command).")
                self._mcp_servers.append(
                    {
                        "id": f"mcp-stdio-{index}",
                        "name": server_name,
                        "transport": server_transport,
                        "command": server_command,
                        "url": server_url,
                        "args": server_args,
                        "framing_mode": server_framing_mode,
                        "prefix": base_prefix,
                        "source": server_source,
                        "client": None,
                        "error": "missing command",
                        "restart_attempts": 0,
                        "last_restart_ts": 0.0,
                    }
                )
                continue
            if server_transport in {"http", "streamable-http"} and not server_url:
                self._trace("mcp.error", f"Skipping MCP HTTP server '{server_name}' (missing url).")
                self._mcp_servers.append(
                    {
                        "id": f"mcp-stdio-{index}",
                        "name": server_name,
                        "transport": server_transport,
                        "command": server_command,
                        "url": server_url,
                        "args": server_args,
                        "framing_mode": server_framing_mode,
                        "prefix": base_prefix,
                        "source": server_source,
                        "client": None,
                        "error": "missing url",
                        "restart_attempts": 0,
                        "last_restart_ts": 0.0,
                    }
                )
                continue

            server_prefix = base_prefix
            if multiple_servers:
                server_prefix = f"{base_prefix}.{self._slugify_mcp_server_name(server_name)}"

            if server_transport in {"http", "streamable-http"}:
                client = MCPHttpClient(
                    url=server_url,
                    headers=server_headers,
                    startup_timeout_seconds=startup_timeout_seconds,
                    request_timeout_seconds=max(1.0, float(timeout_ms) / 1000.0),
                    trace=self._trace,
                )
            else:
                client = MCPStdioClient(
                    command=server_command,
                    args=server_args,
                    env=server_env,
                    framing_mode=server_framing_mode,
                    startup_timeout_seconds=startup_timeout_seconds,
                    trace=self._trace,
                )
            if not client.start():
                self._trace("mcp.error", f"MCP server '{server_name}' failed to start. Continuing.")
                self._mcp_servers.append(
                    {
                        "id": f"mcp-stdio-{index}",
                        "name": server_name,
                        "transport": server_transport,
                        "command": server_command,
                        "url": server_url,
                        "args": server_args,
                        "framing_mode": server_framing_mode,
                        "prefix": server_prefix,
                        "source": server_source,
                        "client": None,
                        "error": "startup failed",
                        "restart_attempts": 0,
                        "last_restart_ts": 0.0,
                    }
                )
                continue

            tools = build_mcp_tools(
                client=client,
                prefix=server_prefix,
                timeout_ms=timeout_ms,
                mutating_tools=mutating_tools,
                allowed_tools=allowed_tools,
                blocked_tools=blocked_tools,
            )
            if not tools:
                self._trace("mcp.start", f"No MCP tools discovered for server '{server_name}'.")
                client.stop()
                self._mcp_servers.append(
                    {
                        "id": f"mcp-stdio-{index}",
                        "name": server_name,
                        "transport": server_transport,
                        "command": server_command,
                        "url": server_url,
                        "args": server_args,
                        "framing_mode": server_framing_mode,
                        "prefix": server_prefix,
                        "source": server_source,
                        "client": None,
                        "error": "no tools discovered",
                        "restart_attempts": 0,
                        "last_restart_ts": 0.0,
                    }
                )
                continue

            self._tool_registry.register_many(tools)
            total_registered += len(tools)
            if self._tooling_config.enabled_tools and bool(cfg.get("append_to_enabled_tools", True)):
                existing = set(self._tooling_config.enabled_tools)
                for tool in tools:
                    if tool.spec.name not in existing:
                        self._tooling_config.enabled_tools.append(tool.spec.name)
                        existing.add(tool.spec.name)

            self._mcp_servers.append(
                {
                    "id": f"mcp-stdio-{index}",
                    "name": server_name,
                    "transport": server_transport,
                    "command": server_command,
                    "url": server_url,
                    "args": server_args,
                    "framing_mode": server_framing_mode,
                    "prefix": server_prefix,
                    "source": server_source,
                    "client": client,
                    "error": "",
                    "restart_attempts": 0,
                    "last_restart_ts": 0.0,
                }
            )

        if total_registered <= 0:
            self._trace("mcp.start", "No MCP tools registered from configured servers.")
            return
        self._trace("mcp.start", f"Registered {total_registered} MCP tool(s) across configured server(s).")
