"""PyKokoro TTS backend -- same Kokoro model with pause control and voice blending."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import logging
import threading

from app.core.settings import TtsSettings
from app.tts.kokoro_service import _REACTION_SPEED

log = logging.getLogger(__name__)

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]

_pykokoro_available = False
try:
    from pykokoro import KokoroPipeline, PipelineConfig, GenerationConfig  # noqa: F401
    _pykokoro_available = True
except ImportError:
    KokoroPipeline = None  # type: ignore[assignment,misc]
    PipelineConfig = None  # type: ignore[assignment,misc]
    GenerationConfig = None  # type: ignore[assignment,misc]


def _resolve_path(path: str | None, base: Path, default: str) -> Path:
    if not path:
        return base / default
    p = Path(path)
    return p if p.is_absolute() else (base / p)


class PyKokoroTtsService:
    """TTS using PyKokoro pipeline (ONNX Kokoro + espeak-ng G2P, pause markers, voice blending)."""

    def __init__(self, settings: TtsSettings, output_device: int | None = None) -> None:
        self._settings = settings
        self._output_device = output_device
        self._lock = threading.Lock()
        self._pipeline: object | None = None
        self._last_error: str | None = None
        self._stop_requested = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._loaded = threading.Event()

        if not _pykokoro_available or np is None or sd is None:
            missing: list[str] = []
            if not _pykokoro_available:
                missing.append("pykokoro")
            if np is None:
                missing.append("numpy")
            if sd is None:
                missing.append("sounddevice")
            self._last_error = f"Missing dependencies: {', '.join(missing)}. pip install {' '.join(missing)}"
            self._loaded.set()
        else:
            threading.Thread(target=self._load_pipeline, daemon=True, name="pykokoro-load").start()

    def _load_pipeline(self) -> None:
        try:
            base = Path(__file__).resolve().parents[2]
            model_path = _resolve_path(
                getattr(self._settings, "kokoro_model_path", None),
                base, "kokoro-v1.0.onnx",
            )
            voices_path = _resolve_path(
                getattr(self._settings, "kokoro_voices_path", None),
                base, "voices-v1.0.bin",
            )

            voice = (self._settings.voice or "af_heart").strip() or "af_heart"
            pause_mode = getattr(self._settings, "pykokoro_pause_mode", "auto") or "auto"

            config = PipelineConfig(
                voice=voice,
                model_path=str(model_path) if model_path.exists() else None,
                voices_path=str(voices_path) if voices_path.exists() else None,
            )
            pipeline = KokoroPipeline(config)

            with self._lock:
                self._pipeline = pipeline
            self._last_error = None
        except Exception as exc:
            self._last_error = f"PyKokoro load failed: {exc}"
            log.warning("PyKokoro load failed: %s", exc)
        finally:
            self._loaded.set()

    # -- Protocol methods --------------------------------------------------

    def get_status(self) -> tuple[str, str]:
        if not self._settings.enabled:
            return "disabled", "TTS disabled"
        if self._last_error:
            return "error", self._last_error
        self._loaded.wait(timeout=0.5)
        with self._lock:
            if self._pipeline is None:
                return "error", self._last_error or "Pipeline not loaded"
        return "ready", "PyKokoro TTS ready"

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True
        if not self._loaded.wait(timeout=60.0):
            self._last_error = "PyKokoro load timed out"
            return False
        with self._lock:
            return self._pipeline is not None

    def warmup_async(self) -> None:
        self._loaded.wait(timeout=30.0)

    def stop(self) -> None:
        self._stop_requested.set()

    def set_output_device(self, device_index: int | None) -> None:
        self._output_device = device_index

    def list_voices(self) -> list[str]:
        return [
            "af_heart", "af_bella", "af_nicole", "af_sarah",
            "am_adam", "am_michael",
            "bf_emma", "bf_isabella",
            "jf_nezumi", "jf_alpha",
        ]

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

    # -- Audio generation --------------------------------------------------

    def generate_audio(self, text: str, speed: float = 1.0) -> tuple | None:
        """Run PyKokoro pipeline, returning (audio_array, sample_rate) or None."""
        if not self._loaded.wait(timeout=30.0):
            return None
        with self._lock:
            pipeline = self._pipeline
        if pipeline is None or np is None:
            return None
        try:
            voice = (self._settings.voice or "af_heart").strip() or "af_heart"
            gen_config = GenerationConfig(
                speed=min(2.0, max(0.5, float(speed))),
            )
            result = pipeline.run(text, voice=voice, generation=gen_config)
            audio = np.asarray(result.audio, dtype=np.float32)
            if audio.size == 0:
                return None
            return audio, result.sample_rate
        except Exception as exc:
            log.warning("PyKokoro generate failed: %s", exc)
            return None

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
            silence_samples = int(sample_rate * 0.15)
            silence = np.zeros(silence_samples, dtype=np.float32)
            audio_data = np.concatenate([audio_data, silence])
            sd.play(
                audio_data.reshape(-1, 1),
                sample_rate,
                device=self._output_device,
            )
            sd.wait()
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    def has_pending_audio(self) -> bool:
        return self._speech_thread is not None and self._speech_thread.is_alive()
