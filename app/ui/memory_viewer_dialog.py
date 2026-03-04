from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from app.core.session_controller import SessionController


class MemoryViewerDialog(QDialog):
    def __init__(self, session: SessionController, parent=None) -> None:
        super().__init__(parent)
        self._session = session

        self.setWindowTitle("Memory Viewer")
        self.resize(900, 680)

        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show last entries:"))

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(20, 2000)
        self._limit_spin.setSingleStep(20)
        self._limit_spin.setValue(400)
        controls.addWidget(self._limit_spin)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._refresh)
        controls.addWidget(self._refresh_button)

        self._clear_button = QPushButton("Clear Memory")
        self._clear_button.clicked.connect(self._clear)
        controls.addWidget(self._clear_button)
        controls.addStretch(1)

        layout.addLayout(controls)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        layout.addWidget(self._text, stretch=1)

        self._status = QLabel("")
        layout.addWidget(self._status)

        self._refresh()

    def _refresh(self) -> None:
        limit = self._limit_spin.value()
        entries = self._session.get_conversation_memory(max_entries=limit)

        if not entries:
            self._text.setPlainText("No stored conversation memory yet.")
            self._status.setText("Entries: 0")
            return

        lines: list[str] = []
        for entry in entries:
            role = str(entry.get("role", "")).title()
            content = str(entry.get("content", ""))
            stamp = str(entry.get("timestamp", ""))
            stamp_view = stamp
            try:
                stamp_view = datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                pass
            lines.append(f"[{stamp_view}] {role}: {content}")

        self._text.setPlainText("\n\n".join(lines))
        self._status.setText(f"Entries: {len(entries)}")

    def _clear(self) -> None:
        self._session.clear_conversation_memory()
        self._refresh()
