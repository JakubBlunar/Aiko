from __future__ import annotations

import os
import sys
import warnings

warnings.filterwarnings("ignore", message="urllib3.*chardet.*charset_normalizer")

from PySide6.QtCore import QEventLoop, QThread
from PySide6.QtWidgets import QApplication

from app.core.crash_logging import configure_logging, install_global_exception_hooks
from app.core.session_controller import SessionController
from app.core.settings import load_settings
from app.ui.main_window import MainWindow
from app.ui.startup_preloader import StartupPreloaderDialog, StartupPrewarmWorker, show_startup_error
from app.ui.theme import get_light_palette, get_stylesheet


def main() -> int:
    install_global_exception_hooks()
    settings = load_settings()
    log_level = os.environ.get("LOG_LEVEL") or getattr(getattr(settings, "logging", None), "level", None)
    configure_logging(log_level)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(get_light_palette())
    app.setStyleSheet(get_stylesheet())

    preloader = StartupPreloaderDialog(settings)
    preloader.show()

    thread = QThread()
    worker = StartupPrewarmWorker(settings)
    worker.moveToThread(thread)

    startup_loop = QEventLoop()
    startup_error: dict[str, str | None] = {"message": None}
    startup_session: dict[str, SessionController | None] = {"session": None}
    startup_cancelled: list[bool] = [False]

    def on_ready(session_obj: object) -> None:
        if isinstance(session_obj, SessionController):
            startup_session["session"] = session_obj
        startup_loop.quit()

    def on_failed(message: str) -> None:
        startup_error["message"] = str(message)
        startup_loop.quit()

    def on_preloader_closed() -> None:
        if not startup_session["session"] and not startup_error["message"]:
            startup_cancelled[0] = True
            startup_loop.quit()

    preloader.finished.connect(on_preloader_closed)

    worker.status.connect(preloader.set_status)
    worker.ready.connect(on_ready)
    worker.failed.connect(on_failed)

    worker.ready.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.started.connect(worker.run)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()

    startup_loop.exec()
    preloader.close()
    preloader.finished.disconnect(on_preloader_closed)
    if not startup_cancelled[0]:
        thread.wait(1500)

    if startup_cancelled[0]:
        # Worker is blocking in prewarm; quit() has no effect. Wait then force-stop so process can exit.
        thread.quit()
        if not thread.wait(3000):
            thread.terminate()
            thread.wait(500)
        app.quit()
        return 0
    if startup_error["message"]:
        show_startup_error(str(startup_error["message"]))
        return 1

    session = startup_session["session"]
    if session is None:
        show_startup_error("Startup did not return a ready session.")
        return 1

    app.setQuitOnLastWindowClosed(True)
    window = MainWindow(settings, session=session)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
