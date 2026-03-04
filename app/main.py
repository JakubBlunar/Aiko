from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.core.settings import load_settings
from app.ui.main_window import MainWindow


def main() -> int:
    settings = load_settings()
    app = QApplication(sys.argv)
    window = MainWindow(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
