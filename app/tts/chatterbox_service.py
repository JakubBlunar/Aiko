"""Chatterbox as a live TTS engine, running in its own interpreter.

Why a subprocess
----------------
Chatterbox requires ``torch==2.6.0``; this app runs ``2.10.0``. That is
not a version range to negotiate, it is two incompatible ABIs, so the
engine cannot be imported into this process at any price. It therefore
runs in its own virtualenv under ``.venvs/`` and speaks JSON over a pipe.

The worker on the far side is ``tools/tts_lab/sidecar.py``, which was
written for the audition bench and imports nothing from this repo by
design. Reusing it means the engine Aiko speaks with is byte-for-byte the
one that was auditioned, rather than a reimplementation that might differ
in exactly the ways a listening test was supposed to settle.

What this class is and isn't
----------------------------
It is synthesis plus process supervision. Everything after "here is a
clip" -- chunking, real-time pacing, gain, the pitch-preserving stretch,
lip-sync amplitude, the block that keeps sentence timing honest -- comes
from :class:`~app.tts.pcm_playback.PcmPlaybackMixin`, shared with
pocket-tts. That shared path is why swapping engines at runtime does not
change how audio reaches the client.

Voices are reference clips, not embeddings
------------------------------------------
Chatterbox clones zero-shot from a wav on every load, so a "voice" here
is a path under ``data/voices/`` rather than a ``.safetensors`` speaker
state. Her committed reference (``reference/aiko_reference.wav``) is the
default, which is the whole reason that file was extracted from
pocket-tts in the first place.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from app.core.infra.settings import TtsSettings
from app.audio.speech_rate import MAX_CORRECTION as MAX_RATE_CORRECTION
from app.audio.timbre import MAX_CORRECTION_DB
from app.tts.clip_cache import ClipCache, SynthesisGate
from app.tts.pcm_playback import PcmPlaybackMixin
from app.tts.shaping import (
    loudness_target_for,
    measure_rate_target,
    measure_tilt_target,
    read_wav_mono as _read_wav,
)
from app.tts.reactions import (
    clamp_length_scale,
    reaction_to_speed as _reaction_to_speed,
    resolve_playback_speed,
)

log = logging.getLogger(__name__)

#: Where saved reference clips live, shared with the voice studio. Not
#: under ``data/`` because these are inputs rather than generated state:
#: ``voices/reference/aiko_reference.wav`` is tracked in git, being the
#: only portable copy of her voice that exists.
VOICES_DIR = Path("voices")

#: Her portable voice, extracted from pocket-tts so that any cloning
#: engine can reproduce it. The default for every Chatterbox variant.
DEFAULT_REFERENCE = "reference/aiko_reference.wav"

#: Subtrees of ``voices/`` holding generated output rather than voices.
#: Kept in step with ``_SCRATCH`` in ``tools/tts_lab/serve.py``.
#: ``sounds/`` is found source material -- the clips a reference gets
#: *built from*, of which this machine has 98. The built reference is the
#: voice; its ingredients are not.
_SCRATCH = ("studio/", "datasets/", "audition/", "speed_ab/", "sounds/")

#: A model download on first run can take minutes; a warm load is
#: seconds. Generous because the alternative to waiting is a mute
#: companion and a confusing error.
LOAD_TIMEOUT_S = 600.0

#: One utterance. Long enough for a slow CPU synthesis of a long
#: sentence, short enough that a wedged subprocess is noticed.
SYNTH_TIMEOUT_S = 120.0


class ChatterboxTtsService(PcmPlaybackMixin):
    """A Chatterbox variant, supervised as a child process."""

    is_null_engine = False

    def __init__(
        self,
        settings: TtsSettings,
        *,
        interpreter: Path,
        sidecar: Path,
        engine_key: str,
        device: str = "cpu",
        voice: str = "",
        threads: int = 0,
        generate: dict[str, float] | None = None,
    ) -> None:
        self._settings = settings
        self._interpreter = Path(interpreter)
        self._sidecar = Path(sidecar)
        self._engine_key = engine_key
        self._device = device
        self._threads = int(threads or 0)
        self._voice = (voice or DEFAULT_REFERENCE).strip() or DEFAULT_REFERENCE

        self._proc: subprocess.Popen | None = None
        self._sample_rate = 24000
        self._actual_device = ""
        self._last_error = ""
        self._metadata: dict[str, Any] = {}

        # Serialises requests to the child. The protocol is
        # one-line-in/one-line-out with no request ids, so two concurrent
        # callers would read each other's replies.
        self._io_lock = threading.Lock()
        self._clip_cache = ClipCache()
        self._synth_gate = SynthesisGate()
        # Pacing, matching pocket-tts. Defaults are "no global change"
        # and "affect channel off", the same as the incumbent, so a
        # provider swap is not also a pacing change.
        self._length_scale: float = 1.0
        self._runtime_speed_enabled: bool = False
        self._loaded = threading.Event()
        self._stop_requested = threading.Event()
        self._speak_thread: threading.Thread | None = None

        # Playback contract (see PcmPlaybackMixin).
        self._pcm_listener: Callable[[int, int, bytes], None] | None = None
        self._clip_end_listener: Callable[[], None] | None = None
        self._clip_cancel_listener: Callable[[], None] | None = None
        self._configure_pre_roll(settings)
        self._pitch_preserving_speed = bool(
            getattr(settings, "pitch_preserving_speed", True)
        )
        # Level matching, on by default here: a cloning engine re-samples
        # her level on every call, and the between-sentence drift is large
        # relative to the expression it carries. pocket-tts defaults the
        # other way -- see the note in its ``__init__``.
        self._loudness_target_dbfs = loudness_target_for(
            self._engine_key, settings,
        )
        # Brightness matching. The target is not known until a reference
        # clip has been cloned, so it starts unset and ``_clone`` fills
        # it; the limit is read up front so it can be logged there.
        self._tilt_limit_db = float(
            getattr(settings, "timbre_match_limit_db", MAX_CORRECTION_DB)
        )
        self._tilt_matching = self._tilt_limit_db > 0.0
        self._tilt_target_db: float | None = None
        # Tempo matching. Same story as brightness: the target comes from
        # the reference, so it is unknown until a clone has happened.
        self._rate_limit = float(
            getattr(settings, "speech_rate_match_limit", MAX_RATE_CORRECTION)
        )
        self._rate_matching = self._rate_limit > 0.0
        self._rate_target_syl_s: float | None = None
        # Sampling knobs. Settings win where set; otherwise the voice's
        # manifest supplies them, so ``_clone`` fills this in too.
        self._settings_kwargs = {
            key: float(value)
            for key, value in dict(generate or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        self._generate_kwargs: dict[str, object] = {}

        self._scratch = Path(tempfile.mkdtemp(prefix="aiko-chatterbox-"))
        threading.Thread(
            target=self._load, daemon=True, name="chatterbox-load"
        ).start()

    # ── process + protocol ───────────────────────────────────────────

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            str(self._interpreter),
            str(self._sidecar),
            "--engine",
            self._engine_key,
            "--device",
            self._device,
            "--threads",
            str(self._threads),
        ]
        log.info(
            "starting %s sidecar on %s", self._engine_key, self._device,
        )
        creationflags = 0
        if sys.platform == "win32":
            # Otherwise a console window flashes up on every start.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr is inherited on purpose: the sidecar sends tracebacks
            # and model download progress there, and swallowing them would
            # make a failed load undiagnosable from the app's logs.
            stderr=None,
            text=True,
            bufsize=1,
            encoding="utf-8",
            creationflags=creationflags,
        )

    def _request(self, payload: dict, timeout: float) -> dict:
        """One round trip. Raises on protocol or engine failure."""
        with self._io_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise RuntimeError("chatterbox sidecar is not running")
            assert proc.stdin is not None and proc.stdout is not None

            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

            # A blocking readline with no timeout would hang the speak
            # thread forever if the child wedged, and that thread is what
            # TtsQueue waits on -- so the whole voice would stop with no
            # error. Reading on a helper thread keeps the timeout real.
            result: dict[str, Any] = {}

            def _read() -> None:
                try:
                    line = proc.stdout.readline()
                    result["line"] = line
                except Exception as exc:
                    result["exc"] = exc

            reader = threading.Thread(target=_read, daemon=True)
            reader.start()
            reader.join(timeout)
            if reader.is_alive():
                raise TimeoutError(
                    f"{self._engine_key} did not answer {payload.get('op')!r} "
                    f"within {timeout:.0f}s"
                )
            if "exc" in result:
                raise RuntimeError(f"sidecar read failed: {result['exc']!r}")

            line = (result.get("line") or "").strip()
            if not line:
                code = proc.poll()
                raise RuntimeError(
                    f"{self._engine_key} sidecar exited (code {code}); "
                    "see stderr for the traceback"
                )
            reply = json.loads(line)

        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "unknown sidecar error")
        return reply

    def _load(self) -> None:
        try:
            self._proc = self._spawn()
            reply = self._request({"op": "load"}, LOAD_TIMEOUT_S)
            self._sample_rate = int(reply.get("sample_rate") or 24000)
            self._actual_device = str(reply.get("device") or self._device)
            self._metadata = {
                key: reply.get(key)
                for key in ("torch", "python", "gpu", "compute_capability")
                if reply.get(key)
            }
            self._clone(self._voice)
            log.info(
                "%s ready on %s at %d Hz (%.1fs)",
                self._engine_key,
                self._actual_device,
                self._sample_rate,
                float(reply.get("load_ms") or 0.0) / 1000.0,
            )
            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)
            log.error("%s failed to load: %s", self._engine_key, exc)
            self._kill()
        finally:
            self._loaded.set()

    def _clone(self, voice: str) -> None:
        reference = self._resolve_voice(voice)
        if reference is None:
            raise RuntimeError(
                f"reference clip not found for voice {voice!r}; expected "
                f"one under {VOICES_DIR}"
            )
        self._request(
            {"op": "clone", "ref": str(reference)}, LOAD_TIMEOUT_S
        )
        self._voice = voice
        self._adopt_tilt_target(reference)
        self._adopt_rate_target(reference)
        self._adopt_generate_kwargs(reference)

    def _adopt_generate_kwargs(self, reference: Path) -> None:
        """Take tuned sampling knobs from the reference's own manifest.

        The app used to send none, so every voice spoke on its engine's
        shipped defaults and anything tuned in the studio was unreachable
        from here. Which mattered: a reproducible artifact on an
        utterance-initial /h/ ("Hey, ...") clears up at a colder
        temperature, and there was no way to carry that finding across.

        Stored per voice, because that is the level the tuning belongs to
        -- knobs that steady one reference say nothing about another --
        and within that, **per engine**. The values are absolute while
        the defaults they were chosen against are not: Nano ships
        ``min_p=0.0`` where the full model ships ``0.05``, so the same
        number is a real intervention on one and a no-op on the other.
        An engine with no entry gets its own defaults rather than
        somebody else's numbers.
        """
        self._generate_kwargs = {}
        if self._settings_kwargs:
            # An explicit setting outranks the voice: it is the lever for
            # a bare wav with no manifest, and for trying a value in the
            # live app without rebuilding a reference to hold it.
            self._generate_kwargs = dict(self._settings_kwargs)
            log.info(
                "%s generating with %s (from settings)",
                self._engine_key,
                self._generate_kwargs,
            )
            return
        manifest = reference.parent / "manifest.json"
        if not manifest.is_file():
            return
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
            block = body.get("generate")
            if not isinstance(block, dict):
                return
            mine = block.get(self._engine_key)
            if not isinstance(mine, dict) or not mine:
                if block:
                    log.info(
                        "%s: %s carries tuning for %s but not for this "
                        "engine, using shipped defaults",
                        self._engine_key,
                        reference.name,
                        ", ".join(sorted(block)),
                    )
                return
            kwargs = {
                key: float(value)
                for key, value in mine.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            if not kwargs:
                return
            self._generate_kwargs = kwargs
            log.info(
                "%s generating with %s (tuned on %s)",
                self._engine_key,
                kwargs,
                reference.name,
            )
        except Exception as exc:
            log.warning(
                "%s could not read generation knobs from %s: %r",
                self._engine_key,
                manifest,
                exc,
            )

    def _adopt_tilt_target(self, reference: Path) -> None:
        """Take the brightness target from the clip being cloned.

        The reference is what her voice is supposed to sound like, which
        makes it a better target than any constant: it both flattens the
        per-sentence timbre drift and closes the 1.2-1.5 dB high-frequency
        deficit that reads as "a little muffled". Failing to read it is
        not fatal -- brightness matching is an improvement, not a
        requirement, so the engine still speaks without it.
        """
        if not self._tilt_matching:
            return
        self._tilt_target_db = measure_tilt_target(reference)
        if self._tilt_target_db is None:
            return
        log.info(
            "%s matching brightness to %s (tilt %.2f dB, limit %.1f dB)",
            self._engine_key,
            reference.name,
            self._tilt_target_db,
            self._tilt_limit_db,
        )

    def _adopt_rate_target(self, reference: Path) -> None:
        """Take the tempo target from the reference clip's own manifest.

        Measuring a rate needs text as well as audio, so unlike the
        brightness target this cannot be read off the wav. What it can be
        read off is the ``manifest.json`` the reference was built with,
        which lists each part's phrase beside its file: measure every part
        and take the median, which is robust to one phrase's syllable
        estimate being off.

        No manifest means no target and no correction, which is the right
        default for a voice somebody dropped in as a bare wav -- guessing
        a tempo for a stranger's clip would be worse than leaving pacing
        alone.

        A manifest may also **declare** ``target_syl_s`` outright, which
        the measured route cannot always reach. Her recovered source pack
        is Japanese game audio: the clips are unquestionably her voice,
        and there is no honest English transcript to measure a rate from,
        so a reference built out of them gets no target and delivers
        about 8% slow with nothing able to correct it. Declaring the
        target says "this is her, hold her to her own pace" and is the
        one case where a constant beats a measurement.
        """
        if not self._rate_matching:
            return
        target = measure_rate_target(reference)
        self._rate_target_syl_s = target.syl_s if target else None
        if target is None:
            log.info(
                "%s: no usable tempo target beside %s, tempo matching is off",
                self._engine_key,
                reference.name,
            )
            return
        log.info(
            "%s matching tempo to %s (%.2f syl/s %s, limit %.0f%%)",
            self._engine_key,
            reference.name,
            target.syl_s,
            (
                "declared by its manifest"
                if target.source == "declared"
                else f"over {target.parts} parts"
            ),
            self._rate_limit * 100.0,
        )

    def _resolve_voice(self, voice: str) -> Path | None:
        name = (voice or "").strip()
        if not name:
            name = DEFAULT_REFERENCE
        for candidate in (Path(name), VOICES_DIR / name):
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and proc.poll() is None:
                proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                proc.stdin.flush()
            proc.wait(timeout=5.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5.0)
            except Exception:
                log.debug("sidecar kill failed", exc_info=True)
        finally:
            # Closed explicitly: an engine swap discards the whole
            # service, so leaving the pipes to the garbage collector
            # leaks a file handle pair per switch, and a settings drawer
            # is a very easy place to switch a dozen times.
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    # ── status ───────────────────────────────────────────────────────

    def get_status(self) -> tuple[str, str]:
        if not getattr(self._settings, "enabled", True):
            return "disabled", "TTS disabled"
        if self._last_error:
            return "error", self._last_error
        if not self._loaded.wait(timeout=0.5):
            return "error", f"{self._engine_key} still loading"
        if self._proc is None:
            return "error", self._last_error or "sidecar not running"
        return "ready", f"{self._engine_key} ready on {self._actual_device}"

    def model_status(self) -> tuple[str, str]:
        state, detail = self.get_status()
        return ("loaded" if state == "ready" else state), detail

    def describe(self) -> dict[str, Any]:
        """What actually happened, for the settings UI and MCP status.

        ``device`` here is what the child really got, which is not always
        what was asked for -- a CUDA request against a CPU-only torch
        wheel is refused by the sidecar, and that refusal has to be
        visible or the user is left guessing why Turbo stutters.
        """
        return {
            "engine": self._engine_key,
            "device": self._actual_device or self._device,
            "device_requested": self._device,
            "sample_rate": self._sample_rate,
            "voice": self._voice,
            **self._metadata,
        }

    def warmup_sync(self) -> bool:
        if not getattr(self._settings, "enabled", True):
            return True
        if not self._loaded.wait(timeout=LOAD_TIMEOUT_S):
            self._last_error = f"{self._engine_key} load timed out"
            return False
        return self._proc is not None and not self._last_error

    def warmup_async(self) -> None:
        return None

    # ── voices ───────────────────────────────────────────────────────

    def list_voices(self) -> list[str]:
        """Reference clips, as paths relative to ``voices/``.

        Scratch subtrees are excluded. ``voices/`` also accumulates
        audition renders, generated fine-tuning datasets and studio takes,
        which on this machine made the settings dropdown 279 entries deep
        -- indistinguishable from having no picker at all. Same rule as
        the voice studio's own list (``_SCRATCH`` in
        ``tools/tts_lab/serve.py``), so the app and the lab agree on what
        counts as a voice.
        """
        if not VOICES_DIR.is_dir():
            return []
        found: list[str] = []
        for path in sorted(VOICES_DIR.rglob("*.wav")):
            rel = path.relative_to(VOICES_DIR).as_posix()
            if any(rel.startswith(p) for p in _SCRATCH) or "roundtrip" in rel:
                continue
            # Fragments of a reference, not voices. A segment match
            # rather than a prefix one: the prefix rule covered the one
            # ``reference/parts/`` that existed when it was written, and
            # every reference set built in the studio since has its own
            # ``<name>/parts/`` holding a dozen more.
            if "parts" in path.relative_to(VOICES_DIR).parent.parts:
                continue
            found.append(rel)
        # Her canonical reference first: it is the answer nearly always
        # wanted, and being top of the list makes the default obvious.
        found.sort(key=lambda name: (name != DEFAULT_REFERENCE, name))
        return found

    def set_voice(self, voice_id: str) -> bool:
        name = (voice_id or "").strip()
        if not name or name == self._voice:
            return False
        if self._resolve_voice(name) is None:
            log.warning("no reference clip for voice %r", name)
            return False
        if not self._loaded.wait(timeout=LOAD_TIMEOUT_S):
            return False
        try:
            self._clone(name)
        except Exception as exc:
            log.warning("voice switch to %r failed: %s", name, exc)
            return False
        # Clips are voice-specific and the key is text-only, so a
        # surviving entry would speak the next repeat in the old voice.
        self._clip_cache.clear()
        log.info("%s now cloning from %s", self._engine_key, name)
        return True

    def reaction_to_speed(self, reaction: str | None) -> float:
        # Shared with pocket-tts so a provider swap does not also change
        # her pacing -- the mapping is a property of her character, not of
        # whichever model is rendering it.
        return _reaction_to_speed(reaction)

    # ── pacing ───────────────────────────────────────────────────────
    #
    # Both knobs below existed only on pocket-tts, and the wiring in
    # ``_apply_assistant_preferences`` reaches them through ``getattr``,
    # so on Chatterbox they were absent rather than broken: the user's
    # pacing slider silently did nothing, and the affect-speed gate being
    # off did not stop the cadence layer's per-sentence hints from being
    # applied here in full. The result was simply that she spoke faster
    # on this engine than on the other one.

    def set_length_scale(self, scale: float) -> None:
        """Set the global pacing multiplier. Above 1.0 is slower."""
        self._length_scale = clamp_length_scale(scale)

    def get_length_scale(self) -> float:
        return self._length_scale

    def set_runtime_speed_enabled(self, enabled: bool) -> None:
        """Gate the affect-driven speed channel (default off).

        Off pins every sentence to 1.0 before the pacing slider, so
        reaction baselines and cadence ``speed_hint`` values are ignored.
        """
        self._runtime_speed_enabled = bool(enabled)

    def get_runtime_speed_enabled(self) -> bool:
        return self._runtime_speed_enabled

    # ── synthesis ────────────────────────────────────────────────────

    def generate_audio(
        self, text: str, speed: float = 1.0, *, temp: float | None = None
    ) -> tuple[np.ndarray, int] | None:
        """Synthesise without playing, for TtsQueue's lookahead.

        ``speed`` is deliberately ignored here: Chatterbox has no native
        rate control, and the shared playback path applies the stretch at
        emission time. Accepting the argument and doing nothing with it
        keeps the engine interchangeable with pocket-tts.

        The clip stays in :attr:`_clip_cache` for the playback call that
        follows a prefetch. Without that, the lookahead thread paid the
        full synthesis and dropped the result on the floor, and playback
        then queued a second synthesis of the same sentence behind the
        sidecar's pipe lock -- so a prefetch made the gap *worse* than no
        prefetch at all.
        """
        prepared = (text or "").strip()
        if not prepared:
            return None
        # Yields to the sentence being spoken. Mandatory here rather than
        # merely tidy: the sidecar takes one request at a time, so a
        # prefetch that wins the pipe delays the audio being waited on by
        # a full generation.
        if not self._synth_gate.wait_for_idle():
            return None
        return self._warm_clip(prepared)

    def _warm_clip(self, prepared: str) -> tuple[np.ndarray, int] | None:
        """Cached synthesis, ungated. One round trip per text across threads."""
        return self._clip_cache.warm(
            ClipCache.key(prepared), lambda: self._synthesise(prepared),
        )

    def _synthesise(self, prepared: str) -> tuple[np.ndarray, int] | None:
        """One round trip to the sidecar. Called through the cache."""
        if not self._loaded.wait(timeout=LOAD_TIMEOUT_S):
            return None
        out = self._scratch / f"synth-{time.monotonic_ns()}.wav"
        try:
            request: dict[str, object] = {
                "op": "synth",
                "text": prepared,
                "voice": 0,
                "out": str(out),
            }
            if self._generate_kwargs:
                # The sidecar drops anything this engine's generate()
                # does not accept, so a knob that moved between versions
                # is inert rather than fatal.
                request["kwargs"] = dict(self._generate_kwargs)
            self._request(request, SYNTH_TIMEOUT_S)
            audio, rate = _read_wav(out)
        except Exception as exc:
            self._last_error = str(exc)
            log.error("%s synth failed: %s", self._engine_key, exc)
            return None
        finally:
            out.unlink(missing_ok=True)
        if audio.size == 0:
            return None
        return audio, rate

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
        if not getattr(self._settings, "enabled", True):
            if on_done:
                on_done()
            return
        prepared = (text or "").strip()
        if not prepared:
            if on_done:
                on_done()
            return
        effective = resolve_playback_speed(
            reaction,
            speed,
            runtime_speed_enabled=self._runtime_speed_enabled,
            length_scale=self._length_scale,
        )
        self._stop_requested.clear()
        # Claimed from the caller's thread so it is ordered ahead of any
        # in-flight prefetch; the worker releases it once synthesised.
        self._synth_gate.claim()
        self._speak_thread = threading.Thread(
            target=self._speak_worker,
            args=(prepared, effective, gain_db, on_done, on_amplitude),
            daemon=True,
            name="chatterbox-speak",
        )
        try:
            self._speak_thread.start()
        except Exception:
            self._synth_gate.release()
            raise

    def _speak_worker(
        self,
        text: str,
        speed: float,
        gain_db: float,
        on_done: Callable[[], None] | None,
        on_amplitude: Callable[[float], None] | None,
    ) -> None:
        gen_t0 = time.monotonic()
        try:
            try:
                result = self._warm_clip(text)
            finally:
                # Freed the moment this sentence is synthesised, so the
                # next one is prefetched while this one plays.
                self._synth_gate.release()
            self._clip_cache.discard(ClipCache.key(text))
            if result is None or self._stop_requested.is_set():
                return
            audio, rate = result
            generate_ms = (time.monotonic() - gen_t0) * 1000.0
            played_ms = self._play_clip(
                audio,
                rate,
                speed=speed,
                gain_factor=_gain_to_factor(gain_db),
                text=text,
                on_amplitude=on_amplitude,
            )
            log.debug(
                "%s play done: chars=%d generate_ms=%.0f played_ms=%.0f "
                "speed=%.2f",
                self._engine_key, len(text), generate_ms, played_ms, speed,
            )
        except Exception as exc:
            self._last_error = str(exc)
            log.error("%s playback failed: %r", self._engine_key, exc)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    def speak_silence_async(
        self, ms: int, on_done: Callable[[], None] | None = None
    ) -> None:
        """A deliberate pause, emitted as real silence.

        Shipped as PCM rather than slept through so the client's audio
        clock stays continuous -- a gap in the stream and a gap in the
        audio are different things to a scheduler.
        """
        duration = max(0, int(ms)) / 1000.0

        def _run() -> None:
            try:
                samples = int(self._sample_rate * duration)
                if samples > 0:
                    self._play_clip(
                        np.zeros(samples, dtype=np.float32),
                        self._sample_rate,
                    )
            finally:
                if on_done:
                    try:
                        on_done()
                    except Exception:
                        pass

        threading.Thread(daemon=True, target=_run, name="chatterbox-sil").start()

    # ── lifecycle ────────────────────────────────────────────────────

    def set_pcm_listener(
        self,
        listener: Callable[[int, int, bytes], None] | None,
        *,
        end_listener: Callable[[], None] | None = None,
        cancel_listener: Callable[[], None] | None = None,
    ) -> None:
        self._pcm_listener = listener
        self._clip_end_listener = end_listener
        self._clip_cancel_listener = cancel_listener

    def stop(self) -> None:
        self._stop_requested.set()
        self._clip_cache.clear()
        # See PocketTtsService.stop: the client is holding pre-roll this
        # engine has decided not to finish.
        self._fire_clip_cancel()

    def is_speaking(self) -> bool:
        thread = self._speak_thread
        return bool(thread and thread.is_alive())

    def release_model(self) -> bool:
        """Free the weights by ending the child process.

        Cheaper and more complete than the in-process equivalent: there
        is no torch allocator left holding freed blocks, and on CUDA the
        VRAM actually returns to the system -- which is the entire point
        when the reason for switching TTS off is to play a game.
        """
        self.stop()
        self._kill()
        self._loaded.clear()
        return True

    def load_model_now(self) -> None:
        if self._proc is not None:
            return
        self._loaded.clear()
        self._last_error = ""
        threading.Thread(
            target=self._load, daemon=True, name="chatterbox-load"
        ).start()

    def shutdown(self) -> None:
        self.release_model()
        try:
            for leftover in self._scratch.glob("*.wav"):
                leftover.unlink(missing_ok=True)
            self._scratch.rmdir()
        except Exception:
            log.debug("scratch cleanup failed", exc_info=True)


def _gain_to_factor(gain_db: float) -> float:
    """dB to a linear multiplier, clamped to the same range as pocket-tts."""
    clamped = max(-12.0, min(6.0, float(gain_db or 0.0)))
    return float(10.0 ** (clamped / 20.0))


