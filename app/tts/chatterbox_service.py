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
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from app.core.infra.settings import TtsSettings
from app.tts.pcm_playback import PcmPlaybackMixin
from app.tts.reactions import reaction_to_speed as _reaction_to_speed

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
_SCRATCH = ("studio/", "datasets/", "audition/", "speed_ab/", "reference/parts/")

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
        self._loaded = threading.Event()
        self._stop_requested = threading.Event()
        self._speak_thread: threading.Thread | None = None

        # Playback contract (see PcmPlaybackMixin).
        self._pcm_listener: Callable[[int, int, bytes], None] | None = None
        self._clip_end_listener: Callable[[], None] | None = None
        self._pitch_preserving_speed = bool(
            getattr(settings, "pitch_preserving_speed", True)
        )

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
        log.info("%s now cloning from %s", self._engine_key, name)
        return True

    def reaction_to_speed(self, reaction: str | None) -> float:
        # Shared with pocket-tts so a provider swap does not also change
        # her pacing -- the mapping is a property of her character, not of
        # whichever model is rendering it.
        return _reaction_to_speed(reaction)

    # ── synthesis ────────────────────────────────────────────────────

    def generate_audio(
        self, text: str, speed: float = 1.0, *, temp: float | None = None
    ) -> tuple[np.ndarray, int] | None:
        """Synthesise without playing, for TtsQueue's lookahead.

        ``speed`` is deliberately ignored here: Chatterbox has no native
        rate control, and the shared playback path applies the stretch at
        emission time. Accepting the argument and doing nothing with it
        keeps the engine interchangeable with pocket-tts.
        """
        prepared = (text or "").strip()
        if not prepared or not self._loaded.wait(timeout=LOAD_TIMEOUT_S):
            return None
        out = self._scratch / f"synth-{time.monotonic_ns()}.wav"
        try:
            self._request(
                {
                    "op": "synth",
                    "text": prepared,
                    "voice": 0,
                    "out": str(out),
                },
                SYNTH_TIMEOUT_S,
            )
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
        effective = (
            float(speed)
            if speed is not None
            else self.reaction_to_speed(reaction)
        )
        self._stop_requested.clear()
        self._speak_thread = threading.Thread(
            target=self._speak_worker,
            args=(prepared, effective, gain_db, on_done, on_amplitude),
            daemon=True,
            name="chatterbox-speak",
        )
        self._speak_thread.start()

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
            result = self.generate_audio(text)
            if result is None or self._stop_requested.is_set():
                return
            audio, rate = result
            generate_ms = (time.monotonic() - gen_t0) * 1000.0
            played_ms = self._play_clip(
                audio,
                rate,
                speed=speed,
                gain_factor=_gain_to_factor(gain_db),
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
    ) -> None:
        self._pcm_listener = listener
        self._clip_end_listener = end_listener

    def stop(self) -> None:
        self._stop_requested.set()

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


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Int16 WAV from the sidecar to float32 in [-1, 1].

    A file round trip per utterance rather than a binary framing protocol
    on stdout: a few hundred KB on an NVMe costs about a millisecond
    against a synthesis measured in hundreds, and it reuses the sidecar's
    existing contract exactly as the bench does.
    """
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, int(rate)
