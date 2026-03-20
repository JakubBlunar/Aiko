from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from app.core.session_controller import SessionController
from app.ui.geometry_mixin import PersistentGeometryMixin


class MemoryViewerDialog(PersistentGeometryMixin, QDialog):
    """Shows what the assistant has learned: personality notes, topics, and summary."""

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
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        self.setWindowTitle("What I Know About You")
        self.init_geometry(
            initial=initial_geometry,
            default_width=700, default_height=520,
            persist_callback=persist_geometry,
        )

        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._refresh)
        controls.addWidget(self._refresh_button)
        self._delete_button = QPushButton("Delete Selected Note")
        self._delete_button.clicked.connect(self._delete_selected)
        controls.addWidget(self._delete_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        layout.addWidget(QLabel("Personality Notes:"))
        self._notes_list = QListWidget()
        self._notes_list.setAlternatingRowColors(True)
        layout.addWidget(self._notes_list, stretch=2)

        layout.addWidget(QLabel("Recent Topics:"))
        self._topics_label = QLabel("")
        self._topics_label.setWordWrap(True)
        layout.addWidget(self._topics_label)

        layout.addWidget(QLabel("Conversation Summary:"))
        self._summary_text = QTextEdit()
        self._summary_text.setReadOnly(True)
        self._summary_text.setMaximumHeight(100)
        layout.addWidget(self._summary_text, stretch=1)

        self._status = QLabel("")
        layout.addWidget(self._status)

        self._refresh()

    def _refresh(self) -> None:
        db = getattr(self._session, "_chat_db", None)
        if db is None:
            self._status.setText("No database available.")
            return

        session_key = self._session.session_key

        notes = db.get_personality_notes(session_key, min_confidence=0.0)
        self._notes_list.clear()
        for n in notes:
            cat = (n.category or "general").replace("_", " ").title()
            label = f"[{cat}] {n.note}  (confidence: {n.confidence:.2f})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, n.id)
            self._notes_list.addItem(item)

        topics = db.get_recent_topics(session_key, limit=15)
        if topics:
            self._topics_label.setText(", ".join(t.topic for t in topics))
        else:
            self._topics_label.setText("(none)")

        summary_row = db.get_latest_summary(session_key)
        if summary_row:
            self._summary_text.setPlainText(summary_row.summary)
        else:
            self._summary_text.setPlainText("(no summary yet)")

        self._status.setText(f"{len(notes)} notes, {len(topics)} topics")

    def _delete_selected(self) -> None:
        item = self._notes_list.currentItem()
        if item is None:
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if note_id is None:
            return
        db = getattr(self._session, "_chat_db", None)
        if db is None:
            return
        try:
            conn = db._get_conn()
            conn.execute("DELETE FROM personality_notes WHERE id = ?", (note_id,))
            conn.commit()
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Could not delete note: {exc}")
            return
        self._refresh()
