"""FastMCP debug server for the lean Aiko app.

Exposes a small surface for Cursor / VSCode MCP clients to drive the running
session: send messages, inspect status, clear history, peek at the latest
metrics. Browser-snapshot and agent-tool tools from the legacy build are
gone -- v1 has no agent tools yet.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP


if TYPE_CHECKING:
    from app.core.session_controller import SessionController


log = logging.getLogger("app.mcp.server")
_session_ref: "SessionController | None" = None


def create_mcp_server(session: "SessionController", port: int = 6274) -> FastMCP:
    """Build a FastMCP server wired to the live ``session``."""
    global _session_ref
    _session_ref = session
    mcp = FastMCP("assistant", host="127.0.0.1", port=port)

    # ── Tools ────────────────────────────────────────────────────────

    @mcp.tool()
    def send_message(message: str, skip_tts: bool = False) -> str:
        """Send a message to Aiko and return her full response.

        The UI updates live (chat bubble, etc.). Set ``skip_tts=True`` to
        suppress audio playback during automated testing.
        """
        session._notify_message("You (MCP)", message)
        original = session._settings.tts.enabled
        if skip_tts:
            session._settings.tts.enabled = False
        try:
            response = session.chat_once(message)
        finally:
            session._settings.tts.enabled = original
        session._notify_message("Assistant", response or "")
        return response or "(empty response)"

    @mcp.tool()
    def get_status() -> str:
        """Return JSON: model, context window, TTS state, last metrics."""
        info = {
            "model": session.effective_chat_model,
            "context_window": session.context_window_size,
            "tts_provider": session.tts_provider,
            "tts_voice": session.tts_voice,
            "tts_enabled": session._settings.tts.enabled,
            "session_key": session.session_key,
            "live_mode": getattr(session, "_live_voice_session_active", False),
            "last_metrics": session.get_last_metrics(),
        }
        return json.dumps(info, indent=2, default=str)

    @mcp.tool()
    def get_last_response_detail() -> str:
        """Return the last turn's full timing + token usage as JSON."""
        return json.dumps(session.get_last_metrics(), indent=2, default=str)

    @mcp.tool()
    def clear_history() -> str:
        """Wipe the active session's conversation memory."""
        try:
            session.clear_conversation_memory()
            return f"History cleared for session '{session.session_key}'."
        except Exception as exc:
            return f"Failed to clear history: {exc}"

    @mcp.tool()
    def get_learner_profile() -> str:
        """Return the current English-tutor profile rows for this session."""
        try:
            db = session._chat_db
            notes = db.get_personality_notes(session.session_key)
        except Exception as exc:
            return f"Error: {exc}"
        out = [
            {
                "category": n.category,
                "note": n.note,
                "confidence": round(float(n.confidence), 3),
                "updated_at": n.updated_at,
            }
            for n in notes
        ]
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def list_agent_tools() -> str:
        """Return JSON list of agent tools (empty in v1, hooked in via TurnRunner later)."""
        return json.dumps([], indent=2)

    # ── Resources ────────────────────────────────────────────────────

    @mcp.resource("assistant://history")
    def get_history() -> str:
        """Recent conversation messages (most recent 40)."""
        try:
            rows = session._chat_db.get_messages(session.session_key, limit=40)
            entries = [
                {"role": r.role, "content": r.content[:500], "created_at": str(r.created_at)}
                for r in rows
            ]
            return json.dumps(entries, indent=2, default=str)
        except Exception as exc:
            return f"Error reading history: {exc}"

    @mcp.resource("assistant://config")
    def get_config() -> str:
        """Current assistant configuration snapshot."""
        s = session._settings
        info = {
            "model": session.effective_chat_model,
            "base_url": s.ollama.base_url,
            "temperature": s.ollama.temperature,
            "context_window": session.context_window_size,
            "tts_provider": s.tts.provider,
            "tts_voice": s.tts.voice,
            "tts_enabled": s.tts.enabled,
            "stt_model": s.stt.model,
            "stt_language": s.stt.language,
            "mcp_server_port": s.mcp_server.port,
        }
        return json.dumps(info, indent=2, default=str)

    log.info("MCP server created (lean v1)")
    return mcp
