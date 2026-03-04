from __future__ import annotations

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.session_controller import SessionController
from app.core.settings import AppSettings, save_runtime_preferences
from app.ui.memory_viewer_dialog import MemoryViewerDialog
from app.ui.live_worker import LivePracticeWorker
from app.ui.widgets.status_panel import StatusPanel


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings
        self._session = SessionController(settings)
        self._live_thread: QThread | None = None
        self._live_worker: LivePracticeWorker | None = None
        self._live_stream_buffer = ""
        self._live_stream_open = False

        self.setWindowTitle(settings.assistant.name)
        self.resize(900, 640)

        root = QWidget(self)
        self.setCentralWidget(root)

        layout = QVBoxLayout()
        root.setLayout(layout)

        self._status = StatusPanel()
        self._status.set_model(settings.ollama.chat_model)
        layout.addWidget(self._status)
        self._latency_label = QLabel(
            "Latency: mode=idle | capture=0ms | stt=0ms | llm=0ms | tts=0ms | total=0ms"
        )
        self._latency_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._latency_label)

        capture_row = QHBoxLayout()
        self._mic_checkbox = QCheckBox("Microphone")
        self._mic_checkbox.setChecked(self._session.state.mic_enabled)
        self._system_checkbox = QCheckBox("System Audio")
        self._system_checkbox.setChecked(self._session.state.system_audio_enabled)
        self._screen_checkbox = QCheckBox("Screen Context")
        self._screen_checkbox.setChecked(self._session.state.screen_enabled)
        self._memory_checkbox = QCheckBox("Remember Conversation")
        self._memory_checkbox.setChecked(self._session.remember_history)

        for widget in (
            self._mic_checkbox,
            self._system_checkbox,
            self._screen_checkbox,
            self._memory_checkbox,
        ):
            capture_row.addWidget(widget)

        self._mic_device_combo = QComboBox()
        self._mic_device_combo.setMinimumWidth(220)
        capture_row.addWidget(QLabel("Mic Device:"))
        capture_row.addWidget(self._mic_device_combo)

        self._loopback_device_combo = QComboBox()
        self._loopback_device_combo.setMinimumWidth(220)
        capture_row.addWidget(QLabel("Loopback Device:"))
        capture_row.addWidget(self._loopback_device_combo)

        self._refresh_devices_button = QPushButton("Refresh Devices")
        self._refresh_devices_button.clicked.connect(self._refresh_audio_devices)
        capture_row.addWidget(self._refresh_devices_button)

        capture_row.addWidget(QLabel("Personality:"))
        self._personality_combo = QComboBox()
        self._personality_combo.setMinimumWidth(140)
        self._personality_combo.currentIndexChanged.connect(self._on_personality_changed)
        capture_row.addWidget(self._personality_combo)

        capture_row.addWidget(QLabel("Model:"))
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(260)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        capture_row.addWidget(self._model_combo)

        self._refresh_models_button = QPushButton("Refresh Models")
        self._refresh_models_button.clicked.connect(self._refresh_models)
        capture_row.addWidget(self._refresh_models_button)

        self._apply_sources_button = QPushButton("Apply Sources")
        self._apply_sources_button.clicked.connect(self._apply_sources)
        capture_row.addWidget(self._apply_sources_button)

        self._clear_memory_button = QPushButton("Clear Memory")
        self._clear_memory_button.clicked.connect(self._clear_memory)
        capture_row.addWidget(self._clear_memory_button)

        self._memory_viewer_button = QPushButton("Memory Viewer")
        self._memory_viewer_button.clicked.connect(self._open_memory_viewer)
        capture_row.addWidget(self._memory_viewer_button)
        capture_row.addStretch(1)
        layout.addLayout(capture_row)

        calibration_row = QHBoxLayout()
        calibration_row.addWidget(QLabel("VAD Threshold:"))
        self._vad_threshold_spin = QDoubleSpinBox()
        self._vad_threshold_spin.setDecimals(3)
        self._vad_threshold_spin.setRange(0.001, 0.500)
        self._vad_threshold_spin.setSingleStep(0.005)
        self._vad_threshold_spin.setValue(self._session.vad_level_threshold)
        calibration_row.addWidget(self._vad_threshold_spin)

        calibration_row.addWidget(QLabel("Silence Stop (s):"))
        self._vad_silence_spin = QDoubleSpinBox()
        self._vad_silence_spin.setDecimals(1)
        self._vad_silence_spin.setRange(0.2, 3.0)
        self._vad_silence_spin.setSingleStep(0.1)
        self._vad_silence_spin.setValue(self._session.vad_silence_seconds)
        calibration_row.addWidget(self._vad_silence_spin)

        calibration_row.addWidget(QLabel("Input Level:"))
        self._input_level_bar = QProgressBar()
        self._input_level_bar.setRange(0, 100)
        self._input_level_bar.setValue(0)
        self._input_level_bar.setTextVisible(True)
        self._input_level_bar.setFormat("%p%")
        self._input_level_bar.setMinimumWidth(180)
        calibration_row.addWidget(self._input_level_bar)

        self._apply_calibration_button = QPushButton("Apply Calibration")
        self._apply_calibration_button.clicked.connect(self._apply_calibration)
        calibration_row.addWidget(self._apply_calibration_button)
        calibration_row.addStretch(1)
        layout.addLayout(calibration_row)

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
        self._refresh_audio_devices()
        self._refresh_personalities()
        self._refresh_models()
        self._apply_calibration()

    def _refresh_status(self) -> None:
        state = self._session.state
        self._status.set_capture_status(
            mic=state.mic_enabled,
            system_audio=state.system_audio_enabled,
            screen=state.screen_enabled,
        )
        self._status.set_service_status("ready")

    def _apply_sources(self) -> None:
        mic_device = self._mic_device_combo.currentData()
        loopback_device = self._loopback_device_combo.currentData()

        self._session.set_microphone_device(mic_device)
        self._session.set_loopback_device(loopback_device)
        self._session.update_sources(
            mic=self._mic_checkbox.isChecked(),
            system_audio=self._system_checkbox.isChecked(),
            screen=self._screen_checkbox.isChecked(),
        )
        self._session.set_remember_history(self._memory_checkbox.isChecked())
        self._session.set_personality(str(self._personality_combo.currentData() or "friendly"))
        self._session.set_chat_model(str(self._model_combo.currentData() or self._session.chat_model))
        self._status.set_model(self._session.chat_model)
        self._persist_preferences()
        self._refresh_status()

    def _clear_memory(self) -> None:
        self._session.clear_conversation_memory()
        self._append("System", "Conversation memory cleared.")

    def _open_memory_viewer(self) -> None:
        dialog = MemoryViewerDialog(self._session, self)
        dialog.exec()

    def _apply_calibration(self) -> None:
        self._session.set_vad_level_threshold(self._vad_threshold_spin.value())
        self._session.set_vad_silence_seconds(self._vad_silence_spin.value())
        self._persist_preferences()

    def _refresh_audio_devices(self) -> None:
        current_mic = self._mic_device_combo.currentData()
        current_loopback = self._loopback_device_combo.currentData()
        if current_mic is None:
            current_mic = self._session.microphone_device
        if current_loopback is None:
            current_loopback = self._session.loopback_device

        self._mic_device_combo.clear()
        self._loopback_device_combo.clear()

        self._mic_device_combo.addItem("Default", None)
        self._loopback_device_combo.addItem("Auto", None)

        for index, name in self._session.list_microphone_devices():
            self._mic_device_combo.addItem(f"{index}: {name}", index)

        for index, name in self._session.list_loopback_devices():
            self._loopback_device_combo.addItem(f"{index}: {name}", index)

        mic_index = self._mic_device_combo.findData(current_mic)
        if mic_index >= 0:
            self._mic_device_combo.setCurrentIndex(mic_index)

        loopback_index = self._loopback_device_combo.findData(current_loopback)
        if loopback_index >= 0:
            self._loopback_device_combo.setCurrentIndex(loopback_index)

    def _refresh_personalities(self) -> None:
        current = self._session.personality
        self._personality_combo.clear()
        for key in self._session.list_personalities():
            self._personality_combo.addItem(key.title(), key)

        index = self._personality_combo.findData(current)
        if index < 0:
            index = self._personality_combo.findData("friendly")
        if index >= 0:
            self._personality_combo.setCurrentIndex(index)

    def _on_personality_changed(self) -> None:
        self._session.set_personality(str(self._personality_combo.currentData() or "friendly"))
        self._persist_preferences()

    def _refresh_models(self) -> None:
        current = self._session.chat_model
        self._model_combo.clear()

        models = self._session.list_chat_models()
        for model_name in models:
            self._model_combo.addItem(model_name, model_name)

        index = self._model_combo.findData(current)
        if index < 0 and self._model_combo.count() > 0:
            index = 0
        if index >= 0:
            self._model_combo.setCurrentIndex(index)

        self._status.set_model(self._session.chat_model)

    def _on_model_changed(self) -> None:
        model_name = str(self._model_combo.currentData() or "").strip()
        if not model_name:
            return
        self._session.set_chat_model(model_name)
        self._status.set_model(model_name)
        self._persist_preferences()

    def _persist_preferences(self) -> None:
        save_runtime_preferences(
            chat_model=self._session.chat_model,
            personality=str(self._personality_combo.currentData() or "friendly"),
            remember_history=self._memory_checkbox.isChecked(),
            microphone_device=self._mic_device_combo.currentData(),
            loopback_device=self._loopback_device_combo.currentData(),
            vad_level_threshold=self._session.vad_level_threshold,
            vad_silence_seconds=self._session.vad_silence_seconds,
            enable_microphone=self._mic_checkbox.isChecked(),
            enable_system_audio=self._system_checkbox.isChecked(),
            enable_screen_context=self._screen_checkbox.isChecked(),
        )

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
            self._refresh_latency_strip()
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
        self._live_worker.level.connect(self._on_live_audio_level)
        self._live_worker.heard.connect(lambda text: self._append("You (live)", text))
        self._live_worker.heard.connect(self._reset_live_stream)
        self._live_worker.replying.connect(self._append_live_stream_token)
        self._live_worker.replied.connect(self._on_live_replied)
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
        self._clear_memory_button.setEnabled(False)
        self._memory_viewer_button.setEnabled(False)
        self._refresh_devices_button.setEnabled(False)
        self._personality_combo.setEnabled(False)
        self._model_combo.setEnabled(False)
        self._refresh_models_button.setEnabled(False)
        self._memory_checkbox.setEnabled(False)
        self._apply_calibration_button.setEnabled(False)

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
        self._clear_memory_button.setEnabled(True)
        self._memory_viewer_button.setEnabled(True)
        self._refresh_devices_button.setEnabled(True)
        self._personality_combo.setEnabled(True)
        self._model_combo.setEnabled(True)
        self._refresh_models_button.setEnabled(True)
        self._memory_checkbox.setEnabled(True)
        self._apply_calibration_button.setEnabled(True)
        self._status.set_service_status("ready")
        self._close_live_stream()
        self._input_level_bar.setValue(0)

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
        self._clear_memory_button.setEnabled(False)
        self._memory_viewer_button.setEnabled(False)
        self._refresh_devices_button.setEnabled(False)
        self._personality_combo.setEnabled(False)
        self._model_combo.setEnabled(False)
        self._refresh_models_button.setEnabled(False)
        self._memory_checkbox.setEnabled(False)
        self._apply_calibration_button.setEnabled(False)

        try:
            user_text, reply = self._session.record_and_chat(seconds=5.0)
            self._append("You (voice)", user_text)
            self._append("Assistant", reply)
            self._refresh_latency_strip()
        except Exception as exc:
            QMessageBox.critical(self, "Voice error", str(exc))
        finally:
            self._send_button.setEnabled(True)
            self._record_button.setEnabled(True)
            self._apply_sources_button.setEnabled(True)
            self._clear_memory_button.setEnabled(True)
            self._memory_viewer_button.setEnabled(True)
            self._refresh_devices_button.setEnabled(True)
            self._personality_combo.setEnabled(True)
            self._model_combo.setEnabled(True)
            self._refresh_models_button.setEnabled(True)
            self._memory_checkbox.setEnabled(True)
            self._apply_calibration_button.setEnabled(True)
            self._status.set_service_status("ready")

    def _append(self, speaker: str, text: str) -> None:
        self._conversation.append(f"<b>{speaker}:</b> {text}")

    def _reset_live_stream(self, _text: str) -> None:
        self._live_stream_buffer = ""
        self._live_stream_open = False

    def _append_live_stream_token(self, token: str) -> None:
        token = token or ""
        if not token:
            return
        if not self._live_stream_open:
            self._conversation.append("<b>Assistant (stream):</b> ")
            self._live_stream_open = True

        self._live_stream_buffer += token
        self._conversation.moveCursor(QTextCursor.MoveOperation.End)
        self._conversation.insertPlainText(token)
        self._conversation.ensureCursorVisible()

    def _on_live_replied(self, text: str) -> None:
        if not self._live_stream_open:
            self._append("Assistant", text)
        self._close_live_stream()
        self._refresh_latency_strip()

    def _close_live_stream(self, _text: str | None = None) -> None:
        if self._live_stream_open:
            self._conversation.append("")
        self._live_stream_open = False

    def _on_live_audio_level(self, level: float) -> None:
        normalized = max(0.0, min(level / max(self._session.vad_level_threshold * 2.0, 1e-6), 1.0))
        self._input_level_bar.setValue(int(normalized * 100))

    def _refresh_latency_strip(self) -> None:
        metrics = self._session.get_last_metrics()
        self._latency_label.setText(
            "Latency: "
            f"mode={metrics.get('mode', 'unknown')} | "
            f"capture={metrics.get('capture_ms', 0)}ms | "
            f"stt={metrics.get('stt_ms', 0)}ms | "
            f"llm={metrics.get('llm_ms', 0)}ms | "
            f"tts={metrics.get('tts_ms', 0)}ms | "
            f"total={metrics.get('total_ms', 0)}ms"
        )
