"""P28 -- the engine used when ``tts.enabled`` is false.

The point of this class is what it *doesn't* do: importing
``app.tts.pocket_tts_service`` pulls in ``pocket_tts`` and with it the
PyTorch CPU runtime (~0.6-1 GB resident), and its constructor
immediately starts a daemon thread that loads the ~100M-param voice
model. A TTS-off install used to pay both, because construction never
consulted ``settings.tts.enabled``.

Returning this instead keeps every call site unchanged --
:class:`~app.core.voice.tts_queue.TtsQueue` and the settings/voice
plumbing already treat the engine as duck-typed and mostly reach it
through ``getattr(..., None)``. Turning TTS on at runtime swaps in the
real engine via ``SessionController.set_tts_enabled``.

Deliberately imports nothing beyond the stdlib.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


log = logging.getLogger("app.tts")


class NullTtsService:
    """Silent stand-in for a real TTS engine.

    Every method is a no-op that returns the "nothing to say" value for
    its slot, so callers that don't check ``tts.enabled`` first still
    behave. ``get_status`` reports ``disabled`` rather than ``error`` --
    the difference matters in the UI: one is a configuration, the other
    is a fault.
    """

    #: Lets ``get_memory_breakdown`` and the tests tell the two engines
    #: apart without importing the heavy module to compare types.
    is_null_engine = True

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._model = None
        self._pcm_listener = None
        self._clip_end_listener = None
        self._clip_cancel_listener = None

    # ── playback wiring ──────────────────────────────────────────────

    def set_pcm_listener(
        self, listener, *, end_listener=None, cancel_listener=None,
    ) -> None:
        self._pcm_listener = listener
        if end_listener is not None:
            self._clip_end_listener = end_listener
        if cancel_listener is not None:
            self._clip_cancel_listener = cancel_listener

    # ── TtsEngine protocol ───────────────────────────────────────────

    def get_status(self) -> tuple[str, str]:
        return "disabled", "TTS disabled (engine not loaded)"

    def model_status(self) -> tuple[str, str]:
        return self.get_status()

    def warmup_sync(self) -> bool:
        return True

    def warmup_async(self) -> None:
        return None

    def speak_async(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def stop(self) -> None:
        # Still fire both hooks: a client that somehow has buffered
        # audio should get told to drop it either way.
        for hook in (self._clip_cancel_listener, self._clip_end_listener):
            if hook is not None:
                try:
                    hook()
                except Exception:
                    pass

    def is_speaking(self) -> bool:
        return False

    def list_voices(self) -> list[str]:
        return []

    def set_voice(self, _voice_id: str) -> bool:
        return False

    def get_model(self) -> None:
        return None

    def reaction_to_speed(self, _reaction: str | None) -> float:
        return 1.0

    def set_length_scale(self, _scale: float) -> None:
        return None

    def set_runtime_temp_enabled(self, _enabled: bool) -> None:
        return None

    def set_runtime_speed_enabled(self, _enabled: bool) -> None:
        return None

    def release_model(self) -> bool:
        return False

    @staticmethod
    def export_voice(_model_state: dict, _dest: str | Path) -> None:
        return None
