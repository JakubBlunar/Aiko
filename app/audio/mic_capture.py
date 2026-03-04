from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
import tempfile
import time
import wave

import numpy as np
import sounddevice as sd

from app.core.settings import AudioSettings


class MicrophoneCapture:
    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings
        self._device: int | None = settings.microphone_device

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
            device=self._device,
        )
        sd.wait()
        return recording.copy()

    def list_input_devices(self) -> list[tuple[int, str]]:
        devices = sd.query_devices()
        output: list[tuple[int, str]] = []
        for index, device in enumerate(devices):
            if int(device.get("max_input_channels", 0)) > 0:
                output.append((index, str(device.get("name", f"Input {index}"))))
        return output

    def set_device(self, device_index: int | None) -> None:
        self._device = device_index

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

    def capture_phrase(
        self,
        *,
        max_seconds: float = 12.0,
        silence_seconds_to_stop: float = 1.0,
        level_threshold: float = 0.02,
        min_speech_seconds_before_stop: float = 1.2,
        speech_start_grace_seconds: float = 0.5,
        stop_requested: Callable[[], bool] | None = None,
        on_speech_start: Callable[[], None] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
    ) -> np.ndarray | None:
        sample_rate = self._settings.sample_rate
        channels = self._settings.channels
        chunk_frames = int(sample_rate * 0.1)
        pre_roll_chunks = 4

        silence_chunks_to_stop = max(1, int(silence_seconds_to_stop / 0.1))
        min_speech_chunks_before_stop = max(1, int(min_speech_seconds_before_stop / 0.1))
        speech_start_grace_chunks = max(0, int(speech_start_grace_seconds / 0.1))
        pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_chunks)
        captured: list[np.ndarray] = []
        speech_started = False
        silence_chunks = 0
        spoken_chunks = 0

        started_at = time.monotonic()

        with sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            blocksize=chunk_frames,
            device=self._device,
        ) as stream:
            while True:
                if stop_requested and stop_requested():
                    return None

                elapsed = time.monotonic() - started_at
                if elapsed >= max_seconds:
                    break

                chunk, _overflow = stream.read(chunk_frames)
                level = float(np.sqrt(np.mean(np.square(chunk))))
                if on_audio_level:
                    on_audio_level(level)

                if not speech_started:
                    pre_roll.append(chunk.copy())
                    if level >= level_threshold:
                        speech_started = True
                        spoken_chunks = 0
                        if on_speech_start:
                            on_speech_start()
                        captured.extend(pre_roll)
                        captured.append(chunk.copy())
                else:
                    captured.append(chunk.copy())
                    spoken_chunks += 1
                    if level < level_threshold:
                        silence_chunks += 1
                    else:
                        silence_chunks = 0

                    if spoken_chunks < speech_start_grace_chunks:
                        continue

                    if (
                        silence_chunks >= silence_chunks_to_stop
                        and spoken_chunks >= min_speech_chunks_before_stop
                    ):
                        break

        if not speech_started or not captured:
            return None

        return np.concatenate(captured, axis=0)

    def capture_phrase_to_wav(
        self,
        *,
        max_seconds: float = 12.0,
        silence_seconds_to_stop: float = 1.0,
        level_threshold: float = 0.02,
        min_speech_seconds_before_stop: float = 1.2,
        speech_start_grace_seconds: float = 0.5,
        stop_requested: Callable[[], bool] | None = None,
        on_speech_start: Callable[[], None] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
    ) -> Path | None:
        samples = self.capture_phrase(
            max_seconds=max_seconds,
            silence_seconds_to_stop=silence_seconds_to_stop,
            level_threshold=level_threshold,
            min_speech_seconds_before_stop=min_speech_seconds_before_stop,
            speech_start_grace_seconds=speech_start_grace_seconds,
            stop_requested=stop_requested,
            on_speech_start=on_speech_start,
            on_audio_level=on_audio_level,
        )
        if samples is None:
            return None

        wav_path = self.create_temp_wav_path(prefix="assistant_phrase_")
        self.write_wav(samples=samples, target_path=wav_path)
        return wav_path
