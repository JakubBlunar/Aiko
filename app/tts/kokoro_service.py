"""Kokoro-82M ONNX TTS with misaki G2P."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading

from app.core.settings import TtsSettings

def _check_tts_deps() -> tuple[bool, str]:
    """Return (ok, missing_message). If not ok, message lists what to install or the real error."""
    missing: list[str] = []
    errors: list[str] = []
    try:
        import numpy as np  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import sounddevice as sd  # noqa: F401
    except ImportError:
        missing.append("sounddevice")
    try:
        from kokoro_onnx import Kokoro  # noqa: F401
    except Exception as e:
        missing.append("kokoro-onnx")
        errors.append(f"kokoro-onnx: {e}")
    try:
        from misaki import en, espeak  # noqa: F401
    except Exception as e:
        missing.append("misaki")
        errors.append(f"misaki: {e}")
    if missing:
        msg = "Missing TTS dependencies: " + ", ".join(missing)
        if errors:
            msg += ". " + " | ".join(errors)
        else:
            msg += ". Install with: pip install " + " ".join(missing)
        return False, msg
    return True, ""


try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None
    sd = None

try:
    from kokoro_onnx import Kokoro
except Exception:
    Kokoro = None

_misaki_error: Exception | None = None
try:
    from misaki import en, espeak
except Exception as e:
    _misaki_error = e
    en = None
    espeak = None


def _resolve_path(path: str | None, base: Path) -> Path:
    if not path:
        return base / "kokoro-v1.0.onnx"
    p = Path(path)
    return p if p.is_absolute() else (base / p)


class KokoroTtsService:
    """TTS using Kokoro ONNX + misaki G2P. Plays via sounddevice."""

    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._kokoro: object | None = None
        self._g2p: object | None = None
        self._last_error: str | None = None
        self._stop_requested = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._loaded = threading.Event()
        if Kokoro is not None and en is not None and espeak is not None and np is not None and sd is not None:
            threading.Thread(target=self._load_models, daemon=True, name="kokoro-load").start()
        else:
            _, msg = _check_tts_deps()
            self._last_error = msg or "Missing kokoro_onnx, misaki, sounddevice, or numpy"
            self._loaded.set()

    def _resolve_model_paths(self) -> tuple[Path, Path]:
        base = Path(__file__).resolve().parents[2]
        model_path = _resolve_path(
            getattr(self._settings, "kokoro_model_path", None) or "kokoro-v1.0.onnx",
            base,
        )
        voices_path = _resolve_path(
            getattr(self._settings, "kokoro_voices_path", None) or "voices-v1.0.bin",
            base,
        )
        return model_path, voices_path

    def _load_models(self) -> None:
        try:
            model_path, voices_path = self._resolve_model_paths()
            if not model_path.exists() or not voices_path.exists():
                self._last_error = f"Kokoro model files not found: {model_path} / {voices_path}"
                self._loaded.set()
                return
            kokoro = Kokoro(str(model_path), str(voices_path))
            fallback = espeak.EspeakFallback(british=False)
            g2p = en.G2P(trf=False, british=False, fallback=fallback)
            with self._lock:
                self._kokoro = kokoro
                self._g2p = g2p
            self._last_error = None
        except Exception as exc:
            self._last_error = f"Kokoro load failed: {exc}"
        finally:
            self._loaded.set()

    def get_status(self) -> tuple[str, str]:
        if not self._settings.enabled:
            return "disabled", "TTS disabled"
        if self._last_error:
            return "error", self._last_error or "Unknown error"
        self._loaded.wait(timeout=0.5)
        with self._lock:
            if self._kokoro is None or self._g2p is None:
                return "error", self._last_error or "Models not loaded"
        return "ready", "Kokoro TTS ready"

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True
        if not self._loaded.wait(timeout=60.0):
            self._last_error = "Kokoro load timed out"
            return False
        with self._lock:
            if self._kokoro is None or self._g2p is None:
                return False
        self._last_error = None
        return True

    def warmup_async(self) -> None:
        self._loaded.wait(timeout=30.0)

    def stop(self) -> None:
        self._stop_requested.set()

    def speak_async(
        self,
        text: str,
        reaction: str | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if not self._settings.enabled or not (text or "").strip():
            return
        self._stop_requested.clear()
        self._speech_thread = threading.Thread(
            target=self._speak_worker,
            args=(text.strip(), on_done),
            daemon=True,
        )
        self._speech_thread.start()

    def _speak_worker(self, text: str, on_done: Callable[[], None] | None = None) -> None:
        try:
            if not self._loaded.wait(timeout=30.0):
                return
            with self._lock:
                kokoro = self._kokoro
                g2p = self._g2p
            if kokoro is None or g2p is None or np is None or sd is None:
                return
            phonemes, _ = g2p(text)
            voice = (self._settings.voice or "af_heart").strip() or "af_heart"
            samples, sample_rate = kokoro.create(phonemes, voice, is_phonemes=True)
            if self._stop_requested.is_set():
                return
            audio_data = np.asarray(samples, dtype=np.float32)
            if audio_data.size == 0:
                return
            sd.play(audio_data.reshape(-1, 1), sample_rate)
            sd.wait()
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    def enqueue_async(self, text: str) -> bool:
        """Simple enqueue: just speak this chunk (no queue; replaces current)."""
        if not self._settings.enabled:
            return False
        if not (text or "").strip():
            return False
        self.speak_async(text)
        return True

    def has_pending_audio(self) -> bool:
        return (
            self._speech_thread is not None
            and self._speech_thread.is_alive()
        )
