from __future__ import annotations


class WhisperService:
    def __init__(self, model_name: str = "base", language: str | None = None) -> None:
        self._model = None
        self._language = (language or "").strip() or None
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
        kwargs: dict[str, object] = {
            "beam_size": 5,
            "best_of": 5,
            "temperature": 0.0,
            "vad_filter": True,
            "condition_on_previous_text": False,
            "word_timestamps": False,
            "task": "transcribe",
            "initial_prompt": "The speaker may have a non-native English accent. Use clear punctuation and preserve meaning.",
        }
        if self._language:
            kwargs["language"] = self._language
        segments, _ = self._model.transcribe(audio_path, **kwargs)
        text = " ".join(segment.text.strip() for segment in segments if segment.text)
        return text.strip() or None
