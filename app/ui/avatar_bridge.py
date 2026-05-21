"""Qt <-> JS bridge for the Live2D avatar.

Lives in the same thread as the QWebEngineView. Python emits signals to drive
the avatar (model swap, speaking envelope, expression, motion). JS calls
slots back to report state, log lines, drag deltas, etc.
"""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)


class AvatarBridge(QObject):
    """Object exposed to the avatar webview via QWebChannel.

    All signals are connected on the JS side using
    ``bridge.<signal>.connect(callback)``. Slots are invoked with
    ``bridge.<slot>(arg, ...)``.
    """

    # --- Python -> JS -------------------------------------------------
    modelChanged = Signal(str, str)  # absoluteUrl, configJson
    speakingStart = Signal(str, int, str)  # envelopeJson, sampleRate, reaction
    speakingEnd = Signal()
    expressionRequested = Signal(str)  # reaction name (lower-case key)
    motionRequested = Signal(str, int)  # group, index
    overlayModeChanged = Signal(bool)

    # --- JS -> Python (re-emitted as Qt signals for the panel) --------
    readyReceived = Signal(dict)
    errorReceived = Signal(str)
    dragReceived = Signal(int, int)
    initialStateRequested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._last_model_url = ""
        self._last_config: dict = {}
        self._overlay_mode = False

    # ------------------------------------------------------------------
    # API used by the AvatarPanel
    # ------------------------------------------------------------------
    def push_model(self, absolute_url: str, config: dict | None = None) -> None:
        self._last_model_url = str(absolute_url or "")
        self._last_config = dict(config or {})
        payload = json.dumps(self._last_config, ensure_ascii=True)
        self.modelChanged.emit(self._last_model_url, payload)

    def push_speaking_start(
        self,
        envelope: list[float],
        sample_rate: int,
        reaction: str | None,
    ) -> None:
        try:
            envelope_json = json.dumps([float(v) for v in (envelope or [])])
        except Exception:
            envelope_json = "[]"
        self.speakingStart.emit(envelope_json, int(sample_rate or 24000), str(reaction or ""))

    def push_speaking_end(self) -> None:
        self.speakingEnd.emit()

    def push_expression(self, reaction: str) -> None:
        self.expressionRequested.emit(str(reaction or ""))

    def push_motion(self, group: str, index: int = 0) -> None:
        self.motionRequested.emit(str(group or ""), int(index or 0))

    def push_overlay_mode(self, overlay: bool) -> None:
        self._overlay_mode = bool(overlay)
        self.overlayModeChanged.emit(self._overlay_mode)

    # ------------------------------------------------------------------
    # Slots invoked by JS
    # ------------------------------------------------------------------
    @Slot(str)
    def onReady(self, payload_json: str) -> None:
        try:
            data = json.loads(payload_json or "{}")
        except Exception:
            data = {}
        self.readyReceived.emit(data if isinstance(data, dict) else {})

    @Slot(str)
    def onError(self, message: str) -> None:
        logger.warning("avatar: %s", message)
        self.errorReceived.emit(str(message))

    @Slot(int, int)
    def onDragMoved(self, dx: int, dy: int) -> None:
        self.dragReceived.emit(int(dx), int(dy))

    @Slot(str)
    def onLog(self, message: str) -> None:
        logger.debug("avatar: %s", message)

    @Slot()
    def requestInitialState(self) -> None:
        """JS asks for the current model + overlay mode after page load."""
        self.initialStateRequested.emit()
        if self._last_model_url:
            self.modelChanged.emit(
                self._last_model_url, json.dumps(self._last_config, ensure_ascii=True)
            )
        self.overlayModeChanged.emit(self._overlay_mode)
