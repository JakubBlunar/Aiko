"""Lightweight earcon (notification sound) player.

Generates short sine-wave tones programmatically and plays them
on a separate sounddevice stream so they don't block TTS playback.
"""
from __future__ import annotations

import threading

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None
    sd = None

_SAMPLE_RATE = 22050

_TONES: dict[str, list[tuple[float, float, float]]] = {
    "listening": [(880, 0.08, 0.25), (1100, 0.08, 0.2)],
    "thinking": [(660, 0.06, 0.15)],
    "done": [(880, 0.06, 0.2), (1320, 0.1, 0.25)],
    "error": [(440, 0.15, 0.3), (330, 0.15, 0.3)],
}


def _generate_tone(freq: float, duration: float, volume: float) -> "np.ndarray":
    t = np.linspace(0, duration, int(_SAMPLE_RATE * duration), endpoint=False)
    fade = int(0.005 * _SAMPLE_RATE)
    wave = np.sin(2 * np.pi * freq * t) * volume
    if fade > 0 and len(wave) > 2 * fade:
        wave[:fade] *= np.linspace(0, 1, fade)
        wave[-fade:] *= np.linspace(1, 0, fade)
    return wave.astype(np.float32)


def _build_sound(name: str) -> "np.ndarray | None":
    if np is None:
        return None
    tones = _TONES.get(name)
    if not tones:
        return None
    parts = [_generate_tone(f, d, v) for f, d, v in tones]
    return np.concatenate(parts)


class EarconPlayer:
    """Plays short notification sounds without blocking TTS."""

    def __init__(self, enabled: bool = True, output_device: int | None = None) -> None:
        self._enabled = enabled and sd is not None and np is not None
        self._output_device = output_device
        self._cache: dict[str, "np.ndarray"] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value) and sd is not None and np is not None

    def play(self, name: str) -> None:
        if not self._enabled:
            return
        if name not in self._cache:
            sound = _build_sound(name)
            if sound is None:
                return
            self._cache[name] = sound
        audio = self._cache[name]
        threading.Thread(
            target=self._play_worker,
            args=(audio,),
            daemon=True,
            name=f"earcon-{name}",
        ).start()

    def _play_worker(self, audio: "np.ndarray") -> None:
        try:
            sd.play(audio, _SAMPLE_RATE, device=self._output_device)
            sd.wait()
        except Exception:
            pass
