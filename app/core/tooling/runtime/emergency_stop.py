from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time


class EmergencyStopState:
    def __init__(self) -> None:
        self._triggered = False
        self._lock = threading.Lock()
        self._triggered_at: float | None = None

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    @property
    def triggered_at(self) -> float | None:
        with self._lock:
            return self._triggered_at

    def trigger(self) -> None:
        with self._lock:
            self._triggered = True
            self._triggered_at = time.time()

    def reset(self) -> None:
        with self._lock:
            self._triggered = False
            self._triggered_at = None


class GlobalHotkeyListener:
    _MOD_MAP = {
        "alt": 0x0001,
        "ctrl": 0x0002,
        "control": 0x0002,
        "shift": 0x0004,
        "win": 0x0008,
    }

    def __init__(self, *, hotkey: str, state: EmergencyStopState) -> None:
        self._hotkey = hotkey
        self._state = state
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._registered_id = 0
        self._thread_id = 0
        self._parsed_hotkey: tuple[int, int] | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True

        parsed = self._parse_hotkey(self._hotkey)
        if parsed is None:
            return False

        self._stop_event.clear()
        self._parsed_hotkey = parsed
        self._thread = threading.Thread(target=self._loop, name="assistant-hotkey-listener", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            WM_QUIT = 0x0012
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._running = False

    def _loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        parsed = self._parsed_hotkey
        if parsed is None:
            self._running = False
            return

        modifiers, vk = parsed
        hotkey_id = 1
        if not user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
            self._running = False
            return

        self._registered_id = hotkey_id
        self._running = True

        msg = wintypes.MSG()
        WM_HOTKEY = 0x0312
        while not self._stop_event.is_set():
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                self._state.trigger()

        if self._registered_id:
            user32.UnregisterHotKey(None, self._registered_id)
            self._registered_id = 0

        self._running = False

    def _parse_hotkey(self, text: str) -> tuple[int, int] | None:
        parts = [token.strip().lower() for token in (text or "").split("+") if token.strip()]
        if not parts:
            return None

        modifiers = 0
        key_token = ""
        for token in parts:
            if token in self._MOD_MAP:
                modifiers |= self._MOD_MAP[token]
            else:
                key_token = token

        if not key_token:
            return None

        vk = self._to_vk(key_token)
        if vk is None:
            return None

        return modifiers, vk

    @staticmethod
    def _to_vk(token: str) -> int | None:
        if len(token) == 1 and token.isalpha():
            return ord(token.upper())

        if len(token) == 1 and token.isdigit():
            return ord(token)

        if token.startswith("f") and token[1:].isdigit():
            value = int(token[1:])
            if 1 <= value <= 24:
                return 0x70 + (value - 1)

        special = {
            "esc": 0x1B,
            "escape": 0x1B,
            "space": 0x20,
            "enter": 0x0D,
            "tab": 0x09,
        }
        return special.get(token)
