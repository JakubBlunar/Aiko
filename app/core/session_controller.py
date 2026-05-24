"""Lean session controller for Aiko (witty companion edition).

This is the single hub the UI talks to. It owns:

- Settings + chat database
- Ollama client + TurnRunner (the conversation loop)
- TtsQueue + TTS engine
- Microphone + RealtimeSTT
- Background workers: SummaryWorker, ProactiveDirector
- Embedded MCP server (optional, for Cursor debugging)

The earlier ~2700-line implementation is preserved on the ``legacy-v0`` git
tag if anything needs to be referenced. This rewrite drops:
  - The LangChain agent + tool dispatch + triage judge + autonomy planner
  - Embedding/recent-topics search
  - Live2D avatar
  - Action/agentic UI automation
  - Structured learner profile + 0.5B judge model

Public surface intentionally retains the method names the UI and MCP server
already use, so callers don't have to change.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.audio.earcons import EarconPlayer
from app.audio.mic_capture import MicrophoneCapture, list_output_devices
from app.core.chat_database import ChatDatabase
from app.core.crash_logging import log_event
from app.core.memory_extractor import MemoryExtractor
from app.core.memory_retriever import MemoryRetriever
from app.core.memory_store import MemoryStore
from app.core.persona_manager import PersonaManager
from app.core.proactive_director import ProactiveDirector
from app.core.prompt_assembler import PromptAssembler
from app.core.services.response_text_service import strip_action_meta_for_tts
from app.core.session_text_utils import (
    infer_tts_reaction,
    prepare_tts_text,
    sanitize_user_text,
)
from app.core.settings import AppSettings
from app.core.summary_worker import SummaryWorker
from app.core.tts_queue import TtsQueue
from app.core.turn_runner import TurnRunner
from app.llm.embedder import Embedder
from app.llm.ollama_client import OllamaClient
from app.stt.realtime_stt_service import RealtimeSttService


log = logging.getLogger("app.session")


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    autonomy_mode: str
    session_type: str


# ── Provider helpers (env-name fallback for OpenAI-compatible base URLs) ──

_PROVIDER_ENV_HINTS: tuple[tuple[str, str], ...] = (
    ("ollama.com", "OLLAMA_API_KEY"),
    ("api.openai.com", "OPENAI_API_KEY"),
    ("api.groq.com", "GROQ_API_KEY"),
    ("api.x.ai", "XAI_API_KEY"),
    ("openrouter.ai", "OPENROUTER_API_KEY"),
)


def _resolve_env_var_name(*, base_url: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    host = (base_url or "").lower()
    for needle, env_name in _PROVIDER_ENV_HINTS:
        if needle in host:
            return env_name
    return ""


# ── Controller ─────────────────────────────────────────────────────────


class SessionController:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._user_id = (settings.assistant.user_id or "default").strip() or "default"
        self._session_id = "main"

        # ── Chat LLM client (Ollama or Ollama Cloud) ──────────────────────
        chat_llm = settings.chat_llm
        chat_provider = (chat_llm.provider or "ollama").strip().lower()
        if chat_provider != "ollama":
            log.warning(
                "chat_llm.provider=%s is not supported in the lean rewrite; "
                "falling back to Ollama. Set the model on settings.ollama.chat_model.",
                chat_provider,
            )
        chat_base_url = (chat_llm.base_url or "").strip() or settings.ollama.base_url
        api_key_explicit = (chat_llm.api_key or "").strip()
        api_key_env_name = _resolve_env_var_name(
            base_url=chat_base_url, explicit=(chat_llm.api_key_env or "").strip(),
        )
        api_key = api_key_explicit or os.environ.get(api_key_env_name, "").strip()
        extra_headers = {
            str(k).strip(): str(v).strip()
            for k, v in dict(chat_llm.extra_headers or {}).items()
            if str(k).strip() and v is not None
        }
        self._ollama = OllamaClient(
            settings.ollama,
            base_url=chat_base_url,
            api_key=api_key or None,
            extra_headers=extra_headers or None,
        )
        self._chat_provider = "ollama"

        chat_model_override = (chat_llm.model or "").strip()
        self._effective_chat_model = (
            chat_model_override
            or (settings.ollama.chat_model or "").strip()
            or "llama3.1:8b"
        )

        # Resolve context window: explicit override > chat_llm > legacy ollama setting
        ctx_override = chat_llm.context_window or getattr(
            settings.ollama, "context_window", None
        )
        self._context_window = int(ctx_override) if ctx_override else 8192
        self._max_tokens = max(64, int(chat_llm.max_tokens or 512))
        temp = chat_llm.temperature
        if temp is None:
            temp = float(settings.ollama.temperature)
        self._temperature = float(temp)

        # ── Database ─────────────────────────────────────────────────────
        storage_path = (
            Path(__file__).resolve().parents[2] / "data" / "chat_sessions.db"
        )
        self._chat_db = ChatDatabase(storage_path)

        # ── Live2D persona manager ───────────────────────────────────────
        personas_root = Path(__file__).resolve().parents[2] / "data" / "personas"
        self._persona_manager = PersonaManager(personas_root)

        # ── Long-term memory (cross-session) ─────────────────────────────
        self._memory_settings = settings.memory
        self._embedder: Embedder | None = None
        self._memory_store: MemoryStore | None = None
        self._memory_retriever: MemoryRetriever | None = None
        self._memory_extractor: MemoryExtractor | None = None
        self._memory_listeners: list[Callable[[Any], None]] = []
        if self._memory_settings.enabled:
            try:
                self._embedder = Embedder(settings.ollama)
                self._memory_store = MemoryStore(
                    storage_path,
                    max_memories=self._memory_settings.max_memories,
                    dedupe_threshold=self._memory_settings.dedupe_threshold,
                )
                self._memory_retriever = MemoryRetriever(
                    self._memory_store,
                    self._embedder,
                    top_k=self._memory_settings.top_k,
                    score_threshold=self._memory_settings.score_threshold,
                )
            except Exception:
                log.warning("memory subsystem failed to initialise", exc_info=True)
                self._embedder = None
                self._memory_store = None
                self._memory_retriever = None

        # ── TTS engine + queue ───────────────────────────────────────────
        self._output_device = getattr(settings.audio, "output_device", None)
        self._tts_engine = self._build_tts_service(
            settings, output_device=self._output_device,
        )
        self._tts = TtsQueue(
            self._tts_engine,
            enabled=bool(settings.tts.enabled),
            state_listener=self._on_tts_state,
        )
        self._apply_assistant_preferences()

        # ── Microphone + STT ─────────────────────────────────────────────
        self._microphone = MicrophoneCapture(settings.audio)
        self._microphone_device = settings.audio.microphone_device
        self._earcons = EarconPlayer(
            enabled=getattr(settings.audio, "earcons_enabled", True),
            output_device=self._output_device,
        )
        self._realtime_stt = RealtimeSttService(settings.stt, settings.audio)

        # ── Prompt + workers + runner ────────────────────────────────────
        self._prompt_assembler = PromptAssembler(
            self._chat_db,
            memory_retriever=self._memory_retriever,
        )

        if (
            self._memory_settings.enabled
            and self._memory_settings.extractor_enabled
            and self._embedder is not None
            and self._memory_store is not None
        ):
            try:
                self._memory_extractor = MemoryExtractor(
                    self._chat_db,
                    self._memory_store,
                    self._embedder,
                    self._ollama,
                    model=self._effective_chat_model,
                )
                self._memory_extractor.add_listener(self._notify_memory_added)
            except Exception:
                log.warning("memory extractor failed to initialise", exc_info=True)
                self._memory_extractor = None

        self._summary_worker = SummaryWorker(
            self._chat_db,
            self._ollama,
            model=self._effective_chat_model,
            is_busy=lambda: self._turn_in_progress,
            memory_extractor=self._memory_extractor,
        )
        self._summary_worker.start()
        self._turn_runner = TurnRunner(
            self._ollama,
            self._chat_db,
            self._prompt_assembler,
            model=self._effective_chat_model,
            context_window=self._context_window,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            summary_worker=self._summary_worker,
            memory_store=self._memory_store,
            embedder=self._embedder,
            self_tagged_salience=self._memory_settings.self_tagged_salience,
            on_memory_added=self._notify_memory_added,
        )
        self._proactive = ProactiveDirector(
            self._ollama,
            self._chat_db,
            self._prompt_assembler,
            model=self._effective_chat_model,
            speak=self._tts.enqueue,
            is_busy=lambda: self._turn_in_progress,
            is_live_mode=lambda: self._live_voice_session_active,
            cooldown_seconds=float(
                getattr(settings.agent, "proactive_cooldown_seconds", 120.0),
            ),
            context_window=self._context_window,
        )

        # ── Runtime state ────────────────────────────────────────────────
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._live_input_mode = getattr(settings.audio, "live_input_mode", None) or "voice_detection"
        self._live_ptt_type = getattr(settings.audio, "live_ptt_type", None) or "keyboard"
        self._live_ptt_key = getattr(settings.audio, "live_ptt_key", None)
        self._live_ptt_mouse_button = getattr(settings.audio, "live_ptt_mouse_button", None)
        self._live_ptt_toggle = getattr(settings.audio, "live_ptt_toggle", False)
        self._ptt_active = False
        self._live_no_speech_streak = 0
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self._remember_history = settings.assistant.remember_history
        self._autonomy_mode = (
            str(getattr(settings.autonomy, "mode", "interactive") or "interactive").strip().lower()
        )
        if self._autonomy_mode not in {"manual", "interactive", "automatic"}:
            self._autonomy_mode = "interactive"
        self._active_goal = settings.autonomy.default_goal
        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            autonomy_mode=self._autonomy_mode,
            session_type="chat",
        )
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)

        # ── Metrics ──────────────────────────────────────────────────────
        self._last_metrics: dict[str, float | int | str] = self._zero_metrics()
        self._metrics_history: deque[dict[str, float | int | str]] = deque(maxlen=10)

        # ── Listeners ────────────────────────────────────────────────────
        self._message_listeners: list[Callable[[str, str], None]] = []
        self._tts_state_listeners: list[Callable[..., None]] = []
        self._tts_amplitude_listeners: list[Callable[[float], None]] = []
        self._tts.set_amplitude_listener(self._on_tts_amplitude)
        self._models_cache: list[str] | None = None
        self._models_cache_time = 0.0
        self._input_devices_cache: list[tuple[int, str]] | None = None
        self._input_devices_cache_time = 0.0
        self._output_devices_cache: list[tuple[int, str]] | None = None
        self._output_devices_cache_time = 0.0
        self._cache_ttl = 60.0

        # ── MCP debug server ─────────────────────────────────────────────
        self._mcp_server_runner = None
        if settings.mcp_server.enabled:
            try:
                from app.mcp.runner import McpServerRunner
                from app.mcp.server import create_mcp_server
                mcp_srv = create_mcp_server(self, port=settings.mcp_server.port)
                self._mcp_server_runner = McpServerRunner(
                    mcp_srv, port=settings.mcp_server.port,
                )
                self._mcp_server_runner.start()
            except Exception:
                log.warning("Failed to start embedded MCP server", exc_info=True)

    # ── State ─────────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    def update_sources(self, *, mic: bool) -> None:
        self._state.mic_enabled = bool(mic)

    @property
    def session_key(self) -> str:
        return f"{self._user_id}:{self._session_id}" if self._user_id else self._session_id

    def switch_session(self, session_id: str) -> None:
        self._session_id = session_id

    def new_session(self) -> str:
        new_id = str(uuid.uuid4())[:8]
        self.switch_session(new_id)
        return new_id

    def clear_conversation_memory(self) -> None:
        self._chat_db.clear_messages(self.session_key, full_reset=True)

    # ── Settings getters / setters ───────────────────────────────────

    @property
    def chat_model(self) -> str:
        return self._settings.ollama.chat_model

    @property
    def effective_chat_model(self) -> str:
        return self._effective_chat_model

    @property
    def context_window_size(self) -> int:
        return self._context_window

    @property
    def context_tokens_used(self) -> int:
        try:
            metrics = self._last_metrics
            return int(metrics.get("prompt_tokens", 0) or 0)
        except Exception:
            return 0

    def set_chat_model(self, model_name: str) -> None:
        normalized = (model_name or "").strip()
        if not normalized:
            return
        self._settings.ollama.chat_model = normalized
        self._effective_chat_model = normalized
        self._turn_runner.update_runtime(model=normalized)
        # Update the cached model on workers too.
        self._summary_worker._model = normalized  # type: ignore[attr-defined]
        self._proactive.update_runtime(model=normalized)
        if self._memory_extractor is not None:
            try:
                self._memory_extractor.update_model(normalized)
            except Exception:
                log.debug("memory extractor model update failed", exc_info=True)

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
        if normalized in {"manual", "interactive", "automatic"} and normalized != self._autonomy_mode:
            self._autonomy_mode = normalized
            self._state.autonomy_mode = normalized

    @property
    def active_goal(self) -> str:
        return self._active_goal

    @property
    def active_session_type(self) -> str:
        return "chat"

    def set_active_session_type(self, session_type: str) -> None:
        _ = session_type  # legacy no-op

    @property
    def agentic_narration_level(self) -> str:
        level = str(
            getattr(self._settings.autonomy, "agentic_narration_level", "summary") or "summary",
        ).strip().lower()
        return level if level in {"full", "summary", "off"} else "summary"

    def get_tooling_config_paths(self) -> tuple[Path, Path]:
        root = Path(__file__).resolve().parents[2]
        default = root / (
            self._settings.tooling.config_default_path or "config/tooling.default.json"
        )
        user = root / (
            self._settings.tooling.config_user_path or "config/tooling.user.json"
        )
        return (default, user)

    # ── Audio: VAD / mic / output devices ───────────────────────────

    def list_microphone_devices(self, *, refresh: bool = False) -> list[tuple[int, str]]:
        now = time.monotonic()
        if not refresh and self._input_devices_cache is not None and (now - self._input_devices_cache_time) < self._cache_ttl:
            return list(self._input_devices_cache)
        devices = self._microphone.list_input_devices()
        self._input_devices_cache = list(devices)
        self._input_devices_cache_time = now
        return devices

    def set_microphone_device(self, device_index: int | None) -> None:
        self._microphone_device = device_index
        self._microphone.set_device(device_index)

    @property
    def microphone_device(self) -> int | None:
        return self._microphone_device

    def list_output_devices(self, *, refresh: bool = False) -> list[tuple[int, str]]:
        now = time.monotonic()
        if not refresh and self._output_devices_cache is not None and (now - self._output_devices_cache_time) < self._cache_ttl:
            return list(self._output_devices_cache)
        try:
            devices = list_output_devices()
        except Exception:
            devices = []
        self._output_devices_cache = list(devices)
        self._output_devices_cache_time = now
        return devices

    def set_output_device(self, device_index: int | None) -> None:
        self._output_device = device_index
        rebuild = getattr(self._tts_engine, "set_output_device", None)
        if callable(rebuild):
            try:
                rebuild(device_index)
            except Exception:
                log.debug("tts engine rejected device switch", exc_info=True)
        try:
            self._earcons = EarconPlayer(
                enabled=getattr(self._settings.audio, "earcons_enabled", True),
                output_device=device_index,
            )
        except Exception:
            log.debug("earcons rebuild failed", exc_info=True)

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
        normalized = (mode or "").strip().lower()
        if normalized:
            self._live_input_mode = normalized

    @property
    def live_ptt_type(self) -> str:
        return self._live_ptt_type

    def set_live_ptt_type(self, ptt_type: str) -> None:
        self._live_ptt_type = (ptt_type or "keyboard").strip().lower() or "keyboard"

    @property
    def live_ptt_key(self) -> str | None:
        return self._live_ptt_key

    def set_live_ptt_key(self, key: str | None) -> None:
        self._live_ptt_key = (key or None) and str(key).strip()

    @property
    def live_ptt_mouse_button(self) -> str | None:
        return self._live_ptt_mouse_button

    def set_live_ptt_mouse_button(self, button: str | None) -> None:
        self._live_ptt_mouse_button = (button or None) and str(button).strip().lower()

    @property
    def live_ptt_toggle(self) -> bool:
        return bool(self._live_ptt_toggle)

    def set_live_ptt_toggle(self, value: bool) -> None:
        self._live_ptt_toggle = bool(value)

    def get_ptt_active(self) -> bool:
        return self._ptt_active

    def set_ptt_active(self, active: bool) -> None:
        self._ptt_active = bool(active)

    @property
    def vad_level_threshold(self) -> float:
        return float(self._vad_level_threshold)

    def set_vad_level_threshold(self, value: float) -> None:
        self._vad_level_threshold = float(value)

    @property
    def vad_silence_seconds(self) -> float:
        return float(self._vad_silence_seconds)

    def set_vad_silence_seconds(self, value: float) -> None:
        self._vad_silence_seconds = float(value)

    @property
    def stt_model(self) -> str:
        return str(self._settings.stt.model or "large-v1").strip() or "large-v1"

    def set_stt_model(self, model_name: str) -> bool:
        normalized = (model_name or "").strip()
        if not normalized:
            return False
        if normalized == self.stt_model:
            return True
        self._settings.stt.model = normalized
        candidate = RealtimeSttService(self._settings.stt, self._settings.audio)
        if not candidate.is_available:
            log.warning("Failed to load STT model: %s", normalized)
            return False
        self._realtime_stt = candidate
        return True

    @property
    def prosody_enabled(self) -> bool:
        return bool(getattr(self._settings.stt.prosody, "enabled", False))

    def set_prosody_enabled(self, value: bool) -> None:
        self._settings.stt.prosody.enabled = bool(value)

    @property
    def prosody_include_in_prompt(self) -> bool:
        return bool(getattr(self._settings.stt.prosody, "include_in_prompt", True))

    def set_prosody_include_in_prompt(self, value: bool) -> None:
        self._settings.stt.prosody.include_in_prompt = bool(value)

    @property
    def action_min_interval_seconds(self) -> float:
        return float(self._settings.actions.min_action_interval_seconds)

    def set_action_min_interval_seconds(self, value: float) -> None:
        self._settings.actions.min_action_interval_seconds = max(0.0, float(value))

    # ── TTS API ──────────────────────────────────────────────────────

    @property
    def tts_provider(self) -> str:
        return (self._settings.tts.provider or "pocket-tts").strip().lower() or "pocket-tts"

    def list_tts_providers(self) -> list[str]:
        return ["pocket-tts"]

    @property
    def tts_voice(self) -> str:
        return self._settings.tts.voice or ""

    def list_tts_voices(self) -> list[str]:
        list_voices = getattr(self._tts_engine, "list_voices", None)
        if callable(list_voices):
            try:
                voices = list_voices()
                if voices:
                    return list(voices)
            except Exception:
                pass
        return []

    def set_tts_voice(self, voice: str) -> None:
        normalized = (voice or "").strip()
        if not normalized:
            return
        self._settings.tts.voice = normalized
        set_voice = getattr(self._tts_engine, "set_voice", None)
        if callable(set_voice):
            try:
                set_voice(normalized)
            except Exception:
                log.debug("tts engine rejected voice switch", exc_info=True)

    def get_tts_model_status(self) -> tuple[str, str]:
        getter = getattr(self._tts_engine, "model_status", None)
        if callable(getter):
            try:
                state, details = getter()
                return str(state), str(details)
            except Exception:
                pass
        return ("unknown", "")

    def stop_tts(self) -> None:
        self._tts.stop()

    def is_tts_playing(self) -> bool:
        return self._tts.is_active()

    def speak_text(self, text: str) -> bool:
        if not bool(getattr(self._settings.tts, "enabled", True)):
            return False
        prepared = prepare_tts_text(text or "")
        if not prepared:
            return False
        reaction = infer_tts_reaction(prepared)
        self._tts.enqueue(prepared, reaction=reaction)
        return True

    def set_tts_provider(self, provider: str) -> None:
        normalized = (provider or "").strip().lower() or "pocket-tts"
        if normalized == self.tts_provider:
            return
        try:
            self._tts.stop()
        except Exception:
            pass
        self._settings.tts.provider = normalized
        self._tts_engine = self._build_tts_service(
            self._settings, output_device=self._output_device,
        )
        self._tts = TtsQueue(
            self._tts_engine,
            enabled=bool(self._settings.tts.enabled),
            state_listener=self._on_tts_state,
            amplitude_listener=self._on_tts_amplitude,
        )
        self._apply_assistant_preferences()
        self._trace("tts.provider", f"Switched TTS provider to {normalized}")

    def prewarm_tts(self) -> None:
        warmup_sync = getattr(self._tts_engine, "warmup_sync", None)
        if callable(warmup_sync):
            try:
                warmup_sync()
            except Exception:
                log.debug("tts warmup_sync failed", exc_info=True)
            return
        warmup_async = getattr(self._tts_engine, "warmup_async", None)
        if callable(warmup_async):
            try:
                warmup_async()
            except Exception:
                log.debug("tts warmup_async failed", exc_info=True)

    def prewarm_runtime(self, on_status: Callable[[str], None] | None = None) -> None:
        def report(message: str) -> None:
            if on_status:
                on_status(message)

        effective = self._effective_chat_model
        cloud_model = effective.endswith("-cloud") or effective.endswith(":cloud")
        report("Checking Ollama availability...")
        try:
            models = self._ollama.list_models()
        except Exception as exc:
            raise RuntimeError(f"Failed to reach Ollama server: {exc}") from exc
        if not cloud_model and effective not in models:
            raise RuntimeError(
                f"Chat model not found in Ollama: {effective}. "
                f"Pull it with: ollama pull {effective}",
            )
        if cloud_model:
            report(f"Using Ollama Cloud model: {effective} (no local warmup)")
        else:
            report(f"Warming chat model: {effective}")
            try:
                self._ollama.chat(
                    [{"role": "user", "content": "Reply with OK."}],
                    model=effective,
                )
            except Exception as exc:
                log.warning("chat model warmup failed: %s", exc)

        report("Warming TTS models...")
        self.prewarm_tts()
        report("Warmup complete")

    # ── Greetings + proactive ────────────────────────────────────────

    def build_startup_greeting(self) -> str:
        return "Welcome back. Audio is ready."

    def generate_proactive_message(self) -> str | None:
        # The new ProactiveDirector speaks directly via TTS. Returning ``None``
        # tells LiveWorker not to also queue something itself.
        self._proactive.notify_silence(self.session_key)
        return None

    def set_live_voice_session_active(self, active: bool) -> None:
        self._live_voice_session_active = bool(active)
        self._state.session_type = "live" if active else "chat"

    # ── Listeners ────────────────────────────────────────────────────

    # ── Persona ─────────────────────────────────────────────────────

    @property
    def persona_manager(self) -> PersonaManager:
        return self._persona_manager

    # ── Memory accessors ────────────────────────────────────────────

    @property
    def memory_store(self) -> "MemoryStore | None":
        return self._memory_store

    @property
    def memory_extractor(self) -> "MemoryExtractor | None":
        return self._memory_extractor

    def list_memories(
        self,
        *,
        limit: int = 50,
        order: str = "recent",
    ) -> list[dict[str, Any]]:
        store = self._memory_store
        if store is None:
            return []
        if order == "top":
            mems = store.list_top(limit=limit)
        else:
            mems = store.list_recent(limit=limit)
        return [m.to_dict() for m in mems]

    def delete_memory(self, memory_id: int) -> bool:
        if self._memory_store is None:
            return False
        return self._memory_store.delete(int(memory_id))

    def add_memory_listener(self, callback: Callable[[Any], None]) -> None:
        if callback and callback not in self._memory_listeners:
            self._memory_listeners.append(callback)

    def _notify_memory_added(self, memory: Any) -> None:
        for listener in list(self._memory_listeners):
            try:
                listener(memory)
            except Exception:
                log.debug("memory listener raised", exc_info=True)

    def add_message_listener(self, callback: Callable[[str, str], None]) -> None:
        if callback and callback not in self._message_listeners:
            self._message_listeners.append(callback)

    def _notify_message(self, speaker: str, text: str) -> None:
        for listener in list(self._message_listeners):
            try:
                listener(speaker, text)
            except Exception:
                log.debug("message listener raised", exc_info=True)

    def add_tts_state_listener(self, callback: Callable[..., None]) -> None:
        if callback and callback not in self._tts_state_listeners:
            self._tts_state_listeners.append(callback)

    def _on_tts_state(self, event: str, payload: dict[str, Any]) -> None:
        for listener in list(self._tts_state_listeners):
            try:
                listener(event, **payload)
            except Exception:
                log.debug("tts state listener raised", exc_info=True)

    def add_tts_amplitude_listener(self, callback: Callable[[float], None]) -> None:
        if callback and callback not in self._tts_amplitude_listeners:
            self._tts_amplitude_listeners.append(callback)

    def _on_tts_amplitude(self, level: float) -> None:
        for listener in list(self._tts_amplitude_listeners):
            try:
                listener(float(level))
            except Exception:
                log.debug("tts amplitude listener raised", exc_info=True)

    # ── Models listing ───────────────────────────────────────────────

    def list_chat_models(self, *, refresh: bool = False) -> list[str]:
        now = time.monotonic()
        if not refresh and self._models_cache is not None and (now - self._models_cache_time) < self._cache_ttl:
            return list(self._models_cache)
        try:
            models = self._ollama.list_models()
        except Exception:
            models = []
        current = self.chat_model
        if current and current not in models:
            models.insert(0, current)
        self._models_cache = list(models)
        self._models_cache_time = now
        return models

    # ── Decision trace + emergency stop (legacy stubs) ──────────────

    def get_decision_trace(self, max_entries: int = 300) -> list[dict[str, str]]:
        items = list(self._decision_trace)
        if max_entries >= len(items):
            return items
        return items[-max_entries:]

    def clear_decision_trace(self) -> None:
        self._decision_trace.clear()

    @property
    def emergency_hotkey(self) -> str:
        return self._settings.actions.emergency_hotkey

    @property
    def emergency_stop_active(self) -> bool:
        return False

    def reset_emergency_stop(self) -> None:
        return

    @property
    def has_pending_action(self) -> bool:
        return False

    @property
    def pending_action_description(self) -> str:
        return "none"

    def approve_pending_action(self) -> tuple[str, str | None]:
        return ("No pending action.", None)

    def reject_pending_action(self) -> str:
        return "No pending action."

    def start_action_hotkey_listener(self) -> bool:
        return False

    def stop_action_hotkey_listener(self) -> None:
        return

    def tts_text_for_followup(self, followup: str) -> str:
        if self.agentic_narration_level == "off":
            return ""
        return self.summarize_followup_for_tts(followup)

    @staticmethod
    def summarize_followup_for_tts(followup: str, *, max_steps: int = 3) -> str:
        source = strip_action_meta_for_tts(str(followup or "")).strip()
        if not source:
            return ""
        compact = " ".join(source.split())
        if len(compact) > 220:
            compact = compact[:217].rstrip() + "..."
        _ = max_steps
        return compact

    # ── Metrics ─────────────────────────────────────────────────────

    @staticmethod
    def _zero_metrics() -> dict[str, float | int | str]:
        return {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def get_last_metrics(self) -> dict[str, float | int | str]:
        return dict(self._last_metrics)

    def get_average_metrics(self) -> dict[str, float | str]:
        if not self._metrics_history:
            return {
                "window": 0,
                "capture_ms": 0.0, "stt_ms": 0.0, "llm_ms": 0.0,
                "tts_ms": 0.0, "total_ms": 0.0,
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
        self._last_metrics = self._zero_metrics()
        self._metrics_history.clear()

    def get_reading_status(self) -> dict[str, bool | int | str]:
        return {"active": False, "window": "", "chunks": 0, "scroll_steps": 0, "max_scroll_steps": 0}

    def get_agentic_status(self) -> dict[str, bool | int | str]:
        return {"active": False, "objective": "", "auto_steps": 0, "max_auto_steps": 0}

    def get_conversation_memory(self, max_entries: int = 200) -> list[dict[str, str]]:
        rows = self._chat_db.get_messages(self.session_key, limit=max_entries)
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at}
            for r in rows
        ]

    # ── The chat loop ────────────────────────────────────────────────

    def chat_once(self, user_text: str) -> str:
        return self.chat_once_streaming(user_text=user_text, mode="typed")

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        mode: str = "typed",
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
        user_vocal_tone: str | None = None,
    ) -> str:
        _ = user_vocal_tone  # not used in v1; reserved for prosody hints
        cleaned = sanitize_user_text(user_text or "")
        if not cleaned:
            return ""

        if on_generation_status:
            on_generation_status("AI is generating response...")

        # If chat history is disabled, replay the message into a transient key
        # so we never persist it across restarts.
        session_key = self.session_key if self._remember_history else f"{self.session_key}:noremember"

        self._turn_in_progress = True
        t0 = time.perf_counter()
        try:
            result = self._turn_runner.run(
                session_key,
                cleaned,
                on_token=on_token,
                on_tts_chunk=self._tts.enqueue if bool(self._settings.tts.enabled) else None,
                stop_requested=stop_requested,
            )
        finally:
            self._turn_in_progress = False

        llm_ms = (time.perf_counter() - t0) * 1000.0
        total_ms = capture_ms + stt_ms + llm_ms
        self._set_last_metrics({
            "mode": mode,
            "capture_ms": round(capture_ms, 1),
            "stt_ms": round(stt_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "tts_ms": 0.0,
            "total_ms": round(total_ms, 1),
            "prompt_tokens": int(result.usage.prompt_tokens),
            "completion_tokens": int(result.usage.completion_tokens),
            "total_tokens": int(result.usage.total_tokens),
        })
        return result.text

    def _set_last_metrics(self, metrics: dict[str, float | int | str]) -> None:
        self._last_metrics = dict(metrics)
        self._metrics_history.append(dict(metrics))

    # ── Voice capture ────────────────────────────────────────────────

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
                "RealtimeSTT is not available. Install with: pip install realtimestt",
            )
        capture_started = time.perf_counter()
        text = self._realtime_stt.record_until_silence(
            max_seconds=max(3.0, min(seconds, 30.0)),
            silence_seconds=float(self._vad_silence_seconds),
        )
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        if not text:
            raise RuntimeError("No speech was detected from microphone audio.")
        text = sanitize_user_text(text)
        if not text:
            raise RuntimeError("No clear speech was detected from microphone audio.")
        self._trace("stt.mic", f"record transcribe ({len(text)} chars)")
        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            on_generation_status=on_generation_status,
            mode="record",
            capture_ms=capture_ms,
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
                "RealtimeSTT is not available. Install with: pip install realtimestt",
            )

        live_level_threshold = max(0.004, float(self._vad_level_threshold) * 0.4)
        if self._live_no_speech_streak > 0:
            relax = min(0.7, 0.18 * float(self._live_no_speech_streak))
            live_level_threshold = max(0.002, live_level_threshold * (1.0 - relax))
        end_threshold = max(0.004, float(self._vad_level_threshold) * 0.4)
        silence_seconds = min(6.0, max(1.5, float(self._vad_silence_seconds) + 0.4))
        use_webrtc = self._live_no_speech_streak < 3

        if on_generation_status:
            on_generation_status("listening")
        capture_started = time.perf_counter()
        wav_path = self._microphone.capture_phrase_to_wav(
            max_seconds=max_listen_seconds,
            max_wait_for_speech_start_seconds=12.0,
            use_webrtc_vad=use_webrtc,
            silence_seconds_to_stop=silence_seconds,
            level_threshold=live_level_threshold,
            end_level_threshold=end_threshold,
            min_speech_seconds_before_stop=1.5,
            speech_start_grace_seconds=0.8,
            max_seconds_after_speech_start=18.0,
            stop_requested=stop_requested,
            on_speech_start=None,
            on_audio_level=on_audio_level,
        )
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        if wav_path is None:
            self._live_no_speech_streak += 1
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
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")
        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt",
            )
        if on_generation_status:
            on_generation_status("push-to-talk")
        return self._microphone.capture_while_ptt_active(
            ptt_active_getter=ptt_active_getter,
            stop_requested=stop_requested,
            on_audio_level=on_audio_level,
            max_seconds=max_seconds,
        )

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
        try:
            self._earcons.play("listening")
        except Exception:
            pass
        try:
            if on_generation_status:
                on_generation_status("transcribing")
            stt_started = time.perf_counter()
            text = self._realtime_stt.transcribe(wav_path)
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
        finally:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except Exception:
                pass

        if not text:
            self._live_no_speech_streak += 1
            if on_generation_status:
                on_generation_status("did not catch that, listening")
            return None
        text = sanitize_user_text(text)
        if not text:
            self._live_no_speech_streak += 1
            if on_generation_status:
                on_generation_status("did not catch that, listening")
            return None
        self._live_no_speech_streak = 0
        self._trace("stt.mic", f"live transcribe ({len(text)} chars)")
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

    def run_stt_diagnostic(
        self,
        *,
        seconds: float = 5.0,
        vad_filter: bool = True,
        initial_prompt: str = "",
    ) -> dict[str, object]:
        if not self._state.mic_enabled:
            return {"ok": False, "reason": "mic-disabled", "message": "Microphone source is disabled."}
        if not self._realtime_stt.is_available:
            return {"ok": False, "reason": "stt-missing", "message": "RealtimeSTT not installed."}
        try:
            text = self._realtime_stt.record_until_silence(
                max_seconds=max(3.0, min(seconds, 30.0)),
                silence_seconds=float(self._vad_silence_seconds),
            )
        except Exception as exc:
            return {"ok": False, "reason": "exception", "message": str(exc)}
        return {
            "ok": True,
            "stt_model": self.stt_model,
            "transcription": (text or "").strip(),
            "vad_filter": bool(vad_filter),
            "initial_prompt": initial_prompt or "",
        }

    # ── Internals ───────────────────────────────────────────────────

    def _apply_assistant_preferences(self) -> None:
        length_scale = getattr(self._settings.assistant, "tts_length_scale", 1.0) or 1.0
        set_length = getattr(self._tts_engine, "set_length_scale", None)
        if callable(set_length):
            try:
                set_length(length_scale)
            except Exception:
                log.debug("tts engine rejected length scale", exc_info=True)

    def _trace(self, stage: str, message: str) -> None:
        from datetime import datetime, timezone
        self._decision_trace.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "message": message,
        })
        if "error" in stage.lower():
            try:
                log_event(stage, message)
            except Exception:
                pass

    @staticmethod
    def _build_tts_service(settings: AppSettings, output_device: int | None = None) -> Any:
        # Lean v1 ships only pocket-tts (matches the active user.json config).
        # Kokoro / PyKokoro were removed -- restore them in v2 if needed.
        from app.tts.pocket_tts_service import PocketTtsService
        return PocketTtsService(settings.tts, output_device=output_device)

    # ── Shutdown ────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._mcp_server_runner is not None:
            try:
                self._mcp_server_runner.stop()
            except Exception:
                log.debug("mcp stop failed", exc_info=True)
        try:
            self._tts.stop()
        except Exception:
            pass
        try:
            self._summary_worker.stop()
        except Exception:
            pass
        if self._memory_store is not None:
            try:
                self._memory_store.close()
            except Exception:
                log.debug("memory store close failed", exc_info=True)
        if self._embedder is not None:
            try:
                self._embedder.close()
            except Exception:
                log.debug("embedder close failed", exc_info=True)
        try:
            t = threading.Thread(target=self._realtime_stt.stop_context, daemon=True)
            t.start()
            t.join(timeout=2.0)
        except Exception:
            pass


