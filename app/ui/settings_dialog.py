"""Minimal settings dialog for S2S assistant."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from app.core.session_controller import SessionController


class SettingsDialog(QDialog):
    def __init__(self, session: SessionController, parent=None) -> None:
        super().__init__(parent)
        self._session = session
        self.setWindowTitle("Settings")
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(200)
        for name in session.list_chat_models():
            self._model_combo.addItem(name, name)
        idx = self._model_combo.findData(session.chat_model)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        form.addRow("Model:", self._model_combo)

        self._mic_checkbox = QCheckBox("Microphone enabled")
        self._mic_checkbox.setChecked(session.state.mic_enabled)
        form.addRow("", self._mic_checkbox)

        self._voice_combo = QComboBox()
        self._voice_combo.setMinimumWidth(180)
        for v in session.list_tts_voices():
            self._voice_combo.addItem(v, v)
        idx = self._voice_combo.findData(session.tts_voice) or self._voice_combo.findText(session.tts_voice)
        if idx >= 0:
            self._voice_combo.setCurrentIndex(idx)
        form.addRow("TTS voice:", self._voice_combo)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.clicked.connect(self._clear_history)
        btn_row.addWidget(self._clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addWidget(QLabel(""))  # spacer
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _clear_history(self) -> None:
        self._session.clear_conversation_memory()

    def accept(self) -> None:
        self._session.set_chat_model(str(self._model_combo.currentData() or self._session.chat_model))
        self._session.update_sources(mic=self._mic_checkbox.isChecked(), screen=False)
        self._session.set_tts_voice(str(self._voice_combo.currentData() or self._voice_combo.currentText() or self._session.tts_voice))
        super().accept()
