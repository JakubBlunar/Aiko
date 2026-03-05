from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QThread, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.session_controller import SessionController
from app.core.settings import (
    AppSettings,
    apply_screen_ocr_profile,
    list_screen_ocr_profiles,
    normalize_screen_ocr_profile,
    save_runtime_preferences,
)
from app.ui.decision_trace_dialog import DecisionTraceDialog
from app.ui.memory_viewer_dialog import MemoryViewerDialog
from app.ui.live_worker import LivePracticeWorker
from app.ui.ocr_test_worker import OcrTestWorker
from app.ui.turn_worker import SingleTurnWorker
from app.ui.widgets.status_panel import StatusPanel


class MainWindow(QMainWindow):
    _STT_PROFILE_TO_MODEL: dict[str, str] = {
        "fast": "base",
        "accurate": "small",
    }

    def __init__(self, settings: AppSettings, session: SessionController | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._session = session or SessionController(settings)
        self._live_thread: QThread | None = None
        self._live_worker: LivePracticeWorker | None = None
        self._turn_thread: QThread | None = None
        self._turn_worker: SingleTurnWorker | None = None
        self._ocr_test_thread: QThread | None = None
        self._ocr_test_worker: OcrTestWorker | None = None
        self._turn_mode: str | None = None
        self._memory_dialog: MemoryViewerDialog | None = None
        self._trace_dialog: DecisionTraceDialog | None = None
        self._live_stream_buffer = ""
        self._live_stream_open = False
        self._stream_speaker = "Assistant"
        self._guardrail_controls_locked = False
        self._live_level_peak = 0.01
        self._live_noise_floor = 0.0
        self._wheel_guard_widgets: set[QWidget] = set()
        self._startup_greeting_done = False

        self.setWindowTitle(settings.assistant.name)
        self.resize(900, 640)
        if settings.ui.window_width and settings.ui.window_height:
            self.resize(max(640, settings.ui.window_width), max(480, settings.ui.window_height))
        if settings.ui.window_x is not None and settings.ui.window_y is not None:
            self.move(settings.ui.window_x, settings.ui.window_y)

        root = QWidget(self)
        self.setCentralWidget(root)

        layout = QVBoxLayout()
        root.setLayout(layout)

        tabs = QTabWidget()
        layout.addWidget(tabs, stretch=1)

        settings_tab = QWidget()
        settings_tab_layout = QVBoxLayout()
        settings_tab.setLayout(settings_tab_layout)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_panel = QWidget()
        left_layout = QVBoxLayout()
        settings_panel.setLayout(left_layout)
        settings_scroll.setWidget(settings_panel)
        settings_tab_layout.addWidget(settings_scroll)

        chat_tab = QWidget()
        chat_layout = QVBoxLayout()
        chat_tab.setLayout(chat_layout)

        tabs.addTab(settings_tab, "Settings")
        tabs.addTab(chat_tab, "Chat & Info")
        tabs.setCurrentIndex(1)

        self._status = StatusPanel()
        self._status.set_model(settings.ollama.chat_model)
        chat_layout.addWidget(self._status)
        self._model_debug_label = QLabel("Active Models: response=unknown | thinking=response")
        self._model_debug_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._model_debug_label)
        self._goal_debug_label = QLabel("Active Goal: unknown")
        self._goal_debug_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._goal_debug_label)
        self._tts_debug_label = QLabel("Active TTS: unknown")
        self._tts_debug_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._tts_debug_label)
        self._stt_debug_label = QLabel("Active STT: unknown")
        self._stt_debug_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._stt_debug_label)
        self._tts_model_status_label = QLabel("TTS Model: status=unknown | details=unavailable")
        self._tts_model_status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._tts_model_status_label)
        self._action_guardrail_label = QLabel("Actions: e-stop=inactive")
        self._action_guardrail_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._action_guardrail_label)
        self._latency_label = QLabel(
            "Latency: mode=idle | capture=0ms | stt=0ms | llm=0ms | tts=0ms | total=0ms"
        )
        self._latency_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._latency_label)
        self._latency_avg_label = QLabel(
            "Latency Avg(0): capture=0ms | stt=0ms | llm=0ms | tts=0ms | total=0ms"
        )
        self._latency_avg_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        chat_layout.addWidget(self._latency_avg_label)

        sources_group = QGroupBox("Sources & Devices")
        sources_layout = QVBoxLayout()
        sources_group.setLayout(sources_layout)

        capture_row = QHBoxLayout()
        self._mic_checkbox = QCheckBox("Microphone")
        self._mic_checkbox.setChecked(self._session.state.mic_enabled)
        self._system_checkbox = QCheckBox("System Audio")
        self._system_checkbox.setChecked(self._session.state.system_audio_enabled)
        self._screen_checkbox = QCheckBox("Screen Context")
        self._screen_checkbox.setChecked(self._session.state.screen_enabled)
        self._ocr_profile_combo = QComboBox()
        self._ocr_profile_combo.setMinimumWidth(110)
        self._ocr_profile_combo.currentIndexChanged.connect(self._on_ocr_profile_changed)
        self._memory_checkbox = QCheckBox("Remember Conversation")
        self._memory_checkbox.setChecked(self._session.remember_history)

        for widget in (
            self._mic_checkbox,
            self._system_checkbox,
            self._screen_checkbox,
            self._memory_checkbox,
        ):
            capture_row.addWidget(widget)

        capture_row.addWidget(QLabel("OCR Profile:"))
        capture_row.addWidget(self._ocr_profile_combo)
        capture_row.addStretch(1)
        sources_layout.addLayout(capture_row)

        self._mic_device_combo = QComboBox()
        self._mic_device_combo.setMinimumWidth(150)
        self._loopback_device_combo = QComboBox()
        self._loopback_device_combo.setMinimumWidth(150)

        devices_form = QFormLayout()
        devices_form.addRow("Mic Device:", self._mic_device_combo)
        devices_form.addRow("Loopback Device:", self._loopback_device_combo)
        sources_layout.addLayout(devices_form)

        self._refresh_devices_button = QPushButton("Refresh Devices")
        self._refresh_devices_button.clicked.connect(self._refresh_audio_devices)
        self._apply_sources_button = QPushButton("Apply Sources")
        self._apply_sources_button.clicked.connect(self._apply_sources)
        self._test_ocr_button = QPushButton("Test OCR")
        self._test_ocr_button.clicked.connect(self._test_ocr)

        source_actions_row = QHBoxLayout()
        source_actions_row.addWidget(self._refresh_devices_button)
        source_actions_row.addWidget(self._apply_sources_button)
        source_actions_row.addWidget(self._test_ocr_button)
        source_actions_row.addStretch(1)
        sources_layout.addLayout(source_actions_row)
        left_layout.addWidget(sources_group)

        models_group = QGroupBox("Models & Voice")
        models_layout = QVBoxLayout()
        models_group.setLayout(models_layout)

        controls_form = QFormLayout()
        self._personality_combo = QComboBox()
        self._personality_combo.setMinimumWidth(120)
        self._personality_combo.currentIndexChanged.connect(self._on_personality_changed)
        controls_form.addRow("Personality:", self._personality_combo)

        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(180)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        controls_form.addRow("Model:", self._model_combo)

        self._thinking_model_combo = QComboBox()
        self._thinking_model_combo.setMinimumWidth(180)
        self._thinking_model_combo.currentIndexChanged.connect(self._on_thinking_model_changed)
        controls_form.addRow("Thinking Model:", self._thinking_model_combo)

        self._tts_provider_combo = QComboBox()
        self._tts_provider_combo.setMinimumWidth(100)
        self._tts_provider_combo.currentIndexChanged.connect(self._on_tts_provider_changed)
        controls_form.addRow("TTS:", self._tts_provider_combo)

        self._tts_voice_combo = QComboBox()
        self._tts_voice_combo.setMinimumWidth(220)
        self._tts_voice_combo.currentIndexChanged.connect(self._on_tts_voice_changed)
        controls_form.addRow("Voice:", self._tts_voice_combo)

        self._stt_profile_combo = QComboBox()
        self._stt_profile_combo.setMinimumWidth(120)
        self._stt_profile_combo.currentIndexChanged.connect(self._on_stt_profile_changed)
        controls_form.addRow("STT Profile:", self._stt_profile_combo)
        models_layout.addLayout(controls_form)

        self._refresh_models_button = QPushButton("Refresh Models")
        self._refresh_models_button.clicked.connect(self._refresh_models)
        models_actions_row = QHBoxLayout()
        models_actions_row.addWidget(self._refresh_models_button)
        models_actions_row.addStretch(1)
        models_layout.addLayout(models_actions_row)
        left_layout.addWidget(models_group)

        memory_group = QGroupBox("Memory & Logs")
        memory_layout = QHBoxLayout()
        memory_group.setLayout(memory_layout)

        self._clear_memory_button = QPushButton("Clear Memory")
        self._clear_memory_button.clicked.connect(self._clear_memory)
        memory_layout.addWidget(self._clear_memory_button)

        self._memory_viewer_button = QPushButton("Memory Viewer")
        self._memory_viewer_button.clicked.connect(self._open_memory_viewer)
        memory_layout.addWidget(self._memory_viewer_button)

        self._trace_viewer_button = QPushButton("Action/Thinking Log")
        self._trace_viewer_button.clicked.connect(self._open_trace_viewer)
        memory_layout.addWidget(self._trace_viewer_button)
        memory_layout.addStretch(1)
        left_layout.addWidget(memory_group)

        audio_group = QGroupBox("Audio Calibration")
        audio_layout = QVBoxLayout()
        audio_group.setLayout(audio_layout)

        audio_controls_form = QFormLayout()
        self._vad_threshold_spin = QDoubleSpinBox()
        self._vad_threshold_spin.setDecimals(3)
        self._vad_threshold_spin.setRange(0.001, 0.500)
        self._vad_threshold_spin.setSingleStep(0.005)
        self._vad_threshold_spin.setValue(self._session.vad_level_threshold)
        audio_controls_form.addRow("VAD Threshold:", self._vad_threshold_spin)

        self._vad_silence_spin = QDoubleSpinBox()
        self._vad_silence_spin.setDecimals(1)
        self._vad_silence_spin.setRange(0.3, 6.0)
        self._vad_silence_spin.setSingleStep(0.1)
        self._vad_silence_spin.setValue(self._session.vad_silence_seconds)
        audio_controls_form.addRow("Silence Stop (s):", self._vad_silence_spin)
        audio_layout.addLayout(audio_controls_form)

        self._apply_calibration_button = QPushButton("Apply Calibration")
        self._apply_calibration_button.clicked.connect(self._apply_calibration)
        calibration_actions_row = QHBoxLayout()
        calibration_actions_row.addWidget(self._apply_calibration_button)
        calibration_actions_row.addStretch(1)
        audio_layout.addLayout(calibration_actions_row)

        input_level_row = QHBoxLayout()
        input_level_row.addWidget(QLabel("Input Level:"))
        self._input_level_bar = QProgressBar()
        self._input_level_bar.setRange(0, 100)
        self._input_level_bar.setValue(0)
        self._input_level_bar.setTextVisible(True)
        self._input_level_bar.setFormat("%p%")
        self._input_level_bar.setMinimumWidth(180)
        input_level_row.addWidget(self._input_level_bar)
        input_level_row.addStretch(1)
        audio_layout.addLayout(input_level_row)

        self._input_level_debug_label = QLabel("Mic raw=0.0000 | threshold=0.0000 | below")
        self._input_level_debug_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        audio_layout.addWidget(self._input_level_debug_label)
        left_layout.addWidget(audio_group)

        actions_group = QGroupBox("Action Guardrails")
        actions_layout = QVBoxLayout()
        actions_group.setLayout(actions_layout)

        actions_controls_form = QFormLayout()
        self._action_cooldown_spin = QDoubleSpinBox()
        self._action_cooldown_spin.setDecimals(1)
        self._action_cooldown_spin.setRange(0.0, 10.0)
        self._action_cooldown_spin.setSingleStep(0.1)
        self._action_cooldown_spin.setValue(self._session.action_min_interval_seconds)
        actions_controls_form.addRow("Action Cooldown (s):", self._action_cooldown_spin)
        actions_layout.addLayout(actions_controls_form)

        self._apply_guardrails_button = QPushButton("Apply Guardrails")
        self._apply_guardrails_button.clicked.connect(self._apply_guardrails)
        actions_grid = QGridLayout()
        actions_grid.addWidget(self._apply_guardrails_button, 0, 0)
        self._approve_action_button = QPushButton("Approve Action")
        self._approve_action_button.clicked.connect(self._approve_pending_action)
        actions_grid.addWidget(self._approve_action_button, 0, 1)
        self._reject_action_button = QPushButton("Reject Action")
        self._reject_action_button.clicked.connect(self._reject_pending_action)
        actions_grid.addWidget(self._reject_action_button, 0, 2)
        self._reset_estop_button = QPushButton("Reset E-Stop")
        self._reset_estop_button.clicked.connect(self._reset_emergency_stop)
        actions_grid.addWidget(self._reset_estop_button, 1, 0)
        self._reset_latency_button = QPushButton("Reset Latency")
        self._reset_latency_button.clicked.connect(self._reset_latency)
        actions_grid.addWidget(self._reset_latency_button, 1, 1)
        actions_layout.addLayout(actions_grid)
        left_layout.addWidget(actions_group)

        chat_layout.addWidget(QLabel("Conversation"))
        self._conversation = QTextEdit()
        self._conversation.setReadOnly(True)
        chat_layout.addWidget(self._conversation, stretch=1)

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
        chat_layout.addLayout(input_row)

        self._hint = QLabel(
            "Tip: Start Ollama first (`ollama serve`) and ensure your model is pulled. "
            "If using LLASA TTS, first run may take longer while Hugging Face models download."
        )
        self._hint.setAlignment(Qt.AlignmentFlag.AlignLeft)
        left_layout.addWidget(self._hint)
        left_layout.addStretch(1)

        self._refresh_status()
        self._refresh_ocr_profiles()
        self._refresh_audio_devices()
        self._refresh_personalities()
        self._refresh_models()
        self._refresh_tts_providers()
        self._refresh_tts_voices()
        self._refresh_stt_profiles()
        self._apply_calibration()
        self._refresh_latency_strip()
        self._refresh_model_debug_label()
        self._refresh_goal_debug_label()
        self._refresh_tts_debug_label()
        self._refresh_stt_debug_label()
        self._refresh_tts_model_status_label()
        if self._session.start_action_hotkey_listener():
            self._append("System", f"Global emergency hotkey active: {self._session.emergency_hotkey}")
        else:
            self._append(
                "System",
                (
                    "Global emergency hotkey could not be registered. "
                    f"Check config value: {self._session.emergency_hotkey}"
                ),
            )
        self._action_guardrail_timer = QTimer(self)
        self._action_guardrail_timer.setInterval(400)
        self._action_guardrail_timer.timeout.connect(self._refresh_action_guardrail_label)
        self._action_guardrail_timer.start()
        self._refresh_action_guardrail_label()
        self._setup_wheel_guard()
        QTimer.singleShot(250, self._play_startup_greeting)

    def _play_startup_greeting(self) -> None:
        if self._startup_greeting_done:
            return
        self._startup_greeting_done = True
        if self._live_thread is not None or self._turn_thread is not None:
            return
        greeting = self._session.build_startup_greeting()
        ok = self._session.speak_text(greeting)
        if ok:
            self._append("System", f"Startup greeting played: {greeting}")

    def _setup_wheel_guard(self) -> None:
        widgets: tuple[QWidget, ...] = (
            self._ocr_profile_combo,
            self._mic_device_combo,
            self._loopback_device_combo,
            self._personality_combo,
            self._model_combo,
            self._thinking_model_combo,
            self._tts_provider_combo,
            self._tts_voice_combo,
            self._stt_profile_combo,
            self._vad_threshold_spin,
            self._vad_silence_spin,
            self._action_cooldown_spin,
        )
        for widget in widgets:
            widget.installEventFilter(self)
            self._wheel_guard_widgets.add(widget)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched in self._wheel_guard_widgets and event.type() == QEvent.Type.Wheel:
            widget = watched if isinstance(watched, QWidget) else None
            while widget is not None:
                if isinstance(widget, QScrollArea):
                    scrollbar = widget.verticalScrollBar()
                    if scrollbar is not None:
                        delta_y = event.angleDelta().y()
                        if delta_y:
                            steps = int(delta_y / 120)
                            if steps != 0:
                                scrollbar.setValue(
                                    scrollbar.value() - (steps * max(1, scrollbar.singleStep()))
                                )
                            else:
                                scrollbar.setValue(
                                    scrollbar.value() - int(delta_y / 8)
                                )
                    return True
                widget = widget.parentWidget()
            return True
        return super().eventFilter(watched, event)

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
        thinking_model = self._thinking_model_combo.currentData()
        self._session.set_thinking_model(str(thinking_model) if thinking_model else None)
        self._session.set_tts_provider(str(self._tts_provider_combo.currentData() or "piper"))
        self._session.set_tts_voice(str(self._tts_voice_combo.currentData() or self._session.tts_voice))
        self._status.set_model(self._session.chat_model)
        self._refresh_model_debug_label()
        self._persist_preferences()
        self._refresh_status()

    def _clear_memory(self) -> None:
        self._session.clear_conversation_memory()
        self._append("System", "Conversation memory cleared.")

    def _open_memory_viewer(self) -> None:
        if self._memory_dialog is None:
            self._memory_dialog = MemoryViewerDialog(self._session, None)
            self._memory_dialog.finished.connect(self._on_memory_dialog_closed)
        self._memory_dialog.show()
        self._memory_dialog.raise_()
        self._memory_dialog.activateWindow()

    def _on_memory_dialog_closed(self) -> None:
        self._memory_dialog = None

    def _open_trace_viewer(self) -> None:
        if self._trace_dialog is None:
            self._trace_dialog = DecisionTraceDialog(self._session, None)
            self._trace_dialog.finished.connect(self._on_trace_dialog_closed)
        self._trace_dialog.show()
        self._trace_dialog.raise_()
        self._trace_dialog.activateWindow()

    def _on_trace_dialog_closed(self) -> None:
        self._trace_dialog = None

    def _test_ocr(self) -> None:
        if self._ocr_test_thread is not None:
            return

        self._test_ocr_button.setEnabled(False)
        self._test_ocr_button.setText("Test OCR (Running...)")
        self._status.set_service_status("ocr")

        self._ocr_test_thread = QThread(self)
        self._ocr_test_worker = OcrTestWorker(self._session)
        self._ocr_test_worker.moveToThread(self._ocr_test_thread)

        self._ocr_test_thread.started.connect(self._ocr_test_worker.run)
        self._ocr_test_worker.done.connect(self._on_test_ocr_done)
        self._ocr_test_worker.failed.connect(self._on_test_ocr_failed)
        self._ocr_test_worker.finished.connect(self._on_test_ocr_finished)
        self._ocr_test_worker.finished.connect(self._ocr_test_thread.quit)
        self._ocr_test_thread.finished.connect(self._on_test_ocr_thread_finished)
        self._ocr_test_thread.finished.connect(self._ocr_test_thread.deleteLater)
        self._ocr_test_worker.finished.connect(self._ocr_test_worker.deleteLater)
        self._ocr_test_thread.start()

    def _on_test_ocr_done(self, result: dict) -> None:
        ok = bool(result.get("ok", False))
        if not ok:
            QMessageBox.warning(
                self,
                "OCR diagnostic",
                str(result.get("message") or "OCR diagnostic failed."),
            )
            return

        chars = int(result.get("chars") or 0)
        lines = int(result.get("line_count") or 0)
        min_chars = int(result.get("min_chars") or 0)
        passes_min = bool(result.get("passes_min_chars", False))
        confidence = float(result.get("avg_confidence") or 0.0)
        text = str(result.get("text") or "")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("OCR diagnostic")
        box.setText(
            "Screen OCR capture succeeded.\n"
            f"Chars: {chars} | Lines: {lines} | Avg confidence: {confidence:.2f} | "
            f"Min chars pass: {'yes' if passes_min else f'no ({chars} < {min_chars})'}"
        )
        box.setDetailedText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

        preview = text[:300] + ("..." if len(text) > 300 else "")
        self._append("System", f"OCR test result: {preview}")

    def _on_test_ocr_failed(self, message: str) -> None:
        QMessageBox.warning(self, "OCR diagnostic", message or "OCR diagnostic failed.")

    def _on_test_ocr_finished(self) -> None:
        self._test_ocr_button.setText("Test OCR")
        self._test_ocr_button.setEnabled(True)
        self._status.set_service_status("ready")

    def _on_test_ocr_thread_finished(self) -> None:
        self._ocr_test_thread = None
        self._ocr_test_worker = None

    def _apply_calibration(self) -> None:
        self._session.set_vad_level_threshold(self._vad_threshold_spin.value())
        self._session.set_vad_silence_seconds(self._vad_silence_spin.value())
        self._persist_preferences()

    def _apply_guardrails(self) -> None:
        self._session.set_action_min_interval_seconds(self._action_cooldown_spin.value())
        self._persist_preferences()
        self._refresh_action_guardrail_label()

    def _reset_latency(self) -> None:
        self._session.reset_latency_metrics()
        self._refresh_latency_strip()

    def _reset_emergency_stop(self) -> None:
        self._session.reset_emergency_stop()
        self._append("System", "Emergency stop reset. Actions can run again if policy allows.")
        self._refresh_action_guardrail_label()

    def _approve_pending_action(self) -> None:
        message = self._session.approve_pending_action()
        self._append("System", message)
        self._refresh_action_guardrail_label()

    def _reject_pending_action(self) -> None:
        message = self._session.reject_pending_action()
        self._append("System", message)
        self._refresh_action_guardrail_label()

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

    def _refresh_ocr_profiles(self) -> None:
        current = normalize_screen_ocr_profile(self._settings.screen.ocr_profile)
        self._ocr_profile_combo.clear()
        for key in list_screen_ocr_profiles():
            self._ocr_profile_combo.addItem(key.title(), key)

        index = self._ocr_profile_combo.findData(current)
        if index < 0:
            index = self._ocr_profile_combo.findData("balanced")
        if index >= 0:
            self._ocr_profile_combo.setCurrentIndex(index)

    def _on_ocr_profile_changed(self) -> None:
        selected = normalize_screen_ocr_profile(self._ocr_profile_combo.currentData())
        if selected == normalize_screen_ocr_profile(self._settings.screen.ocr_profile):
            return
        apply_screen_ocr_profile(self._settings.screen, selected)
        self._persist_preferences()

    def _on_personality_changed(self) -> None:
        self._session.set_personality(str(self._personality_combo.currentData() or "friendly"))
        self._persist_preferences()

    def _refresh_models(self) -> None:
        current = self._session.chat_model
        current_thinking = self._session.thinking_model
        self._model_combo.clear()
        self._thinking_model_combo.clear()

        self._thinking_model_combo.addItem("Use response model", None)

        models = self._session.list_chat_models()
        for model_name in models:
            self._model_combo.addItem(model_name, model_name)
            self._thinking_model_combo.addItem(model_name, model_name)

        index = self._model_combo.findData(current)
        if index < 0 and self._model_combo.count() > 0:
            index = 0
        if index >= 0:
            self._model_combo.setCurrentIndex(index)

        thinking_index = self._thinking_model_combo.findData(current_thinking)
        if thinking_index < 0:
            thinking_index = 0
        self._thinking_model_combo.setCurrentIndex(thinking_index)

        self._status.set_model(self._session.chat_model)
        self._refresh_model_debug_label()

    def _on_model_changed(self) -> None:
        model_name = str(self._model_combo.currentData() or "").strip()
        if not model_name:
            return
        self._session.set_chat_model(model_name)
        self._status.set_model(model_name)
        self._refresh_model_debug_label()
        self._persist_preferences()

    def _on_thinking_model_changed(self) -> None:
        selected = self._thinking_model_combo.currentData()
        self._session.set_thinking_model(str(selected) if selected else None)
        self._refresh_model_debug_label()
        self._persist_preferences()

    def _refresh_tts_providers(self) -> None:
        current = self._session.tts_provider
        self._tts_provider_combo.clear()
        for provider in self._session.list_tts_providers():
            self._tts_provider_combo.addItem(provider.upper(), provider)

        index = self._tts_provider_combo.findData(current)
        if index < 0 and self._tts_provider_combo.count() > 0:
            index = 0
        if index >= 0:
            self._tts_provider_combo.setCurrentIndex(index)

    def _refresh_tts_voices(self) -> None:
        provider = self._session.tts_provider
        current = self._session.tts_voice
        self._tts_voice_combo.clear()

        if provider != "piper":
            self._tts_voice_combo.addItem("N/A (LLASA model-based)", current)
            self._tts_voice_combo.setEnabled(False)
            return

        voices = self._session.list_tts_voices()
        if not voices and current:
            voices = [current]

        for voice_path in voices:
            label = Path(voice_path).name
            self._tts_voice_combo.addItem(label, voice_path)

        if self._tts_voice_combo.count() == 0:
            self._tts_voice_combo.addItem("No .onnx voices found in models/", current)
            self._tts_voice_combo.setEnabled(False)
            return

        self._tts_voice_combo.setEnabled(True)
        index = self._tts_voice_combo.findData(current)
        if index < 0:
            index = 0
        self._tts_voice_combo.setCurrentIndex(index)

    def _on_tts_provider_changed(self) -> None:
        provider = str(self._tts_provider_combo.currentData() or "").strip().lower()
        if not provider:
            return
        if provider == self._session.tts_provider:
            return
        self._session.set_tts_provider(provider)
        self._refresh_tts_voices()
        self._refresh_tts_debug_label()
        self._persist_preferences()

    def _on_tts_voice_changed(self) -> None:
        if not self._tts_voice_combo.isEnabled():
            return
        selected_voice = str(self._tts_voice_combo.currentData() or "").strip()
        if not selected_voice:
            return
        if selected_voice == self._session.tts_voice:
            return
        self._session.set_tts_voice(selected_voice)
        self._persist_preferences()

    @classmethod
    def _stt_model_to_profile(cls, model_name: str) -> str:
        model = str(model_name or "").strip().lower()
        for profile, mapped_model in cls._STT_PROFILE_TO_MODEL.items():
            if model == mapped_model:
                return profile
        return "accurate"

    def _refresh_stt_profiles(self) -> None:
        current_profile = self._stt_model_to_profile(self._session.stt_model)
        self._stt_profile_combo.blockSignals(True)
        self._stt_profile_combo.clear()
        self._stt_profile_combo.addItem("Fast", "fast")
        self._stt_profile_combo.addItem("Accurate", "accurate")
        index = self._stt_profile_combo.findData(current_profile)
        if index < 0:
            index = self._stt_profile_combo.findData("accurate")
        if index >= 0:
            self._stt_profile_combo.setCurrentIndex(index)
        self._stt_profile_combo.blockSignals(False)

    def _on_stt_profile_changed(self) -> None:
        selected_profile = str(self._stt_profile_combo.currentData() or "accurate").strip().lower()
        target_model = self._STT_PROFILE_TO_MODEL.get(selected_profile, "small")
        ok = self._session.set_stt_model(target_model)
        if not ok:
            QMessageBox.warning(
                self,
                "STT profile",
                f"Could not switch STT model to {target_model}. Keeping current model.",
            )
            self._refresh_stt_profiles()
            return
        self._refresh_stt_debug_label()
        self._persist_preferences()

    def _refresh_model_debug_label(self) -> None:
        response_model = self._session.chat_model or "unknown"
        thinking_model = self._session.thinking_model or "response"
        self._model_debug_label.setText(
            f"Active Models: response={response_model} | thinking={thinking_model}"
        )

    def _refresh_goal_debug_label(self) -> None:
        self._goal_debug_label.setText(f"Active Goal: {self._session.active_goal}")

    def _refresh_tts_debug_label(self) -> None:
        self._tts_debug_label.setText(f"Active TTS: {self._session.tts_provider}")

    def _refresh_stt_debug_label(self) -> None:
        self._stt_debug_label.setText(f"Active STT: {self._session.stt_model}")

    def _refresh_tts_model_status_label(self) -> None:
        state, details = self._session.get_tts_model_status()
        self._tts_model_status_label.setText(f"TTS Model: status={state} | details={details}")

    def _refresh_action_guardrail_label(self) -> None:
        state = "ACTIVE" if self._session.emergency_stop_active else "inactive"
        self._action_guardrail_label.setText(
            (
                f"Actions: e-stop={state} | hotkey={self._session.emergency_hotkey} | "
                f"cooldown={round(self._session.action_min_interval_seconds, 2)}s | "
                f"pending={self._session.pending_action_description}"
            )
        )
        self._refresh_goal_debug_label()
        self._refresh_tts_debug_label()
        self._refresh_tts_model_status_label()
        if self._guardrail_controls_locked:
            self._reset_estop_button.setEnabled(False)
            self._approve_action_button.setEnabled(False)
            self._reject_action_button.setEnabled(False)
            return

        self._reset_estop_button.setEnabled(self._session.emergency_stop_active)
        has_pending = self._session.has_pending_action
        self._approve_action_button.setEnabled(has_pending)
        self._reject_action_button.setEnabled(has_pending)

    def _persist_preferences(self) -> None:
        save_runtime_preferences(
            chat_model=self._session.chat_model,
            thinking_model=self._session.thinking_model,
            personality=str(self._personality_combo.currentData() or "friendly"),
            remember_history=self._memory_checkbox.isChecked(),
            microphone_device=self._mic_device_combo.currentData(),
            loopback_device=self._loopback_device_combo.currentData(),
            vad_level_threshold=self._session.vad_level_threshold,
            vad_silence_seconds=self._session.vad_silence_seconds,
            action_min_interval_seconds=self._session.action_min_interval_seconds,
            tts_provider=self._session.tts_provider,
            tts_voice=self._session.tts_voice,
            stt_model=self._session.stt_model,
            enable_microphone=self._mic_checkbox.isChecked(),
            enable_system_audio=self._system_checkbox.isChecked(),
            enable_screen_context=self._screen_checkbox.isChecked(),
            screen_ocr_profile=str(self._ocr_profile_combo.currentData() or "balanced"),
            window_x=self.x(),
            window_y=self.y(),
            window_width=self.width(),
            window_height=self.height(),
        )

    def _send(self) -> None:
        if self._turn_thread is not None:
            return
        text = self._input.text().strip()
        if not text:
            return
        if self._live_thread is not None:
            self._append("System", "Stop Live mode before sending typed messages.")
            return

        self._append("You", text)
        self._input.clear()
        self._start_single_turn(mode="typed", text=text)

    def _set_single_turn_controls_busy(self, busy: bool) -> None:
        self._guardrail_controls_locked = busy
        self._send_button.setEnabled(not busy)
        self._record_button.setEnabled(not busy)
        self._start_live_button.setEnabled((not busy) and self._live_thread is None)
        self._apply_sources_button.setEnabled(not busy)
        self._clear_memory_button.setEnabled(not busy)
        self._memory_viewer_button.setEnabled(not busy)
        self._trace_viewer_button.setEnabled(not busy)
        self._test_ocr_button.setEnabled(not busy)
        self._refresh_devices_button.setEnabled(not busy)
        self._personality_combo.setEnabled(not busy)
        self._ocr_profile_combo.setEnabled(not busy)
        self._model_combo.setEnabled(not busy)
        self._thinking_model_combo.setEnabled(not busy)
        self._tts_provider_combo.setEnabled(not busy)
        self._tts_voice_combo.setEnabled((not busy) and self._session.tts_provider == "piper")
        self._stt_profile_combo.setEnabled(not busy)
        self._refresh_models_button.setEnabled(not busy)
        self._memory_checkbox.setEnabled(not busy)
        self._apply_calibration_button.setEnabled(not busy)
        self._apply_guardrails_button.setEnabled(not busy)
        self._approve_action_button.setEnabled((not busy) and self._session.has_pending_action)
        self._reject_action_button.setEnabled((not busy) and self._session.has_pending_action)
        self._reset_latency_button.setEnabled(not busy)

    def _start_single_turn(self, *, mode: str, text: str = "", record_seconds: float = 5.0) -> None:
        if self._turn_thread is not None:
            return

        self._turn_mode = mode
        self._stream_speaker = "Assistant"
        self._reset_live_stream("")
        self._set_single_turn_controls_busy(True)
        self._status.set_service_status("recording" if mode == "record" else "AI is generating response...")

        self._turn_thread = QThread(self)
        self._turn_worker = SingleTurnWorker(
            self._session,
            mode=mode,
            text=text,
            record_seconds=record_seconds,
        )
        self._turn_worker.moveToThread(self._turn_thread)

        self._turn_thread.started.connect(self._turn_worker.run)
        self._turn_worker.status.connect(self._status.set_service_status)
        self._turn_worker.replying.connect(self._append_live_stream_token)
        self._turn_worker.typed_done.connect(self._on_typed_turn_done)
        self._turn_worker.voice_done.connect(self._on_voice_turn_done)
        self._turn_worker.failed.connect(self._on_single_turn_failed)
        self._turn_worker.finished.connect(self._on_single_turn_finished)
        self._turn_worker.finished.connect(self._turn_thread.quit)
        self._turn_thread.finished.connect(self._on_single_turn_thread_finished)
        self._turn_thread.finished.connect(self._turn_thread.deleteLater)
        self._turn_worker.finished.connect(self._turn_worker.deleteLater)
        self._turn_thread.start()

    def _on_typed_turn_done(self, reply: str) -> None:
        if not self._live_stream_open:
            self._append("Assistant", reply)
        self._close_live_stream()
        self._refresh_latency_strip()
        self._refresh_goal_debug_label()

    def _on_voice_turn_done(self, user_text: str, reply: str) -> None:
        self._append("You (voice)", user_text)
        if not self._live_stream_open:
            self._append("Assistant", reply)
        self._close_live_stream()
        self._refresh_latency_strip()
        self._refresh_goal_debug_label()

    def _on_single_turn_failed(self, message: str) -> None:
        title = "Voice error" if self._turn_mode == "record" else "Assistant error"
        QMessageBox.critical(self, title, message)

    def _on_single_turn_finished(self) -> None:
        self._set_single_turn_controls_busy(False)
        self._status.set_service_status("ready")

    def _on_single_turn_thread_finished(self) -> None:
        self._turn_thread = None
        self._turn_worker = None
        self._turn_mode = None

    def _start_live_mode(self) -> None:
        if self._live_thread is not None:
            return

        self._guardrail_controls_locked = True
        self._live_level_peak = max(0.01, self._session.vad_level_threshold)
        self._stream_speaker = "Assistant (live)"

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
        self._trace_viewer_button.setEnabled(False)
        self._test_ocr_button.setEnabled(False)
        self._refresh_devices_button.setEnabled(False)
        self._personality_combo.setEnabled(False)
        self._ocr_profile_combo.setEnabled(False)
        self._model_combo.setEnabled(False)
        self._thinking_model_combo.setEnabled(False)
        self._tts_provider_combo.setEnabled(False)
        self._tts_voice_combo.setEnabled(False)
        self._stt_profile_combo.setEnabled(False)
        self._refresh_models_button.setEnabled(False)
        self._memory_checkbox.setEnabled(False)
        self._apply_calibration_button.setEnabled(False)
        self._apply_guardrails_button.setEnabled(False)
        self._approve_action_button.setEnabled(False)
        self._reject_action_button.setEnabled(False)
        self._reset_latency_button.setEnabled(False)

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
        self._trace_viewer_button.setEnabled(True)
        self._test_ocr_button.setEnabled(True)
        self._refresh_devices_button.setEnabled(True)
        self._personality_combo.setEnabled(True)
        self._ocr_profile_combo.setEnabled(True)
        self._model_combo.setEnabled(True)
        self._thinking_model_combo.setEnabled(True)
        self._tts_provider_combo.setEnabled(True)
        self._tts_voice_combo.setEnabled(self._session.tts_provider == "piper")
        self._stt_profile_combo.setEnabled(True)
        self._refresh_models_button.setEnabled(True)
        self._memory_checkbox.setEnabled(True)
        self._apply_calibration_button.setEnabled(True)
        self._apply_guardrails_button.setEnabled(True)
        self._approve_action_button.setEnabled(self._session.has_pending_action)
        self._reject_action_button.setEnabled(self._session.has_pending_action)
        self._reset_latency_button.setEnabled(True)
        self._status.set_service_status("ready")
        self._close_live_stream()
        self._input_level_bar.setValue(0)
        self._input_level_debug_label.setText(
            f"Mic raw=0.0000 | threshold={self._session.vad_level_threshold:.4f} | below"
        )

        self._live_worker = None
        self._guardrail_controls_locked = False

    def _on_live_thread_finished(self) -> None:
        self._live_thread = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self._persist_preferences()
        self._stop_live_mode()
        self._session.stop_action_hotkey_listener()
        if self._turn_thread is not None:
            self._turn_thread.quit()
            self._turn_thread.wait(1500)
        if self._ocr_test_thread is not None:
            self._ocr_test_thread.quit()
            self._ocr_test_thread.wait(1500)
        if self._live_thread is not None:
            self._live_thread.quit()
            self._live_thread.wait(1500)
        super().closeEvent(event)

    def _record_and_send(self) -> None:
        if self._turn_thread is not None:
            return
        if self._live_thread is not None:
            self._append("System", "Stop Live mode before using Record 5s.")
            return
        self._start_single_turn(mode="record", record_seconds=5.0)

    def _append(self, speaker: str, text: str) -> None:
        self._conversation.append(f"<b>{speaker}:</b> {text}")
        self._scroll_conversation_to_bottom()

    def _scroll_conversation_to_bottom(self) -> None:
        self._conversation.moveCursor(QTextCursor.MoveOperation.End)
        self._conversation.ensureCursorVisible()

    def _reset_live_stream(self, _text: str) -> None:
        if self._live_stream_open:
            self._close_live_stream()
        self._live_stream_buffer = ""
        self._live_stream_open = False

    def _append_live_stream_token(self, token: str) -> None:
        token = token or ""
        if not token:
            return
        if not self._live_stream_open:
            self._conversation.append(f"<b>{self._stream_speaker}:</b> ")
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
        self._refresh_goal_debug_label()

    def _close_live_stream(self, _text: str | None = None) -> None:
        if self._live_stream_open:
            self._scroll_conversation_to_bottom()
        self._live_stream_open = False

    def _on_live_audio_level(self, level: float) -> None:
        threshold = max(self._session.vad_level_threshold, 1e-6)
        if level < threshold:
            self._live_noise_floor = (self._live_noise_floor * 0.95) + (level * 0.05)

        adjusted_level = max(0.0, level - self._live_noise_floor)
        self._live_level_peak = max(adjusted_level, self._live_level_peak * 0.97)
        adjusted_threshold = max(1e-6, threshold - self._live_noise_floor)
        deadband = adjusted_threshold * 0.25
        if adjusted_level <= deadband:
            normalized = 0.0
        else:
            effective_level = adjusted_level - deadband
            scale_reference = max(adjusted_threshold * 1.2, self._live_level_peak * 0.45, 1e-6)
            normalized = max(0.0, min(effective_level / scale_reference, 1.0))

        percent = int(normalized * 100)
        self._input_level_bar.setValue(percent)
        self._input_level_bar.setFormat(f"{percent}%")
        state = "ABOVE" if level >= threshold else "below"
        self._input_level_debug_label.setText(
            f"Mic raw={level:.4f} | floor={self._live_noise_floor:.4f} | threshold={threshold:.4f} | {state}"
        )

    def _refresh_latency_strip(self) -> None:
        metrics = self._session.get_last_metrics()
        averages = self._session.get_average_metrics()
        self._latency_label.setText(
            "Latency: "
            f"mode={metrics.get('mode', 'unknown')} | "
            f"capture={metrics.get('capture_ms', 0)}ms | "
            f"stt={metrics.get('stt_ms', 0)}ms | "
            f"llm={metrics.get('llm_ms', 0)}ms | "
            f"tts={metrics.get('tts_ms', 0)}ms | "
            f"total={metrics.get('total_ms', 0)}ms"
        )
        self._latency_avg_label.setText(
            f"Latency Avg({averages.get('window', 0)}): "
            f"capture={averages.get('capture_ms', 0)}ms | "
            f"stt={averages.get('stt_ms', 0)}ms | "
            f"llm={averages.get('llm_ms', 0)}ms | "
            f"tts={averages.get('tts_ms', 0)}ms | "
            f"total={averages.get('total_ms', 0)}ms"
        )
