from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from app.core.session_controller import SessionController


class LivePracticeWorker(QObject):
    status = Signal(str)
    heard = Signal(str)
    replying = Signal(str)
    replied = Signal(str)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, session: SessionController) -> None:
        super().__init__()
        self._session = session
        self._stop_requested = False

    @Slot()
    def run(self) -> None:
        self.status.emit("listening")
        try:
            while not self._stop_requested:
                turn = self._session.listen_once_and_chat(
                    stop_requested=self._is_stop_requested,
                    on_token=self.replying.emit,
                )
                if turn is None:
                    continue
                user_text, reply_text = turn
                self.heard.emit(user_text)
                self.replied.emit(reply_text)
                self.status.emit("listening")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.status.emit("ready")
            self.stopped.emit()

    def stop(self) -> None:
        self._stop_requested = True

    def _is_stop_requested(self) -> bool:
        return self._stop_requested
