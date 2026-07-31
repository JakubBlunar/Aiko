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
        self._loaded_device: str = ""
        self._context_active: bool = False
        # P27: construction no longer loads the model. Whisper large-v1 plus
        # RealtimeSTT's transcription child process is the single largest
        # resident cost in the app (~0.9 GB), and a text-only session paid it
        # at boot for a recorder it never fed a frame to. The load now happens
        # on first *use* -- see :meth:`_ensure_recorder` -- or eagerly via
        # :meth:`prewarm` when voice is turned on.
        self._shut_down: bool = False
        if AudioToTextRecorder is None:
            self._last_error = "RealtimeSTT (AudioToTextRecorder) not installed"
            log.warning(
                "STT engine unavailable: RealtimeSTT (AudioToTextRecorder) not installed"
            )
        elif not self.enabled:
            log.info("STT disabled in settings: engine not loaded")
        else:
            log.debug("STT engine deferred: loads on first use")

    @property
    def enabled(self) -> bool:
        # Absent attribute means an older/partial settings object; don't
        # silently disable voice for a shape we didn't expect.
        return bool(getattr(self._settings, "enabled", True))

    def _ensure_recorder(self) -> object | None:
        """Load the recorder on demand, once. Returns None if unavailable.

        Every path that touches ``self._recorder`` goes through here, so
        the lazy load is invisible to callers -- they only see the same
        "no recorder" short-circuit they already handled. A failed load
        latches into ``_last_error`` so a broken install doesn't retry the
        multi-second import on every audio chunk.
        """
        rec = self._recorder
        if rec is not None:
            return rec
        if AudioToTextRecorder is None or self._last_error or self._shut_down:
            return None
        if not self.enabled:
            return None
        with self._lock:
            if self._recorder is not None:
                return self._recorder
            if self._last_error:
                return None
            t0 = time.monotonic()
            try:
                self._recorder = self._create_recorder()
            except Exception as exc:
                self._last_error = f"RealtimeSTT init failed: {exc}"
                self._recorder = None
                log.error(
                    "STT engine init failed: model=%s language=%s device=%s exc=%r",
                    (self._settings.model or "large-v1"),
                    (self._settings.language or "en"),
                    (self._loaded_device or self._settings.device),
                    exc,
                )
                return None
            log.info(
                "STT engine ready: model=%s language=%s device=%s init_ms=%.0f",
                self._loaded_model, self._loaded_language, self._loaded_device,
                (time.monotonic() - t0) * 1000.0,
            )
            return self._recorder

    def prewarm(self) -> bool:
        """Load the model now, so the first voice turn doesn't wait on it.

        Called from the voice-enable path rather than from boot: a
        multi-second load is fine while the user is clicking the mic
        toggle, and unacceptable in the middle of their first sentence.
        Returns True when a recorder is loaded.
        """
        return self._ensure_recorder() is not None

    @property
    def is_loaded(self) -> bool:
        """Whether the weights are actually resident right now."""
        return self._recorder is not None

    def _resolve_device(self) -> str:
        """Resolve the configured device, probing for CUDA when set to "auto".

        RealtimeSTT defaults ``device`` to a hard ``"cuda"``, so on a host or
        container without a usable GPU the recorder raises during init and voice
        is dead. Probing keeps one config working in both places. The torch
        import is local and guarded because torch is a ``[voice]``-extra
        dependency that the text-only install doesn't have.
        """
        configured = (self._settings.device or "auto").strip().lower()
        if configured in {"cuda", "cpu"}:
            return configured
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _create_recorder(self) -> object:
        model = (self._settings.model or "large-v1").strip() or "large-v1"
        language = (self._settings.language or "en").strip() or "en"
        device = self._resolve_device()
        self._loaded_model = model
        self._loaded_language = language
        self._loaded_device = device
        return AudioToTextRecorder(
            model=model,
            language=language,
            device=device,
            compute_type=(self._settings.compute_type or "default").strip() or "default",
            use_microphone=False,
            on_recording_start=self._on_recording_start or (lambda: None),
            on_recording_stop=self._on_recording_stop or (lambda: None),
            spinner=False,
            realtime_model_type=model,
            # Use the local Silero backend from the ``silero-onnx-cpu`` extra
            # rather than a torch.hub download, which otherwise prompts
            # "snakers4/silero-vad ... not in the list of trusted repositories
            # (y/N)" and answers *no* on any non-interactive run, killing VAD.
            #
            # ``silero_backend="auto"`` is what selects it. Do NOT also pass the
            # legacy ``silero_use_onnx`` flag: per RealtimeSTT's
            # ``resolve_silero_backend``, setting it to *either* True or False
            # pins the backend to LEGACY (= the torch.hub path) and undoes this.
            silero_backend="auto",
        )

    @property
    def is_available(self) -> bool:
        """Whether STT *can* serve a request -- not whether it's loaded.

        P27: this used to mean "a recorder object exists", which was
        equivalent while the load happened in ``__init__``. With the lazy
        load it has to mean "could load": the five gate sites in
        ``voice_capture_mixin`` ask this *before* any audio exists, so the
        old meaning would refuse every voice turn forever.
        """
        if AudioToTextRecorder is None or not self.enabled or self._shut_down:
            return False
        # A latched load failure is permanent for this process; a recorder
        # that loaded and then errored mid-stream is still usable.
        if self._last_error is not None and self._recorder is None:
            return False
        return True

    def feed_audio(self, indata: object) -> None:
        """Feed raw audio chunk (e.g. from sounddevice callback). indata: int16 or float32 array."""
        recorder = self._ensure_recorder()
        if recorder is None:
            return
        try:
            if np is not None and hasattr(indata, "tobytes"):
                arr = np.asarray(indata)
                if arr.dtype == np.float32 or arr.dtype == float:
                    arr = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
                recorder.feed_audio(arr.tobytes())
            elif hasattr(recorder, "feed_audio"):
                recorder.feed_audio(indata)
        except Exception as exc:
            self._last_error = str(exc)

    def text(self) -> str:
        """Return current/final transcript."""
        # Deliberately does *not* trigger a load: a transcript read with no
        # recorder means nothing was fed, and loading Whisper to return ""
        # would stall the caller for seconds.
        recorder = self._recorder
        if recorder is None:
            return ""
        try:
            t = getattr(recorder, "text", None)
            if callable(t):
                return (t() or "").strip()
            return ""
        except Exception:
            return ""

    def start_context(self) -> None:
        """Enter recorder context (idempotent). Use with feed_audio then text()."""
        recorder = self._ensure_recorder()
        if recorder is None or not hasattr(recorder, "__enter__"):
            return
        if getattr(self, "_context_active", False):
            return
        try:
            recorder.__enter__()
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
        if np is None or mic_source is None:
            return ""
        # Load before the capture loop, not inside it: the first
        # ``feed_audio`` would otherwise block for the whole model load
        # while the mic queue backs up.
        if self._ensure_recorder() is None:
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

    def shutdown(self) -> None:
        """Terminate the RealtimeSTT subprocesses so they don't orphan-spin.

        RealtimeSTT spawns a transcription (and, with a mic, a reader)
        subprocess whose ``poll_connection`` loop only exits when the
        shared ``shutdown_event`` is set — which happens inside the
        recorder's own ``shutdown()``. If the parent process exits
        *without* calling it, those children are orphaned, their pipe
        ends raise ``BrokenPipeError [WinError 109]`` on the next poll,
        and the loop spins forever flooding ``ERROR:root:`` (and starving
        everything else). Calling ``recorder.shutdown()`` sets the event
        first thing, so the children stop immediately even if the
        subsequent join/terminate is only best-effort.

        Idempotent: the recorder's ``shutdown()`` guards on its own
        ``is_shut_down`` flag, and we null our reference so a second call
        (or any concurrent ``feed_audio`` / ``text``) is a no-op.
        """
        rec = self._recorder
        # Drop the reference first so ``is_available`` flips False and any
        # concurrent feed/text call short-circuits instead of racing the
        # subprocess teardown. The latch matters more now that the load is
        # lazy: without it, a feed arriving during shutdown would helpfully
        # reload the model we are trying to tear down.
        self._shut_down = True
        self._recorder = None
        self._context_active = False
        if rec is None:
            return
        shutdown = getattr(rec, "shutdown", None)
        if not callable(shutdown):
            return
        try:
            shutdown()
        except (BrokenPipeError, OSError, EOFError):
            # The pipe may already be dead — the important side effect
            # (setting shutdown_event) still ran. Nothing more to do.
            pass
        except Exception as exc:
            log.debug("STT recorder shutdown raised: exc=%r", exc)

    def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe a WAV file by feeding its contents to the recorder."""
        if wave is None or np is None:
            return ""
        path = Path(audio_path)
        if not path.exists():
            return ""
        recorder = self._ensure_recorder()
        if recorder is None:
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
                        recorder.feed_audio(data)
                    return self.text()
                finally:
                    if owns_context:
                        self.stop_context()
        except Exception:
            return ""
