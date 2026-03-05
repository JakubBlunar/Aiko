from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import unicodedata

from app.core.tooling.runtime.action_runtime import (
    ActionExecutionResult,
    ActionPlan,
    GuardedActionExecutor,
    PlannedAction,
)
from app.core.tooling.runtime.emergency_stop import EmergencyStopState, GlobalHotkeyListener
from app.audio.mic_capture import MicrophoneCapture
from app.core.conversation_memory import ConversationMemoryStore
from app.core.crash_logging import log_event
from app.audio.system_loopback import SystemLoopbackCapture
from app.core.settings import AppSettings, OllamaSettings
from app.core.tooling import ToolContext, ToolExecutor, ToolRegistry, load_tooling_config
from app.core.tooling.tools import ActionExecutePlanTool, build_default_tools
from app.core.turn_manager import TurnInput, TurnManager
from app.llm.ollama_client import OllamaClient
from app.stt.whisper_service import WhisperService
from app.tts.llasa_service import LlasaTtsService
from app.tts.piper_service import PiperTtsService
from app.vision.screen_capture import ScreenCaptureService


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    system_audio_enabled: bool
    screen_enabled: bool


@dataclass(slots=True)
class TurnAutonomyPlan:
    strategy: str
    should_use_screen: bool
    should_plan_action: bool
    ask_followup: bool
    confidence: float
    action_intent: str = ""


@dataclass(slots=True)
class GoalInference:
    goal: str
    confidence: float
    reason: str
    description: str = ""


