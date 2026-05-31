"""Headless entry point: ``python -m app.web``.

Boots the SessionController without Qt, starts the web server (and the
embedded MCP server, if enabled in config), and blocks until SIGINT.
Open http://localhost:6275 in a browser to use the React UI.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from app.core.infra.crash_logging import (
    configure_logging_full,
    install_global_exception_hooks,
)
from app.core.session.session_controller import SessionController
from app.core.infra.settings import load_settings
from app.web.runner import WebServerRunner
from app.web.server import create_web_app


log = logging.getLogger("app.web")


def main() -> int:
    install_global_exception_hooks()
    settings = load_settings()
    logging_settings = getattr(settings, "logging", None)
    log_level = (
        os.environ.get("LOG_LEVEL")
        or getattr(logging_settings, "level", None)
        or "INFO"
    )
    configure_logging_full(
        level_name=log_level,
        module_levels=getattr(logging_settings, "module_levels", None) or {},
        file_enabled=bool(getattr(logging_settings, "file_enabled", True)),
        file_path=getattr(logging_settings, "file_path", None),
        file_max_bytes=int(getattr(logging_settings, "file_max_bytes", 5 * 1024 * 1024)),
        file_backup_count=int(getattr(logging_settings, "file_backup_count", 5)),
    )

    log.info("Booting Aiko (web mode)...")
    session = SessionController(settings)
    try:
        session.prewarm_runtime(on_status=lambda msg: log.info("[startup] %s", msg))
    except Exception as exc:
        log.warning("Prewarm failed: %s", exc)

    web_settings = getattr(settings, "web_server", None)
    web_port = int(getattr(web_settings, "port", 6275)) if web_settings is not None else 6275
    web_host = str(getattr(web_settings, "host", "127.0.0.1") or "127.0.0.1")

    if web_settings is not None and not getattr(web_settings, "enabled", True):
        log.warning("web_server.enabled is False in config; running in CLI-only mode.")
        runner: WebServerRunner | None = None
    else:
        app = create_web_app(session)
        runner = WebServerRunner(app, host=web_host, port=web_port)
        runner.start()
        log.info("Open http://%s:%d in your browser to chat with Aiko.", web_host, web_port)

    stop_event = threading.Event()

    def _shutdown(*_: object) -> None:
        log.info("Shutdown requested.")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        pass

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                log.debug("web runner stop failed", exc_info=True)
        try:
            session.shutdown()
        except Exception:
            log.debug("session shutdown failed", exc_info=True)

    log.info("Goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
