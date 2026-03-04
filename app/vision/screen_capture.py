from __future__ import annotations

from app.core.settings import ScreenSettings


class ScreenCaptureService:
    def __init__(self, settings: ScreenSettings) -> None:
        self._settings = settings

    def capture_once(self):
        try:
            import mss
            import numpy as np
        except Exception:
            return None

        with mss.mss() as sct:
            monitors = sct.monitors
            monitor_index = int(getattr(self._settings, "monitor_index", 1))

            if not monitors:
                return None

            if monitor_index < 0:
                monitor_index = 1
            if monitor_index >= len(monitors):
                monitor_index = 1 if len(monitors) > 1 else 0

            monitor = monitors[monitor_index]
            frame = sct.grab(monitor)
            array = np.asarray(frame)
            if array.ndim == 3 and array.shape[2] >= 3:
                return array[:, :, :3]
            return array
