from __future__ import annotations

from dataclasses import dataclass

from app.audio.mic_capture import MicrophoneCapture
from app.audio.system_loopback import SystemLoopbackCapture
from app.core.settings import AppSettings
from app.core.turn_manager import TurnInput, TurnManager
from app.llm.ollama_client import OllamaClient
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
        screen_text = None
        if self._state.screen_enabled:
            frame = self._screen.capture_once()
            if frame is not None:
                screen_text = self._ocr.extract_text(frame)

        system_audio_text = None
        if self._state.system_audio_enabled:
            system_audio_text = self._loopback.peek_context_text()

        messages = self._turn_manager.build_chat_messages(
            TurnInput(
                user_text=user_text,
                screen_text=screen_text,
                system_audio_text=system_audio_text,
            )
        )

        try:
            response = self._ollama.chat(messages)
        except Exception as exc:
            return (
                "I could not reach Ollama. Please make sure it is running and the model is available. "
                f"Details: {exc}"
            )

        self._tts.speak_async(response)
        return response
