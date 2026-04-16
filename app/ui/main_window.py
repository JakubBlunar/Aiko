from __future__ import annotations

import html
import json
import os
from pathlib import Path
import time as _time

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QKeySequence, QMouseEvent, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.session_controller import SessionController
from app.core.settings import AppSettings, save_runtime_preferences
from app.ui.decision_trace_dialog import DecisionTraceDialog
from app.ui.memory_viewer_dialog import MemoryViewerDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.theme import (
    BUBBLE_ASSISTANT_BG,
    BUBBLE_ASSISTANT_SPEAKER,
    BUBBLE_ASSISTANT_TEXT,
    BUBBLE_DEFAULT_BG,
    BUBBLE_DEFAULT_SPEAKER,
    BUBBLE_DEFAULT_TEXT,
    BUBBLE_SYSTEM_BG,
    BUBBLE_SYSTEM_SPEAKER,
    BUBBLE_SYSTEM_TEXT,
    BUBBLE_USER_BG,
    BUBBLE_USER_SPEAKER,
    BUBBLE_USER_TEXT,
    CONTENT_MARGIN,
    SPACING,
)
from app.ui.live_worker import LivePracticeWorker
from app.ui.stt_test_worker import SttTestWorker
from app.ui.turn_worker import SingleTurnWorker

_USE_WEB_VIEW = False
_QWebEngineView = None
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView as _QWebEngineView
    _USE_WEB_VIEW = _QWebEngineView is not None
except Exception:
    pass


_PTT_KEY_MAP: dict[str, tuple[Qt.Key, Qt.KeyboardModifier]] = {
    "f2": (Qt.Key.Key_F2, Qt.KeyboardModifier.NoModifier),
    "f3": (Qt.Key.Key_F3, Qt.KeyboardModifier.NoModifier),
    "f4": (Qt.Key.Key_F4, Qt.KeyboardModifier.NoModifier),
    "space": (Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier),
    "ctrl+space": (Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier),
}
_MODIFIER_MASK = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.ShiftModifier
)
_PTT_MOUSE_MAP: dict[str, Qt.MouseButton] = {
    "left": Qt.MouseButton.LeftButton,
    "middle": Qt.MouseButton.MiddleButton,
    "right": Qt.MouseButton.RightButton,
}


def _refresh_button_style(button: QPushButton) -> None:
    """Reapply stylesheet after changing a dynamic property (e.g. danger)."""
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)


