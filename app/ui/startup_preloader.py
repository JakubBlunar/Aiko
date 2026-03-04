from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout

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