class SessionController:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._turn_manager = TurnManager()
        self._ollama = OllamaClient(settings.ollama)
        self._thinking_model = settings.assistant.thinking_model
        self._thinking_ollama: OllamaClient | None = None
        self._rebuild_thinking_client()
        self._microphone = MicrophoneCapture(settings.audio)
        self._loopback = SystemLoopbackCapture(settings.audio)
        self._whisper = WhisperService(
            model_name=settings.stt.model,
            language=settings.stt.language,
        )
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
        self._tool_registry.register_many(
            build_default_tools(settings, self._tooling_config, memory_store=self._memory)
        )
        self._tool_registry.register(ActionExecutePlanTool(self._action_executor))
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
        self._tts = self._build_tts_service(settings)
        self._system_audio_context: deque[str] = deque(maxlen=4)
        self._last_system_audio_capture_at = 0.0
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._microphone_device = settings.audio.microphone_device
        self._loopback_device = settings.audio.loopback_device
        self._remember_history = settings.assistant.remember_history
        self._active_goal = settings.autonomy.default_goal
        self._active_goal_description: str = ""
        self._last_screen_decision_at = 0.0
        self._last_screen_text = ""
        self._last_screen_text_at = 0.0
        self._last_screen_elements: list[dict] = []
        self._open_windows: list[dict] = []
        self._all_windows: list[dict] = []
        self._foreground_window_title: str = ""
        self._last_metrics: dict[str, float | str] = {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
        }
        self._metrics_history: deque[dict[str, float | str]] = deque(maxlen=10)
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)
        self._live_no_speech_streak = 0

        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            system_audio_enabled=settings.audio.enable_system_audio,
            screen_enabled=settings.screen.enable_screen_context,
        )
        self._apply_persona_runtime_preferences()

    @property
    def state(self) -> SessionState:
        return self._state

    def update_sources(self, *, mic: bool, system_audio: bool, screen: bool) -> None:
        self._state.mic_enabled = mic
        self._state.system_audio_enabled = system_audio
        self._state.screen_enabled = screen

    def list_microphone_devices(self) -> list[tuple[int, str]]:
        return self._microphone.list_input_devices()

    def list_loopback_devices(self) -> list[tuple[int, str]]:
        return self._loopback.list_loopback_devices()

    def set_microphone_device(self, device_index: int | None) -> None:
        self._microphone_device = device_index
        self._microphone.set_device(device_index)

    def set_loopback_device(self, device_index: int | None) -> None:
        self._loopback_device = device_index
        self._loopback.set_device(device_index)

    @property
    def microphone_device(self) -> int | None:
        return self._microphone_device

    @property
    def loopback_device(self) -> int | None:
        return self._loopback_device

    @property
    def vad_level_threshold(self) -> float:
        return self._vad_level_threshold

    @property
    def vad_silence_seconds(self) -> float:
        return self._vad_silence_seconds

    @property
    def stt_model(self) -> str:
        return str(self._settings.stt.model or "base").strip() or "base"

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
        message = self._prepare_tts_text(text)
        if not message:
            return False
        try:
            self._tts.speak_async(message)
            return True
        except Exception as exc:
            self._trace("tts.error", f"TTS startup speak failed: {exc}")
            return False

    def build_startup_greeting(self) -> str:
        snapshot = self._persona_snapshot(max_notes=6)
        notes = list(snapshot.get("user_notes", []))
        has_history = bool(self._remember_history and self._history_messages(limit=1))

        if notes and has_history:
            return "Welcome back. I loaded your profile and recent conversation context."
        if notes:
            return "Welcome back. I loaded your profile."
        if has_history:
            return "Welcome back. I loaded recent conversation context."
        return "Welcome back. Audio is ready."

    def prewarm_tts(self) -> None:
        warmup_sync = getattr(self._tts, "warmup_sync", None)
        if callable(warmup_sync):
            try:
                ok = bool(warmup_sync())
                if not ok:
                    state, details = self.get_tts_model_status()
                    self._trace("tts.error", f"TTS warmup failed ({state}): {details}")
            except Exception as exc:
                self._trace("tts.error", f"TTS warmup failed: {exc}")
            return

        warmup_async = getattr(self._tts, "warmup_async", None)
        if callable(warmup_async):
            try:
                warmup_async()
            except Exception as exc:
                self._trace("tts.error", f"TTS warmup async failed: {exc}")

    def prewarm_runtime(self, on_status: Callable[[str], None] | None = None) -> None:
        def report(message: str) -> None:
            if on_status:
                on_status(message)

        report("Checking Ollama availability...")
        try:
            models = self._ollama.list_models()
        except Exception as exc:
            raise RuntimeError(f"Failed to reach Ollama server: {exc}") from exc

        if self.chat_model not in models:
            raise RuntimeError(
                f"Configured chat model not found in Ollama: {self.chat_model}. Pull it first."
            )

        report(f"Warming response model: {self.chat_model}")
        self._ollama.chat(self._build_startup_prewarm_messages())

        if self._thinking_ollama is not None and self._thinking_model:
            report(f"Warming thinking model: {self._thinking_model}")
            self._thinking_ollama.chat([
                {"role": "user", "content": "Reply with OK."},
            ])

        report("Warming TTS models...")
        warmup_sync = getattr(self._tts, "warmup_sync", None)
        if callable(warmup_sync):
            success = bool(warmup_sync())
            if not success:
                state, details = self.get_tts_model_status()
                raise RuntimeError(f"TTS warmup failed ({state}): {details}")
        else:
            self.prewarm_tts()

        report("Warmup complete")

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

    def clear_conversation_memory(self) -> None:
        self._memory.clear()

    @property
    def active_goal(self) -> str:
        return self._active_goal

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
        return pending.kind

    def approve_pending_action(self) -> str:
        result = self._action_executor.approve_pending_action()
        self._trace("action.confirmation", result.message)
        return result.message

    def reject_pending_action(self) -> str:
        result = self._action_executor.reject_pending_action()
        self._trace("action.confirmation", result.message)
        return result.message

    def start_action_hotkey_listener(self) -> bool:
        return self._action_hotkey_listener.start()

    def stop_action_hotkey_listener(self) -> None:
        self._action_hotkey_listener.stop()

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
        result = self._tool_executor.invoke(
            name,
            args=safe_args,
            context=self._tool_context,
            cancel_token=cancel_token,
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

    def _build_startup_prewarm_messages(self) -> list[dict[str, str]]:
        if not self._startup_context_prewarm_enabled:
            return [{"role": "user", "content": "Reply with OK."}]

        persona_snapshot = self._persona_snapshot(max_notes=6)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are preparing startup context for an assistant session. "
                    "Read the profile and history context and reply with only OK."
                ),
            }
        ]

        background = str(persona_snapshot.get("assistant_background", "")).strip()
        user_notes = [str(item).strip() for item in list(persona_snapshot.get("user_notes", [])) if str(item).strip()]
        if background or user_notes:
            profile_lines: list[str] = []
            if background:
                profile_lines.append(f"Assistant background: {background}")
            if user_notes:
                profile_lines.append("User notes:")
                for note in user_notes[-6:]:
                    profile_lines.append(f"- {note}")
            messages.append({"role": "user", "content": "\n".join(profile_lines)})

        if self._history_summary_enabled and self._remember_history:
            summary = self._history_summary(
                limit=self._history_summary_limit,
                max_chars=self._history_summary_max_chars,
            )
            if summary:
                messages.append({"role": "user", "content": f"Conversation summary:\n{summary}"})

        if self._remember_history:
            for item in self._history_messages(limit=self._startup_history_limit):
                role = str(item.get("role", "")).strip().lower()
                content = str(item.get("content", "")).strip()
                if role in {"user", "assistant"} and content:
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": "Reply with OK."})
        return messages

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

    def chat_once(self, user_text: str) -> str:
        return self.chat_once_streaming(user_text=user_text, mode="typed")

    def chat_once_streaming(
        self,
        *,
        user_text: str,
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
        user_text = self._sanitize_user_text(raw_user_text)
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

        try:
            persona_changed = self._persona_update_from_user_text(user_text)
            if persona_changed:
                self._persona_compact_notes()
                self._apply_persona_runtime_preferences()
        except Exception:
            pass

        persona_snapshot = self._persona_snapshot(max_notes=6)

        self._update_goal_from_conversation(user_text=user_text)
        autonomy_plan = self._plan_turn_autonomy(user_text=user_text)
        screen_intent = self._is_screen_intent(user_text)
        screen_text = None
        should_capture = False
        decision_source = "none"
        if autonomy_plan.should_use_screen:
            should_capture = True
            decision_source = "autonomy"
        else:
            should_capture, decision_source = self._should_capture_screen(user_text=user_text)

        if should_capture:
            screen_text = self._capture_screen_text(decision_source=decision_source)
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

        system_audio_text = None
        if self._state.system_audio_enabled:
            system_audio_text = self._transcribe_system_audio(seconds=2.0)

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

        # Build capability list so the model knows what it can do this turn.
        caps = [f"tool:{name}" for name in self._tool_executor.list_available_tools()]
        if self._settings.actions.enabled:
            caps.extend(["click (pyautogui)", "type_text (pyautogui)"])

        messages = self._turn_manager.build_chat_messages(
            TurnInput(
                user_text=user_text,
                screen_text=screen_context_for_llm,
                system_audio_text=system_audio_text,
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
                response = self._sanitize_assistant_text(
                    self._ollama.chat(messages, options=generation_options)
                )
            else:
                pieces: list[str] = []
                for token in self._ollama.chat_stream(messages, options=generation_options):
                    if stop_requested and stop_requested():
                        break
                    safe_token = self._sanitize_assistant_text(
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
                        ready_chunks, stream_tts_buffer = self._drain_tts_stream_chunks(
                            stream_tts_buffer,
                            flush=False,
                        )
                        for chunk in ready_chunks:
                            tts_chunk = self._prepare_tts_text(chunk)
                            if tts_chunk and callable(enqueue_tts):
                                queued_ok = bool(enqueue_tts(tts_chunk))
                                if queued_ok:
                                    stream_tts_used = True
                                    stream_tts_enqueued_chunks += 1
                                else:
                                    self._trace("pipeline.tts.enqueue", "chunk enqueue failed")
                response = "".join(pieces).strip()
                if can_stream_tts and stream_tts_buffer.strip():
                    ready_chunks, stream_tts_buffer = self._drain_tts_stream_chunks(
                        stream_tts_buffer,
                        flush=True,
                    )
                    for chunk in ready_chunks:
                        tts_chunk = self._prepare_tts_text(chunk)
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

        if stop_requested and stop_requested():
            self._trace("pipeline.turn.stopped", f"mode={mode} stop_requested=true")
            return response

        self._trace("pipeline.action.start", f"mode={mode}")
        action_result = self._maybe_execute_action(
            user_text=user_text,
            assistant_reply=response,
            screen_text=screen_text,
            allow_planning_override=autonomy_plan.should_plan_action,
            action_intent=autonomy_plan.action_intent,
            on_token=on_token,
        )
        self._trace(
            "pipeline.action.done",
            "executed" if action_result is not None else "skipped",
        )
        # Keep the pure LLM text for TTS — action status lines are for the UI only.
        llm_response_for_tts = response

        if action_result is not None and action_result.message:
            prefix = "[Action]" if action_result.executed or action_result.requires_confirmation else "[Note]"
            action_suffix = f"\n\n{prefix} {action_result.message}"
            if on_token:
                on_token(action_suffix)
            response = f"{response}{action_suffix}".strip()

        response = self._sanitize_assistant_text(response)

        if self._remember_history:
            self._memory.add(role="user", content=user_text)
            self._memory.add(role="assistant", content=response)

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
                tts_text = self._prepare_tts_text(llm_response_for_tts)
                if tts_text:
                    self._tts.speak_async(tts_text)
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
        defaults = TurnAutonomyPlan(
            strategy="Respond naturally, concise first, ask one focused follow-up when useful.",
            should_use_screen=False,
            should_plan_action=False,
            ask_followup=True,
            confidence=0.4,
            action_intent="",
        )

        if not self._settings.autonomy.enabled:
            return defaults

        recent_memory = self._history_messages(limit=6)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")

        # Build capability list from the registered tooling interface.
        caps: list[str] = ["- respond_to_user (always available)"]
        for tool_name in self._tool_executor.list_available_tools():
            caps.append(f"- tool:{tool_name}")
        if self._settings.actions.enabled:
            caps.append("- execute_click: click a UI element at given screen coordinates")
            caps.append("- execute_type: type text into the focused field")
        caps_block = "\n".join(caps)

        goal_context = self._active_goal or "general_conversation"
        goal_desc = self._active_goal_description or ""

        planner = self._thinking_ollama or self._ollama
        try:
            raw = planner.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an autonomous assistant turn planner. "
                            "Given the current goal, available capabilities, and the user's message, "
                            "decide which capabilities are needed for this turn and plan the strategy. "
                            "Return exactly one JSON object with keys: "
                            "strategy, should_use_screen, should_plan_action, ask_followup, confidence, action_intent. "
                            "strategy: one short sentence under 180 chars describing the response approach. "
                            "should_use_screen: true if reading screen context is needed to answer this turn. "
                            "should_plan_action: true if a UI click or type action should be performed this turn. "
                            "action_intent: if should_plan_action is true, one sentence describing the intended UI action; otherwise empty string. "
                            "ask_followup: true if the assistant needs to ask the user a clarifying question before acting. "
                            "IMPORTANT: should_plan_action and ask_followup are mutually exclusive — "
                            "if you need more information first, set ask_followup=true and should_plan_action=false. "
                            "confidence: 0.0-1.0 reflecting plan certainty. "
                            "Use booleans for flags."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current goal: {goal_context}\n"
                            + (f"Goal description: {goal_desc}\n" if goal_desc else "")
                            + f"\nAvailable capabilities:\n{caps_block}\n\n"
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            "Settings:\n"
                            f"- proactive_conversation={self._settings.autonomy.proactive_conversation}\n"
                            f"- allow_action_suggestions={self._settings.autonomy.allow_action_suggestions}\n"
                            f"- allow_proactive_actions={self._settings.autonomy.allow_proactive_actions}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("autonomy.plan", "Autonomy planner unavailable; using defaults.")
            return defaults

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            self._trace("autonomy.plan", "Autonomy planner returned invalid JSON; using defaults.")
            return defaults

        strategy = str(payload.get("strategy", defaults.strategy)).strip() or defaults.strategy
        max_chars = max(40, int(self._settings.autonomy.max_strategy_chars))
        strategy = strategy[:max_chars]

        confidence = float(payload.get("confidence", defaults.confidence) or defaults.confidence)
        confidence = max(0.0, min(confidence, 1.0))

        action_intent = str(payload.get("action_intent", "")).strip()

        plan = TurnAutonomyPlan(
            strategy=strategy,
            should_use_screen=bool(payload.get("should_use_screen", False)),
            should_plan_action=bool(payload.get("should_plan_action", False)),
            ask_followup=bool(payload.get("ask_followup", True)),
            confidence=confidence,
            action_intent=action_intent,
        )

        if not self._settings.autonomy.allow_action_suggestions:
            plan.should_plan_action = False
        if not self._settings.autonomy.allow_proactive_actions:
            plan.should_plan_action = False
        if not self._settings.autonomy.proactive_conversation:
            plan.ask_followup = False
        # Asking a follow-up question and executing an action are mutually exclusive.
        # If the model wants to ask something, don't fire an action this turn.
        if plan.ask_followup:
            plan.should_plan_action = False
        if not plan.should_plan_action:
            plan.action_intent = ""

        self._trace(
            "autonomy.plan",
            (
                f"strategy='{plan.strategy}' | screen={plan.should_use_screen} | "
                f"action={plan.should_plan_action} | followup={plan.ask_followup} | "
                f"confidence={round(plan.confidence, 2)}"
                + (f" | intent='{plan.action_intent}'" if plan.action_intent else "")
            ),
        )
        return plan

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

        if inferred.goal != self._active_goal:
            previous = self._active_goal
            self._active_goal = inferred.goal
            self._active_goal_description = inferred.description
            self._trace(
                "autonomy.goal",
                (
                    f"Switched goal {previous} -> {self._active_goal} "
                    f"(confidence={round(inferred.confidence, 2)}; reason={inferred.reason or 'n/a'}; "
                    f"desc={inferred.description or 'n/a'})"
                ),
            )

    def _infer_goal(self, *, user_text: str) -> GoalInference:
        recent_memory = self._history_messages(limit=8)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")

        planner = self._thinking_ollama or self._ollama
        try:
            raw = planner.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Infer the most useful current conversation goal from dialogue context. "
                            "Return exactly one JSON object with keys: goal, confidence, reason, description. "
                            "goal: a short snake_case identifier for the task "
                            "(e.g. 'tic_tac_toe', 'english_practice', 'coding_help', 'general_conversation'). "
                            "description: one sentence describing what the user is trying to accomplish. "
                            "confidence: 0.0-1.0 how confident you are in the inferred goal. "
                            "reason: brief explanation of why you chose this goal."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current goal: {self._active_goal}\n\n"
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}"
                        ),
                    },
                ]
            )
        except Exception:
            return GoalInference(goal=self._active_goal, confidence=0.0, reason="goal-planner-unavailable")

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            return GoalInference(goal=self._active_goal, confidence=0.0, reason="invalid-goal-json")

        raw_goal = str(payload.get("goal", self._active_goal)).strip().lower()
        # Sanitize to snake_case: keep alphanumeric/underscores/spaces only.
        sanitized = re.sub(r"[^a-z0-9\s_]", "", raw_goal)
        sanitized = re.sub(r"[\s_]+", "_", sanitized).strip("_")
        goal = sanitized[:60] or self._active_goal

        confidence = float(payload.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(confidence, 1.0))
        reason = str(payload.get("reason", "")).strip()
        description = str(payload.get("description", "")).strip()
        return GoalInference(goal=goal, confidence=confidence, reason=reason, description=description)

    def _should_capture_screen(self, *, user_text: str) -> tuple[bool, str]:
        normalized = (user_text or "").lower()
        if not self._state.screen_enabled:
            return False, "disabled"

        keyword_triggers = (
            "screen",
            "on my screen",
            "look at",
            "look on",
            "check screen",
            "check my screen",
            "what do you see",
            "what can you see",
            "what's on my screen",
            "whats on my screen",
            "this page",
            "this window",
            "this code",
            "here",
            "shown",
            "read this",
        )
        if any(token in normalized for token in keyword_triggers):
            return True, "keyword"

        now = time.monotonic()
        decision_mode = (self._settings.screen.decision_mode or "model").lower().strip()
        if decision_mode == "keywords":
            return False, "keywords-only"

        cooldown = max(1, int(self._settings.screen.decision_cooldown_seconds))
        if (now - self._last_screen_decision_at) < cooldown:
            return False, "decision-cooldown"
        self._last_screen_decision_at = now

        recent_memory = self._history_messages(limit=4)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")
        recent_text = "\n".join(recent_lines)

        try:
            decider = self._thinking_ollama or self._ollama
            decision = decider.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Decide whether checking the user's current screen is needed to answer the user's latest message. "
                            "Use latest message plus recent conversation. "
                            "Reply with only YES or NO."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Latest message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{recent_text or '[none]'}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("screen.decision", "Screen decision unavailable (model error), skipping capture.")
            return False, "model-error"

        capture = decision.strip().lower().startswith("y")
        self._trace(
            "screen.decision",
            (
                f"Decision={'YES' if capture else 'NO'} for latest message: "
                f"{(user_text or '').strip()[:140]}"
            ),
        )
        return capture, "model"

    @staticmethod
    def _is_screen_intent(user_text: str) -> bool:
        normalized = (user_text or "").lower()
        triggers = (
            "screen",
            "on my screen",
            "look at",
            "what do you see",
            "what can you see",
            "see this",
            "read this",
            "from the screen",
        )
        return any(token in normalized for token in triggers)

    def _capture_screen_text(self, *, decision_source: str) -> str | None:
        frame, region = self._screen.capture_once_with_region()
        if frame is None:
            self._trace("screen.capture", "Screen capture unavailable.")
            self._last_screen_elements = []
            return None

        screen_left = int((region or {}).get("left", 0))
        screen_top = int((region or {}).get("top", 0))
        screen_width = int((region or {}).get("width", 0))
        screen_height = int((region or {}).get("height", 0))
        elements_result = self._invoke_tool(
            "ocr.extract_elements",
            args={
                "image": frame,
                "screen_left": screen_left,
                "screen_top": screen_top,
                "screen_width": screen_width,
                "screen_height": screen_height,
            },
        )
        elements = list(elements_result.data.get("elements", [])) if elements_result.success else []
        used_fallback = False
        if not elements and bool(getattr(self._settings.screen, "capture_active_window_only", False)):
            self._trace(
                "screen.capture",
                "Active-window OCR returned no elements; retrying full monitor capture.",
            )
            fb_frame, fb_region = self._screen.capture_once_with_region(active_window_only=False)
            if fb_frame is not None:
                fb_left = int((fb_region or {}).get("left", 0))
                fb_top = int((fb_region or {}).get("top", 0))
                fb_width = int((fb_region or {}).get("width", 0))
                fb_height = int((fb_region or {}).get("height", 0))
                fallback_result = self._invoke_tool(
                    "ocr.extract_elements",
                    args={
                        "image": fb_frame,
                        "screen_left": fb_left,
                        "screen_top": fb_top,
                        "screen_width": fb_width,
                        "screen_height": fb_height,
                    },
                )
                elements = list(fallback_result.data.get("elements", [])) if fallback_result.success else []
                used_fallback = True

        # UIA enrichment — get native window controls for the foreground window
        if getattr(self._settings.screen, "enable_uia", True):
            try:
                fg_result = self._invoke_tool(
                    "uia.get_foreground_elements",
                    args={},
                )
                visible_windows_result = self._invoke_tool(
                    "uia.list_visible_windows",
                    args={},
                )
                all_windows_result = self._invoke_tool(
                    "uia.list_all_windows",
                    args={},
                )
                fw_title = str(fg_result.data.get("title", "")) if fg_result.success else ""
                uia_els = list(fg_result.data.get("elements", [])) if fg_result.success else []
                self._foreground_window_title = fw_title
                self._open_windows = (
                    list(visible_windows_result.data.get("windows", []))
                    if visible_windows_result.success
                    else []
                )
                self._all_windows = (
                    list(all_windows_result.data.get("windows", []))
                    if all_windows_result.success
                    else []
                )
                if uia_els:
                    # Convert UIA dicts to element format compatible with OCR elements
                    uia_dicts = [
                        {
                            "text": f"[{e['type']}] {e['name']}" if e.get("name") else f"[{e['type']}]",
                            "type": e["type"],
                            "name": e.get("name", ""),
                            "cx": e["cx"],
                            "cy": e["cy"],
                            "w": e["w"],
                            "h": e["h"],
                            "enabled": e.get("enabled", True),
                            "source": "uia",
                            "window_title": fw_title,
                        }
                        for e in uia_els
                    ]
                    # Merge: UIA first, then OCR elements that don't overlap with any UIA element
                    def _ocr_overlaps(ocr_el: dict, uia_list: list[dict]) -> bool:
                        cx, cy = ocr_el["cx"], ocr_el["cy"]
                        for u in uia_list:
                            ux1 = u["cx"] - u["w"] // 2
                            ux2 = u["cx"] + u["w"] // 2
                            uy1 = u["cy"] - u["h"] // 2
                            uy2 = u["cy"] + u["h"] // 2
                            if ux1 <= cx <= ux2 and uy1 <= cy <= uy2:
                                return True
                        return False
                    ocr_only = [e for e in elements if not _ocr_overlaps(e, uia_dicts)]
                    elements = uia_dicts + ocr_only
            except Exception as exc:
                self._trace("screen.uia", f"UIA enrichment failed: {exc}")

        self._last_screen_elements = elements
        text = " ".join(e["text"] for e in elements).strip()
        text = " ".join(text.split())

        if not text:
            self._trace("screen.capture", "OCR returned no text.")
            return None

        min_chars = max(0, int(self._settings.screen.min_ocr_chars))
        if len(text) < min_chars:
            self._trace(
                "screen.capture",
                (
                    "OCR text below minimum length: "
                    f"{len(text)} < {min_chars}. Ignoring capture."
                ),
            )
            return None

        now = time.monotonic()
        reuse_window = max(0, int(self._settings.screen.unchanged_reuse_seconds))
        is_unchanged = text == self._last_screen_text
        within_window = (now - self._last_screen_text_at) <= reuse_window if self._last_screen_text_at else False

        if is_unchanged and within_window and decision_source != "keyword":
            self._trace(
                "screen.capture",
                (
                    "Screen OCR unchanged within reuse window "
                    f"({reuse_window}s). Skipping repeated context."
                ),
            )
            return None

        self._last_screen_text = text
        self._last_screen_text_at = now
        self._trace(
            "screen.capture",
            (
                f"Captured screen context ({len(text)} chars, "
                f"{len(elements)} elements, source={decision_source}"
                f", fallback={'yes' if used_fallback else 'no'})."
            ),
        )
        return text

    def run_screen_ocr_diagnostic(self) -> dict[str, object]:
        frame = self._screen.capture_once()
        if frame is None:
            return {
                "ok": False,
                "reason": "capture-unavailable",
                "message": "Screen capture unavailable.",
            }

        used_fallback = False
        details_result = self._invoke_tool(
            "ocr.extract_details",
            args={"image": frame},
        )
        details = details_result.data.get("details") if details_result.success else None
        if not details and bool(getattr(self._settings.screen, "capture_active_window_only", False)):
            fallback_frame = self._screen.capture_once(active_window_only=False)
            if fallback_frame is not None:
                fallback_details_result = self._invoke_tool(
                    "ocr.extract_details",
                    args={"image": fallback_frame},
                )
                details = (
                    fallback_details_result.data.get("details") if fallback_details_result.success else None
                )
                frame = fallback_frame
                used_fallback = True

        if not details:
            return {
                "ok": False,
                "reason": "ocr-empty",
                "message": "OCR returned no text.",
                "capture_mode": "active-window" if bool(getattr(self._settings.screen, "capture_active_window_only", False)) else "monitor",
                "retried_full_monitor": used_fallback,
            }

        text = str(details.get("text") or "").strip()
        text = " ".join(text.split())
        if not text:
            return {
                "ok": False,
                "reason": "ocr-empty",
                "message": "OCR returned no readable text.",
                "capture_mode": "active-window" if bool(getattr(self._settings.screen, "capture_active_window_only", False)) else "monitor",
                "retried_full_monitor": used_fallback,
            }

        min_chars = max(0, int(self._settings.screen.min_ocr_chars))
        frame_height = int(frame.shape[0]) if getattr(frame, "shape", None) is not None and len(frame.shape) >= 2 else 0
        frame_width = int(frame.shape[1]) if getattr(frame, "shape", None) is not None and len(frame.shape) >= 2 else 0
        return {
            "ok": True,
            "reason": "ok",
            "message": "OCR diagnostic captured text.",
            "chars": len(text),
            "min_chars": min_chars,
            "passes_min_chars": len(text) >= min_chars,
            "line_count": int(details.get("line_count") or 0),
            "avg_confidence": float(details.get("avg_confidence") or 0.0),
            "capture_mode": "active-window" if bool(getattr(self._settings.screen, "capture_active_window_only", False)) else "monitor",
            "retried_full_monitor": used_fallback,
            "frame_width": frame_width,
            "frame_height": frame_height,
            "text": text,
        }

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

        try:
            stt_started = time.perf_counter()
            text = self._whisper.transcribe(
                str(wav_path),
                vad_filter=bool(vad_filter),
                initial_prompt=(str(initial_prompt).strip() or None),
            )
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
        finally:
            self._safe_unlink(wav_path)

        sanitized = self._sanitize_user_text(text or "")
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
            "text": sanitized,
        }

    def _trace(self, stage: str, message: str) -> None:
        stage_text = str(stage)
        message_text = str(message)
        self._decision_trace.append(
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
        if not self._settings.actions.enabled:
            wants_action = bool(action_intent) or self._has_action_intent(user_text)
            if wants_action:
                self._trace("action.plan", f"Actions disabled; intent was: {action_intent or user_text[:80]}")
                return ActionExecutionResult(
                    executed=False,
                    dry_run=False,
                    blocked=True,
                    requires_confirmation=False,
                    message="I can't perform UI actions right now — actions are disabled in settings.",
                )
            return None

        if self._settings.actions.max_actions_per_turn < 1:
            return None

        mode = (self._settings.actions.decision_mode or "explicit_only").lower().strip()
        if mode == "explicit_only" and not allow_planning_override and not self._has_action_intent(user_text):
            self._trace("action.plan", "Skipped action planning (no explicit action intent).")
            return None

        if not screen_text and self._state.screen_enabled:
            screen_text = self._capture_screen_text(decision_source="action")

        # Agentic replan loop: the planner can set needs_screen=true when element
        # data is absent; the system re-captures and calls the planner again with
        # fresh context so it can produce an informed plan.
        _MAX_REPLAN = 2
        planned: ActionPlan = ActionPlan(steps=[], description="not yet planned")
        for _attempt in range(_MAX_REPLAN + 1):
            planned = self._plan_action(
                user_text=user_text,
                assistant_reply=assistant_reply,
                screen_text=screen_text,
                action_intent=action_intent,
            )
            if planned.needs_screen and _attempt < _MAX_REPLAN and self._state.screen_enabled:
                self._trace(
                    "action.replan",
                    f"Planner requested screen capture (attempt {_attempt + 1}/{_MAX_REPLAN}); re-capturing.",
                )
                screen_text = self._capture_screen_text(decision_source="action-replan")
                continue
            break

        # Stream plan preview to the chat before executing so the user sees
        # what steps are about to run.
        if planned.steps and on_token:
            hwnd_to_title = {w["hwnd"]: w["title"] for w in self._all_windows}
            lines: list[str] = []
            for idx, s in enumerate(planned.steps, start=1):
                if s.kind == "focus_window":
                    title = hwnd_to_title.get(s.hwnd or 0, str(s.hwnd))
                    line = f"{idx}. focus_window('{title}')"
                elif s.kind == "click":
                    line = f"{idx}. click({s.x}, {s.y})"
                elif s.kind == "type_text":
                    snippet = (s.text or "")[:24]
                    line = f"{idx}. type_text({snippet!r})"
                else:
                    line = f"{idx}. {s.kind}"
                if s.reason:
                    line += f" - {s.reason}"
                lines.append(line)
            plan_text = "\n".join(lines)
            on_token(f"\n\n[Plan]\n{plan_text}\n")

        plan_payload = {
            "description": planned.description,
            "needs_screen": planned.needs_screen,
            "steps": [
                {
                    "kind": step.kind,
                    "x": step.x,
                    "y": step.y,
                    "text": step.text,
                    "hwnd": step.hwnd,
                    "confidence": step.confidence,
                    "reason": step.reason,
                }
                for step in planned.steps
            ],
        }
        execute_result = self._invoke_tool(
            "action.execute_plan",
            args={"plan": plan_payload},
        )
        if execute_result.success:
            result = ActionExecutionResult(
                executed=bool(execute_result.data.get("executed", False)),
                dry_run=bool(execute_result.data.get("dry_run", False)),
                blocked=bool(execute_result.data.get("blocked", False)),
                requires_confirmation=bool(execute_result.data.get("requires_confirmation", False)),
                message=str(execute_result.data.get("message", "")),
            )
        else:
            result = ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=True,
                requires_confirmation=False,
                message=(execute_result.error.message if execute_result.error else "Action tool execution failed."),
            )
        self._trace("action.execute", result.message)
        return result

    @staticmethod
    def _has_action_intent(user_text: str) -> bool:
        normalized = (user_text or "").lower()
        triggers = (
            "click",
            "press",
            "tap",
            "type",
            "write",
            "fill",
            "open",
            "select",
            "choose",
            "submit",
            "send",
        )
        return any(token in normalized for token in triggers)

    def _plan_action(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        screen_text: str | None,
        action_intent: str = "",
    ) -> ActionPlan:
        recent_memory = self._history_messages(limit=4)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")

        # Build a structured element list so the planner uses real coordinates.
        # Label elements that belong to this assistant app so the planner never targets them.
        own_title_lower = "assistant"  # substring that identifies our own window title
        elements_lines: list[str] = []
        for el in self._last_screen_elements[:60]:
            el_text = str(el.get("text", "")).strip()
            if el_text:
                # Mark UIA elements that belong to the assistant's own window
                win_title = str(el.get("window_title", "")).lower()
                is_own = el.get("source") == "uia" and own_title_lower in win_title
                own_tag = " [THIS APP — do not target]" if is_own else ""
                elements_lines.append(
                    f"- \"{el_text}\" at ({el['cx']}, {el['cy']}) size {el['w']}x{el['h']}{own_tag}"
                )
        elements_block = "\n".join(elements_lines) if elements_lines else "[none detected]"

        # Build windows list so planner can reference hwnds for focus_window actions.
        windows_lines: list[str] = []
        for w in self._all_windows[:20]:
            labels: list[str] = []
            if w.get("is_foreground"):
                labels.append("active")
            if w.get("is_minimized"):
                labels.append("MINIMIZED")
            label_str = f" [{', '.join(labels)}]" if labels else ""
            windows_lines.append(f"- hwnd={w['hwnd']} \"{w['title']}\"{label_str}")
        windows_block = "\n".join(windows_lines) if windows_lines else "[none]"

        planner = self._thinking_ollama or self._ollama
        try:
            raw = planner.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an action planner for desktop UI automation. "
                            "Return exactly one JSON object with keys: "
                            "steps (array), description (string), needs_screen (bool). "
                            "Each step object has keys: type, x, y, text, hwnd, confidence, reason. "
                            "Allowed step types: none, click, type_text, focus_window. "
                            "Steps are executed in order — use multiple steps when needed "
                            "(e.g. focus_window first, then click, then type_text). "
                            "For click: x and y are the element center to click. "
                            "For type_text: text is the string to type; "
                            "x and y are the coordinates of the INPUT FIELD to click for focus "
                            "— pick the text input element, not a button. "
                            "For focus_window: hwnd is the integer window handle from the 'Open windows' list; "
                            "use this to restore a minimized window or bring a background window to the front "
                            "before performing a click inside it. Set x, y, text, hwnd to null when unused. "
                            "ALL coordinates MUST be exact values "
                            "copied from the 'Detected UI elements' list — do NOT invent coordinates. "
                            "CRITICAL: elements marked '[THIS APP — do not target]' belong to the assistant "
                            "application itself — NEVER click or type into them. "
                            "Always target elements in the user's intended application (e.g. Notepad, browser). "
                            "OCR may have minor typos (e.g. 'Seltings' for 'Settings'); "
                            "match labels by approximate similarity. "
                            "If 'Detected UI elements' shows [none detected] or the target app is not visible, "
                            "return needs_screen=true and steps=[] — the system will capture a fresh screenshot "
                            "and call you again with updated data. Only request this once. "
                            "confidence per step must be 0.0–1.0."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current goal: {self._active_goal}\n\n"
                            + (f"Action intent: {action_intent}\n\n" if action_intent else "")
                            + f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Assistant draft reply:\n{assistant_reply.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            f"Open windows (hwnd handles for focus_window):\n{windows_block}\n\n"
                            f"Detected UI elements (text \u2192 screen coordinates):\n{elements_block}\n\n"
                            f"Full OCR text:\n{(screen_text or '[none]')[:3000]}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("action.plan", "Planner unavailable. Falling back to no action.")
            return ActionPlan(steps=[], description="Action planner unavailable")

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            self._trace("action.plan", "Planner output was not valid JSON. Falling back to no action.")
            return ActionPlan(steps=[], description="Planner output was not valid JSON")

        def _parse_step(s: dict) -> PlannedAction:
            raw_kind = str(s.get("type", "none")).strip().lower()
            if raw_kind not in {"none", "click", "type_text", "focus_window"}:
                raw_kind = "none"
            conf = float(s.get("confidence", 0.0) or 0.0)
            conf = max(0.0, min(conf, 1.0))
            return PlannedAction(
                kind=raw_kind,
                x=(int(s["x"]) if s.get("x") is not None else None),
                y=(int(s["y"]) if s.get("y") is not None else None),
                text=(str(s.get("text", "")).strip() or None),
                hwnd=(int(s["hwnd"]) if s.get("hwnd") is not None else None),
                confidence=conf,
                reason=str(s.get("reason", "")).strip(),
            )

        # Support both new array format {steps: [...]} and legacy single-action format {type: ...}
        raw_steps = payload.get("steps")
        if isinstance(raw_steps, list):
            steps = [_parse_step(s) for s in raw_steps if isinstance(s, dict)]
        else:
            # Legacy single-action fallback
            steps = [_parse_step(payload)]

        plan = ActionPlan(
            steps=[s for s in steps if s.kind != "none"],
            description=str(payload.get("description", "")).strip(),
            needs_screen=bool(payload.get("needs_screen", False)),
        )
        self._trace(
            "action.plan",
            (
                f"Planned {len(plan.steps)} step(s): "
                + " → ".join(
                    f"{s.kind}(conf={round(s.confidence, 2)})"
                    for s in plan.steps
                )
                or "no actions"
            ),
        )
        return plan

    @staticmethod
    def _extract_json_object(raw_text: str) -> dict | None:
        try:
            direct = json.loads(raw_text)
            return direct if isinstance(direct, dict) else None
        except Exception:
            pass

        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start < 0 or end <= start:
            return None

        fragment = raw_text[start : end + 1]
        try:
            nested = json.loads(fragment)
            return nested if isinstance(nested, dict) else None
        except Exception:
            return None

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
        try:
            stt_started = time.perf_counter()
            text = self._whisper.transcribe(str(wav_path))
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
        finally:
            self._safe_unlink(wav_path)

        if not text:
            raise RuntimeError("No speech was detected from microphone audio.")

        text = self._sanitize_user_text(text)
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

        text = self._sanitize_user_text(text)
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
        )

        return text, response

    def _transcribe_system_audio(self, seconds: float) -> str | None:
        if not self._whisper.is_available:
            return None

        now = time.monotonic()
        if self._system_audio_context and (now - self._last_system_audio_capture_at) < 6.0:
            return " ".join(self._system_audio_context)

        samples = self._loopback.capture_seconds(seconds=seconds)
        if samples is None:
            return " ".join(self._system_audio_context) if self._system_audio_context else None

        wav_path = self._microphone.create_temp_wav_path(prefix="assistant_loopback_")
        try:
            self._microphone.write_wav(samples=samples, target_path=wav_path)
            text = self._whisper.transcribe(str(wav_path))
            if text:
                self._system_audio_context.append(text)
            self._last_system_audio_capture_at = now
            return " ".join(self._system_audio_context) if self._system_audio_context else None
        finally:
            self._safe_unlink(wav_path)

    @staticmethod
    def _prepare_tts_text(text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)
        cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
        cleaned = re.sub(r"_(.+?)_", r"\1", cleaned)
        cleaned = cleaned.replace("`", "")
        cleaned = cleaned.replace("[", "").replace("]", "")
        cleaned = " ".join(cleaned.split())
        return cleaned

    @staticmethod
    def _sanitize_user_text(text: str) -> str:
        cleaned = str(text or "")
        if not cleaned:
            return ""

        # Remove common emoji/dingbat ranges that can break downstream prompting.
        cleaned = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", " ", cleaned)

        out_chars: list[str] = []
        for ch in cleaned:
            category = unicodedata.category(ch)
            if category.startswith("C"):
                continue
            out_chars.append(ch)

        cleaned = "".join(out_chars)
        cleaned = re.sub(r"[^\w\s\.,!?;:'\"()\-]", " ", cleaned)
        cleaned = " ".join(cleaned.split())
        return cleaned.strip()

    @staticmethod
    def _sanitize_assistant_text(
        text: str,
        *,
        preserve_newlines: bool = True,
        trim: bool = True,
    ) -> str:
        cleaned = unicodedata.normalize("NFKC", str(text or ""))
        if not cleaned:
            return ""

        cleaned = (
            cleaned.replace("\u2018", "'")
            .replace("\u2019", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2013", "-")
            .replace("\u2014", "-")
        )

        # Strip Unicode emoji and common text-based emoticons.
        cleaned = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", cleaned)
        cleaned = re.sub(
            r"(?<![\w])([:;=8Xx][-o*']?[)DPpOo03{}\[\]|/\\]|[)DPp][:;=]|\^[_-]?\^|>_<|<3|:\*|;\*)(?![\w])",
            "",
            cleaned,
        )

        out_chars: list[str] = []
        for ch in cleaned:
            code = ord(ch)
            if ch == "\n" and preserve_newlines:
                out_chars.append(ch)
                continue
            if ch == "\n" and not preserve_newlines:
                out_chars.append(" ")
                continue
            if ch == "\t":
                out_chars.append(" ")
                continue
            if 32 <= code <= 126:
                out_chars.append(ch)

        cleaned = "".join(out_chars)
        if preserve_newlines:
            cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        else:
            cleaned = re.sub(r" {2,}", " ", cleaned)

        if trim:
            return cleaned.strip()
        return cleaned

    @staticmethod
    def _drain_tts_stream_chunks(buffer: str, *, flush: bool) -> tuple[list[str], str]:
        text = str(buffer or "")
        if not text:
            return [], ""

        chunks: list[str] = []
        start = 0
        for index, ch in enumerate(text):
            if ch not in ".!?\n":
                continue

            candidate = text[start : index + 1].strip()
            if not candidate:
                start = index + 1
                continue

            if len(candidate) >= 24 or candidate.count(" ") >= 4 or ch == "\n":
                chunks.append(candidate)
                start = index + 1

        remainder = text[start:]
        if flush and remainder.strip():
            chunks.append(remainder.strip())
            remainder = ""

        return chunks, remainder

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return
