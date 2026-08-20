"""Thread-safe sequential TTS queue.

Plays text chunks one at a time on the chosen TTS backend, interleaving
text with real timed silences and stage-direction earcons on one serial
pipeline. The next *sentence* is pre-generated in a daemon thread while
the current chunk plays -- looking past any silence or earcon between
them -- so transitions stay seamless on backends that take >1s to
synthesise. See :meth:`TtsQueue._spawn_lookahead`.

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

from app.core.session.session_text_utils import prepare_tts_text


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
        # *kind* of chunk ("text" | "earcon" | "silence"), enabling a
        # single serialised pipeline that interleaves all three. For
        # "text" the rest of the tuple is (content, reaction, speed,
        # gain_db). For "earcon" only the kind name is meaningful
        # (content holds the earcon name, the other slots are unused).
        # For "silence" ``content`` is the duration in milliseconds
        # (string) and the other slots are unused.
        self._pending: list[
            tuple[str, str, str | None, float | None, float]
        ] = []
        # Text of the sentence a prefetch was last started for, so the
        # several chunks dispatched while it is in flight don't each
        # spawn their own. Guarded by ``_lock``.
        self._lookahead_text: str | None = None
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
        *,
        gain_db: float = 0.0,
    ) -> None:
        """Queue ``text`` for spoken playback (sanitised internally).

        ``speed`` is an optional per-chunk override. When ``None`` the
        backend uses its reaction-derived default
        (:meth:`PocketTtsService.reaction_to_speed`); when provided the
        backend clamps it to its own safe range.

        ``gain_db`` (Layer 1b / Layer 3) is a per-chunk dB offset
        forwarded straight to ``speak_async``. Positive boosts;
        negative attenuates. Backends that don't accept the kwarg
        gracefully ignore it via the ``TypeError`` rungs in
        :meth:`_start_chunk`.
        """
        if not self._enabled:
            return
        cleaned = prepare_tts_text((text or "").strip())
        if not cleaned:
            return
        try:
            gain_value = float(gain_db)
        except (TypeError, ValueError):
            gain_value = 0.0
        with self._lock:
            self._pending.append(
                ("text", cleaned, reaction, speed, gain_value),
            )
            busy = self._playing
            if not busy:
                self._playing = True
                chunk = self._pending.pop(0)
        if busy:
            # Start synthesising the moment the sentence exists, rather
            # than waiting for a chunk boundary to come round. Sentences
            # arrive from the LLM stream while she is already talking, so
            # dispatch-time prefetching alone left the newest sentence
            # cold until the chunk *before* it was dispatched -- which on
            # a two-sentence reply is after the first one has finished.
            self._spawn_lookahead()
            return
        self._dispatch(chunk)

    # Layer 2: real timed pauses. The cadence layer already produces
    # ``ProsodyParams.pause_before_ms`` / ``pause_after_ms`` but the
    # legacy implementation only rewrote punctuation (an ``…`` instead
    # of a ``.``). This path emits actual silent PCM frames so a
    # "let me think... about that" beat lands as a real wall-clock gap.
    # Capped to keep a runaway pause from holding the queue forever.
    _SILENCE_MAX_MS: int = 1500

    def enqueue_silence(self, ms: int) -> None:
        """Queue ``ms`` milliseconds of silent playback at the current tail.

        Behaves like :meth:`enqueue_earcon` -- serial with text and
        earcon items, advances the queue once the silence completes.
        Backends that don't support ``speak_silence_async`` fall back
        to a wall-clock sleep so the queue still paces correctly. Use
        this instead of rewriting punctuation when the cadence layer
        wants a real pause.
        """
        if not self._enabled:
            return
        try:
            duration = int(ms)
        except (TypeError, ValueError):
            return
        if duration <= 0:
            return
        duration = min(self._SILENCE_MAX_MS, duration)
        with self._lock:
            self._pending.append(
                ("silence", str(duration), None, None, 0.0),
            )
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
            self._pending.append(
                ("earcon", cleaned_kind, None, None, 0.0),
            )
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
            self._lookahead_text = None
        try:
            self._tts.stop()
        except Exception:
            log.debug("tts engine stop() failed", exc_info=True)
        if was_playing:
            # P25: mark the difference between "finished speaking" and
            # "cut off". Audio is scheduled *client*-side, so on a natural
            # end the browser must keep playing what it already holds,
            # while on an abort it has to drop it -- barge-in otherwise
            # leaves Aiko talking over the user for as long as the client
            # buffer runs. Same event, so no client is forced to handle a
            # new type; ``aborted`` is simply absent on the natural path.
            self._notify("end", {"aborted": True})

    def is_active(self) -> bool:
        with self._lock:
            return self._playing or bool(self._pending)

    # ── internal ──────────────────────────────────────────────────────────

    def _on_chunk_done(self) -> None:
        next_chunk: tuple[str, str, str | None, float | None, float] | None = None
        with self._lock:
            # ``stop()`` already cleared this, so a late callback from the
            # engine (it finished synthesising the chunk we abandoned) is
            # not the end of anything. Emitting again would follow the
            # aborted end with a second, un-aborted one -- reading as
            # "she finished normally" a beat after the barge-in.
            was_playing = self._playing
            self._playing = False
            if not was_playing:
                return
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
        chunk: tuple[str, str, str | None, float | None, float],
    ) -> None:
        kind, content, reaction, speed, gain_db = chunk
        # A pause or an earcon buys time for the next sentence just as
        # playback does, so the prefetch is spawned for every kind of
        # chunk -- not only from the text path as it once was.
        if kind == "earcon":
            self._spawn_lookahead()
            self._start_earcon(content)
            return
        if kind == "silence":
            self._spawn_lookahead()
            try:
                duration_ms = int(content)
            except (TypeError, ValueError):
                duration_ms = 0
            self._start_silence(duration_ms)
            return
        # Ordered deliberately: ``_start_chunk`` hands the text to the
        # engine, which stakes its claim on the synthesis gate before
        # returning. Spawning the prefetch first instead lets it reach the
        # engine while the gate still looks idle, and it takes the
        # single-request pipe -- which is not a theoretical race. It cost
        # the real sidecar 3.1s to first audio on a one-second opener,
        # because the whole of sentence two was synthesised in front of it.
        self._start_chunk(content, reaction, speed, gain_db=gain_db)
        self._spawn_lookahead()

    def _spawn_lookahead(self) -> None:
        """Pre-synthesise the next *text* chunk on a daemon thread.

        Both engines produce a whole clip before emitting a byte, so
        without this every sentence boundary costs a full synthesis of
        silence -- around 2s on chatterbox-nano, whose RTF is 0.78. The
        engine caches the clip, so the playback call that follows is a
        cache hit rather than a second generation.

        Scanning forward past silence and earcon entries is the part that
        matters. The previous version peeked at one chunk and gave up
        unless it was text, but the cadence layer brackets nearly every
        sentence with ``pause_before`` / ``pause_after`` silences, so the
        chunk after a sentence is almost always a pause -- meaning the
        prefetch quietly never ran in a real turn, on either engine. The
        pause was doing double damage: it hid the gap it was itself
        causing, so the whole thing read as one unnaturally long beat
        after every sentence.

        Timing is safe by construction: a prefetch yields to the sentence
        being spoken (see :class:`~app.tts.clip_cache.SynthesisGate`), so
        the worst case is that it does not finish in time, never that it
        delays the audio that is playing.
        """
        generate = getattr(self._tts, "generate_audio", None)
        if not callable(generate):
            return
        with self._lock:
            target = next(
                (chunk for chunk in self._pending if chunk[0] == "text"), None,
            )
            if target is None:
                return
            _, text, reaction, speed, _gain = target
            # One prefetch per sentence: a pause between two sentences
            # dispatches several chunks that all see the same target, and
            # duplicate threads would contend for the engine.
            if text == self._lookahead_text:
                return
            self._lookahead_text = text
        r2s = getattr(self._tts, "reaction_to_speed", None)
        hint = speed if speed is not None else (
            r2s(reaction) if callable(r2s) else 1.0
        )
        threading.Thread(
            target=generate,
            args=(text, hint),
            daemon=True,
            name="tts-lookahead",
        ).start()

    def _start_silence(self, ms: int) -> None:
        """Layer 2: emit ``ms`` of silent PCM via the engine, then
        advance the queue. Engines without ``speak_silence_async``
        fall back to a daemon thread that sleeps and fires
        ``_on_chunk_done`` -- preserves queue ordering even on bare
        backends.
        """
        if ms <= 0:
            self._on_chunk_done()
            return
        self._notify("start", {"text": "", "reaction": "silence", "ms": ms})
        speak_silence = getattr(self._tts, "speak_silence_async", None)
        if callable(speak_silence):
            try:
                speak_silence(int(ms), on_done=self._on_chunk_done)
                return
            except Exception:
                log.debug("tts speak_silence_async failed", exc_info=True)
        # Fall back to a wall-clock sleep so timing still lines up.

        def _sleep_worker() -> None:
            try:
                time.sleep(int(ms) / 1000.0)
            finally:
                self._on_chunk_done()

        threading.Thread(
            target=_sleep_worker,
            daemon=True,
            name="tts-silence-fallback",
        ).start()

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
        *,
        gain_db: float = 0.0,
    ) -> None:
        self._notify("start", {"text": text, "reaction": reaction or ""})
        amplitude_cb = self._amplitude_listener
        try:
            # Backends that accept ``speed`` get the per-chunk override;
            # legacy backends (no kwarg) are called without it and fall
            # back to their reaction-derived speed. The TypeError rungs
            # walk back gracefully across (gain_db?) -> (speed?) -> bare.
            try:
                self._tts.speak_async(
                    text,
                    reaction=reaction,
                    on_done=self._on_chunk_done,
                    on_amplitude=amplitude_cb,
                    speed=speed,
                    gain_db=float(gain_db),
                )
                return
            except TypeError:
                pass
            try:
                self._tts.speak_async(
                    text,
                    reaction=reaction,
                    on_done=self._on_chunk_done,
                    on_amplitude=amplitude_cb,
                    speed=speed,
                )
                return
            except TypeError:
                pass
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
