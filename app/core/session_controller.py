from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import time

from app.audio.mic_capture import MicrophoneCapture
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

    def chat_once(self, user_text: str) -> str:
        return self.chat_once_streaming(user_text=user_text)

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        on_token: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> str:
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
            )
        )

        try:
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
        except Exception as exc:
            return (
                "I could not reach Ollama. Please make sure it is running and the model is available. "
                f"Details: {exc}"
            )

        if stop_requested and stop_requested():
            return response

        if response:
            self._tts.speak_async(response)
        return response

    def record_and_chat(self, seconds: float = 5.0) -> tuple[str, str]:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")

        if not self._whisper.is_available:
            raise RuntimeError(
                "Whisper is not installed. Install AI extras with: pip install -e .[ai]"
            )

        wav_path = self._microphone.capture_to_wav(seconds=seconds)
        try:
            text = self._whisper.transcribe(str(wav_path))
        finally:
            self._safe_unlink(wav_path)

        if not text:
            raise RuntimeError("No speech was detected from microphone audio.")

        response = self.chat_once(text)
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

        wav_path = self._microphone.capture_phrase_to_wav(
            max_seconds=max_listen_seconds,
            silence_seconds_to_stop=self._vad_silence_seconds,
            level_threshold=self._vad_level_threshold,
            stop_requested=stop_requested,
            on_speech_start=self._tts.stop,
            on_audio_level=on_audio_level,
        )
        if wav_path is None:
            return None

        try:
            text = self._whisper.transcribe(str(wav_path))
        finally:
            self._safe_unlink(wav_path)

        if not text:
            return None

        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            stop_requested=stop_requested,
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
