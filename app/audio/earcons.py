"""Lightweight earcon (notification sound) player.

Generates short sine-wave tones programmatically and plays them
on a separate sounddevice stream so they don't block TTS playback.

Two earcon families live here:

  - **System notifications** (``listening``, ``thinking``, ``done``,
    ``error``): emitted by the controller around speech capture / errors.
  - **Stage directions** (``laugh``, ``sigh``, ``gasp``, ``hum``,
    ``tsk``): Phase 1c — emitted inline by the LLM via ``[[laugh]]`` etc.
    These are *blocking* by default so the TTS queue can splice them
    into the spoken stream at the right point in a sentence.
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


# Stage-direction earcon recipes. Each kind is a small synthesis
# function returning a mono float32 buffer at ``_SAMPLE_RATE``.
# We synth on the fly (not bundle WAVs) so the assistant works without
# any additional asset files in ``data/audio/earcons/``. If a project
# wants to override, drop a ``.wav`` with the matching name into that
# folder and the player will prefer it (see :meth:`_load_or_synth`).
def _stage_laugh() -> "np.ndarray":
    """Three short staccato bursts of slightly varying pitch."""
    bursts = []
    for f in (340.0, 380.0, 360.0):
        chunk = _generate_tone(f, 0.07, 0.18)
        gap = np.zeros(int(_SAMPLE_RATE * 0.06), dtype=np.float32)
        bursts.extend([chunk, gap])
    return np.concatenate(bursts)


def _stage_sigh() -> "np.ndarray":
    """A long descending breathy tone."""
    duration = 0.55
    n = int(_SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    # Glide pitch from 280 -> 180 Hz.
    freq = 280.0 - 100.0 * (t / duration)
    phase = 2.0 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    wave = np.sin(phase) * 0.16
    # Slow attack + steep decay for a breathy shape.
    envelope = np.linspace(0.4, 1.0, n // 3)
    decay = np.linspace(1.0, 0.0, n - len(envelope))
    env = np.concatenate([envelope, decay])
    return (wave * env).astype(np.float32)


def _stage_gasp() -> "np.ndarray":
    """A short quick rising tone with a sharp attack."""
    duration = 0.20
    n = int(_SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    freq = 240.0 + 240.0 * (t / duration)  # 240 -> 480 Hz
    phase = 2.0 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    wave = np.sin(phase) * 0.20
    fade = int(_SAMPLE_RATE * 0.01)
    if fade > 0 and len(wave) > 2 * fade:
        wave[:fade] *= np.linspace(0, 1, fade)
        wave[-fade:] *= np.linspace(1, 0, fade)
    return wave.astype(np.float32)


def _stage_hum() -> "np.ndarray":
    """A short steady mid-pitch tone."""
    return _generate_tone(220.0, 0.45, 0.15)


def _stage_tsk() -> "np.ndarray":
    """A very short percussive click for self-correction. Phase 3c."""
    duration = 0.05
    n = int(_SAMPLE_RATE * duration)
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(n).astype(np.float32) * 0.12
    fade = int(_SAMPLE_RATE * 0.005)
    if fade > 0 and n > 2 * fade:
        noise[:fade] *= np.linspace(0, 1, fade)
        noise[-fade:] *= np.linspace(1, 0, fade)
    return noise


_STAGE_BUILDERS: dict[str, "callable"] = {
    "laugh": _stage_laugh,
    "sigh": _stage_sigh,
    "gasp": _stage_gasp,
    "hum": _stage_hum,
    "tsk": _stage_tsk,
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
    builder = _STAGE_BUILDERS.get(name)
    if builder is not None:
        try:
            return builder()
        except Exception:
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
        """Fire-and-forget playback (default for system notifications)."""
        if not self._enabled:
            return
        audio = self._get(name)
        if audio is None:
            return
        threading.Thread(
            target=self._play_worker,
            args=(audio, False),
            daemon=True,
            name=f"earcon-{name}",
        ).start()

    def play_blocking(self, name: str) -> None:
        """Synchronous playback used by the TTS queue's stage-direction
        splicer (Phase 1c). Returns when the earcon has finished so the
        next text chunk lines up naturally in time."""
        if not self._enabled:
            return
        audio = self._get(name)
        if audio is None:
            return
        self._play_worker(audio, True)

    def _get(self, name: str) -> "np.ndarray | None":
        if name not in self._cache:
            sound = _build_sound(name)
            if sound is None:
                return None
            self._cache[name] = sound
        return self._cache[name]

    def _play_worker(self, audio: "np.ndarray", blocking: bool = False) -> None:
        try:
            sd.play(audio, _SAMPLE_RATE, device=self._output_device)
            if blocking:
                sd.wait()
            else:
                # Fire-and-forget: still call wait() so the *next*
                # ``sd.play`` doesn't truncate this one. The thread we
                # ran on is daemon so total cost is just one extra
                # thread per earcon.
                sd.wait()
        except Exception:
            pass
