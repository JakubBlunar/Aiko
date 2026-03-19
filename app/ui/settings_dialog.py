"""Minimal settings dialog for S2S assistant."""
from __future__ import annotations

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.session_controller import SessionController


class SettingsDialog(QDialog):
    def __init__(self, session: SessionController, parent=None) -> None:
        super().__init__(parent)
        self._session = session
        self.setWindowTitle("Settings")
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_model_tab(), "Model")
        self._tabs.addTab(self._build_audio_tab(), "Audio")
        layout.addWidget(self._tabs)

        layout.addWidget(QLabel(""))
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _build_model_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(200)
        current = self._session.chat_model
        self._model_combo.addItem(current or "Loading...", current)
        form.addRow("Model:", self._model_combo)
        btn_row = QHBoxLayout()
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.clicked.connect(self._clear_history)
        btn_row.addWidget(self._clear_btn)
        btn_row.addStretch()
        form.addRow("", btn_row)
        self._load_models_async()
        return widget

    def _load_models_async(self) -> None:
        def _fetch() -> None:
            models = self._session.list_chat_models()
            self._fetched_models = models
        self._fetched_models: list[str] = []
        threading.Thread(target=_fetch, daemon=True, name="fetch-models").start()
        self._model_poll = QTimer(self)
        self._model_poll.setInterval(100)
        self._model_poll.timeout.connect(self._populate_models)
        self._model_poll.start()

    def _populate_models(self) -> None:
        models = getattr(self, "_fetched_models", None)
        if not models:
            return
        self._model_poll.stop()
        current = self._session.chat_model
        self._model_combo.clear()
        for name in models:
            self._model_combo.addItem(name, name)
        idx = self._model_combo.findData(current)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)

    def _build_audio_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form = QFormLayout()
        self._mic_checkbox = QCheckBox("Microphone enabled")
        self._mic_checkbox.setChecked(self._session.state.mic_enabled)
        form.addRow("", self._mic_checkbox)

        self._input_device_combo = QComboBox()
        self._input_device_combo.setMinimumWidth(220)
        self._refresh_input_devices()
        form.addRow("Input device:", self._input_device_combo)

        self._output_device_combo = QComboBox()
        self._output_device_combo.setMinimumWidth(220)
        self._refresh_output_devices()
        form.addRow("Output device:", self._output_device_combo)

        self._voice_combo = QComboBox()
        self._voice_combo.setMinimumWidth(180)
        for v in self._session.list_tts_voices():
            self._voice_combo.addItem(v, v)
        idx = self._voice_combo.findData(self._session.tts_voice) or self._voice_combo.findText(self._session.tts_voice)
        if idx >= 0:
            self._voice_combo.setCurrentIndex(idx)
        form.addRow("TTS voice:", self._voice_combo)
        layout.addLayout(form)

        live_group = QGroupBox("Live input")
        live_layout = QFormLayout(live_group)
        self._live_input_combo = QComboBox()
        self._live_input_combo.addItem("Voice detection", "voice_detection")
        self._live_input_combo.addItem("Push-to-talk", "push_to_talk")
        live_input_mode = getattr(self._session, "live_input_mode", None) or "voice_detection"
        idx = self._live_input_combo.findData(live_input_mode)
        if idx >= 0:
            self._live_input_combo.setCurrentIndex(idx)
        live_layout.addRow("Mode:", self._live_input_combo)

        self._ptt_type_keyboard = QRadioButton("Keyboard")
        self._ptt_type_mouse = QRadioButton("Mouse")
        ptt_type_layout = QHBoxLayout()
        ptt_type_layout.addWidget(self._ptt_type_keyboard)
        ptt_type_layout.addWidget(self._ptt_type_mouse)
        ptt_type_layout.addStretch()
        live_layout.addRow("PTT trigger:", ptt_type_layout)
        ptt_type = getattr(self._session, "live_ptt_type", None) or "keyboard"
        if ptt_type == "mouse":
            self._ptt_type_mouse.setChecked(True)
        else:
            self._ptt_type_keyboard.setChecked(True)

        self._ptt_key_combo = QComboBox()
        self._ptt_key_combo.setEditable(True)
        for key_name in ("F2", "F3", "F4", "Space", "Ctrl+Space"):
            self._ptt_key_combo.addItem(key_name, key_name)
        ptt_key = getattr(self._session, "live_ptt_key", None) or "F2"
        idx = self._ptt_key_combo.findData(ptt_key)
        if idx >= 0:
            self._ptt_key_combo.setCurrentIndex(idx)
        else:
            self._ptt_key_combo.setCurrentText(ptt_key)
        live_layout.addRow("PTT key:", self._ptt_key_combo)

        self._ptt_mouse_combo = QComboBox()
        for btn in ("Left", "Middle", "Right"):
            self._ptt_mouse_combo.addItem(btn, btn.lower())
        ptt_mouse = getattr(self._session, "live_ptt_mouse_button", None) or "right"
        if isinstance(ptt_mouse, int):
            ptt_mouse = "left" if ptt_mouse == 1 else "right" if ptt_mouse == 2 else "middle"
        idx = self._ptt_mouse_combo.findData(str(ptt_mouse).lower())
        if idx >= 0:
            self._ptt_mouse_combo.setCurrentIndex(idx)
        live_layout.addRow("PTT mouse button:", self._ptt_mouse_combo)

        self._ptt_toggle_checkbox = QCheckBox("Toggle (press to start, press again to stop)")
        self._ptt_toggle_checkbox.setChecked(bool(getattr(self._session, "live_ptt_toggle", False)))
        live_layout.addRow("", self._ptt_toggle_checkbox)

        self._barge_in_checkbox = QCheckBox("Allow barge-in (interrupt while assistant is speaking)")
        self._barge_in_checkbox.setChecked(bool(getattr(self._session, "barge_in_enabled", lambda: False)()))
        live_layout.addRow("", self._barge_in_checkbox)

        self._live_input_combo.currentIndexChanged.connect(self._on_live_input_mode_changed)
        self._ptt_type_keyboard.toggled.connect(self._on_live_input_mode_changed)
        self._ptt_type_mouse.toggled.connect(self._on_live_input_mode_changed)
        self._on_live_input_mode_changed()
        layout.addWidget(live_group)
        layout.addStretch()
        return widget

    def _refresh_input_devices(self) -> None:
        self._input_device_combo.clear()
        devices = self._session.list_microphone_devices()
        self._input_device_combo.addItem("(Default)", None)
        current = self._session.microphone_device
        for idx, name in devices:
            self._input_device_combo.addItem(name, idx)
            if idx == current:
                self._input_device_combo.setCurrentIndex(self._input_device_combo.count() - 1)
        if self._input_device_combo.currentData() is None and current is not None:
            for i in range(1, self._input_device_combo.count()):
                if self._input_device_combo.itemData(i) == current:
                    self._input_device_combo.setCurrentIndex(i)
                    break

    def _refresh_output_devices(self) -> None:
        self._output_device_combo.clear()
        if hasattr(self._session, "list_output_devices"):
            devices = self._session.list_output_devices()
        else:
            devices = []
        self._output_device_combo.addItem("(Default)", None)
        current = getattr(self._session, "output_device", None)
        for idx, name in devices:
            self._output_device_combo.addItem(name, idx)
            if idx == current:
                self._output_device_combo.setCurrentIndex(self._output_device_combo.count() - 1)
        if self._output_device_combo.currentData() is None and current is not None:
            for i in range(1, self._output_device_combo.count()):
                if self._output_device_combo.itemData(i) == current:
                    self._output_device_combo.setCurrentIndex(i)
                    break

    def _on_live_input_mode_changed(self) -> None:
        is_ptt = (self._live_input_combo.currentData() or "") == "push_to_talk"
        self._ptt_type_keyboard.setEnabled(is_ptt)
        self._ptt_type_mouse.setEnabled(is_ptt)
        self._ptt_key_combo.setEnabled(is_ptt and self._ptt_type_keyboard.isChecked())
        self._ptt_mouse_combo.setEnabled(is_ptt and self._ptt_type_mouse.isChecked())
        self._ptt_toggle_checkbox.setEnabled(is_ptt)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._tabs.currentWidget():
            self._refresh_devices_async()

    def _refresh_devices_async(self) -> None:
        def _fetch() -> None:
            self._fetched_input_devices = self._session.list_microphone_devices()
            self._fetched_output_devices = self._session.list_output_devices()
        self._fetched_input_devices: list[tuple[int, str]] | None = None
        self._fetched_output_devices: list[tuple[int, str]] | None = None
        threading.Thread(target=_fetch, daemon=True, name="fetch-devices").start()
        if not hasattr(self, "_device_poll"):
            self._device_poll = QTimer(self)
            self._device_poll.setInterval(100)
            self._device_poll.timeout.connect(self._populate_devices)
        self._device_poll.start()

    def _populate_devices(self) -> None:
        if self._fetched_input_devices is None or self._fetched_output_devices is None:
            return
        self._device_poll.stop()
        self._populate_input_devices(self._fetched_input_devices)
        self._populate_output_devices(self._fetched_output_devices)

    def _populate_input_devices(self, devices: list[tuple[int, str]]) -> None:
        self._input_device_combo.clear()
        self._input_device_combo.addItem("(Default)", None)
        current = self._session.microphone_device
        for idx, name in devices:
            self._input_device_combo.addItem(name, idx)
            if idx == current:
                self._input_device_combo.setCurrentIndex(self._input_device_combo.count() - 1)

    def _populate_output_devices(self, devices: list[tuple[int, str]]) -> None:
        self._output_device_combo.clear()
        self._output_device_combo.addItem("(Default)", None)
        current = getattr(self._session, "output_device", None)
        for idx, name in devices:
            self._output_device_combo.addItem(name, idx)
            if idx == current:
                self._output_device_combo.setCurrentIndex(self._output_device_combo.count() - 1)

    def _clear_history(self) -> None:
        self._session.clear_conversation_memory()

    def accept(self) -> None:
        self._session.set_chat_model(str(self._model_combo.currentData() or self._session.chat_model))
        self._session.update_sources(mic=self._mic_checkbox.isChecked())
        self._session.set_tts_voice(str(self._voice_combo.currentData() or self._voice_combo.currentText() or self._session.tts_voice))
        in_dev = self._input_device_combo.currentData()
        self._session.set_microphone_device(in_dev)
        if hasattr(self._session, "set_output_device"):
            self._session.set_output_device(self._output_device_combo.currentData())
        if hasattr(self._session, "set_live_input_mode"):
            self._session.set_live_input_mode(str(self._live_input_combo.currentData() or "voice_detection"))
        if hasattr(self._session, "set_live_ptt_type"):
            ptt_type = "mouse" if self._ptt_type_mouse.isChecked() else "keyboard"
            self._session.set_live_ptt_type(ptt_type)
        if hasattr(self._session, "set_live_ptt_key"):
            key = (self._ptt_key_combo.currentData() or self._ptt_key_combo.currentText() or "F2").strip()
            self._session.set_live_ptt_key(key or None)
        if hasattr(self._session, "set_live_ptt_mouse_button"):
            btn = (self._ptt_mouse_combo.currentData() or "right").strip().lower()
            self._session.set_live_ptt_mouse_button(btn or None)
        if hasattr(self._session, "set_live_ptt_toggle"):
            self._session.set_live_ptt_toggle(self._ptt_toggle_checkbox.isChecked())
        if hasattr(self._session, "set_barge_in_enabled"):
            self._session.set_barge_in_enabled(self._barge_in_checkbox.isChecked())
        super().accept()
