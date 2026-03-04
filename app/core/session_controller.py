from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import time

from app.audio.mic_capture import MicrophoneCapture
from app.core.conversation_memory import ConversationMemoryStore
from app.audio.system_loopback import SystemLoopbackCapture
from app.core.settings import AppSettings
from app.core.turn_manager import TurnInput, TurnManager
from app.llm.ollama_client import OllamaClient
from app.llm.prompt_builder import available_personalities
from app.stt.whisper_service import WhisperService
from app.tts.piper_service import PiperTtsService
from app.vision.ocr import OcrService
from app.vision.screen_capture import ScreenCaptureService


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    system_audio_enabled: bool
    screen_enabled: bool


class SessionController:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._turn_manager = TurnManager()
        self._ollama = OllamaClient(settings.ollama)
        self._microphone = MicrophoneCapture(settings.audio)
        self._loopback = SystemLoopbackCapture(settings.audio)
        self._whisper = WhisperService()
        self._screen = ScreenCaptureService(settings.screen)
        self._ocr = OcrService()
        self._tts = PiperTtsService(settings.tts)
        self._system_audio_context: deque[str] = deque(maxlen=4)
        self._last_system_audio_capture_at = 0.0
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._microphone_device = settings.audio.microphone_device
        self._loopback_device = settings.audio.loopback_device
        self._personality = settings.assistant.personality
        self._remember_history = settings.assistant.remember_history
        self._memory = ConversationMemoryStore()
        self._last_metrics: dict[str, float | str] = {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
        }
        self._metrics_history: deque[dict[str, float | str]] = deque(maxlen=10)

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
    def personality(self) -> str:
        return self._personality

    def list_personalities(self) -> list[str]:
        return available_personalities()

    def set_personality(self, value: str) -> None:
        valid = set(available_personalities())
        self._personality = value if value in valid else "friendly"

    @property
    def chat_model(self) -> str:
        return self._settings.ollama.chat_model

    def set_chat_model(self, model_name: str) -> None:
        model_name = (model_name or "").strip()
        if model_name:
            self._settings.ollama.chat_model = model_name

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

    def clear_conversation_memory(self) -> None:
        self._memory.clear()

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
        screen_text = None
        if self._state.screen_enabled:
            frame = self._screen.capture_once()
            if frame is not None:
                screen_text = self._ocr.extract_text(frame)

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

        if self._remember_history:
            self._memory.add(role="user", content=user_text)
            self._memory.add(role="assistant", content=response)

        tts_started = time.perf_counter()
        tts_ms = 0.0
        if response:
            self._tts.speak_async(response)
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
        max_listen_seconds: float = 12.0,
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
