"""Background thread that runs the FastMCP SSE server via uvicorn."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger("app.mcp.runner")


class McpServerRunner:
    """Manages a uvicorn server on a daemon thread for the embedded MCP SSE endpoint."""

    def __init__(
        self, mcp_server: FastMCP, port: int = 6274, host: str = "127.0.0.1",
    ) -> None:
        self._mcp = mcp_server
        self._port = port
        self._host = host
        self._server: object | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the uvicorn server in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mcp-sse-server",
        )
        self._thread.start()
        # Log the bind address rather than a hardcoded loopback: when this
        # says 0.0.0.0 the tool surface is reachable off-box, which is worth
        # seeing in the log without having to go read the config.
        log.info("MCP SSE server starting on http://%s:%d/sse", self._host, self._port)

    def _run(self) -> None:
        import uvicorn

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        app = self._mcp.sse_app()
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        try:
            loop.run_until_complete(self._server.serve())
        except SystemExit:
            log.warning("MCP SSE server exited (port %d may be in use)", self._port)
        except Exception:
            log.exception("MCP SSE server crashed")
        finally:
            loop.close()

    def stop(self) -> None:
        """Signal the uvicorn server to shut down gracefully."""
        if self._server is not None and hasattr(self._server, "should_exit"):
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        log.info("MCP SSE server stopped")
