"""Background thread that runs the FastAPI/WS web server via uvicorn.

Mirrors :mod:`app.mcp.runner` so the two services can coexist on different
ports under one Python process.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from fastapi import FastAPI


log = logging.getLogger("app.web.runner")


class WebServerRunner:
    """Manage a uvicorn instance for the FastAPI/WS app on a daemon thread."""

    def __init__(self, app: "FastAPI", *, host: str = "127.0.0.1", port: int = 6275) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._server: object | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="web-uvicorn",
        )
        self._thread.start()
        log.info(
            "Web server starting on http://%s:%d (open this URL in your browser)",
            self._host, self._port,
        )

    def _run(self) -> None:
        import uvicorn

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            loop="asyncio",
            ws_ping_interval=20.0,
            ws_ping_timeout=20.0,
        )
        self._server = uvicorn.Server(config)
        try:
            loop.run_until_complete(self._server.serve())
        except SystemExit:
            log.warning("Web server exited (port %d may be in use)", self._port)
        except Exception:
            log.exception("Web server crashed")
        finally:
            loop.close()

    def stop(self) -> None:
        if self._server is not None and hasattr(self._server, "should_exit"):
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        log.info("Web server stopped")
