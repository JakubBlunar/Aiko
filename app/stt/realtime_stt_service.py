"""Real-time STT using RealtimeSTT (Whisper large-v1 + Silero VAD)."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.infra.settings import AudioSettings, SttSettings


log = logging.getLogger("app.stt.realtime_stt_service")

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

try:
    import wave
except ImportError:
    wave = None

if TYPE_CHECKING:
    from app.audio.client_mic_source import ClientMicSource

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
        self._loaded_model: str = ""
        self._loaded_language: str = ""
        self._context_active: bool = False
        if AudioToTextRecorder is not None:
            t0 = time.monotonic()
            try:
                self._recorder = self._create_recorder()
            except Exception as exc:
                self._last_error = f"RealtimeSTT init failed: {exc}"
                self._recorder = None
                log.error(
                    "STT engine init failed: model=%s language=%s exc=%r",
                    (self._settings.model or "large-v1"),
                    (self._settings.language or "en"),
                    exc,
                )
            else:
                log.info(
                    "STT engine ready: model=%s language=%s init_ms=%.0f",
                    self._loaded_model, self._loaded_language,
                    (time.monotonic() - t0) * 1000.0,
                )
        else:
            self._last_error = "RealtimeSTT (AudioToTextRecorder) not installed"
            log.warning(
                "STT engine unavailable: RealtimeSTT (AudioToTextRecorder) not installed"
            )

    def _create_recorder(self) -> object:
        model = (self._settings.model or "large-v1").strip() or "large-v1"
        language = (self._settings.language or "en").strip() or "en"
        self._loaded_model = model
        self._loaded_language = language
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
        """Enter recorder context (idempotent). Use with feed_audio then text()."""
        if self._recorder is None or not hasattr(self._recorder, "__enter__"):
            return
        if getattr(self, "_context_active", False):
            return
        try:
            self._recorder.__enter__()
            self._context_active = True
        except Exception as exc:
            self._last_error = f"start_context failed: {exc}"
            log.warning("STT start_context failed: exc=%r", exc)

    def stop_context(self) -> None:
        """Exit recorder context (idempotent)."""
        if self._recorder is None or not hasattr(self._recorder, "__exit__"):
            return
        if not getattr(self, "_context_active", False):
            return
        try:
            self._recorder.__exit__(None, None, None)
        except (BrokenPipeError, OSError, EOFError):
            pass
        except Exception as exc:
            log.debug("STT stop_context raised: exc=%r", exc)
        self._context_active = False

    def record_until_silence(
        self,
        max_seconds: float = 15.0,
        silence_seconds: float = 1.2,
        chunk_seconds: float = 0.2,
        mic_source: "ClientMicSource | None" = None,
    ) -> str:
        """Record from the client mic source, feed RealtimeSTT, transcribe.

        Returns the final transcript or ``""``. The mic source must be
        the same :class:`ClientMicSource` the WS hub feeds with
        binary mic frames, so the timing of "silence" lines up with
        what the client is actually streaming.
        """
        if self._recorder is None or np is None or mic_source is None:
            return ""
        sample_rate = self._audio_settings.sample_rate
        channels = self._audio_settings.channels
        chunk_frames = max(1, int(sample_rate * chunk_seconds)) * max(1, channels)
        silence_chunks = max(1, int(silence_seconds / chunk_seconds))
        silent_count = 0
        start = time.perf_counter()
        last_text_len = 0

        log.debug(
            "STT capture start: client-fed sample_rate=%d max_s=%.1f silence_s=%.1f",
            sample_rate, max_seconds, silence_seconds,
        )
        # Avoid double-managing the recorder context: LiveSession keeps
        # it open across phrases, in which case ``record_until_silence``
        # just feeds audio.
        owns_context = not self._context_active
        result = ""
        try:
            if owns_context:
                self.start_context()
            # Mirror the old ``sd.InputStream`` reader on the queue
            # surface that :class:`ClientMicSource` exposes.
            from app.audio.client_mic_source import _QueuedInputStream  # local: shim
            with _QueuedInputStream(mic_source, channels=channels) as stream:
                while (time.perf_counter() - start) < max_seconds:
                    chunk, _ = stream.read(chunk_frames)
                    self.feed_audio(chunk)
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
            result = self.text()
            return result
        finally:
            if owns_context:
                self.stop_context()
            duration_ms = (time.perf_counter() - start) * 1000.0
            chars = len(result)
            log.info(
                "STT capture done: chars=%d duration_ms=%.0f",
                chars, duration_ms,
            )
            if chars and result:
                log.debug("STT transcript: %s", result.replace("\n", " "))

    def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe a WAV file by feeding its contents to the recorder."""
        if self._recorder is None or wave is None or np is None:
            return ""
        path = Path(audio_path)
        if not path.exists():
            return ""
        # Don't fight a context that's already managed elsewhere. When the
        # caller (e.g. LiveSession) holds the context open we still feed
        # the WAV bytes; we just let them manage start/stop.
        owns_context = not self._context_active
        try:
            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                nch = wav.getnchannels()
                width = wav.getsampwidth()
                chunk_frames = rate // 5
                chunk_bytes = chunk_frames * nch * width
                if owns_context:
                    self.start_context()
                try:
                    while True:
                        data = wav.readframes(chunk_frames)
                        if not data:
                            break
                        self._recorder.feed_audio(data)
                    return self.text()
                finally:
                    if owns_context:
                        self.stop_context()
        except Exception:
            return ""
