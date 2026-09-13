"""MCP diagnostics for the Live shadow impulse bus."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from app.core.live.diagnostics import sanitize_live_dump

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_live_impulse_snapshot() -> str:
        """Live posture, generation, bus telemetry, and a short impulse tail."""
        try:
            report = sanitize_live_dump(session.live_impulse_diagnostics())
        except Exception as exc:
            log.debug("get_live_impulse_snapshot failed", exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps(report, default=str)

    @mcp.tool()
    def get_live_situation_frame() -> str:
        """Inspectable Live situation frame, policy proposal, journal."""
        try:
            report = sanitize_live_dump(session.live_situation_diagnostics())
        except Exception as exc:
            log.debug("get_live_situation_frame failed", exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps(report, default=str)
