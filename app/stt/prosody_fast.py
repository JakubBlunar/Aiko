from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
import wave

import numpy as np


@dataclass(slots=True)
class ProsodyAnalysis:
    question_likely: bool
    emotion: str
    confidence: float
    rms: float
    zcr: float
    pitch_start_hz: float | None
    pitch_end_hz: float | None
    analysis_ms: float


class FastProsodyAnalyzer:
    """Very lightweight prosody analyzer for low-latency voice tone hints."""

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def analyze_wav(self, wav_path: str, *, text: str = "") -> ProsodyAnalysis | None:
        if not self._enabled:
            return None

        started = time.perf_counter()
        try:
            sr, samples = self._read_wav_mono_float(wav_path)
        except Exception:
            return None

        if samples.size < max(320, sr // 5):
            return None

        # Keep analysis fast: cap to the last 4 seconds.
        max_len = int(sr * 4.0)
        if samples.size > max_len:
            samples = samples[-max_len:]

        rms = float(np.sqrt(np.mean(samples * samples)))
        zcr = self._zero_cross_rate(samples)
        pitch_start, pitch_end = self._estimate_pitch_trend(samples, sr)

        lowered = str(text or "").strip().lower()
        text_question = lowered.endswith("?") or bool(
            re.search(r"\b(what|why|how|when|where|who|can|could|would|should|is|are|do|did)\b", lowered)
        )
        pitch_rise = (
            pitch_start is not None
            and pitch_end is not None
            and pitch_end > (pitch_start * 1.14)
        )
        question_likely = bool(text_question or pitch_rise)

        if any(token in lowered for token in ("angry", "frustrated", "annoyed", "upset")):
            emotion = "angry"
        elif rms >= 0.12 or ("!" in lowered and rms >= 0.08):
            emotion = "excited"
        elif any(token in lowered for token in ("sorry", "unfortunately", "sad")):
            emotion = "sad"
        elif rms <= 0.035:
            emotion = "calm"
        else:
            emotion = "neutral"

        confidence = 0.35
        if text_question:
            confidence += 0.25
        if pitch_rise:
            confidence += 0.2
        if pitch_start is not None and pitch_end is not None:
            confidence += 0.1
        if rms > 0.02:
            confidence += 0.1
        confidence = max(0.0, min(confidence, 1.0))

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return ProsodyAnalysis(
            question_likely=question_likely,
            emotion=emotion,
            confidence=confidence,
            rms=rms,
            zcr=zcr,
            pitch_start_hz=pitch_start,
            pitch_end_hz=pitch_end,
            analysis_ms=elapsed_ms,
        )

    @staticmethod
    def _read_wav_mono_float(path: str) -> tuple[int, np.ndarray]:
        with wave.open(path, "rb") as wf:
            sr = int(wf.getframerate())
            channels = int(wf.getnchannels())
            sampwidth = int(wf.getsampwidth())
            nframes = int(wf.getnframes())
            raw = wf.readframes(nframes)

        if sampwidth == 2:
            data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 1:
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sampwidth == 4:
            data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"Unsupported sample width: {sampwidth}")

        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)

        # Remove DC offset and clip just in case.
        data = data - float(np.mean(data))
        data = np.clip(data, -1.0, 1.0)
        return sr, data

    @staticmethod
    def _zero_cross_rate(samples: np.ndarray) -> float:
        if samples.size < 2:
            return 0.0
        signs = np.signbit(samples)
        crossings = np.count_nonzero(signs[1:] != signs[:-1])
        return float(crossings) / float(samples.size - 1)

    @staticmethod
    def _estimate_pitch_hz(frame: np.ndarray, sr: int) -> float | None:
        if frame.size < max(120, sr // 100):
            return None

        window = np.hanning(frame.size).astype(np.float32)
        signal = frame * window
        ac = np.correlate(signal, signal, mode="full")
        ac = ac[ac.size // 2 :]

        min_lag = max(1, int(sr / 350.0))
        max_lag = min(int(sr / 80.0), ac.size - 1)
        if max_lag <= min_lag:
            return None

        segment = ac[min_lag:max_lag]
        if segment.size == 0:
            return None
        lag = int(np.argmax(segment)) + min_lag
        if lag <= 0:
            return None

        pitch = float(sr) / float(lag)
        if not math.isfinite(pitch) or pitch < 60.0 or pitch > 400.0:
            return None
        return pitch

    def _estimate_pitch_trend(self, samples: np.ndarray, sr: int) -> tuple[float | None, float | None]:
        frame_len = max(160, int(sr * 0.05))
        hop = frame_len
        if samples.size < frame_len * 3:
            return None, None

        energies: list[float] = []
        frames: list[np.ndarray] = []
        for start in range(0, samples.size - frame_len + 1, hop):
            frame = samples[start : start + frame_len]
            frames.append(frame)
            energies.append(float(np.mean(frame * frame)))

        if not energies:
            return None, None

        threshold = max(1e-6, float(np.percentile(np.array(energies), 65)))
        voiced_indices = [idx for idx, e in enumerate(energies) if e >= threshold]
        if len(voiced_indices) < 2:
            return None, None

        first = frames[voiced_indices[0]]
        last = frames[voiced_indices[-1]]
        return self._estimate_pitch_hz(first, sr), self._estimate_pitch_hz(last, sr)
