from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.crash_logging import install_global_exception_hooks
from app.core.session_controller import SessionController
from app.core.settings import load_settings
from app.ui.main_window import MainWindow
from app.ui.startup_preloader import StartupPreloaderDialog


def main() -> int:
    install_global_exception_hooks()
    settings = load_settings()
    app = QApplication(sys.argv)

    preloader = StartupPreloaderDialog(settings)
    preloader.show()
    app.processEvents()

    try:
        session = SessionController(settings)
        session.prewarm_runtime(on_status=lambda message: (preloader.set_status(message), app.processEvents()))
    except Exception as exc:
        preloader.close()
        QMessageBox.critical(
            None,
            "Startup warmup failed",
            str(exc),
        )
        return 1

    preloader.close()

    window = MainWindow(settings, session=session)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
