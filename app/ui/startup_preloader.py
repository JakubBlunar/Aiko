from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from app.core.crash_logging import log_handled_exception
from app.core.session_controller import SessionController
from app.core.settings import AppSettings


def show_startup_error(message: str, parent=None) -> None:
    """Show a dialog with the error message in a selectable, copyable text area."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Startup warmup failed")
    dialog.setMinimumSize(480, 280)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("An error occurred during startup. You can select and copy the text below:"))
    text = QPlainTextEdit(message.strip())
    text.setReadOnly(True)
    text.setPlaceholderText("")
    layout.addWidget(text)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy_btn = QPushButton("Copy to clipboard")
    copy_btn.clicked.connect(lambda: _copy_text_to_clipboard(text.toPlainText()))
    buttons.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.exec()


def _copy_text_to_clipboard(text: str) -> None:
    from PySide6.QtWidgets import QApplication
    cb = QApplication.clipboard()
    if cb is not None:
        cb.setText(text)


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
