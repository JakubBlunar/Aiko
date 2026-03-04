from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from app.actions.emergency_stop import EmergencyStopState, GlobalHotkeyListener
from app.actions.executor import ActionExecutionResult, GuardedActionExecutor, PlannedAction
from app.audio.mic_capture import MicrophoneCapture
from app.core.conversation_memory import ConversationMemoryStore
from app.core.crash_logging import log_event
from app.audio.system_loopback import SystemLoopbackCapture
from app.core.settings import AppSettings, OllamaSettings
from app.core.turn_manager import TurnInput, TurnManager
from app.llm.ollama_client import OllamaClient
from app.llm.prompt_builder import available_personalities
from app.stt.whisper_service import WhisperService
from app.tts.llasa_service import LlasaTtsService
from app.tts.piper_service import PiperTtsService
from app.vision.ocr import OcrService
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


@dataclass(slots=True)
class GoalInference:
    goal: str
    confidence: float
    reason: str


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
        self._ocr = OcrService()
        self._tts = self._build_tts_service(settings)
        self._action_stop_state = EmergencyStopState()
        self._action_hotkey_listener = GlobalHotkeyListener(
            hotkey=settings.actions.emergency_hotkey,
            state=self._action_stop_state,
        )
        self._action_executor = GuardedActionExecutor(settings.actions, self._action_stop_state)
        self._system_audio_context: deque[str] = deque(maxlen=4)
        self._last_system_audio_capture_at = 0.0
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._microphone_device = settings.audio.microphone_device
        self._loopback_device = settings.audio.loopback_device
        self._personality = settings.assistant.personality
        self._remember_history = settings.assistant.remember_history
        self._memory = ConversationMemoryStore()
        self._active_goal = settings.autonomy.default_goal
        self._last_screen_capture_at = 0.0
        self._last_screen_decision_at = 0.0
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
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)

        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            system_audio_enabled=settings.audio.enable_system_audio,
            screen_enabled=settings.screen.enable_screen_context,
        )

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

    def set_vad_level_threshold(self, value: float) -> None:
        self._vad_level_threshold = max(0.001, min(value, 0.5))

    def set_vad_silence_seconds(self, value: float) -> None:
        self._vad_silence_seconds = max(0.2, min(value, 3.0))

    @property
    def action_min_interval_seconds(self) -> float:
        return float(self._settings.actions.min_action_interval_seconds)

    def set_action_min_interval_seconds(self, value: float) -> None:
        self._settings.actions.min_action_interval_seconds = max(0.0, float(value))

    @property
    def personality(self) -> str:
        return self._personality

    def list_personalities(self) -> list[str]:
        return available_personalities()

    def set_personality(self, value: str) -> None:
        valid = set(available_personalities())
        self._personality = value if value in valid else "friendly"

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
        self._ollama.chat([
            {"role": "user", "content": "Reply with OK."},
        ])

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
        entries = self._memory.recent_entries(max_entries=max_entries)
        return [
            {"role": entry.role, "content": entry.content, "timestamp": entry.timestamp}
            for entry in entries
        ]

    def chat_once(self, user_text: str) -> str:
        return self.chat_once_streaming(user_text=user_text, mode="typed")

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        on_token: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        mode: str = "typed",
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
    ) -> str:
        turn_start = time.perf_counter()
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

        messages = self._turn_manager.build_chat_messages(
            TurnInput(
                user_text=user_text,
                screen_text=screen_text,
                system_audio_text=system_audio_text,
                personality=self._personality,
                memory_messages=(self._memory.recent_messages(12) if self._remember_history else None),
                assistant_strategy=autonomy_plan.strategy,
                active_goal=self._active_goal,
            )
        )

        try:
            llm_started = time.perf_counter()
            if on_token is None:
                response = self._ollama.chat(messages)
            else:
                pieces: list[str] = []
                for token in self._ollama.chat_stream(messages):
                    if stop_requested and stop_requested():
                        break
                    pieces.append(token)
                    on_token(token)
                response = "".join(pieces).strip()
            llm_ms = (time.perf_counter() - llm_started) * 1000.0
        except Exception as exc:
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
            return response

        action_result = self._maybe_execute_action(
            user_text=user_text,
            assistant_reply=response,
            screen_text=screen_text,
            allow_planning_override=autonomy_plan.should_plan_action,
        )
        if action_result is not None and action_result.message:
            response = f"{response}\n\n[Action] {action_result.message}".strip()

        if self._remember_history:
            self._memory.add(role="user", content=user_text)
            self._memory.add(role="assistant", content=response)

        tts_started = time.perf_counter()
        tts_ms = 0.0
        if response:
            try:
                self._tts.speak_async(response)
            except Exception as exc:
                self._trace("tts.error", f"TTS speak failed: {exc}")
            tts_ms = (time.perf_counter() - tts_started) * 1000.0

        self._set_last_metrics({
            "mode": mode,
            "capture_ms": round(capture_ms, 1),
            "stt_ms": round(stt_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "tts_ms": round(tts_ms, 1),
            "total_ms": round((time.perf_counter() - turn_start) * 1000.0, 1),
        })
        return response

    def _plan_turn_autonomy(self, *, user_text: str) -> TurnAutonomyPlan:
        defaults = TurnAutonomyPlan(
            strategy="Respond naturally, concise first, ask one focused follow-up when useful.",
            should_use_screen=False,
            should_plan_action=False,
            ask_followup=True,
            confidence=0.4,
        )

        if not self._settings.autonomy.enabled:
            return defaults

        recent_memory = self._memory.recent_messages(6)
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
                            "Plan the assistant's next turn for natural autonomous conversation. "
                            "Return exactly one JSON object with keys: "
                            "strategy, should_use_screen, should_plan_action, ask_followup, confidence. "
                            "strategy must be one short sentence under 180 chars. "
                            "Use booleans for flags and confidence in range 0.0-1.0."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            "Context:\n"
                            f"- active_goal={self._active_goal}\n"
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

        plan = TurnAutonomyPlan(
            strategy=strategy,
            should_use_screen=bool(payload.get("should_use_screen", False)),
            should_plan_action=bool(payload.get("should_plan_action", False)),
            ask_followup=bool(payload.get("ask_followup", True)),
            confidence=confidence,
        )

        if not self._settings.autonomy.allow_action_suggestions:
            plan.should_plan_action = False
        if not self._settings.autonomy.allow_proactive_actions:
            plan.should_plan_action = False
        if not self._settings.autonomy.proactive_conversation:
            plan.ask_followup = False

        self._trace(
            "autonomy.plan",
            (
                f"strategy='{plan.strategy}' | screen={plan.should_use_screen} | "
                f"action={plan.should_plan_action} | followup={plan.ask_followup} | "
                f"confidence={round(plan.confidence, 2)}"
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
            self._trace(
                "autonomy.goal",
                (
                    f"Switched goal {previous} -> {self._active_goal} "
                    f"(confidence={round(inferred.confidence, 2)}; reason={inferred.reason or 'n/a'})"
                ),
            )

    def _infer_goal(self, *, user_text: str) -> GoalInference:
        recent_memory = self._memory.recent_messages(8)
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
                            "Return exactly one JSON object with keys: goal, confidence, reason. "
                            "goal must be one of: general_conversation, english_practice, coding_help, "
                            "ui_automation, learning_coach, troubleshooting. "
                            "confidence is 0.0-1.0."
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

        allowed_goals = {
            "general_conversation",
            "english_practice",
            "coding_help",
            "ui_automation",
            "learning_coach",
            "troubleshooting",
        }
        goal = str(payload.get("goal", self._active_goal)).strip().lower() or self._active_goal
        if goal not in allowed_goals:
            goal = self._active_goal

        confidence = float(payload.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(confidence, 1.0))
        reason = str(payload.get("reason", "")).strip()
        return GoalInference(goal=goal, confidence=confidence, reason=reason)

    def _should_capture_screen(self, *, user_text: str) -> tuple[bool, str]:
        normalized = (user_text or "").lower()
        if not self._state.screen_enabled:
            return False, "disabled"

        now = time.monotonic()
        min_interval = max(1, int(self._settings.screen.capture_interval_seconds))
        if (now - self._last_screen_capture_at) < min_interval:
            return False, "interval"

        keyword_triggers = (
            "screen",
            "on my screen",
            "look at",
            "what do you see",
            "what can you see",
            "this page",
            "this window",
            "this code",
            "here",
            "shown",
        )
        if any(token in normalized for token in keyword_triggers):
            return True, "keyword"

        decision_mode = (self._settings.screen.decision_mode or "model").lower().strip()
        if decision_mode == "keywords":
            return False, "keywords-only"

        cooldown = max(1, int(self._settings.screen.decision_cooldown_seconds))
        if (now - self._last_screen_decision_at) < cooldown:
            return False, "decision-cooldown"
        self._last_screen_decision_at = now

        recent_memory = self._memory.recent_messages(4)
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
        frame = self._screen.capture_once()
        self._last_screen_capture_at = time.monotonic()
        if frame is None:
            self._trace("screen.capture", "Screen capture unavailable.")
            return None

        text = (self._ocr.extract_text(frame) or "").strip()
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
                f"Captured screen context ({len(text)} chars, source={decision_source})."
            ),
        )
        return text

    def run_screen_ocr_diagnostic(self) -> dict[str, object]:
        frame = self._screen.capture_once()
        self._last_screen_capture_at = time.monotonic()
        if frame is None:
            return {
                "ok": False,
                "reason": "capture-unavailable",
                "message": "Screen capture unavailable.",
            }

        details = self._ocr.extract_details(frame)
        if not details:
            return {
                "ok": False,
                "reason": "ocr-empty",
                "message": "OCR returned no text.",
            }

        text = str(details.get("text") or "").strip()
        text = " ".join(text.split())
        if not text:
            return {
                "ok": False,
                "reason": "ocr-empty",
                "message": "OCR returned no readable text.",
            }

        min_chars = max(0, int(self._settings.screen.min_ocr_chars))
        return {
            "ok": True,
            "reason": "ok",
            "message": "OCR diagnostic captured text.",
            "chars": len(text),
            "min_chars": min_chars,
            "passes_min_chars": len(text) >= min_chars,
            "line_count": int(details.get("line_count") or 0),
            "avg_confidence": float(details.get("avg_confidence") or 0.0),
            "text": text,
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
    ) -> ActionExecutionResult | None:
        if not self._settings.actions.enabled:
            return None

        if self._settings.actions.max_actions_per_turn < 1:
            return None

        mode = (self._settings.actions.decision_mode or "explicit_only").lower().strip()
        if mode == "explicit_only" and not allow_planning_override and not self._has_action_intent(user_text):
            self._trace("action.plan", "Skipped action planning (no explicit action intent).")
            return None

        if not screen_text and self._state.screen_enabled:
            screen_text = self._capture_screen_text(decision_source="action")

        planned = self._plan_action(
            user_text=user_text,
            assistant_reply=assistant_reply,
            screen_text=screen_text,
        )
        result = self._action_executor.execute(planned)
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
    ) -> PlannedAction:
        recent_memory = self._memory.recent_messages(4)
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
                            "You are an action planner for desktop UI automation. "
                            "Return exactly one JSON object with keys: "
                            "type, x, y, text, confidence, reason. "
                            "Allowed types: none, click, type_text. "
                            "Use click only when you can infer a concrete target. "
                            "If uncertain, return type='none'. "
                            "confidence must be 0.0 to 1.0."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Latest user message:\n{user_text.strip()}\n\n"
                            f"Assistant draft reply:\n{assistant_reply.strip()}\n\n"
                            f"Recent conversation:\n{chr(10).join(recent_lines) or '[none]'}\n\n"
                            f"OCR screen text:\n{(screen_text or '[none]')[:5000]}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("action.plan", "Planner unavailable. Falling back to no action.")
            return PlannedAction(kind="none", reason="Action planner unavailable")

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            self._trace("action.plan", "Planner output was not valid JSON. Falling back to no action.")
            return PlannedAction(kind="none", reason="Planner output was not valid JSON")

        raw_kind = str(payload.get("type", "none")).strip().lower()
        if raw_kind not in {"none", "click", "type_text"}:
            raw_kind = "none"

        confidence = float(payload.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(confidence, 1.0))

        planned = PlannedAction(
            kind=raw_kind,
            x=(int(payload["x"]) if payload.get("x") is not None else None),
            y=(int(payload["y"]) if payload.get("y") is not None else None),
            text=(str(payload.get("text", "")).strip() or None),
            confidence=confidence,
            reason=str(payload.get("reason", "")).strip(),
        )
        self._trace(
            "action.plan",
            (
                "Planned "
                f"{planned.kind} (confidence={round(planned.confidence, 2)}; "
                f"reason={planned.reason or 'n/a'})"
            ),
        )
        return planned

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

    def record_and_chat(self, seconds: float = 5.0) -> tuple[str, str]:
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

        preview = " ".join(text.strip().split())
        if len(preview) > 180:
            preview = f"{preview[:177]}..."
        self._trace("stt.mic", f"record transcribe ({len(text)} chars): {preview}")

        response = self.chat_once_streaming(
            user_text=text,
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
    ) -> tuple[str, str] | None:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")

        if not self._whisper.is_available:
            raise RuntimeError(
                "Whisper is not installed. Install AI extras with: pip install -e .[ai]"
            )

        capture_started = time.perf_counter()
        wav_path = self._microphone.capture_phrase_to_wav(
            max_seconds=max_listen_seconds,
            silence_seconds_to_stop=self._vad_silence_seconds,
            level_threshold=self._vad_level_threshold,
            min_speech_seconds_before_stop=1.2,
            speech_start_grace_seconds=0.5,
            stop_requested=stop_requested,
            on_speech_start=self._tts.stop,
            on_audio_level=on_audio_level,
        )
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        if wav_path is None:
            return None

        try:
            stt_started = time.perf_counter()
            text = self._whisper.transcribe(str(wav_path))
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
        finally:
            self._safe_unlink(wav_path)

        if not text:
            return None

        preview = " ".join(text.strip().split())
        if len(preview) > 180:
            preview = f"{preview[:177]}..."
        self._trace("stt.mic", f"live transcribe ({len(text)} chars): {preview}")

        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            stop_requested=stop_requested,
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
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return
