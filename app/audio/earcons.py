"""Lightweight earcon (notification sound) player.

Generates short sine-wave tones programmatically and streams the
resulting Int16 PCM through a ``pcm_listener`` callback so the WS hub
can broadcast them as ``0x11 earcon_pcm`` frames; every connected
client plays them through its own WebAudio context.

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
import time
from collections.abc import Callable

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

_SAMPLE_RATE = 22050

# ``(sample_rate, channels, pcm_int16_le_bytes)`` per chunk; empty
# payload signals end-of-clip.
PcmListener = Callable[[int, int, bytes], None]
PcmEndListener = Callable[[], None]

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


# ── Layer 4: expanded palette ──────────────────────────────────────────


def _stage_chuckle() -> "np.ndarray":
    """A two-burst lighter laugh, smaller pitch range than ``laugh``.

    Sits at lower amplitude (0.13 vs laugh's 0.18) and trims to two
    pulses so it lands as an aside / amused-but-not-laughing-out-loud
    beat -- the difference between ``[[laugh]]`` (3 staccato bursts at
    ~340/380/360 Hz) and ``[[chuckle]]`` (2 bursts at 320/350 Hz).
    """
    bursts = []
    for f in (320.0, 350.0):
        chunk = _generate_tone(f, 0.06, 0.13)
        gap = np.zeros(int(_SAMPLE_RATE * 0.05), dtype=np.float32)
        bursts.extend([chunk, gap])
    return np.concatenate(bursts)


def _stage_soft_sigh() -> "np.ndarray":
    """A slower, lower-pitched sigh than the existing ``sigh`` earcon.

    Glides from 220 Hz down to 140 Hz over 0.7 s with a softer
    envelope (peak 0.11 vs 0.16) so it reads as a gentle exhale
    rather than the wistful drag of the regular sigh.
    """
    duration = 0.70
    n = int(_SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    freq = 220.0 - 80.0 * (t / duration)
    phase = 2.0 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    wave = np.sin(phase) * 0.11
    envelope = np.linspace(0.3, 1.0, n // 4)
    decay = np.linspace(1.0, 0.0, n - len(envelope))
    env = np.concatenate([envelope, decay])
    return (wave * env).astype(np.float32)


def _stage_sharp_gasp() -> "np.ndarray":
    """A fast inhale-noise burst -- sharper than the regular ``gasp``.

    Adds a noise component on top of the rising tone to read as an
    actual breath intake rather than a clean tone glide. Shorter
    (0.13 s) and louder (peak 0.24).
    """
    duration = 0.13
    n = int(_SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    freq = 280.0 + 320.0 * (t / duration)  # 280 -> 600 Hz
    phase = 2.0 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n).astype(np.float32) * 0.06
    wave = (np.sin(phase) * 0.18 + noise) * 0.95
    fade = int(_SAMPLE_RATE * 0.008)
    if fade > 0 and len(wave) > 2 * fade:
        wave[:fade] *= np.linspace(0, 1, fade)
        wave[-fade:] *= np.linspace(1, 0, fade)
    return wave.astype(np.float32)


def _stage_breath() -> "np.ndarray":
    """A quiet inhale before something hard to say.

    Pure breath noise with a soft attack and decay -- meant to land
    on the auto-sprinkle path (cadence rule prepends this on the
    first sentence of a melancholy / wistful / sad turn). Peak at
    0.07 so it stays in the "background texture" register; a louder
    breath would compete with the actual word that follows.
    """
    duration = 0.35
    n = int(_SAMPLE_RATE * duration)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n).astype(np.float32) * 0.07
    # Lowpass-ish smoothing via a simple moving average so it sounds
    # like breath rather than radio static.
    window = max(8, int(_SAMPLE_RATE * 0.002))
    kernel = np.ones(window, dtype=np.float32) / float(window)
    smoothed = np.convolve(noise, kernel, mode="same")
    envelope = np.linspace(0.2, 1.0, n // 3)
    decay = np.linspace(1.0, 0.0, n - len(envelope))
    env = np.concatenate([envelope, decay])
    return (smoothed * env).astype(np.float32)


def _stage_mm() -> "np.ndarray":
    """A thoughtful low-pitched ``mm`` / ``mmm`` hum.

    Lower than the existing ``hum`` (175 Hz vs 220 Hz) and a hair
    longer (0.55 s vs 0.45 s) so it lands as a "let me think about
    that" beat instead of an acknowledgement.
    """
    return _generate_tone(175.0, 0.55, 0.13)


_STAGE_BUILDERS: dict[str, "callable"] = {
    "laugh": _stage_laugh,
    "sigh": _stage_sigh,
    "gasp": _stage_gasp,
    "hum": _stage_hum,
    "tsk": _stage_tsk,
    # Layer 4 additions: lighter / softer / sharper variants and two
    # new background-texture earcons (breath, mm).
    "chuckle": _stage_chuckle,
    "soft_sigh": _stage_soft_sigh,
    "sharp_gasp": _stage_sharp_gasp,
    "breath": _stage_breath,
    "mm": _stage_mm,
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
    """Streams short notification sounds to connected clients."""

    # ~50 ms per chunk so earcons share the same WS framing rhythm
    # as the TTS PCM stream.
    _EMIT_CHUNK_SECONDS: float = 0.05

    def __init__(
        self,
        enabled: bool = True,
        *,
        pcm_listener: PcmListener | None = None,
        clip_end_listener: PcmEndListener | None = None,
    ) -> None:
        self._enabled = enabled and np is not None
        self._cache: dict[str, "np.ndarray"] = {}
        self._pcm_listener: PcmListener | None = pcm_listener
        self._clip_end_listener: PcmEndListener | None = clip_end_listener

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value) and np is not None

    def set_pcm_listener(
        self,
        listener: PcmListener | None,
        *,
        end_listener: PcmEndListener | None = None,
    ) -> None:
        """Install / replace the PCM emitter (wired by SessionController)."""
        self._pcm_listener = listener
        if end_listener is not None:
            self._clip_end_listener = end_listener

    def play(self, name: str) -> None:
        """Fire-and-forget playback (default for system notifications)."""
        if not self._enabled:
            return
        audio = self._get(name)
        if audio is None:
            return
        threading.Thread(
            target=self._play_worker,
            args=(audio,),
            daemon=True,
            name=f"earcon-{name}",
        ).start()

    def play_blocking(self, name: str) -> None:
        """Synchronous playback used by the TTS queue's stage-direction
        splicer (Phase 1c). Returns when the earcon PCM has been pushed
        through the listener so the next text chunk lines up naturally
        in time on the wire."""
        if not self._enabled:
            return
        audio = self._get(name)
        if audio is None:
            return
        self._play_worker(audio)

    def _get(self, name: str) -> "np.ndarray | None":
        if name not in self._cache:
            sound = _build_sound(name)
            if sound is None:
                return None
            self._cache[name] = sound
        return self._cache[name]

    def _play_worker(self, audio: "np.ndarray") -> None:
        listener = self._pcm_listener
        if listener is None or np is None:
            end_listener = self._clip_end_listener
            if end_listener is not None:
                try:
                    end_listener()
                except Exception:
                    pass
            return
        playback_duration_s = 0.0
        try:
            flat = audio.reshape(-1) if audio.ndim > 1 else audio
            if flat.size == 0:
                return
            playback_duration_s = float(flat.size) / float(_SAMPLE_RATE)
            chunk_samples = max(1, int(_SAMPLE_RATE * self._EMIT_CHUNK_SECONDS))
            pcm16 = (np.clip(flat, -1.0, 1.0) * 32767.0).round().astype(np.int16, copy=False)
            ship_t0 = time.monotonic()
            for start in range(0, pcm16.size, chunk_samples):
                end = min(start + chunk_samples, pcm16.size)
                listener(_SAMPLE_RATE, 1, pcm16[start:end].tobytes())
            # Bytes leave the WS at network speed; the actual playback
            # on the client takes ``playback_duration_s`` seconds. Block
            # the worker for that long so :class:`TtsQueue` only
            # advances to the next text chunk after the earcon has
            # really finished — otherwise a stage-direction earcon
            # spliced mid-sentence would let the next sentence's PCM
            # arrive before the earcon finishes playing on the client.
            remaining = playback_duration_s - (time.monotonic() - ship_t0)
            if remaining > 0.0:
                time.sleep(remaining)
        except Exception:
            pass
        finally:
            end_listener = self._clip_end_listener
            if end_listener is not None:
                try:
                    end_listener()
                except Exception:
                    pass
