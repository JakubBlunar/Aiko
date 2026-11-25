"""Continuous voice-detection loop, decoupled from Qt.

A simpler, headless port of :class:`app.ui.live_worker.LivePracticeWorker`.
Only the voice-detection input mode is supported here -- push-to-talk and
wake-word remain in the desktop UI.

The loop runs on a daemon thread and emits events via a single callback::

    on_event("voice_state", {"state": "listening" | "transcribing" | "thinking" | "off"})
    on_event("audio_level", {"level": <0..1>})
    on_event("stt_final",  {"text": "<final transcript>"})
    on_event("token",      {"chunk": "<token chunk>"})
    on_event("turn_done",  {"metrics": {...}})
    on_event("error",      {"message": "..."})

TTS playback state is **not** re-emitted here; the WS bridge already
broadcasts ``tts_state`` events from the existing TTS state listener and
the React UI maps "speaking" via that channel.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.live_session")


EventCallback = Callable[[str, dict[str, Any]], None]


# Audio-level events arrive ~50 Hz from the capture thread; throttle to
# avoid flooding the WebSocket. 20 Hz is plenty for a UI meter.
_AUDIO_LEVEL_MIN_INTERVAL_S = 0.05


@dataclass(frozen=True)
class _PendingPhrase:
    path: Path
    capture_ms: float
    speech_ms: float


class LiveSession:
    """Headless continuous voice loop.

    One instance per :class:`SessionController`. Started/stopped from the
    WebSocket layer; toggles ``set_live_voice_session_active`` so the
    proactive director knows when it is allowed to speak.
    """

    def __init__(
        self,
        session: "SessionController",
        on_event: EventCallback,
    ) -> None:
        self._session = session
        self._on_event = on_event
        self._stop_requested = False
        self._active = False
        self._lock = threading.Lock()
        self._main_thread: threading.Thread | None = None
        self._capture_thread: threading.Thread | None = None
        self._pending_lock = threading.Lock()
        self._pending: deque[_PendingPhrase] = deque()
        self._capturing_speech_ms = 0.0
        self._max_pending = 2
        self._processing = threading.Event()
        # A capture-thread interrupt must finish before the next turn can start.
        self._turn_lock = threading.Lock()
        self._last_audio_emit = 0.0
        # Activity / silence tracking for proactive nudges.
        self._last_activity_monotonic = 0.0
        self._overlap_barge = threading.Event()
        self._overlap_speech_ms = 0.0
        self._last_overlap_frame = 0

    # ── public API ────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def start(self) -> bool:
        with self._lock:
            if self._active:
                return False
            self._stop_requested = False
            self._active = True
        try:
            self._session.set_live_voice_session_active(True)
        except Exception:
            log.debug("set_live_voice_session_active(True) failed", exc_info=True)
        self._main_thread = threading.Thread(
            target=self._run, daemon=True, name="live-session",
        )
        self._main_thread.start()
        log.info("live session started")
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._stop_requested = True
        # Stop any in-flight TTS so the UI flips back quickly.
        try:
            self._session.stop_tts()
        except Exception:
            log.debug("stop_tts during live stop failed", exc_info=True)
        # Ask the runner to halt mid-stream too.
        try:
            self._session._turn_runner.request_stop()
        except Exception:
            pass

    # ── internals ─────────────────────────────────────────────────────────

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        try:
            self._on_event(name, payload)
        except Exception:
            log.debug("on_event raised for %s", name, exc_info=True)

    def _run(self) -> None:
        self._emit("voice_state", {"state": "listening"})
        self._last_activity_monotonic = time.monotonic()
        capture_stop = threading.Event()
        capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(capture_stop,),
            daemon=True,
            name="live-session-capture",
        )
        self._capture_thread = capture_thread
        capture_thread.start()
        try:
            while not self._stop_requested:
                with self._pending_lock:
                    item = self._pending.popleft() if self._pending else None
                if item is None:
                    self._maybe_proactive()
                    time.sleep(0.05)
                    continue

                # Real user activity -- reset the silence clock so we don't
                # immediately nudge after a turn finishes.
                self._last_activity_monotonic = time.monotonic()

                # Barge-in: if Aiko is mid-speech and a new phrase just
                # landed, stop her so we can react to the new input. Gate
                # on a minimum sustained speech duration so a throat-clear
                # or background cough doesn't interrupt her.
                if (
                    self._session.barge_in_enabled()
                    and self._session.is_tts_playing()
                ):
                    if self._capture_meets_barge_in_threshold(item.speech_ms):
                        try:
                            self._session.stop_tts()
                        except Exception:
                            log.debug("barge-in stop_tts failed", exc_info=True)
                    else:
                        log.debug(
                            "barge-in suppressed: speech_ms=%.0f < min_speech_ms=%.0f",
                            item.speech_ms,
                            self._barge_in_min_speech_ms(),
                        )

                with self._turn_lock:
                    self._overlap_barge.clear()
                    self._overlap_speech_ms = 0.0
                    levels = self._session.mic_source.recent_level_chunks()
                    self._last_overlap_frame = levels[-1][0] if levels else 0
                    self._processing.set()
                try:
                    turn = self._session.process_live_capture(
                        wav_path=item.path,
                        capture_ms=item.capture_ms,
                        stop_requested=self._is_turn_aborted,
                        on_token=self._on_token,
                        on_generation_status=self._on_generation_status,
                        on_transcript=self._on_transcript,
                    )
                except Exception as exc:
                    log.exception("process_live_capture failed")
                    self._emit("error", {"message": str(exc)})
                    turn = None
                finally:
                    with self._turn_lock:
                        self._processing.clear()

                if self._stop_requested:
                    break

                if turn is None:
                    self._emit("voice_state", {"state": "listening"})
                    continue

                # Surface the same metrics shape the typed-chat path uses,
                # so the React UI can update its perf panel uniformly.
                try:
                    self._emit(
                        "turn_done",
                        {"metrics": self._session.get_last_metrics()},
                    )
                except Exception:
                    log.debug("get_last_metrics failed", exc_info=True)

                # ``process_live_capture`` returns once the LLM stream is
                # done, but TTS may still be draining. Wait for it before
                # resuming the listening loop, so the user doesn't talk
                # over the tail of the assistant's reply.
                self._wait_for_tts_drain()

                if self._stop_requested:
                    break
                # Reset silence clock once Aiko's turn fully ends -- otherwise
                # we'd immediately nudge again because the timer was running
                # while she spoke.
                self._last_activity_monotonic = time.monotonic()
                self._emit("voice_state", {"state": "listening"})
        except Exception:
            log.exception("live session main loop crashed")
        finally:
            self._stop_requested = True
            capture_stop.set()
            self._processing.clear()
            try:
                capture_thread.join(timeout=2.0)
            except Exception:
                pass
            with self._pending_lock:
                abandoned = list(self._pending)
                self._pending.clear()
                self._capturing_speech_ms = 0.0
            for phrase in abandoned:
                self._discard_capture(phrase.path)
            try:
                self._session.set_live_voice_session_active(False)
            except Exception:
                log.debug(
                    "set_live_voice_session_active(False) failed", exc_info=True,
                )
            with self._lock:
                self._active = False
            self._emit("voice_state", {"state": "off"})
            log.info("live session stopped")

    @staticmethod
    def _discard_capture(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove unused voice capture %s", path, exc_info=True)

    def _capture_loop(self, capture_stop: threading.Event) -> None:
        # Backoff so a missing/broken microphone doesn't spam the log every
        # 500ms; we keep retrying in case the device is plugged in mid-session.
        consecutive_errors = 0
        last_error_message = ""

        def stopping() -> bool:
            return capture_stop.is_set() or self._stop_requested

        def speech_progress(speech_ms: float) -> None:
            with self._pending_lock:
                if stopping():
                    return
                self._capturing_speech_ms = speech_ms
            with self._turn_lock:
                self._interrupt_generation(speech_ms)

        while not stopping():
            if self._processing.is_set():
                with self._turn_lock:
                    self._tick_overlap_watch()
                time.sleep(0.05)
                continue
            with self._pending_lock:
                backlog = len(self._pending)
            if backlog >= self._max_pending:
                time.sleep(0.05)
                continue
            try:
                captured = self._session.capture_live_phrase(
                    stop_requested=stopping,
                    on_audio_level=self._on_audio_level,
                    on_speech_progress=speech_progress,
                    on_generation_status=None,
                )
            except Exception as exc:
                with self._pending_lock:
                    if stopping():
                        return
                    self._capturing_speech_ms = 0.0
                consecutive_errors += 1
                msg = str(exc) or exc.__class__.__name__
                if consecutive_errors == 1:
                    log.exception("capture_live_phrase failed")
                    self._emit("error", {"message": f"microphone error: {msg}"})
                elif msg != last_error_message:
                    log.warning("capture_live_phrase failing: %s", msg)
                last_error_message = msg
                # 0.5s, 1s, 2s, 4s ... capped at 5s.
                backoff = min(0.5 * (2 ** min(consecutive_errors - 1, 4)), 5.0)
                capture_stop.wait(backoff)
                continue
            if consecutive_errors:
                log.info("capture_live_phrase recovered after %d errors", consecutive_errors)
                consecutive_errors = 0
                last_error_message = ""
            with self._pending_lock:
                if not capture_stop.is_set():
                    speech_ms = self._capturing_speech_ms
                    self._capturing_speech_ms = 0.0
                    if captured is not None and not stopping():
                        wav_path, capture_ms = captured
                        self._pending.append(_PendingPhrase(wav_path, capture_ms, speech_ms))
                        continue
            if captured is not None:
                self._discard_capture(captured[0])

    def _maybe_proactive(self) -> None:
        """Nudge the proactive director if the user has been silent.

        Mirrors :meth:`app.ui.live_worker.LivePracticeWorker._maybe_proactive`
        but routes through the new :meth:`SessionController.notify_silence`
        path so the WS client gets a normal assistant message + tts_state
        flow, just like a typed turn.
        """
        if self._processing.is_set():
            return
        try:
            if self._session.is_tts_playing():
                return
        except Exception:
            return
        agent = getattr(self._session, "_settings", None)
        if agent is not None:
            agent = getattr(agent, "agent", None)
        silence_threshold = float(getattr(agent, "proactive_silence_seconds", 45.0))
        idle = time.monotonic() - self._last_activity_monotonic
        if idle < silence_threshold:
            return
        try:
            self._session.generate_proactive_message()
        except Exception:
            log.debug("notify_silence failed", exc_info=True)
        # Whether or not the director actually spoke (cooldown can swallow
        # the call), reset the clock so we don't keep firing every tick.
        self._last_activity_monotonic = time.monotonic()

    def _wait_for_tts_drain(self) -> None:
        # TTS start already marked playback pending. Re-arming here can
        # erase an acknowledgement received before the turn finished.
        for _ in range(600):  # 600 * 0.05s = 30s
            if self._stop_requested or self._overlap_barge.is_set():
                return
            try:
                if not self._session.is_tts_playing() and self._session.is_playback_drained():
                    return
                if self._has_interrupting_phrase():
                    # Even after server synthesis ends the browser can still
                    # have scheduled PCM. stop_tts sends its audio_cancel.
                    self._session.stop_tts()
                    return
            except Exception:
                return
            time.sleep(0.05)

    def _tick_overlap_watch(self) -> None:
        """PCM-only barge-in while transcribe/LLM holds the capture loop."""
        if not self._processing.is_set() or self._overlap_barge.is_set():
            return
        enabled = self._session.barge_in_enabled()
        threshold = max(0.004, float(self._session.vad_level_threshold) * 0.4)
        for end, frames, level in self._session.mic_source.recent_level_chunks():
            if end <= self._last_overlap_frame:
                continue
            if end - frames != self._last_overlap_frame or level < threshold or not enabled:
                self._overlap_speech_ms = 0.0
            self._last_overlap_frame = end
            if enabled and level >= threshold:
                rate = self._session.settings.audio.sample_rate
                self._overlap_speech_ms += 1000.0 * frames / rate
                self._interrupt_generation(self._overlap_speech_ms)
                if self._overlap_barge.is_set():
                    break

    def _interrupt_generation(self, speech_ms: float) -> None:
        if (
            not self._processing.is_set()
            or self._overlap_barge.is_set()
            or not self._session.barge_in_enabled()
            or not self._capture_meets_barge_in_threshold(speech_ms)
        ):
            return
        self._overlap_barge.set()
        self._overlap_speech_ms = 0.0
        try:
            self._session.stop_tts()
        except Exception:
            log.debug("overlap barge-in stop_tts failed", exc_info=True)
        abort = getattr(self._session, "request_turn_stop", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                log.debug("overlap barge-in request_stop failed", exc_info=True)

    def _is_turn_aborted(self) -> bool:
        if self._stop_requested:
            return True
        if self._overlap_barge.is_set():
            return True
        # Barge-in: if a fresh phrase arrived while Aiko was responding,
        # abort the current stream so we can react to the new input.
        # Same sustained-speech gate as the main loop applies — short
        # captures (cough, "yeah", throat-clear) don't abort the turn.
        return self._has_interrupting_phrase()

    def _has_interrupting_phrase(self) -> bool:
        if not self._session.barge_in_enabled():
            return False
        with self._pending_lock:
            return self._capture_meets_barge_in_threshold(self._capturing_speech_ms) or any(
                self._capture_meets_barge_in_threshold(phrase.speech_ms) for phrase in self._pending
            )

    def _barge_in_min_speech_ms(self) -> float:
        try:
            return float(
                self._session._settings.endpointing.barge_in_min_speech_seconds
            ) * 1000.0
        except Exception:
            return 700.0

    def _capture_meets_barge_in_threshold(self, speech_ms: float) -> bool:
        return speech_ms > 0.0 and speech_ms >= self._barge_in_min_speech_ms()

    def _on_audio_level(self, level: float) -> None:
        now = time.monotonic()
        note = getattr(self._session, "note_mic_rms", None)
        if callable(note):
            try:
                note(level)
            except Exception:
                pass
        if now - self._last_audio_emit < _AUDIO_LEVEL_MIN_INTERVAL_S:
            return
        self._last_audio_emit = now
        # Clamp into the UI's expected 0..1 range.
        try:
            value = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return
        self._emit("audio_level", {"level": value})

    def _on_transcript(self, text: str) -> None:
        if not self._stop_requested:
            self._emit("stt_final", {"text": text})

    def _on_token(self, chunk: str) -> None:
        if not chunk:
            return
        self._emit("token", {"chunk": chunk})

    def _on_generation_status(self, status: str) -> None:
        # Map session-status strings to voice_state. The session emits
        # things like "transcribing", "AI is generating response...",
        # "listening", "did not catch that, listening".
        state = self._status_to_state(status)
        if state is not None:
            self._emit("voice_state", {"state": state})

    @staticmethod
    def _status_to_state(status: str) -> str | None:
        s = (status or "").strip().lower()
        if not s:
            return None
        if "transcrib" in s:
            return "transcribing"
        if "generat" in s or "thinking" in s:
            return "thinking"
        if s.startswith("listening"):
            return "listening"
        return None
