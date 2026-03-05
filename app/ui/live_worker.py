from __future__ import annotations

from collections import deque
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot

from app.core.session_controller import SessionController


class LivePracticeWorker(QObject):
    status = Signal(str)
    level = Signal(float)
    heard = Signal(str)
    replying = Signal(str)
    replied = Signal(str)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, session: SessionController) -> None:
        super().__init__()
        self._session = session
        self._stop_requested = False
        self._pending_lock = threading.Lock()
        self._pending: deque[tuple[Path, float]] = deque()
        self._max_pending = 2
        # Set while a captured phrase is being processed (STT → LLM → TTS).
        # The capture thread pauses during this window so TTS audio isn't
        # picked up by the microphone and the audio device isn't shared.
        self._processing = threading.Event()

    @Slot()
    def run(self) -> None:
        self.status.emit("listening")
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
                    time.sleep(0.05)
                    continue

                wav_path, capture_ms = item
                self._processing.set()
                try:
                    turn = self._session.process_live_capture(
                        wav_path=wav_path,
                        capture_ms=capture_ms,
                        stop_requested=self._is_stop_requested,
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
        except Exception as exc:
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

    def _capture_loop(self) -> None:
        while not self._stop_requested:
            # Pause while the processing loop is handling a turn so that
            # speaker audio (TTS) is not captured and the audio device is
            # not opened by two code paths simultaneously.
            if self._processing.is_set():
                time.sleep(0.05)
                continue

            with self._pending_lock:
                backlog = len(self._pending)

            if backlog >= self._max_pending:
                time.sleep(0.05)
                continue

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
