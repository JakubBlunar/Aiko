from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from app.core.session_controller import SessionController


class DecisionTraceDialog(QDialog):
    def __init__(self, session: SessionController, parent=None) -> None:
        super().__init__(parent)
        self._session = session

        self.setWindowTitle("Action + Thinking Trace")
        self.resize(980, 700)

        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show last entries:"))

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(20, 5000)
        self._limit_spin.setSingleStep(20)
        self._limit_spin.setValue(400)
        controls.addWidget(self._limit_spin)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._refresh)
        controls.addWidget(self._refresh_button)

        self._clear_button = QPushButton("Clear Trace")
        self._clear_button.clicked.connect(self._clear)
        controls.addWidget(self._clear_button)

        controls.addStretch(1)
        layout.addLayout(controls)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Stage filters:"))
        self._filter_screen = QCheckBox("screen.decision")
        self._filter_screen.setChecked(True)
        self._filter_screen.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_screen)

        self._filter_capture = QCheckBox("screen.capture")
        self._filter_capture.setChecked(True)
        self._filter_capture.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_capture)

        self._filter_autonomy = QCheckBox("autonomy.plan")
        self._filter_autonomy.setChecked(True)
        self._filter_autonomy.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_autonomy)

        self._filter_goal = QCheckBox("autonomy.goal")
        self._filter_goal.setChecked(True)
        self._filter_goal.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_goal)

        self._filter_stt = QCheckBox("stt.mic")
        self._filter_stt.setChecked(True)
        self._filter_stt.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_stt)

        self._filter_tts = QCheckBox("tts.error")
        self._filter_tts.setChecked(True)
        self._filter_tts.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_tts)

        self._filter_plan = QCheckBox("action.plan")
        self._filter_plan.setChecked(True)
        self._filter_plan.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_plan)

        self._filter_execute = QCheckBox("action.execute")
        self._filter_execute.setChecked(True)
        self._filter_execute.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_execute)

        self._filter_confirm = QCheckBox("action.confirmation")
        self._filter_confirm.setChecked(True)
        self._filter_confirm.stateChanged.connect(lambda _state: self._refresh())
        filters.addWidget(self._filter_confirm)
        filters.addStretch(1)
        layout.addLayout(filters)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        layout.addWidget(self._text, stretch=1)

        self._status = QLabel("")
        layout.addWidget(self._status)

        self._refresh()

    def _refresh(self) -> None:
        limit = self._limit_spin.value()
        entries = self._session.get_decision_trace(max_entries=limit)

        allowed_stages: set[str] = set()
        if self._filter_screen.isChecked():
            allowed_stages.add("screen.decision")
        if self._filter_capture.isChecked():
            allowed_stages.add("screen.capture")
        if self._filter_autonomy.isChecked():
            allowed_stages.add("autonomy.plan")
        if self._filter_goal.isChecked():
            allowed_stages.add("autonomy.goal")
        if self._filter_stt.isChecked():
            allowed_stages.add("stt.mic")
        if self._filter_tts.isChecked():
            allowed_stages.add("tts.error")
        if self._filter_plan.isChecked():
            allowed_stages.add("action.plan")
        if self._filter_execute.isChecked():
            allowed_stages.add("action.execute")
        if self._filter_confirm.isChecked():
            allowed_stages.add("action.confirmation")

        if allowed_stages:
            entries = [entry for entry in entries if str(entry.get("stage", "")) in allowed_stages]
        else:
            entries = []

        if not entries:
            self._text.setPlainText("No action/thinking trace entries yet.")
            self._status.setText("Entries: 0")
            return

        lines: list[str] = []
        for entry in entries:
            stage = str(entry.get("stage", "unknown"))
            message = str(entry.get("message", ""))
            stamp = str(entry.get("timestamp", ""))
            stamp_view = stamp
            try:
                stamp_view = datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                pass
            lines.append(f"[{stamp_view}] {stage}: {message}")

        self._text.setPlainText("\n\n".join(lines))
        self._status.setText(f"Entries: {len(entries)}")

    def _clear(self) -> None:
        self._session.clear_decision_trace()
        self._refresh()
