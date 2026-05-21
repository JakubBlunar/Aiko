"""Minimal settings dialog for S2S assistant."""
from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.session_controller import SessionController
from app.core.settings import save_runtime_preferences
from app.ui.geometry_mixin import PersistentGeometryMixin


class SettingsDialog(PersistentGeometryMixin, QDialog):
    def __init__(
        self,
        session: SessionController,
        parent=None,
        *,
        initial_geometry: dict[str, int] | None = None,
        persist_geometry: Callable[[dict[str, int]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self.setWindowTitle("Settings")
        self.init_geometry(
            initial=initial_geometry,
            default_width=640, default_height=560,
            persist_callback=persist_geometry,
        )
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_model_tab(), "Model")
        self._tabs.addTab(self._build_audio_tab(), "Audio")
        self._tabs.addTab(self._build_advanced_tab(), "Advanced")
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

        self._tts_provider_combo = QComboBox()
        self._tts_provider_combo.setMinimumWidth(180)
        providers = self._session.list_tts_providers()
        current_provider = self._session.tts_provider
        for p in providers:
            self._tts_provider_combo.addItem(p, p)
        idx = self._tts_provider_combo.findData(current_provider)
        if idx >= 0:
            self._tts_provider_combo.setCurrentIndex(idx)
        self._tts_provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("TTS engine:", self._tts_provider_combo)

        self._voice_combo = QComboBox()
        self._voice_combo.setMinimumWidth(180)
        self._populate_voices()
        form.addRow("TTS voice:", self._voice_combo)

        self._pocket_temp_spin = QDoubleSpinBox()
        self._pocket_temp_spin.setRange(0.1, 1.5)
        self._pocket_temp_spin.setSingleStep(0.05)
        self._pocket_temp_spin.setValue(
            getattr(self._session._settings.tts, "pocket_tts_temp", 0.7)
        )
        self._pocket_temp_label = QLabel("Temperature:")
        form.addRow(self._pocket_temp_label, self._pocket_temp_spin)
        self._update_pocket_tts_visibility()

        layout.addLayout(form)

        live_group = QGroupBox("Live input")
        live_layout = QFormLayout(live_group)
        self._live_input_combo = QComboBox()
        self._live_input_combo.addItem("Voice detection", "voice_detection")
        self._live_input_combo.addItem("Push-to-talk", "push_to_talk")
        self._live_input_combo.addItem("Wake word", "wake_word")
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

    def _build_advanced_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        settings = self._session._settings

        stt_group = QGroupBox("Speech-to-Text")
        stt_form = QFormLayout(stt_group)
        self._stt_model_edit = QLineEdit(getattr(settings.stt, "model", ""))
        stt_form.addRow("STT model:", self._stt_model_edit)
        self._stt_language_edit = QLineEdit(getattr(settings.stt, "language", "en"))
        stt_form.addRow("Language:", self._stt_language_edit)
        layout.addWidget(stt_group)

        agent_group = QGroupBox("Agent")
        agent_form = QFormLayout(agent_group)
        agent_settings = getattr(settings, "agent", None)
        self._history_depth_spin = QSpinBox()
        self._history_depth_spin.setRange(1, 100)
        self._history_depth_spin.setValue(getattr(agent_settings, "num_history_runs", 10) if agent_settings else 10)
        agent_form.addRow("History depth (turns):", self._history_depth_spin)

        self._response_style_combo = QComboBox()
        for style in ("balanced", "conversational", "concise", "detailed", "technical"):
            self._response_style_combo.addItem(style, style)
        current_style = getattr(settings.assistant, "response_style", "balanced")
        idx = self._response_style_combo.findData(current_style)
        if idx >= 0:
            self._response_style_combo.setCurrentIndex(idx)
        agent_form.addRow("Response style:", self._response_style_combo)

        self._autonomy_combo = QComboBox()
        for mode in ("disabled", "manual", "interactive", "automatic"):
            self._autonomy_combo.addItem(mode, mode)
        current_autonomy = getattr(settings.autonomy, "mode", "disabled")
        idx = self._autonomy_combo.findData(current_autonomy)
        if idx >= 0:
            self._autonomy_combo.setCurrentIndex(idx)
        agent_form.addRow("Autonomy mode:", self._autonomy_combo)
        layout.addWidget(agent_group)

        personality_group = QGroupBox("Personality")
        personality_form = QFormLayout(personality_group)
        self._assistant_name_edit = QLineEdit(getattr(settings.assistant, "name", ""))
        personality_form.addRow("Assistant name:", self._assistant_name_edit)
        layout.addWidget(personality_group)

        proactive_group = QGroupBox("Proactive Behaviour")
        proactive_form = QFormLayout(proactive_group)
        self._proactive_silence_spin = QDoubleSpinBox()
        self._proactive_silence_spin.setRange(10.0, 600.0)
        self._proactive_silence_spin.setSingleStep(5.0)
        self._proactive_silence_spin.setSuffix(" s")
        self._proactive_silence_spin.setValue(
            getattr(agent_settings, "proactive_silence_seconds", 45.0) if agent_settings else 45.0
        )
        proactive_form.addRow("Silence before proactive:", self._proactive_silence_spin)
        self._proactive_cooldown_spin = QDoubleSpinBox()
        self._proactive_cooldown_spin.setRange(30.0, 600.0)
        self._proactive_cooldown_spin.setSingleStep(10.0)
        self._proactive_cooldown_spin.setSuffix(" s")
        self._proactive_cooldown_spin.setValue(
            getattr(agent_settings, "proactive_cooldown_seconds", 120.0) if agent_settings else 120.0
        )
        proactive_form.addRow("Cooldown between proactive:", self._proactive_cooldown_spin)
        self._proactive_planner_enabled = QCheckBox("Enable background director (JSON planner)")
        self._proactive_planner_enabled.setChecked(
            bool(getattr(agent_settings, "proactive_planner_enabled", True)) if agent_settings else True
        )
        proactive_form.addRow("", self._proactive_planner_enabled)
        self._proactive_planner_model_edit = QLineEdit(
            str(getattr(settings.ollama, "proactive_planner_model", "") or "")
        )
        self._proactive_planner_model_edit.setPlaceholderText("blank = same as judge model")
        proactive_form.addRow("Planner Ollama model:", self._proactive_planner_model_edit)
        self._proactive_context_spin = QSpinBox()
        self._proactive_context_spin.setRange(2, 40)
        self._proactive_context_spin.setValue(
            int(getattr(agent_settings, "proactive_context_messages", 10)) if agent_settings else 10
        )
        proactive_form.addRow("Transcript lines for planner:", self._proactive_context_spin)
        self._proactive_bg_interval_spin = QDoubleSpinBox()
        self._proactive_bg_interval_spin.setRange(20.0, 600.0)
        self._proactive_bg_interval_spin.setSingleStep(10.0)
        self._proactive_bg_interval_spin.setSuffix(" s")
        self._proactive_bg_interval_spin.setValue(
            float(getattr(agent_settings, "proactive_background_interval_seconds", 90.0)) if agent_settings else 90.0
        )
        proactive_form.addRow("Director refresh interval:", self._proactive_bg_interval_spin)
        self._proactive_stale_spin = QDoubleSpinBox()
        self._proactive_stale_spin.setRange(30.0, 600.0)
        self._proactive_stale_spin.setSingleStep(10.0)
        self._proactive_stale_spin.setSuffix(" s")
        self._proactive_stale_spin.setValue(
            float(getattr(agent_settings, "proactive_background_stale_seconds", 120.0)) if agent_settings else 120.0
        )
        proactive_form.addRow("Plan max age for speech:", self._proactive_stale_spin)
        self._proactive_use_main_utterance = QCheckBox("Expand proactive lines with main chat model")
        self._proactive_use_main_utterance.setChecked(
            bool(getattr(agent_settings, "proactive_use_main_for_utterance", False)) if agent_settings else False
        )
        proactive_form.addRow("", self._proactive_use_main_utterance)
        self._proactive_advise_main = QCheckBox("Pass planner hints to main model (not saved as user text)")
        self._proactive_advise_main.setChecked(
            bool(getattr(agent_settings, "proactive_brain_advise_main", True)) if agent_settings else True
        )
        proactive_form.addRow("", self._proactive_advise_main)
        self._proactive_drive_speech = QCheckBox("Director can trigger proactive speech")
        self._proactive_drive_speech.setChecked(
            bool(getattr(agent_settings, "proactive_brain_drive_speech", True)) if agent_settings else True
        )
        proactive_form.addRow("", self._proactive_drive_speech)
        self._proactive_speech_live_only = QCheckBox("Proactive speech only during Start Live")
        self._proactive_speech_live_only.setChecked(
            bool(getattr(agent_settings, "proactive_speech_requires_live", True)) if agent_settings else True
        )
        proactive_form.addRow("", self._proactive_speech_live_only)
        self._proactive_influence_autonomy = QCheckBox("Planner suggested_steps influence autonomy (reserved)")
        self._proactive_influence_autonomy.setChecked(
            bool(getattr(agent_settings, "proactive_brain_influence_autonomy", False)) if agent_settings else False
        )
        proactive_form.addRow("", self._proactive_influence_autonomy)
        layout.addWidget(proactive_group)

        misc_group = QGroupBox("Miscellaneous")
        misc_form = QFormLayout(misc_group)
        self._log_level_combo = QComboBox()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self._log_level_combo.addItem(level, level)
        current_level = getattr(settings.logging, "level", "INFO") if hasattr(settings, "logging") else "INFO"
        idx = self._log_level_combo.findData(current_level)
        if idx >= 0:
            self._log_level_combo.setCurrentIndex(idx)
        misc_form.addRow("Log level:", self._log_level_combo)
        layout.addWidget(misc_group)

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

    def _populate_voices(self) -> None:
        self._voice_combo.clear()
        for v in self._session.list_tts_voices():
            self._voice_combo.addItem(v, v)
        idx = self._voice_combo.findData(self._session.tts_voice)
        if idx < 0:
            idx = self._voice_combo.findText(self._session.tts_voice)
        if idx >= 0:
            self._voice_combo.setCurrentIndex(idx)

    def _update_pocket_tts_visibility(self) -> None:
        is_pocket = (self._tts_provider_combo.currentData() or "") == "pocket-tts"
        self._pocket_temp_spin.setVisible(is_pocket)
        self._pocket_temp_label.setVisible(is_pocket)

    def _on_provider_changed(self) -> None:
        provider = self._tts_provider_combo.currentData()
        if provider:
            self._session.set_tts_provider(str(provider))
            self._populate_voices()
        self._update_pocket_tts_visibility()

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
        selected_provider = str(self._tts_provider_combo.currentData() or self._session.tts_provider)
        if selected_provider != self._session.tts_provider:
            self._session.set_tts_provider(selected_provider)
        self._session.set_tts_voice(str(self._voice_combo.currentData() or self._voice_combo.currentText() or self._session.tts_voice))
        if hasattr(self._session._settings.tts, "pocket_tts_temp"):
            self._session._settings.tts.pocket_tts_temp = self._pocket_temp_spin.value()
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
        settings = self._session._settings
        stt_model = self._stt_model_edit.text().strip()
        if stt_model and hasattr(settings.stt, "model"):
            settings.stt.model = stt_model
        stt_lang = self._stt_language_edit.text().strip()
        if stt_lang and hasattr(settings.stt, "language"):
            settings.stt.language = stt_lang
        agent_settings = getattr(settings, "agent", None)
        if agent_settings and hasattr(agent_settings, "num_history_runs"):
            agent_settings.num_history_runs = self._history_depth_spin.value()
        if hasattr(settings.assistant, "response_style"):
            settings.assistant.response_style = self._response_style_combo.currentData() or "conversational"
        if hasattr(settings.autonomy, "mode"):
            settings.autonomy.mode = self._autonomy_combo.currentData() or "disabled"
        name = self._assistant_name_edit.text().strip()
        if hasattr(settings.assistant, "name"):
            settings.assistant.name = name
        if agent_settings and hasattr(agent_settings, "proactive_silence_seconds"):
            agent_settings.proactive_silence_seconds = self._proactive_silence_spin.value()
        if agent_settings and hasattr(agent_settings, "proactive_cooldown_seconds"):
            agent_settings.proactive_cooldown_seconds = self._proactive_cooldown_spin.value()
        if agent_settings and hasattr(agent_settings, "proactive_planner_enabled"):
            agent_settings.proactive_planner_enabled = self._proactive_planner_enabled.isChecked()
        if hasattr(settings, "ollama") and hasattr(settings.ollama, "proactive_planner_model"):
            settings.ollama.proactive_planner_model = self._proactive_planner_model_edit.text().strip()
        if agent_settings and hasattr(agent_settings, "proactive_context_messages"):
            agent_settings.proactive_context_messages = self._proactive_context_spin.value()
        if agent_settings and hasattr(agent_settings, "proactive_background_interval_seconds"):
            agent_settings.proactive_background_interval_seconds = float(self._proactive_bg_interval_spin.value())
        if agent_settings and hasattr(agent_settings, "proactive_background_stale_seconds"):
            agent_settings.proactive_background_stale_seconds = float(self._proactive_stale_spin.value())
        if agent_settings and hasattr(agent_settings, "proactive_use_main_for_utterance"):
            agent_settings.proactive_use_main_for_utterance = self._proactive_use_main_utterance.isChecked()
        if agent_settings and hasattr(agent_settings, "proactive_brain_advise_main"):
            agent_settings.proactive_brain_advise_main = self._proactive_advise_main.isChecked()
        if agent_settings and hasattr(agent_settings, "proactive_brain_drive_speech"):
            agent_settings.proactive_brain_drive_speech = self._proactive_drive_speech.isChecked()
        if agent_settings and hasattr(agent_settings, "proactive_speech_requires_live"):
            agent_settings.proactive_speech_requires_live = self._proactive_speech_live_only.isChecked()
        if agent_settings and hasattr(agent_settings, "proactive_brain_influence_autonomy"):
            agent_settings.proactive_brain_influence_autonomy = self._proactive_influence_autonomy.isChecked()
        if hasattr(settings, "logging") and hasattr(settings.logging, "level"):
            settings.logging.level = self._log_level_combo.currentData() or "INFO"
        try:
            save_runtime_preferences(
                chat_model=self._session.chat_model,
                remember_history=getattr(settings.assistant, "remember_history", True),
                autonomy_mode=getattr(settings.autonomy, "mode", "interactive"),
                microphone_device=getattr(self._session, "microphone_device", None),
                output_device=getattr(self._session, "output_device", None),
                vad_level_threshold=getattr(self._session, "vad_level_threshold", 0.02),
                vad_silence_seconds=getattr(self._session, "vad_silence_seconds", 1.0),
                action_min_interval_seconds=getattr(self._session, "action_min_interval_seconds", 1.0),
                tts_provider=self._session.tts_provider,
                tts_voice=self._session.tts_voice,
                pocket_tts_voice=getattr(settings.tts, "pocket_tts_voice", None),
                pocket_tts_temp=getattr(settings.tts, "pocket_tts_temp", None),
                enable_microphone=getattr(self._session, "_mic_enabled", True),
            )
        except Exception:
            pass
        super().accept()
