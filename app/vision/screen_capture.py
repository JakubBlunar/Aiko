from __future__ import annotations

from app.core.settings import ScreenSettings


class ScreenCaptureService:
    def __init__(self, settings: ScreenSettings) -> None:
        self._settings = settings

    def capture_once(self):
        try:
            import mss
        except Exception:
            return None

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            return sct.grab(monitor)
