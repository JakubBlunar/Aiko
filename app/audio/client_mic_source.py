"""Microphone source whose audio comes from a connected WebSocket client.

The browser (or Tauri webview) owns the OS audio interfaces now: it
captures through ``getUserMedia`` at 48 kHz Int16 mono, runs the
browser's built-in DSP (echo cancellation, noise suppression, auto
gain control), and streams ~50 ms chunks as binary ``0x01 mic_pcm``
frames over the existing ``/ws`` socket. :class:`ClientMicSource`
mirrors the public surface of the old ``sounddevice``-backed
``MicrophoneCapture`` so :class:`SessionController` can use it
transparently — the legacy capture loops (``capture_phrase``,
``capture_while_ptt_active``, ``monitor_speech_start``) just read
from an in-process queue instead of an OS input stream.

Two responsibilities live here:

  - **Demux**: turn ``feed_pcm`` calls (raw Int16 LE bytes at the
    client's native rate) into mono float32 chunks at the rate the
    STT / VAD pipeline expects (typically 16 kHz). The resampling
    uses ``scipy.signal.resample_poly`` when available; we fall back
    to a simple decimation-by-N path for the common 48k -> 16k case
    so the audio stack still works without scipy installed.

  - **Frame queue**: the demuxer emits fixed-size 100 ms chunks
    (matching the old ``InputStream`` blocksize) into a thread-safe
    queue. The capture loops pull from the queue via
    :class:`_QueuedInputStream`, a thin shim with the same
    ``read(n_frames)`` shape ``sd.InputStream`` had so we didn't
    have to touch every endpointing branch.

The class is hub-agnostic: it knows nothing about WebSockets,
``voice_owner`` semantics, or which client is feeding it. The WS
layer in :mod:`app.web.server` is responsible for gating who can
write to the source.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
import queue
import tempfile
import threading
import time
from typing import Any
import wave

import numpy as np

from app.core.infra.settings import AudioSettings

try:
    import webrtcvad
except Exception:  # pragma: no cover — VAD is optional
    webrtcvad = None

try:
    from scipy.signal import resample_poly as _scipy_resample_poly  # type: ignore
except Exception:  # pragma: no cover — fall back to plain decimation
    _scipy_resample_poly = None


# Defaults for the on-wire format. ``mic_start`` frames may override
# the per-stream sample rate (e.g. 16 kHz directly from a low-quality
# device), but the common path is 48 kHz Int16 mono from the browser.
_DEFAULT_CLIENT_SAMPLE_RATE = 48000


class _QueuedInputStream:
    """Drop-in replacement for ``sd.InputStream`` over a chunk queue.

    The capture loops were written against the
    ``with sd.InputStream(...) as stream: stream.read(n)`` shape, so
    we keep the same protocol. ``read`` blocks on the queue until at
    least ``n`` frames are available or the source signals end-of-stream,
    in which case it returns whatever leftover audio remains padded
    with silence to keep downstream RMS / VAD arithmetic stable.
    """

    def __init__(self, source: "ClientMicSource", *, channels: int) -> None:
        self._source = source
        self._channels = max(1, int(channels))

    def __enter__(self) -> "_QueuedInputStream":
        self._source._on_stream_open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._source._on_stream_close()

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        chunk = self._source._read_frames(int(frames))
        if chunk.ndim == 1 and self._channels > 1:
            chunk = np.tile(chunk[:, None], (1, self._channels))
        elif chunk.ndim == 1 and self._channels == 1:
            chunk = chunk[:, None]
        return chunk, False


class ClientMicSource:
    """Microphone source fed by binary WS frames from the active client.

    ``settings.sample_rate`` is the rate the capture loops want
    (typically 16 kHz, what the STT pipeline expects). Incoming PCM
    arrives at the client's native rate (typically 48 kHz, set via
    the leading ``mic_start`` frame); we resample on the fly so the
    queue always holds samples at ``settings.sample_rate``.
    """

    # Internal blocksize for the chunk queue. 100 ms keeps RMS / VAD
    # arithmetic identical to the old ``InputStream`` path.
    _CHUNK_SECONDS: float = 0.1

    # Cap on queued chunks (~12 s of 100 ms blocks). Prevents an
    # unbounded build-up if STT stalls; older chunks are dropped from
    # the front so freshness wins.
    _MAX_QUEUE_CHUNKS: int = 120

    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._chunk_queue: "queue.Queue[np.ndarray | None]" = queue.Queue(
            maxsize=self._MAX_QUEUE_CHUNKS,
        )
        # Buffer of mono float32 samples at ``settings.sample_rate``
        # waiting to be packed into a 100 ms chunk.
        self._sample_buffer: list[np.ndarray] = []
        self._sample_buffer_len = 0
        # Source-rate state — re-evaluated whenever a ``mic_start``
        # arrives with a different sample rate.
        self._client_sample_rate: int = _DEFAULT_CLIENT_SAMPLE_RATE
        self._client_channels: int = 1
        self._dsp_flags: int = 0
        # When True, drop incoming PCM until a fresh ``mic_start``
        # boundary. Used when the owner releases the mic mid-utterance
        # so stale audio doesn't bleed into the next session.
        self._draining: bool = False
        # Number of consumers currently inside ``with stream:`` so we
        # know whether to keep the source "hot" or let queued frames
        # drain on idle.
        self._open_streams: int = 0

    # ── Hub-facing ingress ─────────────────────────────────────────

    def feed_start(
        self,
        sample_rate: int,
        channels: int,
        dsp_flags: int = 0,
    ) -> None:
        """Reset internal buffers and accept a new stream from the client.

        Called when the WS layer sees a ``0x02 mic_start`` frame. We
        discard any in-flight audio so the next phrase starts clean.
        ``dsp_flags`` is logged for QoS metrics but doesn't influence
        the server-side pipeline.
        """
        with self._lock:
            self._client_sample_rate = max(8000, int(sample_rate))
            self._client_channels = max(1, int(channels))
            self._dsp_flags = int(dsp_flags) & 0xFF
            self._sample_buffer = []
            self._sample_buffer_len = 0
            self._draining = False
            # Drain anything pending without blocking the WS thread.
            try:
                while True:
                    self._chunk_queue.get_nowait()
            except queue.Empty:
                pass

    def feed_end(self) -> None:
        """Mark the current stream as finished.

        Pending readers see an empty queue after the last buffered
        chunk is consumed. Any subsequent ``feed_pcm`` is dropped
        until the next ``feed_start``.
        """
        with self._lock:
            self._flush_buffer_locked()
            self._draining = True

    def feed_pcm(
        self,
        sample_rate: int,
        channels: int,
        pcm_int16_le: bytes,
    ) -> None:
        """Push a chunk of client-recorded PCM into the queue.

        ``sample_rate`` and ``channels`` are taken from the leading
        ``mic_start`` frame; we pass them here too so a misbehaving
        client that forgets the start can still be inferred from the
        first ``mic_pcm`` frame. ``pcm_int16_le`` may be any length;
        we resample + repack into the queue's 100 ms blocks.
        """
        if not pcm_int16_le:
            return
        with self._lock:
            if self._draining:
                return
            if sample_rate and sample_rate != self._client_sample_rate:
                # First ``mic_pcm`` arrived before / instead of a
                # proper ``mic_start`` — adopt its rate.
                self._client_sample_rate = max(8000, int(sample_rate))
            if channels and channels != self._client_channels:
                self._client_channels = max(1, int(channels))
            mono = self._decode_int16_to_mono_float32(pcm_int16_le)
            if mono.size == 0:
                return
            resampled = self._resample_to_target(mono)
            if resampled.size == 0:
                return
            self._sample_buffer.append(resampled)
            self._sample_buffer_len += int(resampled.size)
            self._drain_buffer_locked()

    @property
    def browser_dsp_flags(self) -> int:
        """Last-seen ``dsp_flags`` byte from a ``mic_start`` frame."""
        return self._dsp_flags

    # ── _QueuedInputStream hooks ────────────────────────────────────

    def _on_stream_open(self) -> None:
        with self._lock:
            self._open_streams += 1

    def _on_stream_close(self) -> None:
        with self._lock:
            self._open_streams = max(0, self._open_streams - 1)

    def _read_frames(self, frames: int) -> np.ndarray:
        """Pull ``frames`` mono float32 samples from the queue.

        Blocks until a 100 ms chunk arrives or the source is drained.
        Returns an array of shape ``(frames,)`` zero-padded if the
        underlying queue ran dry (callers treat a zero-energy chunk
        the same way they treated a ``sd.InputStream`` silence read).
        """
        frames = max(1, int(frames))
        collected: list[np.ndarray] = []
        gathered = 0
        # Block up to ~1 s on the first chunk so a fully idle source
        # doesn't busy-loop, then trickle the remaining chunks as
        # they show up (subsequent waits are short since chunks are
        # always 100 ms apart).
        wait = 1.0
        while gathered < frames:
            try:
                chunk = self._chunk_queue.get(timeout=wait)
            except queue.Empty:
                break
            if chunk is None:
                # Sentinel from ``shutdown`` — exit early.
                break
            collected.append(chunk)
            gathered += int(chunk.size)
            wait = 0.05
        if not collected:
            return np.zeros(frames, dtype=np.float32)
        joined = np.concatenate(collected, axis=0)
        if joined.size > frames:
            # Stash the leftover for the next ``read`` call so we
            # don't drop audio when a chunk straddles the boundary.
            extra = joined[frames:]
            joined = joined[:frames]
            with self._lock:
                if extra.size > 0:
                    self._sample_buffer.insert(0, extra)
                    self._sample_buffer_len += int(extra.size)
        elif joined.size < frames:
            pad = np.zeros(frames - joined.size, dtype=np.float32)
            joined = np.concatenate([joined, pad], axis=0)
        return joined.astype(np.float32, copy=False)

    # ── Decode / resample helpers ──────────────────────────────────

    def _decode_int16_to_mono_float32(self, pcm: bytes) -> np.ndarray:
        # ``frombuffer`` would crash if the byte count is odd — round
        # down so a half-sample tail never wedges the source.
        usable = len(pcm) - (len(pcm) % 2)
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        samples = np.frombuffer(pcm[:usable], dtype=np.int16)
        # Reduce to mono if the client briefly sent stereo; we average
        # rather than picking a channel so cross-talk doesn't bias.
        ch = max(1, int(self._client_channels))
        if ch > 1 and samples.size % ch == 0:
            samples = samples.reshape(-1, ch).mean(axis=1).astype(np.float32)
        else:
            samples = samples.astype(np.float32)
        # 32767.0 not 32768.0: matches the int16→float32 convention
        # the rest of the codebase uses (and inverts ``write_wav`` cleanly).
        return samples / 32767.0

    def _resample_to_target(self, mono_float32: np.ndarray) -> np.ndarray:
        target = int(self._settings.sample_rate)
        source = int(self._client_sample_rate)
        if source == target or mono_float32.size == 0:
            return mono_float32.astype(np.float32, copy=False)
        if _scipy_resample_poly is not None:
            try:
                gcd = _gcd(source, target)
                up = target // gcd
                down = source // gcd
                out = _scipy_resample_poly(mono_float32, up, down).astype(np.float32, copy=False)
                return out
            except Exception:
                pass
        # Fallback: integer-ratio decimation / linear interp. Good
        # enough for the 48k -> 16k common case (which is exactly 3:1).
        ratio = source / float(target)
        if ratio.is_integer():
            stride = int(ratio)
            return mono_float32[::stride].astype(np.float32, copy=False)
        # Generic linear interpolation.
        new_len = int(round(mono_float32.size / ratio))
        if new_len <= 0:
            return np.zeros(0, dtype=np.float32)
        xp = np.arange(mono_float32.size, dtype=np.float32)
        x = np.linspace(0, mono_float32.size - 1, new_len, dtype=np.float32)
        return np.interp(x, xp, mono_float32).astype(np.float32, copy=False)

    def _drain_buffer_locked(self) -> None:
        """Pack ``self._sample_buffer`` into 100 ms chunks."""
        chunk_frames = max(1, int(self._settings.sample_rate * self._CHUNK_SECONDS))
        while self._sample_buffer_len >= chunk_frames:
            joined = np.concatenate(self._sample_buffer, axis=0)
            chunk = joined[:chunk_frames].astype(np.float32, copy=True)
            remainder = joined[chunk_frames:]
            if remainder.size > 0:
                self._sample_buffer = [remainder]
                self._sample_buffer_len = int(remainder.size)
            else:
                self._sample_buffer = []
                self._sample_buffer_len = 0
            self._enqueue_locked(chunk)

    def _flush_buffer_locked(self) -> None:
        if self._sample_buffer_len == 0:
            return
        joined = np.concatenate(self._sample_buffer, axis=0).astype(np.float32, copy=False)
        self._sample_buffer = []
        self._sample_buffer_len = 0
        if joined.size == 0:
            return
        self._enqueue_locked(joined)

    def _enqueue_locked(self, chunk: np.ndarray) -> None:
        try:
            self._chunk_queue.put_nowait(chunk)
        except queue.Full:
            # Drop the oldest chunk so freshness wins.
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._chunk_queue.put_nowait(chunk)
            except queue.Full:
                pass

    # ── MicrophoneCapture-compatible surface ───────────────────────

    def start(self) -> None:
        return

    def stop(self) -> None:
        # Send a sentinel to unblock any pending readers.
        try:
            self._chunk_queue.put_nowait(None)
        except queue.Full:
            pass

    def set_device(self, _device_index: int | None) -> None:
        # No-op: clients enumerate and pick their own devices in-browser.
        return

    def list_input_devices(self) -> list[tuple[int, str]]:
        return []

    def read_chunk(self, chunk_ms: int = 80) -> bytes | None:
        frames = max(1, int(self._settings.sample_rate * chunk_ms / 1000.0))
        with _QueuedInputStream(self, channels=1):
            chunk = self._read_frames(frames)
        # Convert to Int16 LE — that's what the wake-word detector expects.
        clipped = np.clip(chunk, -1.0, 1.0)
        return (clipped * 32767).astype(np.int16, copy=False).tobytes()

    def capture_seconds(self, seconds: float = 5.0) -> np.ndarray:
        frames = max(1, int(self._settings.sample_rate * seconds))
        with _QueuedInputStream(self, channels=self._settings.channels) as stream:
            chunk, _ = stream.read(frames)
        return chunk.astype(np.float32, copy=False)

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

        Semantics are unchanged from the old ``MicrophoneCapture``: the
        only thing that's different is where the audio comes from.
        """
        sample_rate = self._settings.sample_rate
        channels = self._settings.channels
        chunk_frames = int(sample_rate * self._CHUNK_SECONDS)
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

        with _QueuedInputStream(self, channels=channels) as stream:
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
                        captured.extend(pre_roll)
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
                            pass
                    spoken_chunks += 1
                    has_vad = vad is not None
                    if has_vad:
                        in_silence = not vad_speech
                    else:
                        in_silence = level < silence_level_threshold
                    if in_silence:
                        silence_chunks += 1
                    else:
                        silence_chunks = 0
                    if spoken_chunks < speech_start_grace_chunks:
                        continue
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
        sample_rate = self._settings.sample_rate
        channels = self._settings.channels
        chunk_frames = int(sample_rate * self._CHUNK_SECONDS)
        if chunk_frames <= 0:
            return None
        started_at = time.perf_counter()
        captured: list[np.ndarray] = []
        try:
            with _QueuedInputStream(self, channels=channels) as stream:
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
        chunk_frames = int(sample_rate * self._CHUNK_SECONDS)
        started_at = time.monotonic()
        consecutive = 0
        vad = self._create_webrtc_vad(level_threshold)
        with _QueuedInputStream(self, channels=channels) as stream:
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


def _gcd(a: int, b: int) -> int:
    """Stdlib ``math.gcd`` clone — avoids an import for one call."""
    a, b = abs(int(a)), abs(int(b))
    while b:
        a, b = b, a % b
    return a or 1
