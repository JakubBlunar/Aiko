from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.audio.mic_capture import MicrophoneCapture
from app.audio.system_loopback import SystemLoopbackCapture
from app.core.settings import AppSettings
from app.core.turn_manager import TurnInput, TurnManager
from app.llm.ollama_client import OllamaClient
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
    ) -> tuple[str, str] | None:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")

        if not self._whisper.is_available:
            raise RuntimeError(
                "Whisper is not installed. Install AI extras with: pip install -e .[ai]"
            )

        wav_path = self._microphone.capture_phrase_to_wav(
            max_seconds=max_listen_seconds,
            silence_seconds_to_stop=1.0,
            level_threshold=0.02,
            stop_requested=stop_requested,
            on_speech_start=self._tts.stop,
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

        samples = self._loopback.capture_seconds(seconds=seconds)
        if samples is None:
            return None

        wav_path = self._microphone.create_temp_wav_path(prefix="assistant_loopback_")
        try:
            self._microphone.write_wav(samples=samples, target_path=wav_path)
            return self._whisper.transcribe(str(wav_path))
        finally:
            self._safe_unlink(wav_path)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return
