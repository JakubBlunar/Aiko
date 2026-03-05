from __future__ import annotations

from datetime import datetime
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QMoveEvent, QResizeEvent
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
    def __init__(
        self,
        session: SessionController,
        *,
        initial_limit: int = 400,
        initial_filters: dict[str, bool] | None = None,
        persist_state: Callable[[dict[str, bool], int], None] | None = None,
        initial_x: int | None = None,
        initial_y: int | None = None,
        initial_width: int | None = None,
        initial_height: int | None = None,
        persist_geometry: Callable[[int, int, int, int], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._persist_state = persist_state
        self._persist_geometry = persist_geometry
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)

        self.setWindowTitle("Action + Thinking Trace")
        self.resize(
            max(300, int(initial_width)) if initial_width is not None else 980,
            max(220, int(initial_height)) if initial_height is not None else 700,
        )
        if initial_x is not None and initial_y is not None:
            self.move(int(initial_x), int(initial_y))

        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show last entries:"))

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(20, 5000)
        self._limit_spin.setSingleStep(20)
        self._limit_spin.setValue(max(20, min(int(initial_limit), 5000)))
        self._limit_spin.valueChanged.connect(self._on_filters_changed)
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
        filters.addWidget(self._filter_screen)

        self._filter_capture = QCheckBox("screen.capture")
        filters.addWidget(self._filter_capture)

        self._filter_autonomy = QCheckBox("autonomy.plan")
        filters.addWidget(self._filter_autonomy)

        self._filter_goal = QCheckBox("autonomy.goal")
        filters.addWidget(self._filter_goal)

        self._filter_stt = QCheckBox("stt.mic")
        filters.addWidget(self._filter_stt)

        self._filter_pipeline = QCheckBox("pipeline.*")
        filters.addWidget(self._filter_pipeline)

        self._filter_tooling = QCheckBox("tool.*")
        filters.addWidget(self._filter_tooling)

        self._filter_agentic = QCheckBox("agentic.*")
        filters.addWidget(self._filter_agentic)

        self._filter_mcp = QCheckBox("mcp.*")
        filters.addWidget(self._filter_mcp)

        self._filter_tts = QCheckBox("tts.error")
        filters.addWidget(self._filter_tts)

        self._filter_plan = QCheckBox("action.plan")
        filters.addWidget(self._filter_plan)

        self._filter_execute = QCheckBox("action.execute")
        filters.addWidget(self._filter_execute)

        self._filter_confirm = QCheckBox("action.confirmation")
        filters.addWidget(self._filter_confirm)
        filters.addStretch(1)
        layout.addLayout(filters)

        self._filters: dict[str, QCheckBox] = {
            "screen.decision": self._filter_screen,
            "screen.capture": self._filter_capture,
            "autonomy.plan": self._filter_autonomy,
            "autonomy.goal": self._filter_goal,
            "stt.mic": self._filter_stt,
            "pipeline.*": self._filter_pipeline,
            "tool.*": self._filter_tooling,
            "agentic.*": self._filter_agentic,
            "mcp.*": self._filter_mcp,
            "tts.error": self._filter_tts,
            "action.plan": self._filter_plan,
            "action.execute": self._filter_execute,
            "action.confirmation": self._filter_confirm,
        }
        for key, box in self._filters.items():
            box.setChecked(bool((initial_filters or {}).get(key, True)))
            box.stateChanged.connect(self._on_filters_changed)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(self._text, stretch=1)

        self._status = QLabel("")
        layout.addWidget(self._status)

        self._refresh()

    def _selected_filters(self) -> dict[str, bool]:
        return {key: box.isChecked() for key, box in self._filters.items()}

    def _on_filters_changed(self) -> None:
        self._refresh()
        if callable(self._persist_state):
            self._persist_state(self._selected_filters(), int(self._limit_spin.value()))

    def _persist_geometry_state(self) -> None:
        if callable(self._persist_geometry):
            self._persist_geometry(self.x(), self.y(), self.width(), self.height())

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self._persist_geometry_state()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._persist_geometry_state()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._persist_geometry_state()
        super().closeEvent(event)

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
        if self._filter_pipeline.isChecked():
            allowed_stages.add("pipeline.*")
        if self._filter_tooling.isChecked():
            allowed_stages.add("tool.*")
        if self._filter_agentic.isChecked():
            allowed_stages.add("agentic.*")
        if self._filter_mcp.isChecked():
            allowed_stages.add("mcp.*")
        if self._filter_tts.isChecked():
            allowed_stages.add("tts.error")
        if self._filter_plan.isChecked():
            allowed_stages.add("action.plan")
        if self._filter_execute.isChecked():
            allowed_stages.add("action.execute")
        if self._filter_confirm.isChecked():
            allowed_stages.add("action.confirmation")

        if allowed_stages:
            filtered: list[dict[str, str]] = []
            for entry in entries:
                stage = str(entry.get("stage", ""))
                if stage in allowed_stages:
                    filtered.append(entry)
                    continue
                if "pipeline.*" in allowed_stages and stage.startswith("pipeline."):
                    filtered.append(entry)
                    continue
                if "tool.*" in allowed_stages and stage.startswith("tool."):
                    filtered.append(entry)
                    continue
                if "agentic.*" in allowed_stages and stage.startswith("agentic."):
                    filtered.append(entry)
                    continue
                if "mcp.*" in allowed_stages and stage.startswith("mcp."):
                    filtered.append(entry)
            entries = filtered
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
