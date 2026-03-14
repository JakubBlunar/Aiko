"""Real-time STT using RealtimeSTT (Whisper large-v1 + Silero VAD)."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.core.settings import AudioSettings, SttSettings

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None
    sd = None

try:
    import wave
except ImportError:
    wave = None

try:
    from RealtimeSTT import AudioToTextRecorder
except Exception:
    AudioToTextRecorder = None


class RealtimeSttService:
    """Real-time speech-to-text via RealtimeSTT. Supports feed_audio() and record_until_silence()."""

    def __init__(
        self,
        settings: SttSettings,
        audio_settings: AudioSettings,
        *,
        on_recording_start: Callable[[], None] | None = None,
        on_recording_stop: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._audio_settings = audio_settings
        self._on_recording_start = on_recording_start
        self._on_recording_stop = on_recording_stop
        self._recorder: object | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None
        if AudioToTextRecorder is not None:
            self._recorder = self._create_recorder()
        else:
            self._last_error = "RealtimeSTT (AudioToTextRecorder) not installed"

    def _create_recorder(self) -> object:
        model = (self._settings.model or "large-v1").strip() or "large-v1"
        language = (self._settings.language or "en").strip() or "en"
        return AudioToTextRecorder(
            model=model,
            language=language,
            use_microphone=False,
            on_recording_start=self._on_recording_start or (lambda: None),
            on_recording_stop=self._on_recording_stop or (lambda: None),
            spinner=False,
            realtime_model_type=model,
        )

    @property
    def is_available(self) -> bool:
        return self._recorder is not None and self._last_error is None

    def feed_audio(self, indata: object) -> None:
        """Feed raw audio chunk (e.g. from sounddevice callback). indata: int16 or float32 array."""
        if self._recorder is None:
            return
        try:
            if np is not None and hasattr(indata, "tobytes"):
                arr = np.asarray(indata)
                if arr.dtype == np.float32 or arr.dtype == float:
                    arr = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
                self._recorder.feed_audio(arr.tobytes())
            elif hasattr(self._recorder, "feed_audio"):
                self._recorder.feed_audio(indata)
        except Exception as exc:
            self._last_error = str(exc)

    def text(self) -> str:
        """Return current/final transcript."""
        if self._recorder is None:
            return ""
        try:
            t = getattr(self._recorder, "text", None)
            if callable(t):
                return (t() or "").strip()
            return ""
        except Exception:
            return ""

    def start_context(self) -> None:
        """Enter recorder context (start processing). Use with feed_audio then text()."""
        if self._recorder is not None and hasattr(self._recorder, "__enter__"):
            self._recorder.__enter__()

    def stop_context(self) -> None:
        """Exit recorder context."""
        if self._recorder is not None and hasattr(self._recorder, "__exit__"):
            self._recorder.__exit__(None, None, None)

    def record_until_silence(
        self,
        max_seconds: float = 15.0,
        silence_seconds: float = 1.2,
        chunk_seconds: float = 0.2,
    ) -> str:
        """
        Record from microphone, feed to RealtimeSTT, until silence or max_seconds.
        Returns transcribed text.
        """
        if self._recorder is None or np is None or sd is None:
            return ""
        sample_rate = self._audio_settings.sample_rate
        channels = self._audio_settings.channels
        chunk_frames = int(sample_rate * chunk_seconds) * channels
        silence_chunks = max(1, int(silence_seconds / chunk_seconds))
        silent_count = 0
        start = time.perf_counter()
        last_text_len = 0

        def callback(indata, _frames, _time_info, _status):
            self.feed_audio(indata)

        try:
            self.start_context()
            with sd.InputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype=np.int16,
                blocksize=chunk_frames,
                callback=callback,
            ):
                while (time.perf_counter() - start) < max_seconds:
                    time.sleep(chunk_seconds)
                    current = self.text()
                    if current:
                        if len(current) > last_text_len:
                            last_text_len = len(current)
                            silent_count = 0
                        else:
                            silent_count += 1
                            if silent_count >= silence_chunks:
                                break
                    else:
                        silent_count += 1
                        if silent_count >= silence_chunks and last_text_len > 0:
                            break
            return self.text()
        finally:
            self.stop_context()

    def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe a WAV file by feeding its contents to the recorder."""
        if self._recorder is None or wave is None or np is None:
            return ""
        path = Path(audio_path)
        if not path.exists():
            return ""
        try:
            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                nch = wav.getnchannels()
                width = wav.getsampwidth()
                chunk_frames = rate // 5
                chunk_bytes = chunk_frames * nch * width
                self.start_context()
                try:
                    while True:
                        data = wav.readframes(chunk_frames)
                        if not data:
                            break
                        self._recorder.feed_audio(data)
                    return self.text()
                finally:
                    self.stop_context()
        except Exception:
            return ""
