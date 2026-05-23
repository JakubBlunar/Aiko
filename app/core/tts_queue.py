"""Thread-safe sequential TTS queue.

Plays text chunks one at a time on the chosen TTS backend. Pre-generates the
next chunk's audio in a daemon thread (lookahead) so playback transitions are
seamless on backends that take >1s to synthesise.

Decoupled from PySide6 / Qt; the only inputs are the TTS engine (the
``TtsEngine``-like object from ``app/tts``) and the optional ``state_listener``
callback. Avatar wiring can subscribe to the listener later without bringing
the queue back into the UI layer.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from app.core.services.response_text_service import prepare_tts_text


StateListener = Callable[[str, dict[str, Any]], None]
"""Signature: ``listener(event, payload)`` where event is "start" | "end"."""


log = logging.getLogger("app.tts_queue")


class TtsQueue:
    """Sequential TTS playback queue with one-chunk lookahead.

    The TTS backend is expected to expose:
      - ``speak_async(text, *, reaction, on_done)`` — non-blocking playback
      - ``stop()`` — abort current playback
      - ``generate_audio(text, speed)`` *(optional)* — synthesise PCM ahead of
        time so the next chunk is ready when the current one ends
      - ``reaction_to_speed(reaction)`` *(optional)* — map reaction -> speed
    """

    def __init__(
        self,
        tts_engine: Any,
        *,
        enabled: bool = True,
        state_listener: StateListener | None = None,
    ) -> None:
        self._tts = tts_engine
        self._enabled = bool(enabled)
        self._listener = state_listener
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str | None]] = []
        self._playing = False

    # ── public API ────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not enabled:
            self.stop()

    def enqueue(self, text: str, reaction: str | None = None) -> None:
        """Queue ``text`` for spoken playback (sanitised internally)."""
        if not self._enabled:
            return
        cleaned = prepare_tts_text((text or "").strip())
        if not cleaned:
            return
        with self._lock:
            self._pending.append((cleaned, reaction))
            if self._playing:
                return
            self._playing = True
            chunk = self._pending.pop(0)
        self._start_chunk(chunk[0], chunk[1])

    def stop(self) -> None:
        """Drop pending chunks and abort current playback."""
        with self._lock:
            self._pending.clear()
            was_playing = self._playing
            self._playing = False
        try:
            self._tts.stop()
        except Exception:
            log.debug("tts engine stop() failed", exc_info=True)
        if was_playing:
            self._notify("end", {})

    def is_active(self) -> bool:
        with self._lock:
            return self._playing or bool(self._pending)

    # ── internal ──────────────────────────────────────────────────────────

    def _on_chunk_done(self) -> None:
        next_chunk: tuple[str, str | None] | None = None
        with self._lock:
            self._playing = False
            if self._pending:
                next_chunk = self._pending.pop(0)
                self._playing = True
        if next_chunk is not None:
            self._start_chunk(next_chunk[0], next_chunk[1])
        else:
            self._notify("end", {})

    def _start_chunk(self, text: str, reaction: str | None) -> None:
        # Spawn lookahead synth for the *next* chunk if the backend supports
        # offline generation — keeps latency low across multi-sentence
        # responses without coupling to the avatar's envelope.
        generate = getattr(self._tts, "generate_audio", None)
        with self._lock:
            peek = self._pending[0] if self._pending else None
        if peek is not None and callable(generate):
            r2s = getattr(self._tts, "reaction_to_speed", None)
            speed = r2s(peek[1]) if callable(r2s) else 1.0
            threading.Thread(
                target=generate,
                args=(peek[0], speed),
                daemon=True,
                name="tts-lookahead",
            ).start()

        self._notify("start", {"text": text, "reaction": reaction or ""})
        try:
            self._tts.speak_async(text, reaction=reaction, on_done=self._on_chunk_done)
        except Exception as exc:
            log.warning("tts speak_async failed: %s", exc)
            with self._lock:
                self._playing = False
            self._notify("end", {})

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        if self._listener is None:
            return
        try:
            self._listener(event, payload)
        except Exception:
            log.debug("tts state listener raised", exc_info=True)
