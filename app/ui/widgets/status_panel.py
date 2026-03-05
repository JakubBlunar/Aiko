from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout


class StatusPanel(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Status")
        self._service = QLabel("Service: idle")
        self._capture = QLabel("Capture: mic=off, screen=off")
        self._model = QLabel("Model: unknown")
        self._autonomy = QLabel("Autonomy: mode=interactive, session=chat")
        self._current_session = QLabel("Current Session: chat")

        layout = QVBoxLayout()
        layout.addWidget(self._service)
        layout.addWidget(self._capture)
        layout.addWidget(self._model)
        layout.addWidget(self._autonomy)
        layout.addWidget(self._current_session)
        self.setLayout(layout)

    def set_service_status(self, text: str) -> None:
        self._service.setText(f"Service: {text}")

    def set_capture_status(self, mic: bool, screen: bool) -> None:
        self._capture.setText(
            f"Capture: mic={'on' if mic else 'off'}, "
            f"screen={'on' if screen else 'off'}"
        )

    def set_model(self, model: str) -> None:
        self._model.setText(f"Model: {model}")

    def set_autonomy_status(self, *, mode: str, session_type: str) -> None:
        self._autonomy.setText(f"Autonomy: mode={mode}, session={session_type}")

    def set_current_session(self, session_type: str) -> None:
        self._current_session.setText(f"Current Session: {session_type}")
