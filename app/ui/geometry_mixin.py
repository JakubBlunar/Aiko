"""Reusable mixin for persisting QDialog geometry (position + size)."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent, QMoveEvent, QResizeEvent


class PersistentGeometryMixin:
    """Mix into a QDialog to auto-save / restore window position and size.

    Usage::

        class MyDialog(PersistentGeometryMixin, QDialog):
            def __init__(self, ..., initial_geometry=None, persist_geometry=None):
                super().__init__(parent)
                self.init_geometry(
                    initial=initial_geometry or {},
                    default_width=800, default_height=600,
                    persist_callback=persist_geometry,
                )
    """

    _geom_persist_cb: Callable[[dict[str, int]], None] | None
    _geom_debounce: QTimer

    def init_geometry(
        self,
        *,
        initial: dict[str, int] | None = None,
        default_width: int = 640,
        default_height: int = 480,
        min_width: int = 300,
        min_height: int = 220,
        persist_callback: Callable[[dict[str, int]], None] | None = None,
    ) -> None:
        geo = initial or {}
        w = max(min_width, geo.get("width", default_width))
        h = max(min_height, geo.get("height", default_height))
        self.resize(w, h)  # type: ignore[attr-defined]
        if "x" in geo and "y" in geo:
            self.move(geo["x"], geo["y"])  # type: ignore[attr-defined]

        self._geom_persist_cb = persist_callback
        self._geom_debounce = QTimer(self)  # type: ignore[arg-type]
        self._geom_debounce.setSingleShot(True)
        self._geom_debounce.setInterval(500)
        self._geom_debounce.timeout.connect(self._flush_geometry)

    def _flush_geometry(self) -> None:
        if callable(self._geom_persist_cb):
            self._geom_persist_cb(
                {
                    "x": self.x(),  # type: ignore[attr-defined]
                    "y": self.y(),  # type: ignore[attr-defined]
                    "width": self.width(),  # type: ignore[attr-defined]
                    "height": self.height(),  # type: ignore[attr-defined]
                }
            )

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)  # type: ignore[misc]
        self._geom_debounce.start()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)  # type: ignore[misc]
        self._geom_debounce.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._flush_geometry()
        super().closeEvent(event)  # type: ignore[misc]
