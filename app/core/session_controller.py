from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any

from app.core.action_intent import has_action_intent
from app.core.tooling import load_tooling_config, resolve_toolkit_entries
from app.core.tooling.runtime.emergency_stop import EmergencyStopState, GlobalHotkeyListener
from app.core.tooling.types import ToolError, ToolResult
from app.audio.mic_capture import MicrophoneCapture
from app.core.crash_logging import log_event, log_handled_exception
from app.core.settings import AppSettings, OllamaSettings
from app.core.services.response_text_service import (
    extract_tts_reaction_tag,
    parse_reaction_at_start,
    parse_two_tier_reply,
    strip_action_meta_for_tts,
    strip_all_reaction_tags,
)
from app.core.session_text_utils import (
    drain_tts_stream_chunks,
    infer_tts_reaction,
    prepare_tts_text,
    sanitize_assistant_text,
    sanitize_user_text,
)
from app.llm.langchain_agent import create_agent, run_agent
from app.llm.ollama_client import OllamaClient
from app.stt.prosody_fast import FastProsodyAnalyzer, ProsodyAnalysis
from app.stt.realtime_stt_service import RealtimeSttService
from app.tts.kokoro_service import KokoroTtsService


def _get_assistant_background(settings: AppSettings) -> str | None:
    """Resolve assistant background: from background_path file if set and readable, else from background string."""
    path_raw = (getattr(settings.assistant, "background_path", None) or "").strip()
    if path_raw:
        root = Path(__file__).resolve().parents[2]
        path = (root / path_raw) if not Path(path_raw).is_absolute() else Path(path_raw)
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception:
                pass
    return (settings.assistant.background or "").strip() or None


class _StubMemory:
    def clear(self) -> None:
        pass

    def recent_entries(self, max_entries: int = 10) -> list:
        return []


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    screen_enabled: bool
    autonomy_mode: str
    session_type: str


@dataclass(slots=True)
class GoalInference:
    goal: str
    confidence: float
    reason: str
    description: str = ""
    session_type: str = "chat"


@dataclass(slots=True)
class TurnAutonomyPlan:
    strategy: str
    should_use_screen: bool
    should_plan_action: bool
    ask_followup: bool
    confidence: float
    action_intent: str = ""


