from __future__ import annotations

import ctypes
from ctypes import wintypes

from app.core.settings import ScreenSettings


class ScreenCaptureService:
    def __init__(self, settings: ScreenSettings) -> None:
        self._settings = settings

    def capture_once(self, *, active_window_only: bool | None = None):
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
            use_active_window_only = (
                bool(getattr(self._settings, "capture_active_window_only", False))
                if active_window_only is None
                else bool(active_window_only)
            )
            if use_active_window_only:
                region = self._active_window_region_within_monitor(monitor)
                if region is not None:
                    monitor = region

            frame = sct.grab(monitor)
            array = np.asarray(frame)
            if array.ndim == 3 and array.shape[2] >= 3:
                return array[:, :, :3]
            return array

    @staticmethod
    def _active_window_region_within_monitor(monitor: dict) -> dict | None:
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None

            left = int(rect.left)
            top = int(rect.top)
            right = int(rect.right)
            bottom = int(rect.bottom)

            m_left = int(monitor.get("left", 0))
            m_top = int(monitor.get("top", 0))
            m_right = m_left + int(monitor.get("width", 0))
            m_bottom = m_top + int(monitor.get("height", 0))

            i_left = max(left, m_left)
            i_top = max(top, m_top)
            i_right = min(right, m_right)
            i_bottom = min(bottom, m_bottom)

            if i_right <= i_left or i_bottom <= i_top:
                return None

            return {
                "left": i_left,
                "top": i_top,
                "width": i_right - i_left,
                "height": i_bottom - i_top,
            }
        except Exception:
            return None
