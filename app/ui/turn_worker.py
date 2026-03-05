from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from app.core.session_controller import SessionController


class SingleTurnWorker(QObject):
    typed_done = Signal(str)
    voice_done = Signal(str, str)
    replying = Signal(str)
    status = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        session: SessionController,
        *,
        mode: str,
        text: str = "",
        record_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self._session = session
        self._mode = mode
        self._text = text
        self._record_seconds = record_seconds

    @Slot()
    def run(self) -> None:
        try:
            if self._mode == "typed":
                self.status.emit("AI is generating response...")
                reply = self._session.chat_once_streaming(
                    user_text=self._text,
                    on_token=self.replying.emit,
                    mode="typed",
                    on_generation_status=self.status.emit,
                )
                self.typed_done.emit(reply)
            else:
                self.status.emit("recording")
                user_text, reply = self._session.record_and_chat(
                    seconds=self._record_seconds,
                    on_token=self.replying.emit,
                    on_generation_status=self.status.emit,
                )
                self.voice_done.emit(user_text, reply)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.status.emit("ready")
            self.finished.emit()