class SessionController:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        # Initialize trace buffer first because background startup paths (e.g. MCP stderr)
        # can emit trace events before the controller has fully finished bootstrapping.
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)
        self._ollama = OllamaClient(settings.ollama)
        self._microphone = MicrophoneCapture(settings.audio)
        self._realtime_stt = RealtimeSttService(settings.stt, settings.audio)
        self._prosody = FastProsodyAnalyzer(enabled=bool(settings.stt.prosody.enabled))
        self._prosody_include_in_prompt = bool(settings.stt.prosody.include_in_prompt)
        self._action_stop_state = EmergencyStopState()
        self._action_hotkey_listener = GlobalHotkeyListener(
            hotkey=settings.actions.emergency_hotkey,
            state=self._action_stop_state,
        )
        self._memory = _StubMemory()
        self._mcp_servers = []

        storage_path = Path(__file__).resolve().parents[2] / "data" / "chat_sessions.db"
        db_provider = getattr(settings.database, "provider", "sqlite") or "sqlite"
        db_url = getattr(settings.database, "url", None)
        root = Path(__file__).resolve().parents[2]
        tooling_config = load_tooling_config(
            default_path=root / (getattr(settings.tooling, "config_default_path", "config/tooling.default.json") or "config/tooling.default.json"),
            user_path=root / (getattr(settings.tooling, "config_user_path", "config/tooling.user.json") or "config/tooling.user.json"),
        )
        toolkit_entries = resolve_toolkit_entries(tooling_config)
        agent_settings = getattr(settings, "agent", None)
        num_history_runs = int(agent_settings.num_history_runs) if agent_settings else 10
        compress_tool_results = bool(agent_settings.compress_tool_results) if agent_settings else True
        compress_limit = getattr(agent_settings, "compress_tool_results_limit", None) if agent_settings else None
        compress_token_limit = getattr(agent_settings, "compress_token_limit", None) if agent_settings else None
        chat_model = (settings.ollama.chat_model or "").strip() or "llama3.1:8b"
        mcp_settings = tooling_config.tool_settings("mcp")
        mcp_enabled = bool(mcp_settings.get("enabled", False))
        self._effective_chat_model = chat_model
        self._agent = create_agent(
            chat_model=chat_model,
            base_url=settings.ollama.base_url,
            temperature=float(settings.ollama.temperature),
            assistant_background=_get_assistant_background(settings),
            add_tools=True,
            add_mcp=mcp_enabled,
            database_provider=db_provider,
            database_url=db_url,
            storage_path=storage_path,
            toolkit_entries=toolkit_entries or None,
            num_history_runs=num_history_runs,
            compress_tool_results=compress_tool_results,
            compress_tool_results_limit=compress_limit,
            compress_token_limit=compress_token_limit,
            mcp_config=mcp_settings if mcp_enabled else None,
            project_root=root,
        )
        self._user_id = settings.assistant.user_id or "default"
        self._microphone_device = settings.audio.microphone_device
        self._output_device = getattr(settings.audio, "output_device", None)
        self._tts = self._build_tts_service(settings, output_device=self._output_device)
        self._apply_assistant_preferences()
        self._session_id = "main"
        self._tts_playing = False
        self._pending_tts_chunks: list[tuple[str, str | None]] = []
        self._tts_queue_lock = threading.Lock()
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._live_input_mode = getattr(settings.audio, "live_input_mode", None) or "voice_detection"
        self._live_ptt_type = getattr(settings.audio, "live_ptt_type", None) or "keyboard"
        self._live_ptt_key = getattr(settings.audio, "live_ptt_key", None)
        self._live_ptt_mouse_button = getattr(settings.audio, "live_ptt_mouse_button", None)
        self._live_ptt_toggle = getattr(settings.audio, "live_ptt_toggle", False)
        self._ptt_active = False
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
        self._last_screen_text = ""
        self._last_screen_text_at = 0.0
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
            session_type="chat",
        )
        self._startup_context_prewarm_enabled = False
        self._startup_history_limit = 1
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

    def list_output_devices(self) -> list[tuple[int, str]]:
        from app.audio.mic_capture import list_output_devices as _list
        return _list()

    def set_output_device(self, device_index: int | None) -> None:
        self._output_device = device_index
        set_od = getattr(self._tts, "set_output_device", None)
        if set_od is not None:
            set_od(device_index)

    @property
    def output_device(self) -> int | None:
        return self._output_device

    def barge_in_enabled(self) -> bool:
        return bool(getattr(self._settings.audio, "barge_in_enabled", False))

    def set_barge_in_enabled(self, enabled: bool) -> None:
        self._settings.audio.barge_in_enabled = bool(enabled)

    @property
    def live_input_mode(self) -> str:
        return self._live_input_mode

    def set_live_input_mode(self, mode: str) -> None:
        self._live_input_mode = (str(mode or "").strip() or "voice_detection").lower()
        if self._live_input_mode not in ("voice_detection", "push_to_talk"):
            self._live_input_mode = "voice_detection"

    @property
    def live_ptt_type(self) -> str:
        return self._live_ptt_type

    def set_live_ptt_type(self, ptt_type: str) -> None:
        t = (str(ptt_type or "").strip().lower() or "keyboard")
        self._live_ptt_type = t if t in ("keyboard", "mouse") else "keyboard"

    @property
    def live_ptt_key(self) -> str | None:
        return self._live_ptt_key

    def set_live_ptt_key(self, key: str | None) -> None:
        self._live_ptt_key = (str(key).strip() or None) if key is not None else None

    @property
    def live_ptt_mouse_button(self) -> str | None:
        return self._live_ptt_mouse_button

    def set_live_ptt_mouse_button(self, button: str | None) -> None:
        self._live_ptt_mouse_button = (str(button).strip().lower() or None) if button is not None else None

    @property
    def live_ptt_toggle(self) -> bool:
        return self._live_ptt_toggle

    def set_live_ptt_toggle(self, value: bool) -> None:
        self._live_ptt_toggle = bool(value)

    def get_ptt_active(self) -> bool:
        return self._ptt_active

    def set_ptt_active(self, active: bool) -> None:
        self._ptt_active = bool(active)

    @property
    def vad_level_threshold(self) -> float:
        return self._vad_level_threshold

    @property
    def vad_silence_seconds(self) -> float:
        return self._vad_silence_seconds

    @property
    def stt_model(self) -> str:
        return str(self._settings.stt.model or "large-v1").strip() or "large-v1"

    @property
    def prosody_enabled(self) -> bool:
        return bool(self._prosody.enabled)

    def set_prosody_enabled(self, value: bool) -> None:
        enabled = bool(value)
        self._settings.stt.prosody.enabled = enabled
        self._prosody.set_enabled(enabled)

    @property
    def prosody_include_in_prompt(self) -> bool:
        return bool(self._prosody_include_in_prompt)

    def set_prosody_include_in_prompt(self, value: bool) -> None:
        include = bool(value)
        self._settings.stt.prosody.include_in_prompt = include
        self._prosody_include_in_prompt = include

    def set_stt_model(self, model_name: str) -> bool:
        normalized = str(model_name or "").strip()
        if not normalized:
            return False
        if normalized == self.stt_model:
            return True
        self._settings.stt.model = normalized
        candidate = RealtimeSttService(self._settings.stt, self._settings.audio)
        if not candidate.is_available:
            self._trace("stt.error", f"Failed to load STT model: {normalized}")
            return False
        self._realtime_stt = candidate
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
        return (self._settings.tts.provider or "kokoro").strip().lower() or "kokoro"

    def list_tts_providers(self) -> list[str]:
        return ["kokoro"]

    @property
    def tts_voice(self) -> str:
        return str(self._settings.tts.voice or "").strip()

    def list_tts_voices(self) -> list[str]:
        # Kokoro voice profiles from voices-v1.0.bin (common ones)
        voices = ["af_heart", "af_bella", "af_nicole", "af_sarah", "am_adam", "am_michael", "bf_emma", "bf_isabella"]
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

    def _on_tts_chunk_done(self) -> None:
        next_chunk: tuple[str, str | None] | None = None
        with self._tts_queue_lock:
            self._tts_playing = False
            if self._pending_tts_chunks:
                next_chunk = self._pending_tts_chunks.pop(0)
                self._tts_playing = True
        if next_chunk:
            text, reaction = next_chunk
            try:
                self._tts.speak_async(
                    text,
                    reaction=reaction,
                    on_done=self._on_tts_chunk_done,
                )
            except Exception as exc:
                self._trace("tts.error", f"TTS queue speak failed: {exc}")
                with self._tts_queue_lock:
                    self._tts_playing = False

    def _enqueue_tts_chunk(self, text: str, reaction: str | None = None) -> None:
        if not (text or "").strip():
            return
        msg = prepare_tts_text(text.strip())
        if not msg:
            return
        with self._tts_queue_lock:
            self._pending_tts_chunks.append((msg, reaction))
            if self._tts_playing:
                return
            self._tts_playing = True
            chunk = self._pending_tts_chunks.pop(0)
        try:
            self._tts.speak_async(
                chunk[0],
                reaction=chunk[1],
                on_done=self._on_tts_chunk_done,
            )
        except Exception as exc:
            self._trace("tts.error", f"TTS queue speak failed: {exc}")
            with self._tts_queue_lock:
                self._tts_playing = False

    def stop_tts(self) -> None:
        with self._tts_queue_lock:
            self._pending_tts_chunks.clear()
            self._tts_playing = False
        try:
            self._tts.stop()
        except Exception:
            pass

    def is_tts_playing(self) -> bool:
        with self._tts_queue_lock:
            return self._tts_playing or len(self._pending_tts_chunks) > 0

    def speak_text(self, text: str) -> bool:
        if not bool(getattr(self._settings.tts, "enabled", True)):
            return False
        message = prepare_tts_text(text)
        if not message:
            return False
        reaction = infer_tts_reaction(message)
        self._enqueue_tts_chunk(message, reaction=reaction)
        return True

    def build_startup_greeting(self) -> str:
        return "Welcome back. Audio is ready."

    def prewarm_tts(self) -> None:
        tts = self._tts
        warmup_sync = getattr(tts, "warmup_sync", None)
        if callable(warmup_sync):
            try:
                ok = bool(warmup_sync())
                if not ok:
                    state, details = self.get_tts_model_status()
                    self._trace("tts.error", f"TTS warmup failed ({state}): {details}")
            except Exception as exc:
                self._trace("tts.error", f"TTS warmup failed: {exc}")
            return
        warmup_async = getattr(tts, "warmup_async", None)
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

        effective = self.effective_chat_model
        if effective not in models:
            raise RuntimeError(
                f"Response model not found in Ollama: {effective}. Pull it first (e.g. ollama pull {effective})."
            )
        report(f"Warming response model: {effective}")
        self._ollama.chat(self._build_startup_prewarm_messages(), model=effective)

        report("Warming TTS models...")
        tts = self._tts
        warmup_sync = getattr(tts, "warmup_sync", None)
        if callable(warmup_sync):
            success = bool(warmup_sync())
            if not success:
                state, details = self.get_tts_model_status()
                raise RuntimeError(f"TTS warmup failed ({state}): {details}")
        else:
            self.prewarm_tts()

        self._check_mcp_runtime_prewarm(report)
        report("Warmup complete")

    def _build_startup_prewarm_messages(self) -> list[dict[str, str]]:
        return [{"role": "user", "content": "Reply with OK."}]

    def _check_mcp_runtime_prewarm(self, report: Callable[[str], None]) -> None:
        try:
            status = self.get_mcp_runtime_status()
        except Exception as exc:
            self._trace("mcp.error", f"Failed to read MCP runtime status during preload: {exc}")
            raise RuntimeError(f"Failed to check MCP runtime: {exc}") from exc
        if not isinstance(status, dict):
            self._trace("mcp.error", "Invalid MCP runtime status payload during preload.")
            raise RuntimeError("Invalid MCP runtime status payload.")
        enabled = bool(status.get("enabled", False))
        if not enabled:
            report("MCP disabled; skipping MCP preload checks")
            return
        server_count = int(status.get("server_count", 0) or 0)
        connected_count = int(status.get("connected_count", 0) or 0)
        report(f"Checking MCP servers: {connected_count}/{server_count} running")
        if server_count <= 0:
            raise RuntimeError("MCP is enabled but no servers are configured or running.")
        if connected_count <= 0:
            raise RuntimeError("MCP is enabled but no MCP servers are running.")
        if connected_count < server_count:
            self._trace(
                "mcp.warn",
                f"MCP preload check partial readiness: {connected_count}/{server_count} server(s) running.",
            )

    def set_tts_provider(self, provider: str) -> None:
        normalized = (provider or "").strip().lower() or "kokoro"
        if normalized != "kokoro":
            normalized = "kokoro"
        if normalized == self.tts_provider:
            return
        try:
            self._tts.stop()
        except Exception:
            pass
        self._settings.tts.provider = normalized
        self._tts = self._build_tts_service(self._settings, output_device=self._output_device)
        self._apply_assistant_preferences()
        self._trace("tts.provider", f"Switched TTS provider to {normalized}")

    @property
    def chat_model(self) -> str:
        return self._settings.ollama.chat_model

    @property
    def effective_chat_model(self) -> str:
        """Model used for the reply."""
        return getattr(self, "_effective_chat_model", None) or self._settings.ollama.chat_model

    def set_chat_model(self, model_name: str) -> None:
        model_name = (model_name or "").strip()
        if model_name:
            self._settings.ollama.chat_model = model_name

    def get_tooling_config_paths(self) -> tuple[Path, Path]:
        """Return (default_path, user_path) for tooling config (e.g. for Settings Coding tab)."""
        root = Path(__file__).resolve().parents[2]
        default = root / (self._settings.tooling.config_default_path or "config/tooling.default.json")
        user = root / (self._settings.tooling.config_user_path or "config/tooling.user.json")
        return (default, user)

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
        self._sync_action_confirmation_policy()
        self._trace("autonomy.mode", f"Switched autonomy mode {previous} -> {normalized}")

    def _sync_action_confirmation_policy(self) -> None:
        mode = str(getattr(self, "_autonomy_mode", "interactive") or "interactive").strip().lower()
        require_confirmation = mode != "automatic"
        settings = getattr(self, "_settings", None)
        actions = getattr(settings, "actions", None) if settings is not None else None
        if actions is None:
            return
        setattr(actions, "require_confirmation", bool(require_confirmation))
        self._trace(
            "action.confirmation.policy",
            f"mode={mode} require_confirmation={str(require_confirmation).lower()}",
        )

    def _trace_turn_action_policy(self) -> None:
        settings = getattr(self, "_settings", None)
        actions = getattr(settings, "actions", None) if settings is not None else None
        if actions is None:
            return
        mode = str(getattr(self, "_autonomy_mode", "interactive") or "interactive").strip().lower()
        require_confirmation = bool(getattr(actions, "require_confirmation", True))
        self._trace(
            "action.confirmation",
            f"turn mode={mode} require_confirmation={str(require_confirmation).lower()}",
        )

    def set_active_session_type(self, session_type: str) -> None:
        """No-op: LangChain agent manages conversation; session type is not used."""
        _ = session_type

    def clear_conversation_memory(self) -> None:
        self._memory.clear()

    @property
    def active_goal(self) -> str:
        return self._active_goal

    @property
    def active_session_type(self) -> str:
        return "chat"

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
        return self._action_stop_state.triggered

    def reset_emergency_stop(self) -> None:
        self._action_stop_state.reset()

    @property
    def has_pending_action(self) -> bool:
        return False

    @property
    def pending_action_description(self) -> str:
        return "none"

    def approve_pending_action(self) -> tuple[str, str | None]:
        return ("No pending action.", None)

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
        return "No pending action."

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
        return {
            "active": False,
            "window": "",
            "chunks": 0,
            "scroll_steps": 0,
            "max_scroll_steps": 0,
        }

    def get_agentic_status(self) -> dict[str, bool | int | str]:
        return {
            "active": False,
            "objective": "",
            "auto_steps": 0,
            "max_auto_steps": 0,
        }

    def get_mcp_runtime_status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "server_count": 0,
            "connected_count": 0,
            "servers": [],
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

    @staticmethod
    def _build_memory_assistant_text(response: str) -> str:
        cleaned = strip_action_meta_for_tts(str(response or ""))
        cleaned = sanitize_assistant_text(cleaned)
        return cleaned.strip()

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
    ) -> ToolResult:
        _ = cancel_token
        self._trace("tool.invoke", f"{name} args={self._preview_tool_args(dict(args or {}))} (stubbed)")
        return ToolResult(
            success=False,
            error=ToolError(code="stub", message="Tooling disabled; agent-only mode."),
        )

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

        screen_intent = False

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

        stream_tts_used = False
        stream_tts_enqueued_chunks: list[int] = [0]
        llm_ms = 0.0
        tool_calls_executed = False

        try:
            if on_generation_status:
                on_generation_status("AI is generating response...")
            self._trace("pipeline.llm.start", f"mode={mode} model={self.chat_model}")
            llm_started = time.perf_counter()
            message_for_agent = user_text
            if user_vocal_tone and user_vocal_tone.strip():
                message_for_agent = f"{user_text}\n\n[Vocal: {user_vocal_tone.strip()}]"
            def _tool_status(name: str, summary: str) -> None:
                if on_generation_status:
                    msg = f"Tool: {name} - {summary[:80]}{'...' if len(summary) > 80 else ''}" if summary else f"Tool: {name}"
                    on_generation_status(msg)

            agent_to_use = self._agent
            use_stream_tts = mode == "live"
            if use_stream_tts:
                stream_buffer: list[str] = []
                reply_mood: list[str | None] = [None]
                last_ui_sent_len: list[int] = [0]

                def _on_content(delta: str) -> None:
                    if not delta:
                        return
                    stream_buffer.append(delta)
                    buf = "".join(stream_buffer)
                    if reply_mood[0] is None:
                        mood, rest = parse_reaction_at_start(buf)
                        if mood is not None:
                            reply_mood[0] = mood
                            stream_buffer.clear()
                            stream_buffer.append(rest)
                            buf = rest
                    chunks, remainder = drain_tts_stream_chunks(buf, flush=False)
                    for sent in chunks:
                        tts_text = prepare_tts_text(strip_action_meta_for_tts(sent))
                        if tts_text:
                            self._enqueue_tts_chunk(tts_text, reaction=reply_mood[0] or "neutral")
                            stream_tts_enqueued_chunks[0] += 1
                    stream_buffer.clear()
                    stream_buffer.append(remainder)
                    if on_token and reply_mood[0] is not None:
                        display_buf = strip_all_reaction_tags(buf)
                        to_send = display_buf[last_ui_sent_len[0]:]
                        if to_send:
                            on_token(to_send)
                        last_ui_sent_len[0] = len(display_buf)

                response = run_agent(
                    agent_to_use,
                    message_for_agent,
                    session_id=self._session_id,
                    user_id=self._user_id,
                    stream=True,
                    on_content=_on_content,
                    on_tool_use=_tool_status if on_generation_status else None,
                    stop_requested=stop_requested,
                )
                buf = "".join(stream_buffer)
                final_chunks, _ = drain_tts_stream_chunks(buf, flush=True)
                for sent in final_chunks:
                    tts_text = prepare_tts_text(strip_action_meta_for_tts(sent))
                    if tts_text:
                        self._enqueue_tts_chunk(tts_text, reaction=reply_mood[0] or "neutral")
                        stream_tts_enqueued_chunks[0] += 1
                stream_tts_used = True
            else:
                response = run_agent(
                    agent_to_use,
                    message_for_agent,
                    session_id=self._session_id,
                    user_id=self._user_id,
                    stream=False,
                    on_tool_use=_tool_status if on_generation_status else None,
                )

            response = sanitize_assistant_text(response)
            llm_ms = (time.perf_counter() - llm_started) * 1000.0
            if not response or not response.strip():
                self._trace(
                    "pipeline.llm.empty",
                    f"mode={mode} llm_ms={round(llm_ms, 1)} agent returned empty; check LangChain/Ollama response shape",
                )
                response = "I didn’t get a reply from the model. Try again or check the console for details."
            llm_for_tts = strip_action_meta_for_tts(response)
            spoken_part, full_for_display = parse_two_tier_reply(llm_for_tts)
            display_for_ui = (sanitize_assistant_text(full_for_display).strip() if full_for_display.strip() else response) or response
            if on_token and display_for_ui and not use_stream_tts:
                on_token(display_for_ui)
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
            display_for_ui = f"— {explicit_reaction}\n\n" + (display_for_ui or "")

        if stop_requested and stop_requested():
            self._trace("pipeline.turn.stopped", f"mode={mode} stop_requested=true")
            return display_for_ui

        # Action execution delegated to agent (MCP/registry tools). No separate action pipeline.
        # Two-tier: spoken_part for TTS, full_for_display (display_for_ui) for transcript; already computed above.

        tts_started = time.perf_counter()
        tts_ms = 0.0
        should_speak_full_response = bool(response and not stream_tts_used)
        if stream_tts_used and stream_tts_enqueued_chunks[0] < 1:
            self._trace("pipeline.tts.enqueue", "stream mode used but no chunks enqueued; fallback full TTS")
            should_speak_full_response = bool(response)
        self._trace(
            "pipeline.tts.plan",
            (
                f"stream_used={stream_tts_used} "
                f"queued_chunks={stream_tts_enqueued_chunks[0]} "
                f"full_response={'yes' if should_speak_full_response else 'no'}"
            ),
        )
        if response and stream_tts_used:
            try:
                should_speak_full_response = not self.is_tts_playing()
            except Exception:
                should_speak_full_response = True

        if should_speak_full_response:
            try:
                tts_content = strip_all_reaction_tags(spoken_part) if spoken_part.strip() else strip_all_reaction_tags(llm_for_tts)
                tts_text = prepare_tts_text(tts_content) if tts_content.strip() else ""
                if tts_text:
                    reaction = explicit_reaction or infer_tts_reaction(spoken_part or llm_for_tts)
                    self._enqueue_tts_chunk(tts_text, reaction=reaction)
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
        return display_for_ui

    def _apply_assistant_preferences(self) -> None:
        length_scale = getattr(self._settings.assistant, "tts_length_scale", 1.0) or 1.0
        set_length_scale = getattr(self._tts, "set_length_scale", None)
        if callable(set_length_scale):
            try:
                set_length_scale(length_scale)
            except Exception:
                pass

    def _response_num_predict(self) -> int:
        style = str(getattr(self._settings.assistant, "response_style", "balanced") or "balanced").strip().lower()
        if style == "concise":
            return 96
        if style == "detailed":
            return 220
        return 140

    def _plan_turn_autonomy(self, *, user_text: str) -> TurnAutonomyPlan:
        default_strategy = "Respond naturally, concise first, ask one focused follow-up when useful."
        if not self._settings.autonomy.enabled:
            return TurnAutonomyPlan(
                strategy=default_strategy,
                should_use_screen=False,
                should_plan_action=False,
                ask_followup=True,
                confidence=0.4,
                action_intent="",
            )

        if self._is_agentic_intent(user_text):
            return TurnAutonomyPlan(
                strategy=(
                    "Confirm agentic mode is active and ask for the first objective. "
                    "Do not propose or perform UI actions in this turn."
                ),
                should_use_screen=False,
                should_plan_action=False,
                ask_followup=True,
                confidence=1.0,
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

        normalized = str(user_text or "").strip().lower()
        turn_cfg = self._settings.autonomy.turn_planning
        proactive = bool(turn_cfg.proactive_conversation)
        allow_action_suggestions = bool(turn_cfg.allow_action_suggestions)
        allow_proactive_actions = bool(turn_cfg.allow_proactive_actions)
        explicit_action_intent = has_action_intent(user_text)
        wants_screen = self._is_screen_intent(user_text)
        should_plan_action = (
            bool(self._settings.actions.enabled)
            and allow_action_suggestions
            and (explicit_action_intent or allow_proactive_actions)
        )
        strategy = default_strategy
        if "reading" in normalized or "continue" in normalized:
            strategy = "Continue the active reading flow and summarize key points before the next step."
        elif should_plan_action:
            strategy = "Confirm the requested UI action, then execute safely and report the result."
        elif proactive:
            strategy = "Answer directly and ask one targeted follow-up if it helps move the task forward."

        plan = TurnAutonomyPlan(
            strategy=strategy[: max(40, int(turn_cfg.max_strategy_chars))],
            should_use_screen=bool(wants_screen),
            should_plan_action=bool(should_plan_action),
            ask_followup=bool(proactive and not should_plan_action),
            confidence=0.7 if should_plan_action else 0.6,
            action_intent=(str(user_text or "").strip() if should_plan_action else ""),
        )

        if self._autonomy_mode == "automatic":
            plan.ask_followup = False
            plan.should_use_screen = bool(self._state.screen_enabled)
            plan.should_plan_action = bool(self._settings.actions.enabled)
            if plan.should_plan_action and not plan.action_intent:
                plan.action_intent = "Execute the next best UI step for the active objective."
            self._trace("autonomy.mode", "Automatic mode forced action planning and disabled follow-up prompt.")
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

        goal_changed = inferred.goal != self._active_goal
        if not goal_changed:
            return

        previous_goal = self._active_goal
        self._active_goal = inferred.goal
        self._active_goal_description = inferred.description
        self._trace(
            "autonomy.goal",
            (
                f"Switched goal {previous_goal} -> {self._active_goal} "
                f"(confidence={round(inferred.confidence, 2)}; reason={inferred.reason or 'n/a'}; "
                f"desc={inferred.description or 'n/a'})."
            ),
        )

    def _infer_goal(self, *, user_text: str) -> GoalInference:
        normalized = str(user_text or "").strip().lower()
        if not normalized:
            return GoalInference(goal=self._active_goal, confidence=0.0, reason="empty_user_text")

        if self._is_agentic_intent(user_text):
            return GoalInference(
                goal="ui_automation",
                confidence=0.95,
                reason="explicit_agentic_intent",
                description="User asked for autonomous or agentic operation.",
                session_type="agentic",
            )

        if (
            "read" in normalized
            or "article" in normalized
            or "continue" in normalized
            or "scroll" in normalized
        ):
            return GoalInference(
                goal="reading_assistance",
                confidence=0.72,
                reason="reading_keywords",
                description="User is asking to read or continue screen content.",
                session_type="reading",
            )

        if any(token in normalized for token in ("code", "python", "bug", "test", "refactor")):
            return GoalInference(
                goal="coding_help",
                confidence=0.7,
                reason="coding_keywords",
                description="User is asking for coding help.",
                session_type="chat",
            )

        if has_action_intent(user_text):
            return GoalInference(
                goal="ui_automation",
                confidence=0.68,
                reason="action_intent_detected",
                description="User requested a desktop action.",
                session_type="agentic",
            )

        return GoalInference(
            goal="general_conversation",
            confidence=0.55,
            reason="default_conversation",
            description="General conversational request.",
            session_type="chat",
        )

    @staticmethod
    def _is_agentic_intent(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = (
            "go fully automatic",
            "fully automatic",
            "enter agentic mode",
            "start agentic session",
            "agentic mode",
            "work autonomously",
        )
        return any(token in lowered for token in tokens)

    def _is_screen_intent(self, user_text: str) -> bool:
        """Stub: screen context disabled in agent-only mode."""
        return False

    def run_screen_ocr_diagnostic(self) -> dict[str, object]:
        """Stub: OCR disabled in agent-only mode."""
        return {
            "ok": False,
            "reason": "disabled",
            "message": "Screen OCR is disabled in agent-only mode.",
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

        if not self._realtime_stt.is_available:
            return {
                "ok": False,
                "reason": "stt-unavailable",
                "message": "RealtimeSTT is unavailable.",
            }

        duration = max(1.0, min(float(seconds), 30.0))
        capture_started = time.perf_counter()
        wav_path = self._microphone.capture_to_wav(seconds=duration)
        capture_ms = (time.perf_counter() - capture_started) * 1000.0

        prosody: ProsodyAnalysis | None = None
        try:
            stt_started = time.perf_counter()
            text = self._realtime_stt.transcribe(wav_path)
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
        try:
            log_event(stage_text, message_text)
        except Exception:
            pass

    @staticmethod
    def _build_tts_service(settings: AppSettings, output_device: int | None = None):
        provider = (settings.tts.provider or "kokoro").strip().lower()
        return KokoroTtsService(settings.tts, output_device=output_device)

    def record_and_chat(
        self,
        seconds: float = 5.0,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")

        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt"
            )

        capture_started = time.perf_counter()
        text = self._realtime_stt.record_until_silence(
            max_seconds=max(3.0, min(seconds, 30.0)),
            silence_seconds=float(self._vad_silence_seconds),
        )
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        stt_ms = 0.0
        prosody: ProsodyAnalysis | None = None

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

        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt"
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

    def capture_ptt_phrase(
        self,
        *,
        ptt_active_getter: Callable[[], bool],
        stop_requested: Callable[[], bool] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
        max_seconds: float = 30.0,
    ) -> tuple[Path, float] | None:
        """Record while ptt_active_getter() is True; return (wav_path, capture_ms) or None."""
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")
        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt"
            )
        if on_generation_status:
            on_generation_status("push-to-talk")
        result = self._microphone.capture_while_ptt_active(
            ptt_active_getter=ptt_active_getter,
            stop_requested=stop_requested,
            on_audio_level=on_audio_level,
            max_seconds=max_seconds,
        )
        return result

    def process_live_capture(
        self,
        *,
        wav_path: Path,
        capture_ms: float,
        stop_requested: Callable[[], bool] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str] | None:
        if not self._realtime_stt.is_available:
            return None

        prosody: ProsodyAnalysis | None = None
        try:
            if on_generation_status:
                on_generation_status("transcribing")
            self._trace("pipeline.stt.start", f"capture_ms={round(capture_ms, 1)}")
            stt_started = time.perf_counter()
            text = self._realtime_stt.transcribe(wav_path)
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
                "Emergency stop is active. Reset emergency stop before resuming."
            )
        return ""

    def _stop_agentic_for_emergency(self, *, source: str) -> bool:
        if not bool(getattr(self, "emergency_stop_active", False)):
            return False

        mode = str(getattr(self, "_autonomy_mode", "interactive") or "interactive").strip().lower()
        if mode == "automatic":
            self._autonomy_mode = "interactive"
            state = getattr(self, "_state", None)
            if state is not None:
                setattr(state, "autonomy_mode", "interactive")
            self._sync_action_confirmation_policy()

        self._trace(
            "autonomy.safety",
            f"Emergency stop is active (source={source}); mode set to interactive.",
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
        # No separate planner: single agent handles conversation and tools.
        # Return done so autonomy does not run a separate planning LLM.
        self._trace("agentic.plan.output", "done=true reason=single_agent_no_planner")
        return {
            "done": True,
            "progress_note": "",
            "next_tool": "",
            "next_args": {},
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
            return f"Autonomy mode set to {self._autonomy_mode}."

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
        self._mcp_servers = []
        self._trace("mcp.start", "MCP tools loaded from config when enabled.")

    def _build_agno_tools_from_registry(self) -> list[Any]:
        """Stub: agent uses MCP tools from config; no registry tools."""
        return []
