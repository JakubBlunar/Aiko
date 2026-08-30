"""MCP window onto the C6 activity collection pipeline."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_activity_timeline(limit: int = 20) -> str:
        """C6 — dump recent activity sessions and the last envelope.

        Collection only: what was stored after redaction. Does not
        interpret. ``registered_sources`` is the Python handler set
        (mirrors the Rust cheap sources). Unknown sources never persist.
        """
        try:
            report = session.activity_timeline_snapshot(limit=limit)
        except Exception as exc:
            log.debug("get_activity_timeline failed", exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps(report, default=str)
