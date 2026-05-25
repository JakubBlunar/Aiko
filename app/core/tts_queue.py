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
import time
from typing import Any, Callable

from app.core.session_text_utils import prepare_tts_text


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
        amplitude_listener: Callable[[float], None] | None = None,
        earcon_player: Any | None = None,
    ) -> None:
        self._tts = tts_engine
        self._enabled = bool(enabled)
        self._listener = state_listener
        self._amplitude_listener = amplitude_listener
        # Phase 1c: optional player for stage-direction earcons spliced
        # into the spoken stream. When ``None`` the queue silently
        # drops earcon entries — handy for tests and TTS-disabled mode.
        self._earcon_player = earcon_player
        self._lock = threading.Lock()
        # Each pending entry is a tuple whose first element is the
        # *kind* of chunk ("text" | "earcon"), enabling a single
        # serialised pipeline that interleaves both. For "text" the
        # rest of the tuple is (content, reaction, speed). For "earcon"
        # only the kind name is meaningful (content holds the earcon
        # name, reaction/speed are ignored).
        self._pending: list[tuple[str, str, str | None, float | None]] = []
        self._playing = False
        self._session_started_at: float | None = None
        self._chunks_played = 0

    def set_amplitude_listener(
        self,
        listener: Callable[[float], None] | None,
    ) -> None:
        self._amplitude_listener = listener

    # ── public API ────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not enabled:
            self.stop()

    def enqueue(
        self,
        text: str,
        reaction: str | None = None,
        speed: float | None = None,
    ) -> None:
        """Queue ``text`` for spoken playback (sanitised internally).

        ``speed`` is an optional per-chunk override. When ``None`` the
        backend uses its reaction-derived default
        (:meth:`PocketTtsService.reaction_to_speed`); when provided the
        backend clamps it to its own safe range.
        """
        if not self._enabled:
            return
        cleaned = prepare_tts_text((text or "").strip())
        if not cleaned:
            return
        with self._lock:
            self._pending.append(("text", cleaned, reaction, speed))
            if self._playing:
                return
            self._playing = True
            chunk = self._pending.pop(0)
        self._dispatch(chunk)

    def enqueue_earcon(self, kind: str) -> None:
        """Queue a stage-direction earcon (``laugh``/``sigh``/``gasp``/
        ``hum``/``tsk``) at the *current* tail of the queue.

        Earcons are serial with text playback so timing lands naturally
        mid-sentence: the queue waits for the prior text chunk to
        finish, plays the earcon to completion, and then proceeds to
        the next chunk. ``EarconPlayer`` instances are autoloaded by
        :class:`SessionController`; if no player was supplied the
        earcon is silently dropped (e.g. tests, TTS disabled).
        """
        if not self._enabled or not (kind or "").strip():
            return
        cleaned_kind = (kind or "").strip().lower()
        with self._lock:
            self._pending.append(("earcon", cleaned_kind, None, None))
            if self._playing:
                return
            self._playing = True
            chunk = self._pending.pop(0)
        self._dispatch(chunk)

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
        next_chunk: tuple[str, str, str | None, float | None] | None = None
        with self._lock:
            self._playing = False
            self._chunks_played += 1
            if self._pending:
                next_chunk = self._pending.pop(0)
                self._playing = True
        if next_chunk is not None:
            self._dispatch(next_chunk)
        else:
            self._notify("end", {})

    def _dispatch(
        self,
        chunk: tuple[str, str, str | None, float | None],
    ) -> None:
        kind, content, reaction, speed = chunk
        if kind == "earcon":
            self._start_earcon(content)
            return
        self._start_chunk(content, reaction, speed)

    def _start_earcon(self, kind: str) -> None:
        """Play a stage-direction earcon synchronously on a worker
        thread, then advance the queue. Falls back to a noop if no
        :class:`EarconPlayer` was wired in.
        """
        player = self._earcon_player
        if player is None or not getattr(player, "enabled", False):
            self._on_chunk_done()
            return
        self._notify("start", {"text": "", "reaction": "earcon", "earcon": kind})

        def _worker() -> None:
            try:
                player.play_blocking(kind)
            except Exception:
                log.debug("earcon playback failed", exc_info=True)
            finally:
                self._on_chunk_done()

        threading.Thread(
            target=_worker, daemon=True, name=f"tts-earcon-{kind}",
        ).start()

    def _start_chunk(
        self,
        text: str,
        reaction: str | None,
        speed: float | None = None,
    ) -> None:
        # Spawn lookahead synth for the *next* chunk if the backend supports
        # offline generation — keeps latency low across multi-sentence
        # responses without coupling to the avatar's envelope. We only
        # pre-synth text chunks; earcons are pre-cached by EarconPlayer.
        generate = getattr(self._tts, "generate_audio", None)
        with self._lock:
            peek = self._pending[0] if self._pending else None
        if peek is not None and peek[0] == "text" and callable(generate):
            r2s = getattr(self._tts, "reaction_to_speed", None)
            _, peek_text, peek_reaction, peek_speed = peek
            speed_for_lookahead = peek_speed if peek_speed is not None else (
                r2s(peek_reaction) if callable(r2s) else 1.0
            )
            threading.Thread(
                target=generate,
                args=(peek_text, speed_for_lookahead),
                daemon=True,
                name="tts-lookahead",
            ).start()

        self._notify("start", {"text": text, "reaction": reaction or ""})
        amplitude_cb = self._amplitude_listener
        try:
            # Backends that accept ``speed`` get the per-chunk override;
            # legacy backends (no kwarg) are called without it and fall
            # back to their reaction-derived speed. The two TypeError
            # rungs let us walk back gracefully.
            try:
                self._tts.speak_async(
                    text,
                    reaction=reaction,
                    on_done=self._on_chunk_done,
                    on_amplitude=amplitude_cb,
                    speed=speed,
                )
            except TypeError:
                try:
                    self._tts.speak_async(
                        text,
                        reaction=reaction,
                        on_done=self._on_chunk_done,
                        on_amplitude=amplitude_cb,
                    )
                except TypeError:
                    self._tts.speak_async(
                        text,
                        reaction=reaction,
                        on_done=self._on_chunk_done,
                    )
        except Exception as exc:
            log.warning("tts speak_async failed: %s", exc)
            with self._lock:
                self._playing = False
            self._notify("end", {})

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        # Per plan: state transitions are tweaking-only telemetry (the WS
        # layer already broadcasts them to the UI). Keep at DEBUG so default
        # INFO logs aren't flooded with one entry per spoken sentence.
        if event == "start":
            self._session_started_at = time.monotonic()
            self._chunks_played = 0
            with self._lock:
                queue_depth = len(self._pending)
            log.debug(
                "tts state: idle -> speaking queue_depth=%d reaction=%s",
                queue_depth, payload.get("reaction") or "-",
            )
        elif event == "end":
            elapsed_ms = 0.0
            if self._session_started_at is not None:
                elapsed_ms = (time.monotonic() - self._session_started_at) * 1000.0
            log.debug(
                "tts state: speaking -> idle drained_chunks=%d elapsed_ms=%.0f",
                self._chunks_played, elapsed_ms,
            )
            self._session_started_at = None
            self._chunks_played = 0

        if self._listener is None:
            return
        try:
            self._listener(event, payload)
        except Exception:
            log.debug("tts state listener raised", exc_info=True)
