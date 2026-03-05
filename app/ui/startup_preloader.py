from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout

from app.core.crash_logging import log_handled_exception
from app.core.session_controller import SessionController
from app.core.settings import AppSettings


class StartupPreloaderDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

        self.setWindowTitle("Starting Assistant")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(520, 140)

        layout = QVBoxLayout(self)
        self._title = QLabel("Initializing models...", self)
        self._status = QLabel("Preparing startup warmup", self)
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)

        layout.addWidget(self._title)
        layout.addWidget(self._status)
        layout.addWidget(self._progress)

    def set_status(self, message: str) -> None:
        self._status.setText(message)


class StartupPrewarmWorker(QObject):
    status = Signal(str)
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings

    @Slot()
    def run(self) -> None:
        try:
            session = SessionController(self._settings)
            session.prewarm_runtime(on_status=self.status.emit)
            self.ready.emit(session)
        except Exception as exc:
            log_handled_exception(exc, context="ui.startup_prewarm")
            self.failed.emit(str(exc))
