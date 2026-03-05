from __future__ import annotations


class WhisperService:
    _DEFAULT_INITIAL_PROMPT = (
        "The speaker may have a non-native English accent. "
        "Use clear punctuation and preserve meaning."
    )

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

    def transcribe(
        self,
        audio_path: str,
        *,
        vad_filter: bool = True,
        initial_prompt: str | None = _DEFAULT_INITIAL_PROMPT,
    ) -> str | None:
        if self._model is None:
            return None
        kwargs: dict[str, object] = {
            "beam_size": 5,
            "best_of": 5,
            "temperature": 0.0,
            "vad_filter": bool(vad_filter),
            "condition_on_previous_text": False,
            "word_timestamps": False,
            "task": "transcribe",
        }
        if initial_prompt:
            kwargs["initial_prompt"] = str(initial_prompt)
        if self._language:
            kwargs["language"] = self._language
        segments, _ = self._model.transcribe(audio_path, **kwargs)
        text = " ".join(segment.text.strip() for segment in segments if segment.text)
        text = text.strip()
        if not text:
            return None

        # Guard against occasional prompt-echo outputs when speech is not detected.
        if initial_prompt:
            normalized_text = " ".join(text.lower().split())
            normalized_prompt = " ".join(str(initial_prompt).lower().split())
            if normalized_text == normalized_prompt or normalized_text in normalized_prompt:
                return None

        return text
