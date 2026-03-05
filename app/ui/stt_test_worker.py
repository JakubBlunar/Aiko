from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from app.core.crash_logging import log_handled_exception
from app.core.session_controller import SessionController


class SttTestWorker(QObject):
    done = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        session: SessionController,
        *,
        seconds: float,
        vad_filter: bool,
        initial_prompt: str,
    ) -> None:
        super().__init__()
        self._session = session
        self._seconds = seconds
        self._vad_filter = vad_filter
        self._initial_prompt = initial_prompt

    @Slot()
    def run(self) -> None:
        try:
            result = self._session.run_stt_diagnostic(
                seconds=self._seconds,
                vad_filter=self._vad_filter,
                initial_prompt=self._initial_prompt,
            )
            self.done.emit(result)
        except Exception as exc:
            log_handled_exception(exc, context="ui.stt_test_worker")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()
