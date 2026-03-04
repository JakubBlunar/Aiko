from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from app.core.session_controller import SessionController


class OcrTestWorker(QObject):
    done = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, session: SessionController) -> None:
        super().__init__()
        self._session = session

    @Slot()
    def run(self) -> None:
        try:
            result = self._session.run_screen_ocr_diagnostic()
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()
