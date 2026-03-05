from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout


class StatusPanel(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Status")
        self._service = QLabel("Service: idle")
        self._capture = QLabel("Capture: mic=off, screen=off")
        self._model = QLabel("Model: unknown")

        layout = QVBoxLayout()
        layout.addWidget(self._service)
        layout.addWidget(self._capture)
        layout.addWidget(self._model)
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
