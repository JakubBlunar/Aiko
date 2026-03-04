from __future__ import annotations

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.session_controller import SessionController
from app.core.settings import AppSettings
from app.ui.live_worker import LivePracticeWorker
from app.ui.widgets.status_panel import StatusPanel


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings
        self._session = SessionController(settings)
        self._live_thread: QThread | None = None
        self._live_worker: LivePracticeWorker | None = None

        self.setWindowTitle(settings.assistant.name)
        self.resize(900, 640)

        root = QWidget(self)
        self.setCentralWidget(root)

        layout = QVBoxLayout()
        root.setLayout(layout)

        self._status = StatusPanel()
        self._status.set_model(settings.ollama.chat_model)
        layout.addWidget(self._status)

        capture_row = QHBoxLayout()
        self._mic_checkbox = QCheckBox("Microphone")
        self._mic_checkbox.setChecked(self._session.state.mic_enabled)
        self._system_checkbox = QCheckBox("System Audio")
        self._system_checkbox.setChecked(self._session.state.system_audio_enabled)
        self._screen_checkbox = QCheckBox("Screen Context")
        self._screen_checkbox.setChecked(self._session.state.screen_enabled)

        for widget in (self._mic_checkbox, self._system_checkbox, self._screen_checkbox):
            capture_row.addWidget(widget)

        self._apply_sources_button = QPushButton("Apply Sources")
        self._apply_sources_button.clicked.connect(self._apply_sources)
        capture_row.addWidget(self._apply_sources_button)
        capture_row.addStretch(1)
        layout.addLayout(capture_row)

        layout.addWidget(QLabel("Conversation"))
        self._conversation = QTextEdit()
        self._conversation.setReadOnly(True)
        layout.addWidget(self._conversation, stretch=1)

        input_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type what you want to say...")
        self._input.returnPressed.connect(self._send)
        self._send_button = QPushButton("Send")
        self._send_button.clicked.connect(self._send)
        self._record_button = QPushButton("Record 5s")
        self._record_button.clicked.connect(self._record_and_send)
        self._start_live_button = QPushButton("Start Live")
        self._start_live_button.clicked.connect(self._start_live_mode)
        self._stop_live_button = QPushButton("Stop Live")
        self._stop_live_button.clicked.connect(self._stop_live_mode)
        self._stop_live_button.setEnabled(False)
        input_row.addWidget(self._input, stretch=1)
        input_row.addWidget(self._send_button)
        input_row.addWidget(self._record_button)
        input_row.addWidget(self._start_live_button)
        input_row.addWidget(self._stop_live_button)
        layout.addLayout(input_row)

        self._hint = QLabel("Tip: Start Ollama first (`ollama serve`) and ensure your model is pulled.")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._hint)

        self._refresh_status()

    def _refresh_status(self) -> None:
        state = self._session.state
        self._status.set_capture_status(
            mic=state.mic_enabled,
            system_audio=state.system_audio_enabled,
            screen=state.screen_enabled,
        )
        self._status.set_service_status("ready")

    def _apply_sources(self) -> None:
        self._session.update_sources(
            mic=self._mic_checkbox.isChecked(),
            system_audio=self._system_checkbox.isChecked(),
            screen=self._screen_checkbox.isChecked(),
        )
        self._refresh_status()

    def _send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return

        self._append("You", text)
        self._input.clear()

        self._status.set_service_status("thinking")
        self._send_button.setEnabled(False)
        self._record_button.setEnabled(False)
        self._apply_sources_button.setEnabled(False)

        try:
            reply = self._session.chat_once(text)
            self._append("Assistant", reply)
        except Exception as exc:
            QMessageBox.critical(self, "Assistant error", str(exc))
        finally:
            self._send_button.setEnabled(True)
            self._record_button.setEnabled(True)
            self._apply_sources_button.setEnabled(True)
            self._status.set_service_status("ready")

    def _start_live_mode(self) -> None:
        if self._live_thread is not None:
            return

        self._live_thread = QThread(self)
        self._live_worker = LivePracticeWorker(self._session)
        self._live_worker.moveToThread(self._live_thread)

        self._live_thread.started.connect(self._live_worker.run)
        self._live_worker.status.connect(self._status.set_service_status)
        self._live_worker.heard.connect(lambda text: self._append("You (live)", text))
        self._live_worker.replied.connect(lambda text: self._append("Assistant", text))
        self._live_worker.failed.connect(self._on_live_error)
        self._live_worker.stopped.connect(self._on_live_stopped)
        self._live_worker.stopped.connect(self._live_thread.quit)
        self._live_thread.finished.connect(self._on_live_thread_finished)
        self._live_thread.finished.connect(self._live_thread.deleteLater)
        self._live_worker.stopped.connect(self._live_worker.deleteLater)

        self._send_button.setEnabled(False)
        self._record_button.setEnabled(False)
        self._start_live_button.setEnabled(False)
        self._stop_live_button.setEnabled(True)
        self._apply_sources_button.setEnabled(False)

        self._live_thread.start()

    def _stop_live_mode(self) -> None:
        if self._live_worker is not None:
            self._live_worker.stop()
            self._stop_live_button.setEnabled(False)
        self._status.set_service_status("stopping")

    def _on_live_error(self, message: str) -> None:
        QMessageBox.critical(self, "Live mode error", message)

    def _on_live_stopped(self) -> None:
        self._send_button.setEnabled(True)
        self._record_button.setEnabled(True)
        self._start_live_button.setEnabled(True)
        self._stop_live_button.setEnabled(False)
        self._apply_sources_button.setEnabled(True)
        self._status.set_service_status("ready")

        self._live_worker = None

    def _on_live_thread_finished(self) -> None:
        self._live_thread = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_live_mode()
        if self._live_thread is not None:
            self._live_thread.quit()
            self._live_thread.wait(1500)
        super().closeEvent(event)

    def _record_and_send(self) -> None:
        self._status.set_service_status("recording")
        self._send_button.setEnabled(False)
        self._record_button.setEnabled(False)
        self._apply_sources_button.setEnabled(False)

        try:
            user_text, reply = self._session.record_and_chat(seconds=5.0)
            self._append("You (voice)", user_text)
            self._append("Assistant", reply)
        except Exception as exc:
            QMessageBox.critical(self, "Voice error", str(exc))
        finally:
            self._send_button.setEnabled(True)
            self._record_button.setEnabled(True)
            self._apply_sources_button.setEnabled(True)
            self._status.set_service_status("ready")

    def _append(self, speaker: str, text: str) -> None:
        self._conversation.append(f"<b>{speaker}:</b> {text}")
