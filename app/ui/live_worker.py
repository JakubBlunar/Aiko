from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot

from app.core.crash_logging import log_handled_exception
from app.core.session_controller import SessionController


class LivePracticeWorker(QObject):
    status = Signal(str)
    level = Signal(float)
    heard = Signal(str)
    replying = Signal(str)
    replied = Signal(str)
    proactive = Signal(str)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, session: SessionController) -> None:
        super().__init__()
        self._session = session
        self._stop_requested = False
        self._pending_lock = threading.Lock()
        self._pending: deque[tuple[Path, float]] = deque()
        self._max_pending = 2
        # Set while a captured phrase is being processed (STT -> LLM -> TTS).
        # The capture thread pauses during this window so TTS audio isn't
        # picked up by the microphone and the audio device isn't shared.
        self._processing = threading.Event()
        self._last_activity = time.monotonic()
        self._last_proactive = 0.0
        self._unanswered_proactive = 0
        self._max_unanswered = 3
        self._log = logging.getLogger("app.ui.live_worker")

    @Slot()
    def run(self) -> None:
        self.status.emit("listening")
        self._last_activity = time.monotonic()
        capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="live-capture"
        )
        capture_thread.start()
        try:
            while not self._stop_requested:
                item: tuple[Path, float] | None = None
                with self._pending_lock:
                    if self._pending:
                        item = self._pending.popleft()

                if item is None:
                    self._maybe_proactive()
                    time.sleep(0.05)
                    continue

                self._last_activity = time.monotonic()
                self._unanswered_proactive = 0

                wav_path, capture_ms = item
                if self._session.barge_in_enabled() and self._session.is_tts_playing():
                    self._session.stop_tts()
                self._processing.set()
                try:
                    turn = self._session.process_live_capture(
                        wav_path=wav_path,
                        capture_ms=capture_ms,
                        stop_requested=self._stop_requested_for_turn,
                        on_token=self.replying.emit,
                        on_generation_status=self.status.emit,
                    )
                finally:
                    self._processing.clear()

                if turn is None:
                    self.status.emit("listening")
                    continue

                user_text, reply_text = turn
                self.heard.emit(user_text)
                self.replied.emit(reply_text)
                self.status.emit("listening")
                self._last_activity = time.monotonic()
        except Exception as exc:
            log_handled_exception(exc, context="ui.live_worker")
            self.failed.emit(str(exc))
        finally:
            self._stop_requested = True
            self._processing.clear()
            capture_thread.join(timeout=2.0)
            self.status.emit("ready")
            self.stopped.emit()

    def stop(self) -> None:
        self._stop_requested = True

    def _is_stop_requested(self) -> bool:
        return self._stop_requested

    def _has_pending_phrase(self) -> bool:
        with self._pending_lock:
            return len(self._pending) > 0

    def _stop_requested_for_turn(self) -> bool:
        if self._stop_requested:
            return True
        if self._session.barge_in_enabled() and self._has_pending_phrase():
            return True
        return False

    def _maybe_proactive(self) -> None:
        """Check if we should generate a proactive message during silence."""
        if self._processing.is_set() or self._session.is_tts_playing():
            return
        if self._unanswered_proactive >= self._max_unanswered:
            return

        now = time.monotonic()
        silence_threshold = getattr(
            self._session._settings.agent, "proactive_silence_seconds", 45.0
        )
        cooldown = getattr(
            self._session._settings.agent, "proactive_cooldown_seconds", 120.0
        )

        idle_duration = now - self._last_activity
        since_last_proactive = now - self._last_proactive

        if idle_duration < silence_threshold:
            return
        if since_last_proactive < cooldown:
            return

        self._log.info(
            "Silence %.0fs > threshold %.0fs, generating proactive message (attempt %d/%d)",
            idle_duration, silence_threshold,
            self._unanswered_proactive + 1, self._max_unanswered,
        )

        msg = self._session.generate_proactive_message()
        if msg:
            self._session.speak_text(msg)
            self.proactive.emit(msg)
            self._last_proactive = time.monotonic()
            self._unanswered_proactive += 1
            self._last_activity = time.monotonic()
        else:
            self._last_proactive = time.monotonic()

    def _capture_loop(self) -> None:
        while not self._stop_requested:
            if self._processing.is_set():
                time.sleep(0.05)
                continue

            with self._pending_lock:
                backlog = len(self._pending)

            if backlog >= self._max_pending:
                time.sleep(0.05)
                continue

            if self._session.live_input_mode == "push_to_talk":
                while not self._stop_requested and not self._session.get_ptt_active():
                    time.sleep(0.05)
                if self._stop_requested:
                    break
                captured = self._session.capture_ptt_phrase(
                    ptt_active_getter=lambda: self._session.get_ptt_active(),
                    stop_requested=self._is_stop_requested,
                    on_audio_level=self.level.emit,
                    on_generation_status=self.status.emit,
                )
            else:
                captured = self._session.capture_live_phrase(
                    stop_requested=self._is_stop_requested,
                    on_audio_level=self.level.emit,
                    on_generation_status=self.status.emit,
                )

            if self._stop_requested:
                break
            if captured is None:
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
