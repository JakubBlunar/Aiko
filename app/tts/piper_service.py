from __future__ import annotations

from app.core.settings import TtsSettings


class PiperTtsService:
    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings

    def speak_async(self, text: str) -> None:
        if not self._settings.enabled:
            return
        return
