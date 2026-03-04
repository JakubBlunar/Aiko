from __future__ import annotations

from app.core.settings import AudioSettings


class MicrophoneCapture:
    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings

    def start(self) -> None:
        return

    def stop(self) -> None:
        return