class MainWindow(QMainWindow):
    _external_message = Signal(str, str)

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
        self._stt_test_thread: QThread | None = None
        self._stt_test_worker: SttTestWorker | None = None
        self._turn_mode: str | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._live_stream_buffer = ""
        self._live_stream_open = False
        self._stream_speaker = "Assistant"
        self._guardrail_controls_locked = False
        self._live_level_peak = 0.01
        self._live_noise_floor = 0.0
        self._wheel_guard_widgets: set[QWidget] = set()
        self._startup_greeting_done = False
        self._ptt_app_filter_installed = False
        self._message_count = 0
        self._max_visible_messages = 200
        self._pending_tokens: list[str] = []
        self._web_page_ready = False
        self._pending_js: list[str] = []
        self._stream_flush_timer = QTimer(self)
        self._stream_flush_timer.setInterval(60)
        self._stream_flush_timer.timeout.connect(self._flush_stream_tokens)
        self._last_audio_level_time = 0.0

        self.setWindowTitle(settings.assistant.name)
        self.resize(900, 640)
        if settings.ui.window_width and settings.ui.window_height:
            self.resize(max(640, settings.ui.window_width), max(480, settings.ui.window_height))
        if settings.ui.window_x is not None and settings.ui.window_y is not None:
            self.move(settings.ui.window_x, settings.ui.window_y)

        root = QWidget(self)
        self.setCentralWidget(root)

        layout = QVBoxLayout()
        layout.setContentsMargins(CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN)
        layout.setSpacing(SPACING)
        root.setLayout(layout)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(4)
        sidebar_layout.addWidget(QLabel("Sessions"))
        self._session_list = self._build_session_list_widget()
        sidebar_layout.addWidget(self._session_list, stretch=1)
        new_btn = QPushButton("New conversation")
        new_btn.clicked.connect(self._new_session)
        sidebar_layout.addWidget(new_btn)
        sidebar.setMaximumWidth(220)
        sidebar.setMinimumWidth(120)
        main_splitter.addWidget(sidebar)

        conversation_page = QWidget()
        conversation_layout = QVBoxLayout()
        conversation_layout.setSpacing(SPACING)
        conversation_page.setLayout(conversation_layout)
        self._conversation_panel = conversation_page
        main_splitter.addWidget(conversation_page)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([180, 720])
        layout.addWidget(main_splitter, stretch=1)

        self._status_label = QLabel(self._ready_status_text())
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._status_label.setObjectName("statusStrip")

        conversation_layout.addWidget(QLabel("Conversation"))

        search_row = QHBoxLayout()
        search_row.setSpacing(SPACING)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search conversation history...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.returnPressed.connect(self._do_search)
        search_row.addWidget(self._search_input, stretch=1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self._search_btn)
        conversation_layout.addLayout(search_row)

        from PySide6.QtWidgets import QListWidget
        self._search_results = QListWidget()
        self._search_results.setMaximumHeight(120)
        self._search_results.setVisible(False)
        self._search_results.itemClicked.connect(self._on_search_result_scroll)
        self._search_results.itemDoubleClicked.connect(self._on_search_result_clicked)
        conversation_layout.addWidget(self._search_results)

        self._topics_strip = QLabel("")
        self._topics_strip.setWordWrap(True)
        self._topics_strip.setStyleSheet(
            "font-size: 11px; color: #64748b; padding: 2px 4px;"
        )
        self._topics_strip.setVisible(False)
        conversation_layout.addWidget(self._topics_strip)

        self._conversation = None
        self._conversation_web = None
        if _USE_WEB_VIEW and _QWebEngineView is not None:
            self._conversation_web = _QWebEngineView()
            self._conversation_web.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
            self._conversation_web.loadFinished.connect(self._on_web_page_loaded)
            chat_html = Path(__file__).resolve().parents[2] / "resources" / "chat.html"
            if chat_html.is_file():
                self._conversation_web.setUrl(QUrl.fromLocalFile(str(chat_html)))
            conversation_layout.addWidget(self._conversation_web, stretch=1)
        else:
            self._conversation = QTextEdit()
            self._conversation.setReadOnly(True)
            self._conversation.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            conversation_layout.addWidget(self._conversation, stretch=1)

        input_row = QHBoxLayout()
        input_row.setSpacing(SPACING)
        self._input = QTextEdit()
        self._input.setPlaceholderText("Type here… Enter to send, Shift+Enter for new line")
        self._input.setAcceptRichText(False)
        self._input.setFixedHeight(88)
        self._input.installEventFilter(self)
        self._send_button = QPushButton("Send")
        self._send_button.setObjectName("sendButton")
        self._send_button.clicked.connect(self._send)
        self._live_toggle_button = QPushButton("Start Live")
        self._live_toggle_button.setObjectName("liveToggleButton")
        self._live_toggle_button.clicked.connect(self._on_live_toggle_clicked)
        self._clear_chat_button = QPushButton("Clear Chat")
        self._clear_chat_button.setObjectName("clearChatButton")
        self._clear_chat_button.clicked.connect(self._clear_conversation_view)
        self._settings_button = QPushButton("Settings")
        self._settings_button.setObjectName("settingsButton")
        self._settings_button.clicked.connect(self._open_settings)
        input_row.addWidget(self._input, stretch=1)
        input_row.addWidget(self._send_button)
        input_row.addWidget(self._live_toggle_button)
        input_row.addWidget(self._clear_chat_button)
        input_row.addWidget(self._settings_button)
        conversation_layout.addLayout(input_row)

        conversation_layout.addWidget(self._status_label)

        self._settings_dialog = None
        self._memory_dialog: MemoryViewerDialog | None = None
        self._trace_dialog: DecisionTraceDialog | None = None
        self._voice_cloning_dialog = None

        menu_bar = self.menuBar()
        view_menu = menu_bar.addMenu("View")
        view_menu.addAction("Memory Viewer", self._open_memory_viewer)
        view_menu.addAction("Decision Trace", self._open_trace_viewer)
        try:
            from pocket_tts import TTSModel as _PT  # noqa: F401
            view_menu.addAction("Voice Cloning", self._open_voice_cloning)
        except ImportError:
            pass

        help_menu = menu_bar.addMenu("Help")
        help_menu.addAction("Keyboard Shortcuts", self._show_shortcuts_help)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(self._shortcut_stop_tts)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(lambda: self._input.setFocus())
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(lambda: self._search_input.setFocus())
        QShortcut(QKeySequence("Ctrl+,"), self).activated.connect(self._open_settings)
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self._new_session)
        QShortcut(QKeySequence("Ctrl+M"), self).activated.connect(self._on_live_toggle_clicked)

        self._external_message.connect(self._on_external_message)
        self._session.add_message_listener(self._emit_external_message)

        self._setup_system_tray()
        self._force_quit = False

        QTimer.singleShot(250, self._play_startup_greeting)

    def _setup_system_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon = None
            return
        self._tray_icon = QSystemTrayIcon(self)
        app_icon = self.windowIcon()
        if not app_icon.isNull():
            self._tray_icon.setIcon(app_icon)
        else:
            self._tray_icon.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))
        tray_menu = QMenu()
        tray_menu.addAction("Show / Hide", self._toggle_window_visibility)
        tray_menu.addAction("Toggle Live", self._on_live_toggle_clicked)
        tray_menu.addSeparator()
        tray_menu.addAction("Quit", self._tray_quit)
        self._tray_icon.setContextMenu(tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.setToolTip(self.windowTitle())
        self._tray_icon.show()

    def _toggle_window_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visibility()

    def _tray_quit(self) -> None:
        self._force_quit = True
        self.close()

    def _show_shortcuts_help(self) -> None:
        shortcuts = (
            "<b>Keyboard Shortcuts</b><br><br>"
            "<table cellpadding='4'>"
            "<tr><td><b>Enter</b></td><td>Send message</td></tr>"
            "<tr><td><b>Shift+Enter</b></td><td>New line in input</td></tr>"
            "<tr><td><b>Escape</b></td><td>Stop TTS playback</td></tr>"
            "<tr><td><b>Ctrl+L</b></td><td>Focus input field</td></tr>"
            "<tr><td><b>Ctrl+F</b></td><td>Focus search bar</td></tr>"
            "<tr><td><b>Ctrl+,</b></td><td>Open settings</td></tr>"
            "<tr><td><b>Ctrl+N</b></td><td>New conversation</td></tr>"
            "<tr><td><b>Ctrl+M</b></td><td>Toggle live mode</td></tr>"
            "<tr><td colspan='2'><hr></td></tr>"
            "<tr><td><b>F2</b> (or configured PTT key)</td><td>Push-to-talk (hold)</td></tr>"
            "<tr><td><b>Ctrl+Space</b></td><td>Push-to-talk alternate</td></tr>"
            "</table>"
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Keyboard Shortcuts")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(shortcuts)
        msg.exec()

    def _shortcut_stop_tts(self) -> None:
        if self._session.is_tts_playing():
            self._session.stop_tts()

    def _open_settings(self) -> None:
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(
                self._session, self,
                initial_geometry=self._settings.ui.dialog_geometries.get("settings"),
                persist_geometry=lambda geo: self._persist_dialog_geometry("settings", geo),
            )
            self._settings_dialog.finished.connect(self._on_settings_dialog_finished)
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _on_settings_dialog_finished(self) -> None:
        self._status_label.setText(self._ready_status_text())

    def _emit_external_message(self, speaker: str, text: str) -> None:
        """Thread-safe bridge: emit the Qt signal from any thread."""
        self._external_message.emit(speaker, text)

    def _on_external_message(self, speaker: str, text: str) -> None:
        """Handle external messages (MCP) on the GUI thread."""
        if text:
            self._append(speaker, text)

    def _play_startup_greeting(self) -> None:
        if self._startup_greeting_done:
            return
        self._startup_greeting_done = True
        if self._live_thread is not None or self._turn_thread is not None:
            return
        import threading

        def _generate_and_speak() -> None:
            greeting = self._session.build_startup_greeting()
            ok = self._session.speak_text(greeting)
            if ok:
                self._external_message.emit("Assistant", greeting)

        threading.Thread(target=_generate_and_speak, daemon=True, name="startup-greeting").start()

    

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if self._live_thread is not None and getattr(self._session, "live_input_mode", "") == "push_to_talk":
            if event.type() == QEvent.Type.KeyPress and getattr(self._session, "live_ptt_type", "") == "keyboard":
                key_name = (getattr(self._session, "live_ptt_key", None) or "f2").strip().lower()
                entry = _PTT_KEY_MAP.get(key_name)
                if entry is not None and isinstance(event, QKeyEvent) and event.key() == entry[0] and (event.modifiers() & _MODIFIER_MASK) == entry[1]:
                    if getattr(self._session, "live_ptt_toggle", False):
                        self._session.set_ptt_active(not self._session.get_ptt_active())
                    else:
                        self._session.set_ptt_active(True)
            elif event.type() == QEvent.Type.KeyRelease and getattr(self._session, "live_ptt_type", "") == "keyboard":
                key_name = (getattr(self._session, "live_ptt_key", None) or "f2").strip().lower()
                entry = _PTT_KEY_MAP.get(key_name)
                if entry is not None and isinstance(event, QKeyEvent) and event.key() == entry[0] and (event.modifiers() & _MODIFIER_MASK) == entry[1]:
                    if not getattr(self._session, "live_ptt_toggle", False):
                        self._session.set_ptt_active(False)
            elif event.type() == QEvent.Type.MouseButtonPress and getattr(self._session, "live_ptt_type", "") == "mouse":
                btn_name = (getattr(self._session, "live_ptt_mouse_button", None) or "right").strip().lower()
                qt_btn = _PTT_MOUSE_MAP.get(btn_name)
                if qt_btn is not None and isinstance(event, QMouseEvent) and event.button() == qt_btn:
                    if getattr(self._session, "live_ptt_toggle", False):
                        self._session.set_ptt_active(not self._session.get_ptt_active())
                    else:
                        self._session.set_ptt_active(True)
            elif event.type() == QEvent.Type.MouseButtonRelease and getattr(self._session, "live_ptt_type", "") == "mouse":
                btn_name = (getattr(self._session, "live_ptt_mouse_button", None) or "right").strip().lower()
                qt_btn = _PTT_MOUSE_MAP.get(btn_name)
                if qt_btn is not None and isinstance(event, QMouseEvent) and event.button() == qt_btn:
                    if not getattr(self._session, "live_ptt_toggle", False):
                        self._session.set_ptt_active(False)

        if watched is self._input and event.type() == QEvent.Type.KeyPress:
            key = getattr(event, "key", lambda: None)()
            modifiers = getattr(event, "modifiers", lambda: Qt.KeyboardModifier.NoModifier)()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    # Shift+Enter: insert new line (let default behavior run)
                    return False
                # Enter: submit
                self._send()
                return True

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

    def _format_tokens(self, tokens: int) -> str:
        if tokens >= 1024:
            return f"{tokens / 1024:.1f}K"
        return str(tokens) if tokens else "0"

    def _ctx_status_part(self) -> str:
        total = self._session.context_window_size
        used = self._session.context_tokens_used
        if not total:
            return ""
        total_str = self._format_tokens(total)
        if used > 0:
            return f" | ctx: {self._format_tokens(used)} / {total_str}"
        return f" | ctx: {total_str}"

    def _ready_status_text(self) -> str:
        model = self._session.effective_chat_model
        return f"Ready | model: {model}{self._ctx_status_part()}"

    def _refresh_status(self) -> None:
        self._status_label.setText(self._ready_status_text())

    def _apply_sources(self) -> None:
        self._persist_preferences()
        self._refresh_status()

    def _clear_memory(self) -> None:
        self._session.clear_conversation_memory()
        self._append("System", "Conversation memory cleared.")

    # ── Session sidebar ──

    def _build_session_list_widget(self):
        from PySide6.QtWidgets import QListWidget, QMenu, QInputDialog
        lst = QListWidget()
        lst.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lst.customContextMenuRequested.connect(self._session_context_menu)
        lst.itemClicked.connect(self._on_session_clicked)
        self._refresh_session_list(lst)
        return lst

    def _refresh_session_list(self, lst=None):
        lst = lst or self._session_list
        lst.clear()
        db = getattr(self._session, "_chat_db", None)
        if db is None:
            return
        sessions = db.list_sessions()
        current_key = self._session.session_key
        if not sessions:
            from PySide6.QtWidgets import QListWidgetItem
            item = QListWidgetItem("main")
            item.setData(Qt.ItemDataRole.UserRole, current_key)
            lst.addItem(item)
            return
        for s in sessions:
            from PySide6.QtWidgets import QListWidgetItem
            sid = s["session_id"]
            count = s["message_count"]
            last = s.get("last_activity", "")
            label = sid.split(":")[-1] if ":" in sid else sid
            time_str = ""
            if last:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    time_str = dt.strftime("%m/%d %H:%M")
                except Exception:
                    time_str = last[:10]
            display = f"{label} ({count} msgs)"
            if time_str:
                display += f"\n  {time_str}"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            lst.addItem(item)
            if sid == current_key:
                item.setSelected(True)

    def _on_session_clicked(self, item):
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid:
            return
        parts = sid.split(":", 1)
        session_id = parts[-1] if len(parts) > 1 else parts[0]
        self._session.switch_session(session_id)
        self._clear_conversation_view()
        self._load_session_history(sid)

    def _load_session_history(self, session_key: str):
        db = getattr(self._session, "_chat_db", None)
        if db is None:
            return
        msgs = db.get_messages(session_key, limit=50)
        for m in msgs:
            speaker = "You" if m.role == "user" else "Assistant" if m.role == "assistant" else "System"
            self._append(speaker, m.content, timestamp=m.created_at)
        self._refresh_topics_strip(session_key)

    def _refresh_topics_strip(self, session_key: str | None = None):
        db = getattr(self._session, "_chat_db", None)
        if db is None:
            self._topics_strip.setVisible(False)
            return
        key = session_key or self._session.session_key
        topics = db.get_recent_topics(key, limit=10)
        if not topics:
            self._topics_strip.setVisible(False)
            return
        chips = " \u00b7 ".join(t.topic for t in topics[:8])
        self._topics_strip.setText(f"Topics: {chips}")
        self._topics_strip.setVisible(True)

    def _new_session(self):
        new_id = self._session.new_session()
        self._clear_conversation_view()
        self._append("System", f"New conversation started ({new_id}).")
        self._refresh_session_list()

    def _session_context_menu(self, pos):
        item = self._session_list.itemAt(pos)
        if item is None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        export_json_action = menu.addAction("Export as JSON")
        export_md_action = menu.addAction("Export as Markdown")
        action = menu.exec(self._session_list.mapToGlobal(pos))
        if action == delete_action:
            db = getattr(self._session, "_chat_db", None)
            if db:
                db.delete_session(sid)
                self._refresh_session_list()
                if sid == self._session.session_key:
                    self._clear_conversation_view()
        elif action == export_json_action:
            db = getattr(self._session, "_chat_db", None)
            if db:
                from PySide6.QtWidgets import QFileDialog
                path, _ = QFileDialog.getSaveFileName(self, "Export Session", f"{sid}.json", "JSON (*.json)")
                if path:
                    import json as _json
                    data = db.export_session(sid)
                    Path(path).write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        elif action == export_md_action:
            db = getattr(self._session, "_chat_db", None)
            if db:
                from PySide6.QtWidgets import QFileDialog
                path, _ = QFileDialog.getSaveFileName(self, "Export Session", f"{sid}.md", "Markdown (*.md)")
                if path:
                    msgs = db.get_messages(sid)
                    lines = [f"# Session: {sid}\n"]
                    for m in msgs:
                        ts = m.created_at[:16] if m.created_at else ""
                        speaker = "User" if m.role == "user" else "Assistant"
                        lines.append(f"**{speaker}** ({ts}):\n{m.content}\n\n---\n")
                    Path(path).write_text("\n".join(lines), encoding="utf-8")

    def _open_memory_viewer(self) -> None:
        if self._memory_dialog is None:
            self._memory_dialog = MemoryViewerDialog(
                self._session, None,
                initial_geometry=self._settings.ui.dialog_geometries.get("memory_viewer"),
                persist_geometry=lambda geo: self._persist_dialog_geometry("memory_viewer", geo),
            )
            self._memory_dialog.finished.connect(self._on_memory_dialog_closed)
        self._memory_dialog.show()
        self._memory_dialog.raise_()
        self._memory_dialog.activateWindow()

    def _on_memory_dialog_closed(self) -> None:
        self._memory_dialog = None

    def _open_voice_cloning(self) -> None:
        if self._voice_cloning_dialog is None:
            from app.ui.voice_cloning_dialog import VoiceCloningDialog
            self._voice_cloning_dialog = VoiceCloningDialog(
                self._session, parent=None,
                initial_geometry=self._settings.ui.dialog_geometries.get("voice_cloning"),
                persist_geometry=lambda geo: self._persist_dialog_geometry("voice_cloning", geo),
            )
            self._voice_cloning_dialog.finished.connect(self._on_voice_cloning_closed)
        self._voice_cloning_dialog.show()
        self._voice_cloning_dialog.raise_()
        self._voice_cloning_dialog.activateWindow()

    def _on_voice_cloning_closed(self) -> None:
        self._voice_cloning_dialog = None

    def _open_trace_viewer(self) -> None:
        if self._trace_dialog is None:
            self._trace_dialog = DecisionTraceDialog(
                self._session,
                initial_limit=(self._settings.ui.decision_trace_limit or 400),
                initial_filters=dict(self._settings.ui.decision_trace_filters or {}),
                persist_state=self._persist_trace_viewer_preferences,
                initial_x=self._settings.ui.decision_trace_window_x,
                initial_y=self._settings.ui.decision_trace_window_y,
                initial_width=self._settings.ui.decision_trace_window_width,
                initial_height=self._settings.ui.decision_trace_window_height,
                persist_geometry=self._persist_trace_viewer_geometry,
                parent=None,
            )
            self._trace_dialog.finished.connect(self._on_trace_dialog_closed)
        self._trace_dialog.show()
        self._trace_dialog.raise_()
        self._trace_dialog.activateWindow()

    def _on_trace_dialog_closed(self) -> None:
        self._trace_dialog = None

    def _persist_trace_viewer_preferences(self, filters: dict[str, bool], limit: int) -> None:
        self._settings.ui.decision_trace_filters = dict(filters)
        self._settings.ui.decision_trace_limit = int(limit)
        self._persist_preferences(trace_filters=filters, trace_limit=limit)

    def _persist_trace_viewer_geometry(self, x: int, y: int, width: int, height: int) -> None:
        self._settings.ui.decision_trace_window_x = int(x)
        self._settings.ui.decision_trace_window_y = int(y)
        self._settings.ui.decision_trace_window_width = int(width)
        self._settings.ui.decision_trace_window_height = int(height)
        self._persist_preferences(
            trace_window_x=x,
            trace_window_y=y,
            trace_window_width=width,
            trace_window_height=height,
        )

    def _persist_dialog_geometry(self, key: str, geo: dict[str, int]) -> None:
        self._settings.ui.dialog_geometries[key] = geo
        self._persist_preferences(dialog_geometries={key: geo})

    def _run_stt_test(self) -> None:
        if self._stt_test_thread is not None:
            return
        if self._live_thread is not None or self._turn_thread is not None:
            QMessageBox.information(self, "STT diagnostic", "Stop active turn/live mode before running STT test.")
            return

        self._run_stt_test_button.setEnabled(False)
        self._run_stt_test_button.setText("Run STT Test (Recording...)")
        stt_status = getattr(self, "_stt_test_status", None)
        if stt_status is not None:
            stt_status.setText("Recording and transcribing...")
        self._status_label.setText("STT diagnostic...")

        self._stt_test_thread = QThread(self)
        self._stt_test_worker = SttTestWorker(
            self._session,
            seconds=float(self._stt_test_seconds_spin.value()),
            vad_filter=bool(self._stt_test_vad_checkbox.isChecked()),
            initial_prompt=str(self._stt_test_prompt_input.text() or "").strip(),
        )
        self._stt_test_worker.moveToThread(self._stt_test_thread)

        self._stt_test_thread.started.connect(self._stt_test_worker.run)
        self._stt_test_worker.done.connect(self._on_stt_test_done)
        self._stt_test_worker.failed.connect(self._on_stt_test_failed)
        self._stt_test_worker.finished.connect(self._on_stt_test_finished)
        self._stt_test_worker.finished.connect(self._stt_test_thread.quit)
        self._stt_test_thread.finished.connect(self._on_stt_test_thread_finished)
        self._stt_test_thread.finished.connect(self._stt_test_thread.deleteLater)
        self._stt_test_worker.finished.connect(self._stt_test_worker.deleteLater)
        self._stt_test_thread.start()

    def _on_stt_test_done(self, result: dict) -> None:
        stt_out = getattr(self, "_stt_test_output", None)
        if not bool(result.get("ok", False)):
            if stt_out is not None:
                stt_out.append(f"[error] {result.get('message', 'STT diagnostic failed.')}")
            return

        text = str(result.get("text", ""))
        capture_ms = float(result.get("capture_ms", 0.0) or 0.0)
        stt_ms = float(result.get("stt_ms", 0.0) or 0.0)
        chars = int(result.get("chars", 0) or 0)
        model = str(result.get("stt_model", self._session.stt_model))
        spin = getattr(self, "_stt_test_seconds_spin", None)
        seconds = float(result.get("seconds", spin.value() if spin else 0.0) or 0.0)
        vad_cb = getattr(self, "_stt_test_vad_checkbox", None)
        vad_filter = bool(result.get("vad_filter", vad_cb.isChecked() if vad_cb else False))
        prosody = result.get("prosody") if isinstance(result.get("prosody"), dict) else None

        if stt_out is not None:
            stt_out.append(
            (
                f"[ok] model={model} seconds={seconds:.1f} "
                f"capture_ms={capture_ms:.1f} stt_ms={stt_ms:.1f} chars={chars} "
                f"vad_filter={'on' if vad_filter else 'off'}"
            )
            )
        if prosody is not None and stt_out is not None:
            stt_out.append(
                (
                    "[prosody] "
                    f"emotion={prosody.get('emotion', 'unknown')} "
                    f"question_likely={prosody.get('question_likely', False)} "
                    f"confidence={prosody.get('confidence', 0.0)} "
                    f"analysis_ms={prosody.get('analysis_ms', 0.0)}"
                )
            )
        if stt_out is not None:
            stt_out.append(text or "[empty transcript]")
            stt_out.append("-" * 60)
        stt_status = getattr(self, "_stt_test_status", None)
        if stt_status is not None:
            stt_status.setText(
                f"Last run: capture={capture_ms:.1f}ms | stt={stt_ms:.1f}ms | chars={chars}"
            )

    def _on_stt_test_failed(self, message: str) -> None:
        stt_status = getattr(self, "_stt_test_status", None)
        if stt_status is not None:
            stt_status.setText("Error")
        QMessageBox.warning(self, "STT diagnostic", message or "STT diagnostic failed.")

    def _on_stt_test_finished(self) -> None:
        btn = getattr(self, "_run_stt_test_button", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText("Run STT Test")
        self._status_label.setText(self._ready_status_text())

    def _on_stt_test_thread_finished(self) -> None:
        self._stt_test_thread = None
        self._stt_test_worker = None

    def _apply_stt_test_config(self) -> None:
        spin = getattr(self, "_stt_test_seconds_spin", None)
        if spin is not None:
            self._settings.stt.diagnostics.record_seconds = float(spin.value())
        vad_cb = getattr(self, "_stt_test_vad_checkbox", None)
        if vad_cb is not None:
            self._settings.stt.diagnostics.vad_filter = bool(vad_cb.isChecked())
        prompt_in = getattr(self, "_stt_test_prompt_input", None)
        if prompt_in is not None:
            self._settings.stt.diagnostics.initial_prompt = str(prompt_in.text() or "").strip()
        prosody_cb = getattr(self, "_stt_prosody_enabled_checkbox", None)
        if prosody_cb is not None:
            self._settings.stt.prosody.enabled = bool(prosody_cb.isChecked())
        prompt_cb = getattr(self, "_stt_prosody_prompt_checkbox", None)
        if prompt_cb is not None:
            self._settings.stt.prosody.include_in_prompt = bool(prompt_cb.isChecked())
        self._session.set_prosody_enabled(self._settings.stt.prosody.enabled)
        self._session.set_prosody_include_in_prompt(self._settings.stt.prosody.include_in_prompt)
        self._persist_preferences(include_stt_testing=True)
        stt_status = getattr(self, "_stt_test_status", None)
        if stt_status is not None:
            stt_status.setText("STT config saved to user config.")
        self._append("System", "Saved STT diagnostic config to user preferences.")

    def _apply_calibration(self) -> None:
        self._persist_preferences()

    def _apply_guardrails(self) -> None:
        self._persist_preferences()

    def _reset_latency(self) -> None:
        self._session.reset_latency_metrics()

    def _reset_emergency_stop(self) -> None:
        self._session.reset_emergency_stop()
        self._append("System", "Emergency stop reset. Actions can run again if policy allows.")
        self._refresh_action_guardrail_label()

    def _approve_pending_action(self) -> None:
        message, followup = self._session.approve_pending_action()
        self._append("System", message)
        if followup:
            self._append("Assistant", followup)
            spoken = self._session.tts_text_for_followup(followup)
            if spoken:
                self._session.speak_text(spoken)
        self._refresh_action_guardrail_label()

    def _reject_pending_action(self) -> None:
        message = self._session.reject_pending_action()
        self._append("System", message)
        self._refresh_action_guardrail_label()

    def _refresh_audio_devices(self) -> None:
        pass

    def _refresh_models(self) -> None:
        self._status_label.setText(self._ready_status_text())

    def _on_model_changed(self) -> None:
        self._persist_preferences()

    def _refresh_tts_providers(self) -> None:
        pass

    def _refresh_tts_voices(self) -> None:
        pass

    def _on_tts_provider_changed(self) -> None:
        self._persist_preferences()

    def _on_tts_voice_changed(self) -> None:
        self._persist_preferences()

    @classmethod
    def _stt_model_to_profile(cls, model_name: str) -> str:
        model = str(model_name or "").strip().lower()
        for profile, mapped_model in cls._STT_PROFILE_TO_MODEL.items():
            if model == mapped_model:
                return profile
        return "accurate"

    def _refresh_stt_profiles(self) -> None:
        pass

    def _on_stt_profile_changed(self) -> None:
        self._persist_preferences()

    def _refresh_model_debug_label(self) -> None:
        pass

    def _refresh_goal_debug_label(self) -> None: pass
    def _refresh_action_guardrail_label(self) -> None: pass
    def _refresh_latency_strip(self) -> None:
        m = self._session.get_last_metrics()
        if not m or m.get("mode") == "idle":
            return
        parts = []
        llm = m.get("llm_ms", 0)
        tts = m.get("tts_ms", 0)
        stt = m.get("stt_ms", 0)
        total = m.get("total_ms", 0)
        if stt:
            parts.append(f"STT {stt:.0f}ms")
        if llm:
            parts.append(f"LLM {llm:.0f}ms")
        if tts:
            parts.append(f"TTS {tts:.0f}ms")
        if total:
            parts.append(f"total {total:.0f}ms")
        if parts:
            timing = " | ".join(parts)
            self._status_label.setText(f"Ready | {timing}{self._ctx_status_part()}")

    def _persist_preferences(
        self,
        *,
        include_stt_testing: bool = False,
        trace_filters: dict[str, bool] | None = None,
        trace_limit: int | None = None,
        trace_window_x: int | None = None,
        trace_window_y: int | None = None,
        trace_window_width: int | None = None,
        trace_window_height: int | None = None,
        dialog_geometries: dict[str, dict[str, int]] | None = None,
        sync: bool = False,
    ) -> None:
        import threading
        state = self._session.state
        kwargs = dict(
            chat_model=self._session.chat_model,
            remember_history=self._session.remember_history,
            autonomy_mode=self._session.autonomy_mode,
            microphone_device=self._session.microphone_device,
            output_device=getattr(self._session, "output_device", None),
            vad_level_threshold=self._session.vad_level_threshold,
            vad_silence_seconds=self._session.vad_silence_seconds,
            live_input_mode=getattr(self._session, "live_input_mode", None),
            live_ptt_type=getattr(self._session, "live_ptt_type", None),
            live_ptt_key=getattr(self._session, "live_ptt_key", None),
            live_ptt_mouse_button=getattr(self._session, "live_ptt_mouse_button", None),
            live_ptt_toggle=getattr(self._session, "live_ptt_toggle", None),
            barge_in_enabled=self._session.barge_in_enabled() if hasattr(self._session, "barge_in_enabled") else None,
            action_min_interval_seconds=self._session.action_min_interval_seconds,
            tts_provider=self._session.tts_provider,
            tts_voice=self._session.tts_voice,
            pocket_tts_voice=getattr(self._session._settings.tts, "pocket_tts_voice", None),
            pocket_tts_temp=getattr(self._session._settings.tts, "pocket_tts_temp", None),
            stt_model=self._session.stt_model,
            stt_diagnostic_record_seconds=None,
            stt_diagnostic_vad_filter=None,
            stt_diagnostic_initial_prompt=None,
            stt_prosody_enabled=None,
            stt_prosody_include_in_prompt=None,
            enable_microphone=state.mic_enabled,
            window_x=self.x(),
            window_y=self.y(),
            window_width=self.width(),
            window_height=self.height(),
            ui_decision_trace_filters=trace_filters,
            ui_decision_trace_limit=trace_limit,
            ui_decision_trace_window_x=trace_window_x,
            ui_decision_trace_window_y=trace_window_y,
            ui_decision_trace_window_width=trace_window_width,
            ui_decision_trace_window_height=trace_window_height,
            ui_dialog_geometries=dialog_geometries,
        )
        if sync:
            save_runtime_preferences(**kwargs)
        else:
            threading.Thread(
                target=save_runtime_preferences, kwargs=kwargs,
                daemon=True, name="save-prefs",
            ).start()

    def _send(self) -> None:
        if self._turn_thread is not None:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        if self._live_thread is not None:
            self._append("System", "Stop Live mode before sending typed messages.")
            return

        if self._session.is_tts_playing():
            self._session.stop_tts()

        self._append("You", text)
        self._input.setPlainText("")
        self._start_single_turn(mode="typed", text=text)

    def _set_single_turn_controls_busy(self, busy: bool) -> None:
        self._guardrail_controls_locked = busy
        self._send_button.setEnabled(not busy)
        self._live_toggle_button.setEnabled(not busy)
        self._clear_chat_button.setEnabled(not busy)
        self._input.setEnabled(not busy)
        self._settings_button.setEnabled(not busy)

    def _start_single_turn(self, *, mode: str, text: str = "", record_seconds: float = 5.0) -> None:
        if self._turn_thread is not None:
            return

        self._turn_mode = mode
        self._stream_speaker = "Assistant"
        self._reset_live_stream("")
        self._set_single_turn_controls_busy(True)
        if mode == "record":
            self._status_label.setText("Recording...")
        else:
            self._status_label.setText(f"Generating...{self._ctx_status_part()}")

        self._turn_thread = QThread(self)
        self._turn_worker = SingleTurnWorker(
            self._session,
            mode=mode,
            text=text,
            record_seconds=record_seconds,
        )
        self._turn_worker.moveToThread(self._turn_thread)

        self._turn_thread.started.connect(self._turn_worker.run)
        self._turn_worker.status.connect(self._on_worker_status)
        self._turn_worker.status.connect(self._on_status_for_transcript)
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

    def _on_voice_turn_done(self, user_text: str, reply: str) -> None:
        self._append("You (voice)", user_text)
        if not self._live_stream_open:
            self._append("Assistant", reply)
        self._close_live_stream()
        self._refresh_latency_strip()

    def _on_single_turn_failed(self, message: str) -> None:
        title = "Voice error" if self._turn_mode == "record" else "Assistant error"
        QMessageBox.critical(self, title, message)

    def _on_single_turn_finished(self) -> None:
        self._set_single_turn_controls_busy(False)
        self._refresh_latency_strip()

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
        self._live_worker.status.connect(self._on_worker_status)
        self._live_worker.status.connect(self._on_status_for_transcript)
        self._live_worker.level.connect(self._on_live_audio_level)
        self._live_worker.heard.connect(lambda text: self._append("You (live)", text))
        self._live_worker.heard.connect(self._reset_live_stream)
        self._live_worker.replying.connect(self._append_live_stream_token)
        self._live_worker.replied.connect(self._on_live_replied)
        self._live_worker.proactive.connect(lambda text: self._append("Assistant", text))
        self._live_worker.failed.connect(self._on_live_error)
        self._live_worker.stopped.connect(self._on_live_stopped)
        self._live_worker.stopped.connect(self._live_thread.quit)
        self._live_thread.finished.connect(self._on_live_thread_finished)
        self._live_thread.finished.connect(self._live_thread.deleteLater)
        self._live_worker.stopped.connect(self._live_worker.deleteLater)

        self._send_button.setEnabled(False)
        self._live_toggle_button.setText("Stop Live")
        self._live_toggle_button.setProperty("danger", True)
        _refresh_button_style(self._live_toggle_button)
        self._live_toggle_button.setEnabled(True)
        self._clear_chat_button.setEnabled(False)
        self._input.setEnabled(False)
        self._settings_button.setEnabled(False)

        if getattr(self._session, "live_input_mode", "") == "push_to_talk":
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
                self._ptt_app_filter_installed = True
        else:
            self._ptt_app_filter_installed = False

        self._live_thread.start()

    def _stop_live_mode(self) -> None:
        if self._live_worker is not None:
            self._live_worker.stop()
        self._live_toggle_button.setEnabled(False)
        self._status_label.setText("Stopping...")

    def _on_live_error(self, message: str) -> None:
        QMessageBox.critical(self, "Live mode error", message)

    def _on_live_stopped(self) -> None:
        self._session.set_ptt_active(False)
        if getattr(self, "_ptt_app_filter_installed", False):
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._ptt_app_filter_installed = False
        self._send_button.setEnabled(True)
        self._live_toggle_button.setText("Start Live")
        self._live_toggle_button.setProperty("danger", False)
        _refresh_button_style(self._live_toggle_button)
        self._live_toggle_button.setEnabled(True)
        self._clear_chat_button.setEnabled(True)
        self._input.setEnabled(True)
        self._settings_button.setEnabled(True)
        self._status_label.setText(self._ready_status_text())
        self._close_live_stream()
        level_bar = getattr(self, "_input_level_bar", None)
        if level_bar is not None:
            level_bar.setValue(0)
        level_debug = getattr(self, "_input_level_debug_label", None)
        if level_debug is not None:
            level_debug.setText(
                f"Mic raw=0.0000 | threshold={self._session.vad_level_threshold:.4f} | below"
            )

        self._live_worker = None
        self._guardrail_controls_locked = False

    def _on_live_thread_finished(self) -> None:
        self._live_thread = None

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._force_quit and self._tray_icon is not None and self._tray_icon.isVisible():
            event.ignore()
            self.hide()
            return

        import threading as _threading
        _threading.Timer(5.0, lambda: os._exit(0)).start()

        if self._tray_icon is not None:
            self._tray_icon.hide()

        self._persist_preferences(sync=True)
        self._stop_live_mode()

        shutdown_thread = _threading.Thread(target=self._session.shutdown, daemon=True)
        shutdown_thread.start()
        shutdown_thread.join(timeout=3.0)

        for thread in (
            self._turn_thread,
            self._stt_test_thread,
            self._live_thread,
        ):
            if thread is not None:
                thread.quit()
                if not thread.wait(1500):
                    thread.terminate()
                    thread.wait(500)
        super().closeEvent(event)
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _on_live_toggle_clicked(self) -> None:
        if self._live_thread is not None:
            self._stop_live_mode()
        else:
            self._start_live_mode()

    def _trim_old_messages(self) -> None:
        if self._conversation is None or self._message_count <= self._max_visible_messages:
            return
        doc = self._conversation.document()
        excess = self._message_count - self._max_visible_messages
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(excess):
            table = cursor.currentTable()
            if table is not None:
                table.removeRows(0, table.rows())
                continue
            if not cursor.movePosition(QTextCursor.MoveOperation.Down, QTextCursor.MoveMode.KeepAnchor):
                break
        cursor.removeSelectedText()
        self._message_count = self._max_visible_messages

    def _on_web_page_loaded(self, ok: bool) -> None:
        self._web_page_ready = ok
        if ok and self._pending_js:
            for js in self._pending_js:
                self._conversation_web.page().runJavaScript(js)
            self._pending_js.clear()

    def _run_web_js(self, js: str) -> None:
        if self._conversation_web is None:
            return
        if self._web_page_ready:
            self._conversation_web.page().runJavaScript(js)
        else:
            self._pending_js.append(js)

    def _do_search(self) -> None:
        query = self._search_input.text().strip()
        self._last_search_query = query
        if not query:
            self._search_results.setVisible(False)
            return
        db = getattr(self._session, "_chat_db", None)
        if db is None:
            return
        session_id = getattr(self._session, "_session_id", "main")
        user_id = getattr(self._session, "_user_id", "default")
        key = f"{user_id}:{session_id}" if user_id else session_id
        try:
            all_msgs = db.get_messages(key)
            q_lower = query.lower()
            matches = [m for m in all_msgs if q_lower in m.content.lower()]
            self._search_results.clear()
            if not matches:
                self._search_results.setVisible(False)
                return
            from PySide6.QtWidgets import QListWidgetItem
            for m in matches[-20:]:
                preview = m.content[:100].replace("\n", " ")
                item = QListWidgetItem(f"[{m.role}] {m.created_at[:16]}: {preview}")
                item.setData(Qt.ItemDataRole.UserRole, {"role": m.role, "content": m.content, "created_at": m.created_at})
                self._search_results.addItem(item)
            self._search_results.setVisible(True)
        except Exception:
            self._search_results.setVisible(False)

    def _on_search_result_scroll(self, item) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data or not isinstance(data, dict):
            return
        content = data.get("content", "")
        snippet = content[:80].replace("\n", " ").strip()
        if not snippet:
            return
        if self._conversation_web is not None:
            js = f"window.find({json.dumps(snippet)});"
            self._run_web_js(js)
        elif self._conversation is not None:
            cursor = self._conversation.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self._conversation.setTextCursor(cursor)
            found = self._conversation.find(snippet)
            if found:
                self._conversation.ensureCursorVisible()

    def _on_search_result_clicked(self, item) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and isinstance(data, dict):
            role = data.get("role", "?").title()
            content = data.get("content", "")
            created = data.get("created_at", "")[:16]
            msg = QMessageBox(self)
            msg.setWindowTitle(f"Message from {role} ({created})")
            msg.setText(content[:2000])
            msg.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            msg.exec()
        self._search_results.setVisible(False)

    def _clear_conversation_view(self) -> None:
        if self._conversation_web is not None:
            self._run_web_js("clearMessages();")
        elif self._conversation is not None:
            self._conversation.clear()
        self._message_count = 0

    

    def _on_worker_status(self, status: str) -> None:
        """Enrich worker status strings with context usage info."""
        s = (status or "").strip()
        if not s or s == "ready":
            return
        lower = s.lower()
        if lower.startswith("listening") or lower.startswith("recording"):
            self._status_label.setText(s)
            return
        ctx = self._ctx_status_part()
        self._status_label.setText(f"{s}{ctx}" if ctx else s)

    def _on_status_for_transcript(self, status: str) -> None:
        """Append tool-use (and similar) status messages to the transcript."""
        s = (status or "").strip()
        if not s:
            return
        lower = s.lower()
        if "tool" in lower or lower.startswith("tool "):
            self._append_tool_output(s)
            return

    def _append_tool_output(self, text: str) -> None:
        """Show tool output in a collapsible block."""
        preview = text[:80].replace("\n", " ")
        if len(text) > 80:
            preview += "..."
        if self._conversation_web is not None:
            content_html = (
                f"<details><summary style='cursor:pointer;font-size:12px;color:#64748b;'>"
                f"{html.escape(preview)}</summary>"
                f"<pre style='white-space:pre-wrap;font-size:11px;color:#475569;margin:4px 0;'>"
                f"{html.escape(text)}</pre></details>"
            )
            js = f"appendMessage('Background', {json.dumps(content_html)});"
            self._run_web_js(js)
            return
        self._append("Background", preview)

    @staticmethod
    def _classify_speaker(label: str) -> str:
        """Return 'user', 'assistant', 'system', or 'default' for a speaker label."""
        lower = label.lower()
        if lower in {"you", "user"} or lower.startswith("you ") or lower.startswith("user "):
            return "user"
        if lower == "assistant" or lower.startswith("assistant "):
            return "assistant"
        if lower == "system":
            return "system"
        return "default"

    def _append(self, speaker: str, text: str, assistant_model: str | None = None, timestamp: str | None = None) -> None:
        speaker_label = str(speaker or "Assistant").strip() or "Assistant"
        kind = self._classify_speaker(speaker_label)
        if kind == "assistant" and assistant_model is None:
            assistant_model = getattr(self._session, "effective_chat_model", None)
        if assistant_model and speaker_label.lower() == "assistant":
            speaker_label = f"Assistant ({assistant_model})"
        is_assistant = kind == "assistant"
        ts_display = ""
        if timestamp:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                ts_display = dt.strftime("%H:%M")
            except Exception:
                ts_display = timestamp
        if self._conversation_web is not None:
            try:
                from app.ui.markdown_renderer import markdown_to_html
                content_html = markdown_to_html(text) if is_assistant else html.escape(str(text or "")).replace("\n", "<br>")
            except ImportError:
                content_html = html.escape(str(text or "")).replace("\n", "<br>")
            js = f"appendMessage({json.dumps(speaker_label)}, {json.dumps(content_html)}, {json.dumps(ts_display)});"
            self._run_web_js(js)
            return
        safe_speaker = html.escape(speaker_label)
        if is_assistant:
            try:
                from app.ui.markdown_renderer import markdown_to_html
                safe_text = markdown_to_html(text)
            except ImportError:
                safe_text = html.escape(str(text or "")).replace("\n", "<br>")
        else:
            safe_text = html.escape(str(text or "")).replace("\n", "<br>")
        bubble_bg = BUBBLE_DEFAULT_BG
        bubble_text_color = BUBBLE_DEFAULT_TEXT
        speaker_color = BUBBLE_DEFAULT_SPEAKER
        if kind == "user":
            bubble_bg = BUBBLE_USER_BG
            bubble_text_color = BUBBLE_USER_TEXT
            speaker_color = BUBBLE_USER_SPEAKER
        elif kind == "assistant":
            bubble_bg = BUBBLE_ASSISTANT_BG
            bubble_text_color = BUBBLE_ASSISTANT_TEXT
            speaker_color = BUBBLE_ASSISTANT_SPEAKER
        elif kind == "system":
            bubble_bg = BUBBLE_SYSTEM_BG
            bubble_text_color = BUBBLE_SYSTEM_TEXT
            speaker_color = BUBBLE_SYSTEM_SPEAKER

        ts_html = f"<span style='float:right; font-size:10px; color:#94a3b8;'>{html.escape(ts_display)}</span>" if ts_display else ""
        self._conversation.append(
            (
                "<table width='100%' cellspacing='0' cellpadding='0' style='margin:8px 0;'>"
                "<tr><td "
                f"style='background-color:{bubble_bg}; color:{bubble_text_color}; border-radius:8px; padding:8px 10px;'>"
                f"<div style='font-size:12px; color:{speaker_color}; margin-bottom:4px;'><b>{safe_speaker}</b>{ts_html}</div>"
                f"<div style='color:{bubble_text_color};'>{safe_text}</div>"
                "</td></tr></table>"
            )
        )
        self._message_count += 1
        self._trim_old_messages()
        self._scroll_conversation_to_bottom()

    def _scroll_conversation_to_bottom(self) -> None:
        if self._conversation is not None:
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
        self._live_stream_buffer += token
        if self._conversation_web is not None:
            if not self._live_stream_open:
                speaker = self._stream_speaker or "Assistant"
                self._run_web_js(f"startStreamBubble({json.dumps(speaker)});")
                self._live_stream_open = True
            self._pending_tokens.append(token)
            if not self._stream_flush_timer.isActive():
                self._stream_flush_timer.start()
            return
        self._pending_tokens.append(token)
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()

    def _flush_stream_tokens(self) -> None:
        if not self._pending_tokens:
            self._stream_flush_timer.stop()
            return
        chunk = "".join(self._pending_tokens)
        self._pending_tokens.clear()
        if self._conversation_web is not None:
            self._run_web_js(f"appendStreamToken({json.dumps(chunk)});")
            return
        if not self._live_stream_open:
            self._conversation.append(f"<b>{self._stream_speaker}:</b> ")
            self._live_stream_open = True
        self._conversation.moveCursor(QTextCursor.MoveOperation.End)
        self._conversation.insertPlainText(chunk)
        self._conversation.ensureCursorVisible()

    def _on_live_replied(self, text: str) -> None:
        if not self._live_stream_open:
            self._append("Assistant", text)
            self._close_live_stream()
        else:
            self._close_live_stream(final_text=text)
        self._refresh_latency_strip()
        self._refresh_goal_debug_label()

    def _close_live_stream(self, _text: str | None = None, *, final_text: str | None = None) -> None:
        if self._pending_tokens:
            self._flush_stream_tokens()
        self._stream_flush_timer.stop()
        if self._live_stream_open and self._conversation_web is not None and final_text:
            try:
                from app.ui.markdown_renderer import markdown_to_html
                content_html = markdown_to_html(final_text)
            except ImportError:
                content_html = html.escape(str(final_text or "")).replace("\n", "<br>")
            self._run_web_js(f"finalizeStreamBubble({json.dumps(content_html)});")
        elif self._live_stream_open and self._conversation_web is not None:
            self._run_web_js("finalizeStreamBubble(null);")
        if self._live_stream_open:
            self._scroll_conversation_to_bottom()
        self._live_stream_open = False

    def _on_live_audio_level(self, level: float) -> None:
        now = _time.monotonic()
        if now - self._last_audio_level_time < 0.20:
            return
        self._last_audio_level_time = now
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
        level_bar = getattr(self, "_input_level_bar", None)
        if level_bar is not None:
            level_bar.setValue(percent)
            level_bar.setFormat(f"{percent}%")
        state = "ABOVE" if level >= threshold else "below"
        level_debug = getattr(self, "_input_level_debug_label", None)
        if level_debug is not None:
            level_debug.setText(
                f"Mic raw={level:.4f} | floor={self._live_noise_floor:.4f} | threshold={threshold:.4f} | {state}"
            )

    

    def _on_autonomy_mode_changed(self) -> None:
        self._persist_preferences()

    def _on_session_type_changed(self) -> None:
        self._persist_preferences()
