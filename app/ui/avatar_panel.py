"""AvatarPanel and AvatarOverlayWindow.

The panel wraps a transparent QWebEngineView pointing at
``resources/avatar/index.html`` and registers an :class:`AvatarBridge` on a
QWebChannel. The same widget can live inside the main window (embedded mode)
or be reparented into a frameless, translucent, always-on-top
:class:`AvatarOverlayWindow` (overlay mode).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget

from app.ui.avatar_bridge import AvatarBridge

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AVATAR_HTML = _PROJECT_ROOT / "resources" / "avatar" / "index.html"
_CUBISM_CORE = _PROJECT_ROOT / "resources" / "vendor" / "live2d" / "live2dcubismcore.min.js"


def webengine_available() -> bool:
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        from PySide6.QtWebChannel import QWebChannel  # noqa: F401
    except Exception:
        return False
    return True


def cubism_core_available() -> bool:
    return _CUBISM_CORE.is_file()


def avatar_page_url() -> QUrl:
    return QUrl.fromLocalFile(str(_AVATAR_HTML))


class AvatarPanel(QWidget):
    """Hosts the Live2D web view + QWebChannel bridge.

    The widget is created once and may be reparented between the main window
    and an :class:`AvatarOverlayWindow`. The bridge survives reparenting.
    """

    readyChanged = Signal(bool)
    errorReported = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent;")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self.bridge = AvatarBridge(self)
        self._channel: QObject | None = None
        self._view: QWidget | None = None
        self._ready = False
        self._page_loaded = False
        self._pending_model: tuple[str, dict] | None = None

        self._build_view()

        self.bridge.readyReceived.connect(self._on_ready)
        self.bridge.errorReceived.connect(self.errorReported.emit)
        self.bridge.initialStateRequested.connect(self._replay_pending_model)

    # ------------------------------------------------------------------
    def _build_view(self) -> None:
        if not webengine_available():
            from PySide6.QtWidgets import QLabel

            placeholder = QLabel(
                "QtWebEngine is not installed. Install the 'persona' extra:\n"
                "    pip install PySide6-WebEngine\n"
                "then restart the app."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #f8fafc; background: rgba(15,23,42,180); padding: 12px;")
            placeholder.setWordWrap(True)
            self._layout.addWidget(placeholder)
            self._view = placeholder
            return

        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView

        view = QWebEngineView(self)
        view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        try:
            page = view.page()
            page.setBackgroundColor(Qt.GlobalColor.transparent)
        except Exception:
            pass

        self._channel = QWebChannel(view)
        try:
            self._channel.registerObject("bridge", self.bridge)
            view.page().setWebChannel(self._channel)
        except Exception:
            logger.exception("Failed to attach QWebChannel to avatar view")

        view.loadFinished.connect(self._on_load_finished)
        if not _AVATAR_HTML.is_file():
            from PySide6.QtWidgets import QLabel

            placeholder = QLabel(
                f"Avatar HTML not found at {_AVATAR_HTML}.\nReinstall the project resources."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #f8fafc; background: rgba(15,23,42,180); padding: 12px;")
            placeholder.setWordWrap(True)
            self._layout.addWidget(placeholder)
            self._view = placeholder
            return

        view.setUrl(avatar_page_url())
        self._layout.addWidget(view, stretch=1)
        self._view = view

    def _on_load_finished(self, ok: bool) -> None:
        self._page_loaded = bool(ok)
        if not ok:
            logger.warning("Avatar page failed to load")

    def _on_ready(self, payload: dict) -> None:
        self._ready = True
        self.readyChanged.emit(True)
        logger.info("Avatar ready: %s", payload)

    def _replay_pending_model(self) -> None:
        if self._pending_model is not None:
            url, cfg = self._pending_model
            self.bridge.push_model(url, cfg)

    # ------------------------------------------------------------------
    # Public API used by MainWindow
    # ------------------------------------------------------------------
    def set_model(self, model_path: str | Path | None, config: dict | None = None) -> None:
        """Tell the avatar view to load a ``.model3.json`` file."""
        config = dict(config or {})
        if not model_path:
            self._pending_model = ("", config)
            self.bridge.push_model("", config)
            return
        path = Path(model_path)
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        if not path.is_file():
            logger.warning("Avatar model not found: %s", path)
            self.errorReported.emit(f"Model file not found: {path}")
            self._pending_model = ("", config)
            self.bridge.push_model("", config)
            return
        url = QUrl.fromLocalFile(str(path)).toString()
        self._pending_model = (url, config)
        self.bridge.push_model(url, config)

    def set_overlay_mode(self, overlay: bool) -> None:
        self.bridge.push_overlay_mode(bool(overlay))

    def on_tts_event(self, event: str, **kwargs) -> None:
        """Slot for :meth:`SessionController.add_tts_state_listener`."""
        if event == "start":
            envelope = kwargs.get("envelope") or []
            sample_rate = int(kwargs.get("sample_rate") or 24000)
            reaction = str(kwargs.get("reaction") or "")
            self.bridge.push_speaking_start(envelope, sample_rate, reaction)
        elif event == "end":
            self.bridge.push_speaking_end()

    def trigger_expression(self, reaction: str) -> None:
        self.bridge.push_expression(reaction)


class AvatarOverlayWindow(QMainWindow):
    """Frameless translucent always-on-top host for the persona.

    The :class:`AvatarPanel` is reparented in/out by ``set_content``. Mouse drag
    moves the window (driven by the JS side via ``onDragMoved``).
    """

    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")
        self.resize(360, 540)

        self._container = QWidget(self)
        self._container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._container.setStyleSheet("background: transparent;")
        self.setCentralWidget(self._container)
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(0)

        self._panel: AvatarPanel | None = None
        self._drag_origin: tuple[int, int] | None = None

    def set_content(self, panel: AvatarPanel | None) -> None:
        # Remove any existing child widget from the layout (do not delete).
        for i in reversed(range(self._container_layout.count())):
            item = self._container_layout.itemAt(i)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self._panel = panel
        if panel is not None:
            self._container_layout.addWidget(panel)
            panel.set_overlay_mode(True)

    def closeEvent(self, event) -> None:  # noqa: D401 - Qt override
        if self._panel is not None:
            self._panel.set_overlay_mode(False)
        self.closed.emit()
        super().closeEvent(event)

    # Allow dragging the frameless window from anywhere (in addition to
    # the JS handle, which emits onDragMoved through the bridge).
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = (event.globalPosition().toPoint().x() - self.x(),
                                 event.globalPosition().toPoint().y() - self.y())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            gp = event.globalPosition().toPoint()
            self.move(gp.x() - self._drag_origin[0], gp.y() - self._drag_origin[1])
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def apply_bridge_drag(self, dx: int, dy: int) -> None:
        """Move the window by ``(dx, dy)`` in response to a JS drag event."""
        self.move(self.x() + int(dx), self.y() + int(dy))


def status_message() -> str:
    """One-line summary of why the persona is enabled or disabled."""
    bits: list[str] = []
    if not webengine_available():
        bits.append("QtWebEngine missing")
    if not _AVATAR_HTML.is_file():
        bits.append("avatar HTML missing")
    if not cubism_core_available():
        bits.append("Cubism core missing")
    return ", ".join(bits) if bits else "ok"


def serialize_config(persona) -> dict:
    """Pull the JS-facing config out of a PersonaSettings dataclass."""
    return {
        "expression_map": dict(getattr(persona, "expression_map", {}) or {}),
        "scale": float(getattr(persona, "scale", 0.25) or 0.25),
        "anchor": str(getattr(persona, "anchor", "bottom-center") or "bottom-center"),
        "mirror": bool(getattr(persona, "mirror", False)),
        "lip_sync_gain": float(getattr(persona, "lip_sync_gain", 1.2) or 1.2),
    }


# Re-export for callers that want to dump config eagerly
__all__ = [
    "AvatarPanel",
    "AvatarOverlayWindow",
    "avatar_page_url",
    "webengine_available",
    "cubism_core_available",
    "serialize_config",
    "status_message",
]
