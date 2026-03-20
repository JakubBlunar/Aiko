"""Pocket TTS backend -- CPU-only, 100M params, voice cloning support."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading

from app.core.settings import TtsSettings

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]

try:
    from pocket_tts import TTSModel, export_model_state as _export_model_state
except ImportError:
    TTSModel = None  # type: ignore[assignment,misc]
    _export_model_state = None

_BUILTIN_VOICES = ["alba", "marius", "javert", "jean", "fantine", "cosette", "eponine", "azelma"]

_REACTION_SPEED: dict[str, float] = {
    "excited": 1.1,
    "enthusiastic": 1.08,
    "cheerful": 1.08,
    "angry": 1.05,
    "surprised": 1.05,
    "friendly": 1.02,
    "neutral": 1.0,
    "calm": 0.95,
    "serious": 0.95,
    "sad": 0.92,
    "gentle": 0.92,
}


class PocketTtsService:
    """TTS using Kyutai Pocket TTS. Runs on CPU, supports voice cloning."""

    def __init__(self, settings: TtsSettings, output_device: int | None = None) -> None:
        self._settings = settings
        self._output_device = output_device
        self._lock = threading.Lock()
        self._model: TTSModel | None = None
        self._voice_state: dict | None = None
        self._last_error: str | None = None
        self._stop_requested = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._loaded = threading.Event()
        self._audio_cache: dict[str, tuple] = {}
        self._cache_lock = threading.Lock()

        if TTSModel is not None and np is not None and sd is not None:
            threading.Thread(target=self._load_model, daemon=True, name="pocket-tts-load").start()
        else:
            parts = []
            if TTSModel is None:
                parts.append("pocket-tts")
            if np is None:
                parts.append("numpy")
            if sd is None:
                parts.append("sounddevice")
            self._last_error = f"Missing: {', '.join(parts)}. pip install {' '.join(parts)}"
            self._loaded.set()

    def _load_model(self) -> None:
        try:
            temp = getattr(self._settings, "pocket_tts_temp", 0.7) or 0.7
            model = TTSModel.load_model(temp=float(temp))

            voice_id = getattr(self._settings, "pocket_tts_voice", "alba") or "alba"
            voice_state = self._resolve_voice(model, voice_id)

            with self._lock:
                self._model = model
                self._voice_state = voice_state
            self._last_error = None
        except Exception as exc:
            self._last_error = f"Pocket TTS load failed: {exc}"
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
            return True
        except Exception:
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

    def stop(self) -> None:
        self._stop_requested.set()
        with self._cache_lock:
            self._audio_cache.clear()
        try:
            if sd is not None:
                sd.stop()
        except Exception:
            pass

    def set_output_device(self, device_index: int | None) -> None:
        self._output_device = device_index

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

    def speak_async(
        self,
        text: str,
        reaction: str | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if not self._settings.enabled or not (text or "").strip():
            return
        self._stop_requested.clear()
        speed = self.reaction_to_speed(reaction)
        self._speech_thread = threading.Thread(
            target=self._speak_worker,
            args=(text.strip(), on_done, speed),
            daemon=True,
        )
        self._speech_thread.start()

    def _cache_key(self, text: str, speed: float) -> str:
        return f"{text}||{speed:.3f}"

    def generate_audio(self, text: str, speed: float = 1.0) -> tuple | None:
        """Generate audio, returning (numpy_array, sample_rate) or None."""
        key = self._cache_key(text, speed)
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

        audio_tensor = model.generate_audio(voice_state, text, copy_state=True)
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
    ) -> None:
        try:
            if sd is None:
                return
            result = self.generate_audio(text, speed)
            if result is None or self._stop_requested.is_set():
                return
            audio_data, sample_rate = result
            with self._cache_lock:
                self._audio_cache.pop(self._cache_key(text, speed), None)

            silence = np.zeros(int(sample_rate * 0.15), dtype=np.float32)
            audio_data = np.concatenate([audio_data, silence])
            sd.play(audio_data.reshape(-1, 1), sample_rate, device=self._output_device)
            sd.wait()
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
