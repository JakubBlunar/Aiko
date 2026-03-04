from __future__ import annotations

import numpy as np
import sounddevice as sd

from app.core.settings import AudioSettings


class SystemLoopbackCapture:
    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings
        self._device: int | None = settings.loopback_device

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def peek_context_text(self) -> str | None:
        return None

    def _find_loopback_device(self) -> int | None:
        if self._device is not None:
            return self._device

        devices = sd.query_devices()
        for index, device in enumerate(devices):
            name = str(device.get("name", "")).lower()
            if "loopback" in name and int(device.get("max_input_channels", 0)) > 0:
                return index
        return None

    def list_loopback_devices(self) -> list[tuple[int, str]]:
        devices = sd.query_devices()
        output: list[tuple[int, str]] = []
        for index, device in enumerate(devices):
            name = str(device.get("name", ""))
            if "loopback" in name.lower() and int(device.get("max_input_channels", 0)) > 0:
                output.append((index, name))
        return output

    def set_device(self, device_index: int | None) -> None:
        self._device = device_index

    def capture_seconds(self, seconds: float = 2.0) -> np.ndarray | None:
        device = self._find_loopback_device()
        if device is None:
            return None

        frames = int(self._settings.sample_rate * seconds)
        try:
            recording = sd.rec(
                frames,
                samplerate=self._settings.sample_rate,
                channels=self._settings.channels,
                dtype="float32",
                device=device,
            )
            sd.wait()
            return recording.copy()
        except Exception:
            return None
