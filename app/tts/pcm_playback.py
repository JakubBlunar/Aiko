"""Everything between "here is a clip" and "the client heard it".

Extracted from :class:`~app.tts.pocket_tts_service.PocketTtsService` when
a second engine arrived. None of it is engine-specific: chunk sizing,
pre-roll depth, real-time pacing, barge-in checks, gain, the lip-sync
amplitude pacer and the block that keeps :class:`TtsQueue`'s sentence
timing honest are all properties of *how Aiko's client plays audio*, not
of which model produced it.

The *signal* half -- brightness, level, tempo and the pitch-preserving
stretch -- moved out again into :mod:`app.tts.shaping`, because it was
interleaved with this emission loop and the audition lab therefore could
not run it. What the lab played was the engine; what Aiko plays is the
engine plus those four stages.

Duplicating them into a second engine would have been the wrong kind of
cheap. Every constant here was tuned against observed client behaviour --
the pre-roll depth against audio-scheduler underruns, the chunk size
against Live2D render stutter -- and a second copy would drift from the
first silently, producing an engine that sounds subtly worse for reasons
nobody would connect to a number in a different file.

A host class must provide:

``_pcm_listener`` / ``_clip_end_listener``
    Where the bytes go, installed by ``set_pcm_listener``.
``_stop_requested``
    A ``threading.Event`` for barge-in. Checked between chunks, so a
    stop cuts mid-clip rather than at the end of it.
``_clip_cancel_listener``
    Fired instead of a plain clip-end when a clip was *cut* rather than
    finished, so the client can drop what it is still holding. Optional:
    defaults to ``None``, and an engine that leaves it unset behaves
    exactly as before.
Plus the shaping targets read by :meth:`app.tts.shaping.Shaping.of` --
``_pitch_preserving_speed``, ``_loudness_target_dbfs``,
``_tilt_target_db`` / ``_tilt_limit_db`` and ``_rate_target_syl_s`` /
``_rate_limit``. Every one of those is optional and defaults to inert, so
an engine written before a given stage existed still plays.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

from app.audio.speech_rate import MAX_CORRECTION as MAX_RATE_CORRECTION
from app.audio.timbre import MAX_CORRECTION_DB
from app.tts.shaping import (
    GUARD_SILENCE_SECONDS,
    Shaping,
    resolve_loudness_target,
    shape_clip,
)

log = logging.getLogger(__name__)

#: Re-exported: both live in :mod:`app.tts.shaping` now, and the engines
#: import them from here.
__all__ = [
    "GUARD_SILENCE_SECONDS",
    "PcmPlaybackMixin",
    "resolve_loudness_target",
]


class PcmPlaybackMixin:
    """The emission half of a TTS engine."""

    # Emit ~50 ms chunks so the client scheduler has predictable buffer
    # sizes and so the WS message rate caps at ~20 frames/sec/clip.
    _EMIT_CHUNK_SECONDS: float = 0.05
    # Number of chunks shipped immediately before we start pacing the
    # rest at real-time. 12 x 50 ms = 600 ms, which is enough to ride
    # out a phone-on-Tailscale hop (RTT + Wi-Fi jitter) without the
    # client scheduler running dry. Localhost still starts on the first
    # slice; this only keeps more audio *queued* ahead of the speaker.
    # Tests that need the emit loop to finish instantly assign a huge
    # value on the instance. ``tts.pcm_pre_roll_ms`` retunes it.
    _PRE_ROLL_CHUNKS: int = 12

    # Declared for the type checker; the host owns them.
    _pcm_listener: Callable[[int, int, bytes], None] | None
    _clip_end_listener: Callable[[], None] | None
    _stop_requested: threading.Event
    _pitch_preserving_speed: bool
    # Defaulted so an engine written before these existed still works.
    _clip_cancel_listener: Callable[[], None] | None = None
    _loudness_target_dbfs: float = 0.0
    _tilt_target_db: float | None = None
    _tilt_limit_db: float = MAX_CORRECTION_DB
    _rate_target_syl_s: float | None = None
    _rate_limit: float = MAX_RATE_CORRECTION

    def _configure_pre_roll(self, settings: object | None = None) -> None:
        """Set ``_PRE_ROLL_CHUNKS`` from ``tts.pcm_pre_roll_ms``.

        Default 600 ms. Tests that need the emit loop to finish
        instantly still assign ``_PRE_ROLL_CHUNKS`` on the instance
        afterwards; a ``MagicMock`` settings object falls through to
        that default rather than crashing on ``float(Mock)``.
        """
        raw: object = 600
        if settings is not None:
            raw = getattr(settings, "pcm_pre_roll_ms", 600)
        ms = 600.0
        if isinstance(raw, bool):
            pass
        elif isinstance(raw, (int, float)):
            ms = float(raw)
        elif isinstance(raw, str):
            try:
                ms = float(raw)
            except ValueError:
                ms = 600.0
        ms = max(250.0, min(1500.0, ms))
        chunk_ms = float(self._EMIT_CHUNK_SECONDS) * 1000.0
        self._PRE_ROLL_CHUNKS = max(5, int(round(ms / chunk_ms)))

    # ── the whole playback of one clip ───────────────────────────────

    def _play_clip(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        speed: float = 1.0,
        gain_factor: float = 1.0,
        text: str = "",
        on_amplitude: Callable[[float], None] | None = None,
    ) -> float:
        """Rate-adjust, ship, and block for the real playback duration.

        Returns milliseconds actually spent playing, for logging.

        ``text`` is what was spoken, and is used only to measure the
        clip's delivered tempo. Optional so a caller that has not got it
        still works, with tempo matching simply inactive.

        The block at the end is not padding. ``_emit_pcm`` returns as soon
        as the bytes are on the socket, but the client is still playing
        them, and :class:`TtsQueue` uses this call's return to decide when
        to dispatch the next sentence. Without the wait, sentences pile up
        and the lip-sync pacer is cut off mid-utterance.
        """
        stop_amplitude = threading.Event()
        amplitude_thread: threading.Thread | None = None

        # Brightness, level, tempo and the stretch, in that order and with
        # their reasoning, live in ``app.tts.shaping`` -- pure, so the
        # audition lab can run the identical path instead of playing the
        # engine raw and calling it a preview.
        shaped = shape_clip(
            audio,
            sample_rate,
            shaping=Shaping.of(self),
            speed=speed,
            gain_factor=gain_factor,
            text=text,
        )
        audio = shaped.audio
        gain_factor = shaped.gain_factor
        playback_rate = shaped.playback_rate
        duration_s = float(audio.size) / float(playback_rate)
        play_t0 = time.monotonic()

        try:
            if on_amplitude is not None:
                amplitude_thread = threading.Thread(
                    target=self._amplitude_pacer,
                    args=(audio, playback_rate, on_amplitude, stop_amplitude),
                    daemon=True,
                    name="tts-amp",
                )
                amplitude_thread.start()

            self._emit_pcm(audio, playback_rate, gain_factor=gain_factor)

            # Poll the stop flag so barge-in still cuts cleanly.
            deadline = play_t0 + duration_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                if self._stop_requested.wait(timeout=min(remaining, 0.05)):
                    break
        finally:
            stop_amplitude.set()
            if amplitude_thread is not None:
                amplitude_thread.join(timeout=0.25)
            if on_amplitude is not None:
                try:
                    on_amplitude(0.0)
                except Exception:
                    pass
        return (time.monotonic() - play_t0) * 1000.0

    # ── PCM emission ─────────────────────────────────────────────────

    def _emit_pcm(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        gain_factor: float = 1.0,
    ) -> None:
        """Push the clip out through ``pcm_listener`` in paced slices.

        Audio arrives as float32 in roughly ``[-1, 1]``. We convert to
        Int16 LE in 50 ms slices and call the listener once per slice.

        ``gain_factor`` is a linear sample multiplier applied before the
        float-to-Int16 conversion. Values < 1.0 attenuate (whisper / soft
        prosody / quiet rooms); values > 1.0 boost (firm prosody / noisy
        rooms). The ``np.clip`` saturation handles peaks for boosts.

        After a pre-roll of :attr:`_PRE_ROLL_CHUNKS` slices the rest are
        paced at real-time wall-clock so that:

        - the WebSocket doesn't burst 20+ binary frames in a single tick,
          which forced a matching burst of AudioBuffer allocations on the
          client and stuttered the Live2D render thread;
        - long utterances spread encoder / network load evenly rather
          than front-loading it;
        - barge-in stops shipping the rest of the clip the moment
          ``_stop_requested`` flips.
        """
        listener = self._pcm_listener
        flat = audio.reshape(-1) if audio.ndim > 1 else audio
        if flat.size == 0:
            return
        if listener is None:
            # Nowhere to play it, so discard -- but still fire clip-end,
            # so any state machine waiting on it (UI ducking) advances.
            end_listener = self._clip_end_listener
            if end_listener is not None:
                try:
                    end_listener()
                except Exception:
                    pass
            return

        chunk_samples = max(1, int(sample_rate * self._EMIT_CHUNK_SECONDS))
        total = flat.size
        # Gain before saturation, so a +6 dB boost lifts quiet samples
        # without smearing the peaks beyond the safe range.
        if abs(float(gain_factor) - 1.0) > 1e-3:
            scaled = np.clip(flat * float(gain_factor), -1.0, 1.0) * 32767.0
        else:
            scaled = np.clip(flat, -1.0, 1.0) * 32767.0
        # ``astype`` truncates toward zero, so round first or the
        # quietest samples collapse to zero asymmetrically.
        pcm16 = scaled.round().astype(np.int16, copy=False)
        ship_t0 = time.monotonic()
        chunk_index = 0
        cut = False
        try:
            for start in range(0, total, chunk_samples):
                if self._stop_requested.is_set():
                    cut = True
                    break
                end = min(start + chunk_samples, total)
                listener(int(sample_rate), 1, pcm16[start:end].tobytes())
                chunk_index += 1
                if chunk_index > self._PRE_ROLL_CHUNKS:
                    target = (
                        ship_t0
                        + (chunk_index - self._PRE_ROLL_CHUNKS)
                        * self._EMIT_CHUNK_SECONDS
                    )
                    delay = target - time.monotonic()
                    if delay > 0.0:
                        # ``Event.wait`` returns True when set, so
                        # barge-in cuts over without waiting out the
                        # rest of this chunk's slice.
                        if self._stop_requested.wait(timeout=delay):
                            cut = True
                            break
        finally:
            # Order matters. We are up to ``_PRE_ROLL_CHUNKS`` ahead of
            # what the client has actually played, so a cut leaves it
            # holding audio nobody wants -- tell it to drop that before
            # anything else reacts to the clip being over.
            if cut:
                self._fire_clip_cancel()
            end_listener = self._clip_end_listener
            if end_listener is not None:
                try:
                    end_listener()
                except Exception:
                    pass

    def _fire_clip_cancel(self) -> None:
        """Ask the client to drop scheduled audio it has not played yet.

        Safe to call more than once for one cut: discarding an empty
        queue is a no-op, and ``stop()`` and the emit loop both notice
        the same barge-in.
        """
        cancel_listener = self._clip_cancel_listener
        if cancel_listener is None:
            return
        try:
            cancel_listener()
        except Exception:
            pass

    # ── lip sync ─────────────────────────────────────────────────────

    def _amplitude_pacer(
        self,
        audio: np.ndarray,
        sample_rate: int,
        on_amplitude: Callable[[float], None],
        stop_event: threading.Event,
    ) -> None:
        """Compute RMS in ~50 ms windows and emit them at audio-clock pace."""
        if audio.size == 0:
            return
        flat = audio.reshape(-1) if audio.ndim > 1 else audio
        hop_seconds = 0.05
        hop = max(1, int(sample_rate * hop_seconds))
        n_chunks = (flat.size + hop - 1) // hop
        if n_chunks <= 0:
            return

        rms_values: list[float] = []
        for i in range(n_chunks):
            start = i * hop
            chunk = flat[start : min(start + hop, flat.size)]
            if chunk.size == 0:
                rms_values.append(0.0)
                continue
            rms_values.append(float(np.sqrt(np.mean(chunk * chunk))))

        # 95th percentile rather than the absolute peak, so a single loud
        # syllable doesn't flatten the rest of the curve.
        positives = sorted(v for v in rms_values if v > 0.0)
        peak = (
            positives[max(0, int(len(positives) * 0.95) - 1)]
            if positives
            else 1.0
        ) or 1.0
        if peak < 1e-6:
            peak = 1.0

        start_time = time.monotonic()
        for i, rms in enumerate(rms_values):
            if stop_event.is_set() or self._stop_requested.is_set():
                return
            delay = (start_time + i * hop_seconds) - time.monotonic()
            if delay > 0.001 and stop_event.wait(timeout=delay):
                return
            try:
                on_amplitude(min(1.0, max(0.0, rms / peak)))
            except Exception:
                pass
