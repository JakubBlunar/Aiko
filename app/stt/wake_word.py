"""Wake word detection using openwakeword (optional dependency)."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

try:
    import openwakeword
    from openwakeword.model import Model as OwwModel

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    OwwModel = None  # type: ignore[assignment,misc]

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class WakeWordDetector:
    """Lightweight wake word detector wrapping openwakeword.

    Listens on audio chunks and fires *on_detected* when the wake phrase is heard.
    """

    is_available: bool = _AVAILABLE

    def __init__(
        self,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        sample_rate: int = 16000,
    ) -> None:
        self._model_name = model_name
        self._threshold = threshold
        self._sample_rate = sample_rate
        self._model: Any = None
        self._lock = threading.Lock()

        if _AVAILABLE:
            try:
                openwakeword.utils.download_models()
                self._model = OwwModel(
                    wakeword_models=[model_name],
                    inference_framework="onnx",
                )
                log.info("Wake word model loaded: %s (threshold=%.2f)", model_name, threshold)
            except Exception:
                log.warning("Failed to load wake word model '%s'", model_name, exc_info=True)
                self._model = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def process_audio(self, audio_chunk: bytes | Any) -> bool:
        """Feed a 16-bit PCM chunk and return True if the wake word was detected."""
        if self._model is None or np is None:
            return False

        if isinstance(audio_chunk, bytes):
            arr = np.frombuffer(audio_chunk, dtype=np.int16)
        else:
            arr = np.asarray(audio_chunk, dtype=np.int16).flatten()

        with self._lock:
            prediction = self._model.predict(arr)

        for name, score in prediction.items():
            if score >= self._threshold:
                log.info("Wake word '%s' detected (score=%.3f)", name, score)
                self._model.reset()
                return True
        return False

    def reset(self) -> None:
        if self._model is not None:
            with self._lock:
                self._model.reset()

    def wait_for_wake_word(
        self,
        audio_source: Callable[[], bytes | None],
        stop_requested: Callable[[], bool],
        *,
        chunk_ms: int = 80,
    ) -> bool:
        """Block until the wake word is detected or stop is requested.

        *audio_source* should return a PCM16 chunk each call (or None to retry).
        Returns True if wake word detected, False if stopped.
        """
        import time

        self.reset()
        while not stop_requested():
            chunk = audio_source()
            if chunk is None:
                time.sleep(chunk_ms / 1000.0)
                continue
            if self.process_audio(chunk):
                return True
        return False
