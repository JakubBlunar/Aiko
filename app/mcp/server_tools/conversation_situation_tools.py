"""MCP diagnostics for the typed conversation-situation facade."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_conversation_situation() -> str:
        """Present semantic/world snapshot, extractor stats, and mover gate."""
        try:
            report = session.conversation_situation_diagnostics()
        except Exception as exc:
            log.debug("get_conversation_situation failed", exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps(report, default=str)
