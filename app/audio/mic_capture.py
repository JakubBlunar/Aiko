from __future__ import annotations

from pathlib import Path
import tempfile
import wave

import numpy as np
import sounddevice as sd

from app.core.settings import AudioSettings


class MicrophoneCapture:
    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def capture_seconds(self, seconds: float = 5.0) -> np.ndarray:
        frames = int(self._settings.sample_rate * seconds)
        recording = sd.rec(
            frames,
            samplerate=self._settings.sample_rate,
            channels=self._settings.channels,
            dtype="float32",
        )
        sd.wait()
        return recording.copy()

    def capture_to_wav(self, seconds: float = 5.0) -> Path:
        samples = self.capture_seconds(seconds=seconds)
        wav_path = self.create_temp_wav_path(prefix="assistant_mic_")
        self.write_wav(samples=samples, target_path=wav_path)
        return wav_path

    @staticmethod
    def create_temp_wav_path(prefix: str = "assistant_audio_") -> Path:
        return Path(tempfile.mkstemp(suffix=".wav", prefix=prefix)[1])

    def write_wav(self, samples: np.ndarray, target_path: Path) -> None:
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)

        with wave.open(str(target_path), "wb") as wav_file:
            wav_file.setnchannels(self._settings.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._settings.sample_rate)
            wav_file.writeframes(pcm.tobytes())
