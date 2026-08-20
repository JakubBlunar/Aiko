"""Pocket TTS backend -- CPU-only, 100M params, voice cloning support.

The synthesis still happens locally with ``pocket_tts``; the only thing
that's moved is **playback**. Instead of pushing samples through
``sounddevice.play``, the service emits Int16 LE PCM chunks (~50 ms each)
through a ``pcm_listener`` callback. :class:`SessionController` wires
that listener to the WS hub, which broadcasts the bytes as
``0x10 tts_pcm`` binary frames to every connected client; each client
plays them through its own WebAudio context. See the design note in
``app/web/server.py``.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
import threading

from app.core.infra.settings import TtsSettings
from app.tts.pcm_playback import PcmPlaybackMixin
from app.tts.reactions import (
    REACTION_SPEED,
    REACTION_SPEED_CAPS,
    SPEED_MAX,
    SPEED_MIN,
    resolve_speed_caps,
)


log = logging.getLogger("app.tts.pocket_tts_service")

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

try:
    from pocket_tts import TTSModel, export_model_state as _export_model_state
except ImportError:
    TTSModel = None  # type: ignore[assignment,misc]
    _export_model_state = None


# Type alias for the per-clip PCM emitter: ``(sample_rate, channels,
# pcm_bytes_int16_le)`` per chunk; ``pcm_bytes`` is empty on the trailing
# end-of-clip notification so the receiver can flush its playback queue.
PcmListener = Callable[[int, int, bytes], None]
PcmEndListener = Callable[[], None]

_BUILTIN_VOICES = ["alba", "marius", "javert", "jean", "fantine", "cosette", "eponine", "azelma"]

# Reaction speed tables now live in :mod:`app.tts.reactions`, shared with
# the Chatterbox engine -- how fast she talks when excited describes Aiko,
# not whichever model is rendering her. Re-exported under the old private
# names so existing callers and tests are unaffected.
_REACTION_SPEED = REACTION_SPEED
_REACTION_SPEED_CAPS = REACTION_SPEED_CAPS
_SPEED_MIN = SPEED_MIN
_SPEED_MAX = SPEED_MAX
_resolve_speed_caps = resolve_speed_caps
# Layer 1c: per-reaction temperature deltas applied on top of the
# settings baseline -- ONLY when ``_runtime_temp_enabled`` is true
# (gated by ``agent.tts_runtime_temp_enabled``, default OFF). A
# flatter temp produces more deliberate / choked delivery; a livelier
# temp introduces more variation in the acoustic stream. Reactions
# outside this table inherit the baseline unchanged.
#
# IMPORTANT: keep these deltas TINY. Pocket-TTS is sensitive enough
# to temperature that a ±0.10 swing can introduce pitch / timbre
# artefacts on some voices (a "hall echo" / chipmunk feel was
# reported on the original ±0.10 table). The current values are the
# halved-down version -- raise back gradually only after listening
# to the active voice through ``tools/tts_speed_ab.py`` at the
# proposed deltas. The combined value is clamped to ``[0.3, 1.2]``
# inside :meth:`_resolve_runtime_temp` so a stacked reaction-plus-
# manual override can't drive the model into noise / pure-silence
# territory.
_REACTION_TEMP_DELTA: dict[str, float] = {
    # Flatter delivery for serious / heavy beats.
    "serious":    -0.05,
    "wistful":    -0.05,
    "sad":        -0.05,
    "melancholy": -0.05,
    "cry":        -0.05,
    "tired":      -0.04,
    "concerned":  -0.03,
    # Livelier delivery for high-arousal beats.
    "excited":    +0.05,
    "playful":    +0.05,
    "surprised":  +0.05,
    "amused":     +0.03,
    "cheerful":   +0.03,
}

# Hard floor / ceiling on the runtime temperature so a misbehaving
# reaction map can never drive the model into noise.
_TEMP_MIN = 0.30
_TEMP_MAX = 1.20

# Hard caps on the user-facing pacing slider. The slider feeds
# :meth:`PocketTtsService.set_length_scale`; values outside this
# band are clamped silently. The band is narrower than ``[0.65, 1.35]``
# in :class:`AssistantSettings` because the pacing slider stacks
# multiplicatively with reaction speed AND the cadence layer's
# per-sentence ``speed_hint``, so a 0.65 slider would routinely
# blow past the per-reaction floor and chip into chipmunk territory.
_LENGTH_SCALE_MIN = 0.85
_LENGTH_SCALE_MAX = 1.15


class PocketTtsService(PcmPlaybackMixin):
    """TTS using Kyutai Pocket TTS. Runs on CPU, supports voice cloning.

    Synthesis only. Everything from "here is a clip" onward -- chunking,
    pacing, gain, the pitch-preserving stretch, lip-sync amplitude --
    comes from :class:`~app.tts.pcm_playback.PcmPlaybackMixin`, which the
    Chatterbox engine shares.
    """

    def __init__(
        self,
        settings: TtsSettings,
        *,
        pcm_listener: PcmListener | None = None,
        clip_end_listener: PcmEndListener | None = None,
    ) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._model: TTSModel | None = None
        self._voice_state: dict | None = None
        self._last_error: str | None = None
        self._stop_requested = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._loaded = threading.Event()
        self._audio_cache: dict[str, tuple] = {}
        self._cache_lock = threading.Lock()
        # Latched once if the installed Pocket-TTS predates ``frames_after_eos``,
        # so the TypeError probe costs one utterance rather than every one.
        self._frames_after_eos_unsupported = False
        self._pcm_listener: PcmListener | None = pcm_listener
        self._clip_end_listener: PcmEndListener | None = clip_end_listener
        # Layer 1a: global pacing knob fed by ``assistant.tts_length_scale``.
        # ``set_length_scale`` clamps this to ``[_LENGTH_SCALE_MIN,
        # _LENGTH_SCALE_MAX]`` and ``speak_async`` divides the requested
        # speed by it (length_scale > 1.0 = slower; < 1.0 = faster).
        self._length_scale: float = 1.0
        # Layer 1c: per-call temperature override. Pocket-TTS reads
        # ``model.temp`` at every ``generate_audio`` call so we can
        # mutate it under ``self._lock`` immediately before each
        # generation and reset back to ``_temp_baseline`` after.
        # Gated by :meth:`set_runtime_temp_enabled` -- default OFF so
        # the engine sticks to the configured baseline on every call.
        # Pocket-TTS is sensitive enough to temperature that even a
        # ±0.05 excursion can introduce pitch artefacts on some
        # voices; the user-facing ``agent.tts_runtime_temp_enabled``
        # setting flips it on once a voice has been validated.
        self._temp_baseline: float = float(
            getattr(settings, "pocket_tts_temp", 0.7) or 0.7
        )
        self._runtime_temp_enabled: bool = False
        # Layer 5 gate: per-reaction speed sub-caps + cadence-supplied
        # ``speed_hint`` are silenced unless this is flipped on.
        #
        # It was switched off for a specific reason that no longer holds.
        # Speed used to be varispeed -- a scaled playback sample rate --
        # so per-sentence pacing dragged pitch with it at ~1.6 semitones
        # per 10%, and driving that from the affect channel came across
        # as "her voice keeps changing between sentences". With
        # ``pitch_preserving_speed`` the rate change no longer touches
        # pitch, which removes that objection.
        #
        # Still defaulting OFF, because the objection was only half the
        # story: even at constant pitch, per-sentence pacing is an
        # audible personality change and that is the user's call to make,
        # not a default to flip on their behalf. The user-facing
        # ``agent.tts_runtime_speed_enabled`` turns it on.
        #
        # The pacing slider (``assistant.tts_length_scale``) is honoured
        # regardless -- a deliberate static knob, not affect drift.
        self._runtime_speed_enabled: bool = False
        # Whether a rate change goes through the pitch-preserving stretch
        # or the old varispeed trick. On by default; the escape hatch is
        # for A/B listening, since "does WSOLA add an artefact on *her*
        # voice" is a question only a listen can answer.
        self._pitch_preserving_speed: bool = bool(
            getattr(settings, "pitch_preserving_speed", True)
        )
        # Gated level every clip is matched to before affect gain, or 0.0
        # for the engine's raw output. Applies here too, not only to the
        # cloning engines: pocket-tts's own per-sentence spread measured
        # 8.4 dB over a twelve-sentence turn, which was simply never
        # looked at until a second engine made it worth measuring.
        self._loudness_target_dbfs: float = float(
            getattr(settings, "loudness_target_dbfs", 0.0) or 0.0
        )

        if TTSModel is not None and np is not None:
            threading.Thread(target=self._load_model, daemon=True, name="pocket-tts-load").start()
        else:
            parts = []
            if TTSModel is None:
                parts.append("pocket-tts")
            if np is None:
                parts.append("numpy")
            self._last_error = f"Missing: {', '.join(parts)}. pip install {' '.join(parts)}"
            self._loaded.set()

    # ── playback wiring ──────────────────────────────────────────────

    def set_pcm_listener(
        self,
        listener: PcmListener | None,
        *,
        end_listener: PcmEndListener | None = None,
    ) -> None:
        """Install / replace the PCM emitter.

        Called from :class:`SessionController` once the WS hub is
        wired so audio frames flow to every connected client. Safe to
        call before or after :meth:`speak_async`.
        """
        self._pcm_listener = listener
        if end_listener is not None:
            self._clip_end_listener = end_listener

    def _load_model(self) -> None:
        t0 = time.monotonic()
        try:
            temp = getattr(self._settings, "pocket_tts_temp", 0.7) or 0.7
            model = TTSModel.load_model(temp=float(temp))

            voice_id = getattr(self._settings, "pocket_tts_voice", "alba") or "alba"
            voice_state = self._resolve_voice(model, voice_id)

            with self._lock:
                self._model = model
                self._voice_state = voice_state
            self._last_error = None
            log.info(
                "TTS engine ready: provider=pocket-tts voice=%s temp=%.2f init_ms=%.0f",
                voice_id, float(temp), (time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:
            self._last_error = f"Pocket TTS load failed: {exc}"
            log.error("TTS engine init failed: exc=%r", exc)
        finally:
            self._loaded.set()

    def _resolve_voice(self, model: TTSModel, voice_id: str) -> dict:
        """Resolve a voice identifier to a model state dict."""
        if voice_id in _BUILTIN_VOICES:
            return model.get_state_for_audio_prompt(voice_id)

        path = Path(voice_id)
        if not path.is_absolute():
            base = Path(__file__).resolve().parents[2]
            voices_dir = getattr(self._settings, "pocket_tts_custom_voices_dir", "") or ""
            if voices_dir:
                path = base / voices_dir / voice_id
            else:
                path = base / "voices" / voice_id

        if path.exists():
            return model.get_state_for_audio_prompt(str(path))

        # Substituting a stock speaker keeps her talking, which is the
        # right call -- but doing it silently cost a Docker release its
        # voice with nothing in the log to explain it: the image shipped
        # config/default.json naming ``aiko1_refined.safetensors`` and no
        # ``voices/`` directory, so every container spoke as "alba" and
        # read as working. A wrong voice is not a quiet failure.
        log.warning(
            "TTS voice %r not found at %s; falling back to the stock "
            "'alba' speaker. She will not sound like herself.",
            voice_id, path,
        )
        return model.get_state_for_audio_prompt("alba")

    # ── Public model access for Voice Cloning dialog ──

    def get_model(self) -> TTSModel | None:
        self._loaded.wait(timeout=60.0)
        with self._lock:
            return self._model

    def set_voice(self, voice_id: str) -> bool:
        """Hot-swap the active voice at runtime. Returns True on success."""
        if not self._loaded.wait(timeout=10.0):
            return False
        with self._lock:
            model = self._model
        if model is None:
            return False
        try:
            new_state = self._resolve_voice(model, voice_id)
            with self._lock:
                self._voice_state = new_state
            with self._cache_lock:
                self._audio_cache.clear()
            self._settings.pocket_tts_voice = voice_id
            log.info("TTS voice switched: voice=%s", voice_id)
            return True
        except Exception as exc:
            log.warning("TTS voice switch failed: voice=%s exc=%r", voice_id, exc)
            return False

    @staticmethod
    def export_voice(model_state: dict, dest: str | Path) -> None:
        if _export_model_state is not None:
            _export_model_state(model_state, str(dest))

    # ── TtsEngine Protocol ──

    def get_status(self) -> tuple[str, str]:
        if not self._settings.enabled:
            return "disabled", "TTS disabled"
        if self._last_error:
            return "error", self._last_error
        self._loaded.wait(timeout=0.5)
        with self._lock:
            if self._model is None:
                return "error", self._last_error or "Model not loaded"
        return "ready", "Pocket TTS ready"

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True
        if not self._loaded.wait(timeout=60.0):
            self._last_error = "Pocket TTS load timed out"
            return False
        with self._lock:
            if self._model is None:
                return False
        return True

    def warmup_async(self) -> None:
        self._loaded.wait(timeout=30.0)

    def release_model(self) -> bool:
        """Drop the loaded model so the weights can be collected (P28b).

        ``stop()`` only clears the audio cache; the ~100M-param model
        stayed resident for the process lifetime, so toggling TTS off at
        runtime freed nothing. Returns True when a model was actually
        released.

        The PyTorch *runtime* stays imported -- that can't be undone in
        a live process -- so this recovers the voice weights, not the
        full footprint. Starting with ``tts.enabled=false`` avoids both.

        A subsequent :meth:`load_model_now` (or an engine rebuild) brings
        it back; ``get_status`` reports ``error`` in between, which is
        why the caller is expected to be the enable/disable path rather
        than a background sweep.
        """
        self.stop()
        with self._lock:
            had_model = self._model is not None
            self._model = None
            self._voice_state = None
        if had_model:
            # Only meaningful after the reference is gone. Torch tensors
            # are refcounted like everything else, but the model graph
            # holds cycles, so an explicit collect is what actually
            # returns the pages.
            import gc

            gc.collect()
            self._loaded.clear()
            self._last_error = "Model released (TTS disabled)"
            log.info("TTS model released")
        return had_model

    def load_model_now(self) -> None:
        """Start the load thread again after :meth:`release_model`.

        Idempotent: returns immediately when a model is already loaded or
        a load is in flight.
        """
        with self._lock:
            if self._model is not None:
                return
        if TTSModel is None or np is None:
            return
        self._loaded.clear()
        self._last_error = None
        threading.Thread(
            target=self._load_model, daemon=True, name="pocket-tts-load",
        ).start()

    def stop(self) -> None:
        self._stop_requested.set()
        with self._cache_lock:
            self._audio_cache.clear()
        # Fire the end-of-clip notification so listeners can flush any
        # buffered audio on the client. PCM emitter itself is stateless.
        end_listener = self._clip_end_listener
        if end_listener is not None:
            try:
                end_listener()
            except Exception:
                pass

    def list_voices(self) -> list[str]:
        voices = list(_BUILTIN_VOICES)
        base = Path(__file__).resolve().parents[2]
        voices_dir = getattr(self._settings, "pocket_tts_custom_voices_dir", "") or ""
        scan_dir = base / voices_dir if voices_dir else base / "voices"
        if scan_dir.is_dir():
            for f in sorted(scan_dir.iterdir()):
                if f.suffix in (".safetensors", ".wav", ".mp3"):
                    voices.append(f.name)
        return voices

    def reaction_to_speed(self, reaction: str | None) -> float:
        if not (reaction or "").strip():
            return 1.0
        return _REACTION_SPEED.get((reaction or "").strip().lower(), 1.0)

    # Layer 1a: pacing slider. Wired from
    # :meth:`SessionController._apply_assistant_preferences` so the
    # ``assistant.tts_length_scale`` setting actually changes playback
    # rate at runtime instead of silently doing nothing.
    def set_length_scale(self, scale: float) -> None:
        """Set the global pacing multiplier.

        Values > 1.0 slow speech down; values < 1.0 speed it up.
        Clamped to ``[_LENGTH_SCALE_MIN, _LENGTH_SCALE_MAX]``. The
        scale is divided into the requested speed at synthesis time
        so it stacks multiplicatively with the per-reaction baseline
        and the cadence layer's per-sentence ``speed_hint``.
        """
        try:
            value = float(scale)
        except (TypeError, ValueError):
            value = 1.0
        if value <= 0.0:
            value = 1.0
        self._length_scale = max(
            _LENGTH_SCALE_MIN, min(_LENGTH_SCALE_MAX, value),
        )

    def get_length_scale(self) -> float:
        return self._length_scale

    def set_runtime_temp_enabled(self, enabled: bool) -> None:
        """Layer 1c gate: enable or disable per-reaction ``model.temp`` mutation.

        Default is ``False`` (disabled) -- the engine stays on the
        configured baseline temperature on every call. Wired from
        :meth:`SessionController._apply_assistant_preferences` so the
        ``agent.tts_runtime_temp_enabled`` setting takes effect at
        startup and on subsequent settings reloads. An explicit
        ``temp=`` kwarg on :meth:`speak_async` still overrides the
        baseline regardless of this gate -- the gate only governs
        whether the per-reaction *delta* table is applied.
        """
        self._runtime_temp_enabled = bool(enabled)

    def get_runtime_temp_enabled(self) -> bool:
        return self._runtime_temp_enabled

    def set_runtime_speed_enabled(self, enabled: bool) -> None:
        """Layer 5 gate: enable or disable per-reaction speed jitter.

        Default ``False``. Historically that was because rate changes
        pitch-shifted the voice; ``pitch_preserving_speed`` fixed that, so
        what remains is simply that per-sentence pacing is an audible
        personality choice the user should opt into.

        When OFF, :meth:`speak_async` ignores both the cadence layer's
        ``speed_hint`` AND the per-reaction sub-cap table, pinning every
        sentence to ``1.0×`` before the user's :attr:`_length_scale` is
        applied -- so the whole affect-driven pacing channel is inert.

        Flipped on through ``agent.tts_runtime_speed_enabled``, ideally
        after a listen through ``tools/tts_speed_ab.py`` at the proposed
        band.
        """
        self._runtime_speed_enabled = bool(enabled)

    def get_runtime_speed_enabled(self) -> bool:
        return self._runtime_speed_enabled

    @staticmethod
    def _gain_db_to_factor(gain_db: float) -> float:
        """Convert a dB offset to an Int16 sample multiplier.

        Clamped to ``[-12, +6]`` dB so a runaway caller can never
        scale samples enough to clip the entire clip into noise (the
        PCM step ``np.clip(..., -1.0, 1.0)`` already saturates loud
        peaks; this clamp keeps quiet clips from being amplified into
        a wall of noise either).
        """
        try:
            value = float(gain_db)
        except (TypeError, ValueError):
            return 1.0
        value = max(-12.0, min(6.0, value))
        if abs(value) < 1e-3:
            return 1.0
        return float(10.0 ** (value / 20.0))

    def _resolve_runtime_temp(
        self, reaction: str | None, override: float | None,
    ) -> float:
        """Combine baseline temp + per-reaction delta + caller override.

        Caller override wins when supplied; otherwise -- and only
        when :attr:`_runtime_temp_enabled` is true -- we apply the
        :data:`_REACTION_TEMP_DELTA` adjustment on top of the
        baseline. With the gate off (the default) the baseline is
        returned untouched. Always clamped to ``[_TEMP_MIN, _TEMP_MAX]``.
        """
        if override is not None:
            try:
                value = float(override)
            except (TypeError, ValueError):
                value = self._temp_baseline
        elif self._runtime_temp_enabled:
            delta = _REACTION_TEMP_DELTA.get(
                (reaction or "").strip().lower(), 0.0,
            )
            value = self._temp_baseline + float(delta)
        else:
            value = self._temp_baseline
        return max(_TEMP_MIN, min(_TEMP_MAX, value))

    def speak_async(
        self,
        text: str,
        reaction: str | None = None,
        on_done: Callable[[], None] | None = None,
        on_amplitude: Callable[[float], None] | None = None,
        *,
        speed: float | None = None,
        gain_db: float = 0.0,
        temp: float | None = None,
    ) -> None:
        """Synthesise and play ``text``.

        ``speed`` (when provided) overrides the reaction-derived
        baseline so the cadence layer can apply per-sentence nudges on
        top of the per-reaction default. Final value is clamped to the
        per-reaction sub-cap from :func:`_resolve_speed_caps` (and the
        global ``[_SPEED_MIN, _SPEED_MAX]`` envelope), then divided by
        :attr:`_length_scale` so the user's pacing slider stacks
        multiplicatively.

        ``gain_db`` (Layer 1b / Layer 3) is a small dB offset applied
        to the Int16 PCM samples just before the listener emits them.
        ``+`` boosts (e.g. ``firm``); ``-`` attenuates (e.g.
        ``whisper`` / ambient-noise compensation). Clamped to
        ``[-12, +6]`` dB.

        ``temp`` (Layer 1c) overrides the per-reaction temperature
        delta. ``None`` uses the reaction-derived value; an explicit
        float pins generation stochasticity for this one call.
        """
        if not self._settings.enabled or not (text or "").strip():
            return
        self._stop_requested.clear()
        if not self._runtime_speed_enabled:
            # Gate OFF (default): pin every sentence to 1.0× before
            # length-scale. Per-reaction sub-caps and any caller-
            # supplied ``speed=`` from the cadence layer are ignored
            # so the voice stays at the engine's tuned baseline pitch
            # across the whole reply. The user's pacing slider
            # (``_length_scale``) still applies below.
            final_speed = 1.0
        else:
            if speed is None:
                final_speed = self.reaction_to_speed(reaction)
            else:
                try:
                    final_speed = float(speed)
                except (TypeError, ValueError):
                    final_speed = self.reaction_to_speed(reaction)
            # Per-reaction sub-cap first, then the global outer envelope.
            sub_min, sub_max = _resolve_speed_caps(reaction)
            final_speed = max(sub_min, min(sub_max, final_speed))
            final_speed = max(_SPEED_MIN, min(_SPEED_MAX, final_speed))
        # Length-scale stacks AFTER the reaction clamp so a slow user
        # pacing setting doesn't fight the per-reaction floor (cry
        # already sits near 0.92; dividing by 1.10 lands at ~0.84,
        # which is below ``_SPEED_MIN`` -- the final clamp below
        # catches that case so we never produce unsafe values).
        if abs(self._length_scale - 1.0) > 1e-3:
            final_speed = final_speed / self._length_scale
        final_speed = max(_SPEED_MIN, min(_SPEED_MAX, final_speed))
        gain_factor = self._gain_db_to_factor(gain_db)
        runtime_temp = self._resolve_runtime_temp(reaction, temp)
        self._speech_thread = threading.Thread(
            target=self._speak_worker,
            args=(
                text.strip(),
                on_done,
                final_speed,
                on_amplitude,
                gain_factor,
                runtime_temp,
            ),
            daemon=True,
        )
        self._speech_thread.start()

    def speak_silence_async(
        self,
        ms: int,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Layer 2: emit ``ms`` milliseconds of silent PCM.

        Used by :class:`TtsQueue.enqueue_silence` to splice real timed
        gaps between text chunks (vs the legacy ellipsis-rewrite trick
        in ``_apply_text_pauses``). Cap is enforced upstream
        (``TtsQueue`` clamps to 1500 ms); we just guard against
        zero / negative values here.
        """
        if not self._settings.enabled or ms is None:
            self._fire_silence_done(on_done)
            return
        try:
            duration_ms = int(ms)
        except (TypeError, ValueError):
            duration_ms = 0
        if duration_ms <= 0:
            self._fire_silence_done(on_done)
            return
        self._stop_requested.clear()
        self._speech_thread = threading.Thread(
            target=self._silence_worker,
            args=(duration_ms, on_done),
            daemon=True,
            name="pocket-tts-silence",
        )
        self._speech_thread.start()

    def _silence_worker(
        self,
        duration_ms: int,
        on_done: Callable[[], None] | None,
    ) -> None:
        sample_rate = 24000
        with self._lock:
            model = self._model
        if model is not None:
            try:
                sample_rate = int(model.sample_rate)
            except Exception:
                sample_rate = 24000
        try:
            n_samples = max(1, int(sample_rate * duration_ms / 1000.0))
            # Deadline-based wait: the queue advances when ``on_done``
            # fires, so the total wall-clock between enqueue_silence
            # and the next text chunk MUST equal ``duration_ms``. The
            # original implementation called ``_emit_pcm`` (which
            # paces frames in real-time after a 5-chunk pre-roll) and
            # then ALSO slept for the full duration, doubling the
            # gap on long pauses (e.g. 600 ms requested -> ~950 ms
            # actual). The user reported "big echo / hall feel" on
            # multi-sentence replies; this was the underlying timing
            # bug. Now we record the start, run ``_emit_pcm`` (which
            # may itself take some of the budget), and wait out only
            # the *remaining* slice up to the deadline.
            emit_t0 = time.monotonic()
            if np is not None:
                silence = np.zeros(n_samples, dtype=np.float32)
                self._emit_pcm(silence, sample_rate)
            deadline = emit_t0 + (duration_ms / 1000.0)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                if self._stop_requested.wait(timeout=min(remaining, 0.05)):
                    break
        except Exception:
            log.debug("silence emission failed", exc_info=True)
        finally:
            self._fire_silence_done(on_done)

    @staticmethod
    def _fire_silence_done(on_done: Callable[[], None] | None) -> None:
        if on_done is None:
            return
        try:
            on_done()
        except Exception:
            pass

    def _cache_key(self, text: str, speed: float, temp: float = 0.0) -> str:
        # ``temp`` participates in the cache key only when a non-default
        # value is in effect (Layer 1c per-reaction delta or caller
        # override). Stays out of the key for the bulk of calls so the
        # baseline cache hit rate doesn't regress.
        if abs(temp - self._temp_baseline) < 1e-3:
            return f"{text}||{speed:.3f}"
        return f"{text}||{speed:.3f}||t{temp:.3f}"

    def _generate(self, model, voice_state, text: str):
        """Synthesise ``text``, bounding the audio decoded after EOS.

        Pocket-TTS keeps decoding for a guessed number of frames once the
        model has signalled it is done, and pads that guess by two. At the
        Mimi frame rate (12.5 Hz) that is 240 ms per clip, or 400 ms for
        utterances of four words or fewer. Measured with the RNG pinned --
        so the two takes are sample-identical up to the tail -- that audio
        sits at 14-42% of the body's RMS. It is not decay, it is a syllable
        the text never asked for, and it lands at the end of every spoken
        chunk. Frame 1 is the genuine phoneme release; from frame 2 on it
        is invention, which is what :attr:`pocket_tts_frames_after_eos`
        trims.

        Older Pocket-TTS builds don't accept the argument; those fall back
        to the library default rather than failing the utterance.
        """
        frames = getattr(self._settings, "pocket_tts_frames_after_eos", 1)
        if frames is None or self._frames_after_eos_unsupported:
            return model.generate_audio(voice_state, text, copy_state=True)
        try:
            return model.generate_audio(
                voice_state, text, copy_state=True, frames_after_eos=int(frames),
            )
        except TypeError:
            self._frames_after_eos_unsupported = True
            log.info(
                "pocket-tts: frames_after_eos unsupported by this build, "
                "falling back to the library default tail",
            )
            return model.generate_audio(voice_state, text, copy_state=True)

    def generate_audio(
        self,
        text: str,
        speed: float = 1.0,
        *,
        temp: float | None = None,
    ) -> tuple | None:
        """Generate audio, returning (numpy_array, sample_rate) or None.

        Layer 1c: ``temp`` (when provided) is applied to ``model.temp``
        for the duration of this generation under :attr:`_lock`, then
        the baseline value is restored. A ``None`` ``temp`` keeps the
        baseline in place — the path the lookahead synthesiser takes
        when it doesn't know the reaction yet.
        """
        runtime_temp = (
            float(temp) if temp is not None else self._temp_baseline
        )
        key = self._cache_key(text, speed, runtime_temp)
        with self._cache_lock:
            cached = self._audio_cache.get(key)
            if cached is not None:
                return cached

        if not self._loaded.wait(timeout=30.0):
            return None
        with self._lock:
            model = self._model
            voice_state = self._voice_state
            if model is None or voice_state is None or np is None:
                return None
            prior_temp = float(getattr(model, "temp", self._temp_baseline))
            temp_changed = abs(runtime_temp - prior_temp) > 1e-3
            if temp_changed:
                try:
                    model.temp = runtime_temp
                except Exception:
                    temp_changed = False
            try:
                audio_tensor = self._generate(model, voice_state, text)
            finally:
                if temp_changed:
                    try:
                        model.temp = prior_temp
                    except Exception:
                        pass
        audio_data = audio_tensor.numpy().astype(np.float32)
        if audio_data.size == 0:
            return None

        sample_rate = model.sample_rate
        result = (audio_data, sample_rate)
        with self._cache_lock:
            self._audio_cache[key] = result
            if len(self._audio_cache) > 8:
                oldest = next(iter(self._audio_cache))
                del self._audio_cache[oldest]
        return result

    def _speak_worker(
        self,
        text: str,
        on_done: Callable[[], None] | None = None,
        speed: float = 1.0,
        on_amplitude: Callable[[float], None] | None = None,
        gain_factor: float = 1.0,
        runtime_temp: float | None = None,
    ) -> None:
        chunk_chars = len(text)
        gen_t0 = time.monotonic()
        log.debug(
            "TTS enqueue: chunk_chars=%d speed=%.2f gain=%.2fx temp=%.2f",
            chunk_chars,
            speed,
            float(gain_factor),
            float(runtime_temp if runtime_temp is not None else self._temp_baseline),
        )
        try:
            result = self.generate_audio(text, speed, temp=runtime_temp)
            if result is None or self._stop_requested.is_set():
                return
            audio_data, sample_rate = result
            with self._cache_lock:
                self._audio_cache.pop(
                    self._cache_key(
                        text,
                        speed,
                        runtime_temp if runtime_temp is not None else self._temp_baseline,
                    ),
                    None,
                )

            generate_ms = (time.monotonic() - gen_t0) * 1000.0
            played_ms = self._play_clip(
                audio_data,
                sample_rate,
                speed=speed,
                gain_factor=gain_factor,
                on_amplitude=on_amplitude,
            )
            log.debug(
                "TTS play done: chunk_chars=%d generate_ms=%.0f played_ms=%.0f speed=%.2f",
                chunk_chars, generate_ms, played_ms, speed,
            )
        except Exception as exc:
            self._last_error = str(exc)
            log.error(
                "TTS playback failed: chunk_chars=%d exc=%r",
                chunk_chars, exc,
            )
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
