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

    @mcp.tool()
    def get_conversation_judgment_shadow(
        include_rows: bool = False, include_evidence: bool = False, limit: int = 20,
    ) -> str:
        """Read-only shadow counts; opt in to rows or private source excerpts (max 100)."""
        try:
            report = session.conversation_judgment_shadow_diagnostics(
                include_rows=include_rows, include_evidence=include_evidence, limit=limit,
            )
        except Exception as exc:
            log.debug("get_conversation_judgment_shadow failed", exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps(report, default=str)
