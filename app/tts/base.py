"""TtsEngine protocol -- the contract every TTS backend must satisfy."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class TtsEngine(Protocol):
    """Minimal interface that SessionController relies on for any TTS backend."""

    def get_status(self) -> tuple[str, str]:
        """Return (status_key, human_message). status_key is one of 'ready', 'error', 'disabled'."""
        ...

    def warmup_sync(self) -> bool:
        """Block until the engine is ready. Return True on success."""
        ...

    def warmup_async(self) -> None:
        """Non-blocking best-effort warmup (e.g. wait for model load thread)."""
        ...

    def stop(self) -> None:
        """Request immediate stop of any in-progress playback."""
        ...

    def set_output_device(self, device_index: int | None) -> None:
        ...

    def speak_async(
        self,
        text: str,
        reaction: str | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Start speaking *text* in a background thread. Call *on_done* when finished."""
        ...

    def list_voices(self) -> list[str]:
        """Return voice identifiers available for this engine."""
        ...

    def reaction_to_speed(self, reaction: str | None) -> float:
        """Map a reaction tag to a speed multiplier (1.0 = normal)."""
        ...

    # ------------------------------------------------------------------
    # Optional -- checked via getattr by SessionController for lookahead
    # ------------------------------------------------------------------
    # def generate_audio(self, text: str, speed: float = 1.0) -> tuple[np.ndarray, int] | None: ...
