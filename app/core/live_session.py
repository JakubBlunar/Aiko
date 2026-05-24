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
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session_controller import SessionController


log = logging.getLogger("app.live_session")


EventCallback = Callable[[str, dict[str, Any]], None]


# Audio-level events arrive ~50 Hz from the capture thread; throttle to
# avoid flooding the WebSocket. 20 Hz is plenty for a UI meter.
_AUDIO_LEVEL_MIN_INTERVAL_S = 0.05


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
        self._pending: deque[tuple[Path, float]] = deque()
        self._max_pending = 2
        self._processing = threading.Event()
        self._last_audio_emit = 0.0

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
        capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="live-session-capture",
        )
        self._capture_thread = capture_thread
        capture_thread.start()
        try:
            while not self._stop_requested:
                item: tuple[Path, float] | None = None
                with self._pending_lock:
                    if self._pending:
                        item = self._pending.popleft()
                if item is None:
                    time.sleep(0.05)
                    continue

                wav_path, capture_ms = item

                # Barge-in: if Aiko is mid-speech and a new phrase just
                # landed, stop her so we can react to the new input.
                if (
                    self._session.barge_in_enabled()
                    and self._session.is_tts_playing()
                ):
                    try:
                        self._session.stop_tts()
                    except Exception:
                        log.debug("barge-in stop_tts failed", exc_info=True)

                self._processing.set()
                try:
                    turn = self._session.process_live_capture(
                        wav_path=wav_path,
                        capture_ms=capture_ms,
                        stop_requested=self._is_turn_aborted,
                        on_token=self._on_token,
                        on_generation_status=self._on_generation_status,
                    )
                except Exception as exc:
                    log.exception("process_live_capture failed")
                    self._emit("error", {"message": str(exc)})
                    turn = None
                finally:
                    self._processing.clear()

                if self._stop_requested:
                    break

                if turn is None:
                    self._emit("voice_state", {"state": "listening"})
                    continue

                user_text, _reply = turn
                if user_text:
                    self._emit("stt_final", {"text": user_text})

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
                self._emit("voice_state", {"state": "listening"})
        except Exception:
            log.exception("live session main loop crashed")
        finally:
            self._stop_requested = True
            self._processing.clear()
            try:
                capture_thread.join(timeout=2.0)
            except Exception:
                pass
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

    def _capture_loop(self) -> None:
        # Backoff so a missing/broken microphone doesn't spam the log every
        # 500ms; we keep retrying in case the device is plugged in mid-session.
        consecutive_errors = 0
        last_error_message = ""
        while not self._stop_requested:
            if self._processing.is_set():
                time.sleep(0.05)
                continue
            with self._pending_lock:
                backlog = len(self._pending)
            if backlog >= self._max_pending:
                time.sleep(0.05)
                continue
            try:
                captured = self._session.capture_live_phrase(
                    stop_requested=lambda: self._stop_requested,
                    on_audio_level=self._on_audio_level,
                    on_generation_status=None,
                )
            except Exception as exc:
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
                time.sleep(backoff)
                continue
            if consecutive_errors:
                log.info("capture_live_phrase recovered after %d errors", consecutive_errors)
                consecutive_errors = 0
                last_error_message = ""
            if self._stop_requested or captured is None:
                continue
            wav_path, capture_ms = captured
            with self._pending_lock:
                if len(self._pending) < self._max_pending:
                    self._pending.append((wav_path, capture_ms))
                else:
                    try:
                        wav_path.unlink(missing_ok=True)
                    except Exception:
                        pass

    def _wait_for_tts_drain(self) -> None:
        # Up to ~30s; bail early if a stop was requested.
        for _ in range(600):  # 600 * 0.05s = 30s
            if self._stop_requested:
                return
            try:
                if not self._session.is_tts_playing():
                    return
            except Exception:
                return
            time.sleep(0.05)

    def _is_turn_aborted(self) -> bool:
        if self._stop_requested:
            return True
        # Barge-in: if a fresh phrase arrived while Aiko was responding,
        # abort the current stream so we can react to the new input.
        if self._session.barge_in_enabled():
            with self._pending_lock:
                if self._pending:
                    return True
        return False

    def _on_audio_level(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_audio_emit < _AUDIO_LEVEL_MIN_INTERVAL_S:
            return
        self._last_audio_emit = now
        # Clamp into the UI's expected 0..1 range.
        try:
            value = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return
        self._emit("audio_level", {"level": value})

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
