from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import winsound

from app.core.settings import TtsSettings


class PiperTtsService:
    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._speech_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_wav: Path | None = None

    def get_status(self) -> tuple[str, str]:
        if not self._settings.enabled:
            return "disabled", "TTS disabled"
        return "ready", "Piper runtime ready"

    def warmup_async(self) -> None:
        return

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True

        warmup_wav = Path(tempfile.mkstemp(suffix=".wav", prefix="assistant_tts_warmup_")[1])
        try:
            proc = self._spawn_piper_process(output_file=warmup_wav)
            if proc is None:
                return False

            try:
                if proc.stdin is not None:
                    proc.stdin.write("warmup")
                    proc.stdin.close()
                proc.wait(timeout=45)
            finally:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

            if proc.returncode not in (0, None):
                return False

            deadline = time.time() + 2.0
            while time.time() < deadline:
                if warmup_wav.exists() and warmup_wav.stat().st_size > 44:
                    return True
                time.sleep(0.05)
            return warmup_wav.exists()
        except Exception:
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

    def stop(self) -> None:
        self._stop_event.set()

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

    def _speak_worker(self, text: str) -> None:
        wav_path = Path(tempfile.mkstemp(suffix=".wav", prefix="assistant_tts_")[1])

        try:
            with self._lock:
                self._active_wav = wav_path

            proc = self._spawn_piper_process(output_file=wav_path)
            if proc is None:
                return

            with self._lock:
                self._active_process = proc

            if proc.stdin is not None:
                proc.stdin.write(text)
                proc.stdin.close()

            proc.wait(timeout=30)

            with self._lock:
                self._active_process = None

            if self._stop_event.is_set() or not wav_path.exists():
                return

            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            return
        finally:
            with self._lock:
                self._active_process = None
                self._active_wav = None

    def _spawn_piper_process(self, *, output_file: Path) -> subprocess.Popen[str] | None:
        primary_cmd = [
            "piper",
            "--model",
            self._settings.voice,
            "--output_file",
            str(output_file),
        ]
        fallback_cmd = [
            sys.executable,
            "-m",
            "piper",
            "--model",
            self._settings.voice,
            "--output_file",
            str(output_file),
        ]

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
