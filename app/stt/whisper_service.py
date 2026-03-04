from __future__ import annotations


class WhisperService:
    def __init__(self, model_name: str = "base") -> None:
        self._model = None
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(model_name)
        except Exception:
            self._model = None

    @property
    def is_available(self) -> bool:
        return self._model is not None

    def transcribe(self, audio_path: str) -> str | None:
        if self._model is None:
            return None
        segments, _ = self._model.transcribe(audio_path)
        text = " ".join(segment.text.strip() for segment in segments if segment.text)
        return text.strip() or None
