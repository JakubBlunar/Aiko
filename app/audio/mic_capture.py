from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
import tempfile
import time
from typing import Any
import wave

import numpy as np
import sounddevice as sd

from app.core.settings import AudioSettings

try:
    import webrtcvad
except Exception:
    webrtcvad = None


def list_output_devices() -> list[tuple[int, str]]:
    """Return list of (device_index, name) for devices with output channels."""
    devices = sd.query_devices()
    result: list[tuple[int, str]] = []
    for index, device in enumerate(devices):
        if int(device.get("max_output_channels", 0)) > 0:
            result.append((index, str(device.get("name", f"Output {index}"))))
    return result


class MicrophoneCapture:
    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings
        self._device: int | None = settings.microphone_device

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def read_chunk(self, chunk_ms: int = 80) -> bytes | None:
        """Read a short PCM16 chunk from the microphone for wake word detection."""
        try:
            frames = int(self._settings.sample_rate * chunk_ms / 1000.0)
            recording = sd.rec(
                frames,
                samplerate=self._settings.sample_rate,
                channels=1,
                dtype=np.int16,
                device=self._device,
            )
            sd.wait()
            return recording.tobytes()
        except Exception:
            return None

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

    def _create_webrtc_vad(self, level_threshold: float) -> Any | None:
        if webrtcvad is None:
            return None
        if self._settings.sample_rate not in {8000, 16000, 32000, 48000}:
            return None

        # Map existing UI threshold to VAD aggressiveness without adding new settings.
        if level_threshold >= 0.06:
            mode = 3
        elif level_threshold >= 0.035:
            mode = 2
        elif level_threshold >= 0.02:
            mode = 1
        else:
            mode = 0

        try:
            return webrtcvad.Vad(mode)
        except Exception:
            return None

    @staticmethod
    def _chunk_has_vad_speech(
        chunk: np.ndarray,
        *,
        sample_rate: int,
        vad: Any,
        frame_ms: int = 30,
    ) -> bool:
        if chunk.size == 0:
            return False

        mono = chunk[:, 0] if chunk.ndim > 1 else chunk
        pcm = np.clip(mono, -1.0, 1.0)
        pcm_i16 = (pcm * 32767).astype(np.int16)

        frame_samples = int(sample_rate * frame_ms / 1000)
        if frame_samples <= 0:
            return False

        total_samples = int(pcm_i16.shape[0])
        if total_samples < frame_samples:
            return False

        voiced = 0
        frames = 0
        for start in range(0, total_samples - frame_samples + 1, frame_samples):
            frame = pcm_i16[start : start + frame_samples]
            frames += 1
            try:
                if bool(vad.is_speech(frame.tobytes(), sample_rate)):
                    voiced += 1
            except Exception:
                return False

        if frames == 0:
            return False

        return voiced >= 1

    def capture_phrase(
        self,
        *,
        max_seconds: float = 12.0,
        max_wait_for_speech_start_seconds: float | None = None,
        use_webrtc_vad: bool = True,
        silence_seconds_to_stop: float = 1.0,
        level_threshold: float = 0.02,
        end_level_threshold: float | None = None,
        min_speech_seconds_before_stop: float = 1.2,
        speech_start_grace_seconds: float = 0.5,
        max_seconds_after_speech_start: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
        on_speech_start: Callable[[], None] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        on_silence_level: Callable[[float], None] | None = None,
        on_chunk: Callable[[np.ndarray], None] | None = None,
        endpoint_check: Callable[[float, int], str] | None = None,
    ) -> np.ndarray | None:
        """Capture a single phrase and return mono float32 samples.

        ``on_chunk`` is invoked with every speech-region chunk (after a
        copy) so the caller can feed a parallel STT recorder for live
        partial transcripts. It is **not** called during pre-roll or
        silence-only chunks before speech starts; once speech is detected
        the pre-roll buffer is replayed so the parallel recorder gets the
        same audio the WAV does.

        ``endpoint_check`` lets the caller override the simple
        ``silence_seconds_to_stop`` cap with a tiered decision based on
        the live partial transcript. Called once per silence chunk after
        ``min_speech_seconds_before_stop`` has elapsed; receives
        ``(silence_seconds, spoken_chunks)`` and returns one of:

        - ``"commit"`` — break now (e.g. sentence-final partial at the
          fast tier, or hard turn boundary reached).
        - ``"extend"`` — reset the silence counter (hesitation marker
          detected; user gets a fresh phrase budget).
        - ``"wait"`` — fall through to the loop's normal cap; the
          ``silence_seconds_to_stop`` value the caller passed acts as the
          hard turn boundary.

        When ``endpoint_check`` is ``None`` the loop uses pure
        ``silence_seconds_to_stop`` (legacy behaviour).
        """
        sample_rate = self._settings.sample_rate
        channels = self._settings.channels
        chunk_frames = int(sample_rate * 0.1)
        # Keep more lead-in audio so slow starts and soft first words are retained.
        pre_roll_chunks = 12

        silence_chunks_to_stop = max(1, int(silence_seconds_to_stop / 0.1))
        min_speech_chunks_before_stop = max(1, int(min_speech_seconds_before_stop / 0.1))
        speech_start_grace_chunks = max(0, int(speech_start_grace_seconds / 0.1))
        silence_level_threshold = (
            float(end_level_threshold)
            if end_level_threshold is not None
            else float(level_threshold)
        )
        max_speech_duration = (
            float(max_seconds_after_speech_start)
            if max_seconds_after_speech_start is not None
            else None
        )
        vad = self._create_webrtc_vad(level_threshold) if use_webrtc_vad else None
        pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_chunks)
        captured: list[np.ndarray] = []
        speech_started = False
        speech_started_at: float | None = None
        silence_chunks = 0
        spoken_chunks = 0
        speech_candidate_chunks = 0

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

                if (
                    not speech_started
                    and max_wait_for_speech_start_seconds is not None
                    and elapsed >= max(0.5, float(max_wait_for_speech_start_seconds))
                ):
                    break

                chunk, _overflow = stream.read(chunk_frames)
                level = float(np.sqrt(np.mean(np.square(chunk))))
                vad_speech = bool(vad) and self._chunk_has_vad_speech(
                    chunk,
                    sample_rate=sample_rate,
                    vad=vad,
                )
                if on_audio_level:
                    on_audio_level(level)

                # Phase 4b: ambient-noise sampling. We only fire the
                # silence callback when *neither* VAD nor a basic
                # energy floor thinks this chunk has speech in it; the
                # controller folds the level into a 30-second EMA used
                # for the prompt cue + Pocket-TTS volume nudge.
                if (
                    on_silence_level is not None
                    and not vad_speech
                    and level < float(level_threshold)
                ):
                    try:
                        on_silence_level(level)
                    except Exception:
                        pass

                if not speech_started:
                    pre_roll.append(chunk.copy())
                    has_vad = vad is not None
                    energy_start_threshold = float(level_threshold)
                    if has_vad:
                        # When VAD is active it's the primary detector. Keep a very low
                        # energy fallback so quiet mics can still trigger if VAD misses.
                        energy_start_threshold = max(0.002, float(level_threshold) * 0.5)

                    speech_detected = vad_speech or (level >= energy_start_threshold)
                    if speech_detected:
                        speech_candidate_chunks += 1
                    else:
                        speech_candidate_chunks = 0

                    required_start_chunks = 1 if vad_speech else 2
                    if speech_candidate_chunks >= required_start_chunks:
                        speech_started = True
                        speech_started_at = time.monotonic()
                        spoken_chunks = 0
                        if on_speech_start:
                            on_speech_start()
                        # pre_roll already includes the current chunk; do not append it twice.
                        captured.extend(pre_roll)
                        # Replay the pre-roll into the parallel STT recorder
                        # so its partial reflects the same audio the WAV
                        # contains (otherwise the first ~1.2 s of speech
                        # is missing from `stt.text()`).
                        if on_chunk is not None:
                            for buffered in pre_roll:
                                try:
                                    on_chunk(buffered)
                                except Exception:
                                    pass
                else:
                    if (
                        max_speech_duration is not None
                        and speech_started_at is not None
                        and (time.monotonic() - speech_started_at) >= max_speech_duration
                    ):
                        break

                    captured.append(chunk.copy())
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk)
                        except Exception:
                            # The parallel STT feed is best-effort; we never
                            # want a feed_audio error to abort the capture.
                            pass
                    spoken_chunks += 1
                    has_vad = vad is not None
                    if has_vad:
                        # Let VAD decide end-of-speech to avoid getting stuck by steady noise floors.
                        in_silence = not vad_speech
                    else:
                        in_silence = level < silence_level_threshold
                    if in_silence:
                        silence_chunks += 1
                    else:
                        silence_chunks = 0

                    if spoken_chunks < speech_start_grace_chunks:
                        continue

                    # Tiered endpointing: ask the caller's check first. It
                    # can break early (commit) or extend the window
                    # (extend → reset the silence counter so the user gets
                    # a fresh phrase budget after a hesitation marker).
                    # The loop's own silence_chunks_to_stop is the hard
                    # cap and fires regardless when reached.
                    if (
                        endpoint_check is not None
                        and spoken_chunks >= min_speech_chunks_before_stop
                    ):
                        try:
                            decision = endpoint_check(
                                silence_chunks * 0.1, spoken_chunks
                            )
                        except Exception:
                            decision = "wait"
                        if decision == "commit":
                            break
                        elif decision == "extend":
                            silence_chunks = 0

                    if (
                        silence_chunks >= silence_chunks_to_stop
                        and spoken_chunks >= min_speech_chunks_before_stop
                    ):
                        break

        if not speech_started or not captured:
            return None

        return np.concatenate(captured, axis=0)

    def capture_while_ptt_active(
        self,
        *,
        ptt_active_getter: Callable[[], bool],
        stop_requested: Callable[[], bool] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        max_seconds: float = 30.0,
    ) -> tuple[Path, float] | None:
        """Record while ptt_active_getter() returns True; write WAV and return (path, capture_ms) or None."""
        sample_rate = self._settings.sample_rate
        channels = self._settings.channels
        chunk_frames = int(sample_rate * 0.1)
        if chunk_frames <= 0:
            return None
        started_at = time.perf_counter()
        captured: list[np.ndarray] = []

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="float32",
                blocksize=chunk_frames,
                device=self._device,
            ) as stream:
                while ptt_active_getter():
                    if stop_requested and stop_requested():
                        break
                    elapsed = time.perf_counter() - started_at
                    if elapsed >= max_seconds:
                        break
                    chunk, _ = stream.read(chunk_frames)
                    if chunk is not None and chunk.size > 0:
                        captured.append(chunk.copy())
                    if on_audio_level:
                        level = float(np.sqrt(np.mean(np.square(chunk))))
                        on_audio_level(level)
        except Exception:
            return None

        if not captured:
            return None
        samples = np.concatenate(captured, axis=0)
        capture_ms = (time.perf_counter() - started_at) * 1000.0
        wav_path = self.create_temp_wav_path(prefix="assistant_ptt_")
        self.write_wav(samples=samples, target_path=wav_path)
        return wav_path, capture_ms

    def capture_phrase_to_wav(
        self,
        *,
        max_seconds: float = 12.0,
        max_wait_for_speech_start_seconds: float | None = None,
        use_webrtc_vad: bool = True,
        silence_seconds_to_stop: float = 1.0,
        level_threshold: float = 0.02,
        end_level_threshold: float | None = None,
        min_speech_seconds_before_stop: float = 1.2,
        speech_start_grace_seconds: float = 0.5,
        max_seconds_after_speech_start: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
        on_speech_start: Callable[[], None] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        on_silence_level: Callable[[float], None] | None = None,
        on_chunk: Callable[[np.ndarray], None] | None = None,
        endpoint_check: Callable[[float, int], str] | None = None,
    ) -> Path | None:
        samples = self.capture_phrase(
            max_seconds=max_seconds,
            max_wait_for_speech_start_seconds=max_wait_for_speech_start_seconds,
            use_webrtc_vad=use_webrtc_vad,
            silence_seconds_to_stop=silence_seconds_to_stop,
            level_threshold=level_threshold,
            end_level_threshold=end_level_threshold,
            min_speech_seconds_before_stop=min_speech_seconds_before_stop,
            speech_start_grace_seconds=speech_start_grace_seconds,
            max_seconds_after_speech_start=max_seconds_after_speech_start,
            stop_requested=stop_requested,
            on_speech_start=on_speech_start,
            on_audio_level=on_audio_level,
            on_silence_level=on_silence_level,
            on_chunk=on_chunk,
            endpoint_check=endpoint_check,
        )
        if samples is None:
            return None

        wav_path = self.create_temp_wav_path(prefix="assistant_phrase_")
        self.write_wav(samples=samples, target_path=wav_path)
        return wav_path

    def monitor_speech_start(
        self,
        *,
        max_seconds: float = 20.0,
        level_threshold: float = 0.02,
        min_consecutive_chunks: int = 2,
        stop_requested: Callable[[], bool] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
    ) -> bool:
        sample_rate = self._settings.sample_rate
        channels = self._settings.channels
        chunk_frames = int(sample_rate * 0.1)
        started_at = time.monotonic()
        consecutive = 0
        vad = self._create_webrtc_vad(level_threshold)

        with sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            blocksize=chunk_frames,
            device=self._device,
        ) as stream:
            while True:
                if stop_requested and stop_requested():
                    return False

                elapsed = time.monotonic() - started_at
                if elapsed >= max_seconds:
                    return False

                chunk, _overflow = stream.read(chunk_frames)
                level = float(np.sqrt(np.mean(np.square(chunk))))
                vad_speech = bool(vad) and self._chunk_has_vad_speech(
                    chunk,
                    sample_rate=sample_rate,
                    vad=vad,
                )
                if on_audio_level:
                    on_audio_level(level)

                if vad_speech or (level >= level_threshold):
                    consecutive += 1
                    if consecutive >= max(1, int(min_consecutive_chunks)):
                        return True
                else:
                    consecutive = 0
