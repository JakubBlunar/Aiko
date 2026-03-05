from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
import threading
import wave
import winsound

from app.core.settings import TtsSettings

try:
    from piper import PiperVoice as _PiperVoice  # type: ignore[import]
    from piper.config import SynthesisConfig as _SynthesisConfig  # type: ignore[import]
except Exception:
    _PiperVoice = None
    _SynthesisConfig = None


class PiperTtsService:
    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        # Persistent in-process voice — loaded once, reused for every synthesis.
        self._voice: object | None = None
        self._voice_loaded = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_wav: Path | None = None
        self._length_scale = 1.0
        self._length_scale_supported: bool | None = None
        self._last_error: str | None = None
        self._queue: Queue[str] = Queue()
        self._queue_speaking = False
        self._queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self._queue_thread.start()
        if _PiperVoice is not None:
            threading.Thread(target=self._load_voice, daemon=True, name="piper-load").start()
        else:
            self._voice_loaded.set()

    def _load_voice(self) -> None:
        try:
            voice = _PiperVoice.load(self._settings.voice)
            with self._lock:
                self._voice = voice
            self._last_error = None
        except Exception as exc:
            self._last_error = f"Piper voice load failed: {exc}"
        finally:
            self._voice_loaded.set()

    def set_length_scale(self, value: float) -> None:
        self._length_scale = max(0.65, min(float(value), 1.35))

    def get_status(self) -> tuple[str, str]:
        if not self._settings.enabled:
            return "disabled", "TTS disabled"
        if self._last_error:
            return "error", self._last_error
        return "ready", "Piper runtime ready"

    def warmup_async(self) -> None:
        return

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True

        # When using the in-process API, warmup = wait for voice load to finish.
        if _PiperVoice is not None:
            loaded = self._voice_loaded.wait(timeout=60.0)
            if not loaded:
                self._last_error = "Piper voice load timed out"
                return False
            with self._lock:
                voice_ok = self._voice is not None
            if not voice_ok:
                return False
            self._last_error = None
            return True

        # Subprocess fallback: do a short synthesis to prove the binary works.
        warmup_wav = Path(tempfile.mkstemp(suffix=".wav", prefix="assistant_tts_warmup_")[1])
        try:
            ok = self._synthesize_to_wav(text="warmup", output_file=warmup_wav, timeout=45)
            if not ok:
                self._last_error = self._last_error or "Piper warmup failed"
                return False
            self._last_error = None
            return True
        except Exception:
            self._last_error = "Piper warmup failed"
            return False
        finally:
            try:
                warmup_wav.unlink(missing_ok=True)
            except Exception:
                pass

    def speak_async(self, text: str) -> None:
        if not self._settings.enabled:
            return
        if not text.strip():
            return

        self.stop()
        self._stop_event.clear()
        self._speech_thread = threading.Thread(target=self._speak_worker, args=(text,), daemon=True)
        self._speech_thread.start()

    def enqueue_async(self, text: str) -> bool:
        if not self._settings.enabled:
            return False
        queued = str(text or "").strip()
        if not queued:
            return False
        self._stop_event.clear()
        self._queue.put(queued)
        return True

    def has_pending_audio(self) -> bool:
        if not self._queue.empty():
            return True
        with self._lock:
            proc_running = self._active_process is not None
            queue_speaking = self._queue_speaking
        return bool(proc_running or queue_speaking)

    def stop(self) -> None:
        self._stop_event.set()
        self._clear_queue()

        with self._lock:
            proc = self._active_process
            self._active_process = None

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break

    def _queue_worker(self) -> None:
        while True:
            text = self._queue.get()
            if self._stop_event.is_set():
                continue
            with self._lock:
                self._queue_speaking = True
            try:
                self._speak_blocking(text)
            finally:
                with self._lock:
                    self._queue_speaking = False

    def _speak_blocking(self, text: str) -> None:
        wav_path = Path(tempfile.mkstemp(suffix=".wav", prefix="assistant_tts_stream_")[1])
        try:
            with self._lock:
                self._active_wav = wav_path

            ok = self._synthesize_to_wav(text=text, output_file=wav_path, timeout=30)
            if not ok:
                self._last_error = self._last_error or "Piper synthesis failed"
                return

            self._last_error = None
            if self._stop_event.is_set() or not self._is_valid_wav(wav_path):
                return

            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_SYNC)
        except Exception:
            self._last_error = "Piper playback failed"
            return
        finally:
            with self._lock:
                self._active_wav = None
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _speak_worker(self, text: str) -> None:
        wav_path = Path(tempfile.mkstemp(suffix=".wav", prefix="assistant_tts_")[1])

        try:
            with self._lock:
                self._active_wav = wav_path

            ok = self._synthesize_to_wav(text=text, output_file=wav_path, timeout=30)
            if not ok:
                self._last_error = self._last_error or "Piper synthesis failed"
                return

            self._last_error = None
            if self._stop_event.is_set() or not self._is_valid_wav(wav_path):
                return

            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            self._last_error = "Piper playback failed"
            return
        finally:
            with self._lock:
                self._active_process = None
                self._active_wav = None

    def _synthesize_to_wav(self, *, text: str, output_file: Path, timeout: int) -> bool:
        # Prefer in-process synthesis with the persistent loaded voice.
        with self._lock:
            voice = self._voice
        if voice is not None:
            return self._synthesize_with_api(voice=voice, text=text, output_file=output_file)

        # Subprocess fallback (used when piper module is unavailable).
        use_length_scale = self._length_scale_supported is not False
        proc = self._spawn_piper_process(output_file=output_file, use_length_scale=use_length_scale)
        if proc is None:
            self._last_error = "Piper executable not found"
            return False

        ok, stderr_text = self._run_process(proc=proc, text=text, timeout=timeout)
        if ok and self._is_valid_wav(output_file):
            if use_length_scale:
                self._length_scale_supported = True
            return True

        if use_length_scale and stderr_text and "length_scale" in stderr_text.lower():
            self._length_scale_supported = False
            retry = self._spawn_piper_process(output_file=output_file, use_length_scale=False)
            if retry is not None:
                retry_ok, retry_err = self._run_process(proc=retry, text=text, timeout=timeout)
                if retry_ok and self._is_valid_wav(output_file):
                    return True
                stderr_text = retry_err or stderr_text

        self._last_error = (stderr_text or "Piper synthesis failed").strip()
        return False

    def _synthesize_with_api(self, *, voice: object, text: str, output_file: Path) -> bool:
        try:
            syn_config = _SynthesisConfig(length_scale=self._length_scale)
            chunks = list(voice.synthesize(text, syn_config=syn_config))
            if not chunks:
                self._last_error = "Piper returned no audio"
                return False
            first = chunks[0]
            with wave.open(str(output_file), "wb") as wf:
                wf.setnchannels(first.sample_channels)
                wf.setsampwidth(first.sample_width)
                wf.setframerate(first.sample_rate)
                for chunk in chunks:
                    wf.writeframes(chunk.audio_int16_bytes)
            self._last_error = None
            return self._is_valid_wav(output_file)
        except Exception as exc:
            self._last_error = f"Piper in-process synthesis failed: {exc}"
            return False

    def _run_process(self, *, proc: subprocess.Popen[str], text: str, timeout: int) -> tuple[bool, str]:
        with self._lock:
            self._active_process = proc

        stderr_text = ""
        try:
            stdout_text, stderr_text = proc.communicate(input=text, timeout=timeout)
            _ = stdout_text
            return proc.returncode == 0, stderr_text or ""
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            return False, "Piper synthesis timed out"
        except Exception as exc:
            return False, str(exc)
        finally:
            with self._lock:
                self._active_process = None

    @staticmethod
    def _is_valid_wav(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 44
        except Exception:
            return False

    def _spawn_piper_process(
        self,
        *,
        output_file: Path,
        use_length_scale: bool,
    ) -> subprocess.Popen[str] | None:
        length_scale = max(0.65, min(float(self._length_scale), 1.35))
        primary_cmd = ["piper", "--model", self._settings.voice]
        fallback_cmd = [sys.executable, "-m", "piper", "--model", self._settings.voice]
        if use_length_scale:
            primary_cmd.extend(["--length_scale", f"{length_scale:.2f}"])
            fallback_cmd.extend(["--length_scale", f"{length_scale:.2f}"])
        primary_cmd.extend(["--output_file", str(output_file)])
        fallback_cmd.extend(["--output_file", str(output_file)])

        try:
            return subprocess.Popen(
                primary_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            try:
                return subprocess.Popen(
                    fallback_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except Exception:
                return None
