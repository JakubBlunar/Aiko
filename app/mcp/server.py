"""FastMCP server definition with tools and resources for the live assistant."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from app.core.session_controller import SessionController

log = logging.getLogger("app.mcp.server")


_session_ref: "SessionController | None" = None


def _get_mcp_manager() -> Any | None:
    """Get the MCP manager from the active session's agent."""
    if _session_ref is None:
        return None
    agent = getattr(_session_ref, "_agent", None)
    if agent is None:
        return None
    return getattr(agent, "mcp_manager", None)


def _find_mcp_tool(name: str) -> Any | None:
    """Look up a Playwright MCP tool by name from the running manager."""
    manager = _get_mcp_manager()
    if manager is None:
        return None
    for t in manager.tools:
        if getattr(t, "name", None) == name:
            return t
    return None


def _call_mcp_tool(name: str, **kwargs: Any) -> str:
    """Call a Playwright MCP tool synchronously via the persistent event loop."""
    manager = _get_mcp_manager()
    tool = _find_mcp_tool(name)
    if tool is None:
        return f"Tool '{name}' not available (no browser session or MCP manager not running)."
    if manager is None:
        return "MCP manager not running."
    coro_fn = getattr(tool, "coroutine", None)
    if coro_fn is None:
        func = getattr(tool, "func", None)
        if func is not None:
            return str(func(**kwargs))
        return f"Tool '{name}' has no callable."
    return str(manager.run_coro(coro_fn(**kwargs)))


def create_mcp_server(session: SessionController, port: int = 6274) -> FastMCP:
    """Build a FastMCP server wired to the live *session*."""
    global _session_ref
    _session_ref = session

    mcp = FastMCP("assistant", host="127.0.0.1", port=port)

    # ------------------------------------------------------------------
    # Tools: assistant interaction
    # ------------------------------------------------------------------

    @mcp.tool()
    def send_message(message: str, skip_tts: bool = False) -> str:
        """Send a message to the assistant and return its full response.

        The assistant UI updates live (chat bubble, browser windows, etc.).
        Set skip_tts=True to suppress text-to-speech during testing.
        """
        session._notify_message("You (MCP)", message)
        original_tts_enabled = session._settings.tts.enabled
        if skip_tts:
            session._settings.tts.enabled = False
        try:
            response = session.chat_once(message)
        finally:
            session._settings.tts.enabled = original_tts_enabled
        session._notify_message("Assistant", response or "")
        return response or "(empty response)"

    @mcp.tool()
    def get_status() -> str:
        """Get current assistant status: model, context window, tools, TTS, last metrics."""
        agent = getattr(session, "_agent", None)
        tool_names: list[str] = []
        if agent and hasattr(agent, "_tools"):
            tool_names = [getattr(t, "name", str(t)) for t in agent._tools]

        info = {
            "model": session.effective_chat_model,
            "context_window": session.context_window_size,
            "tts_provider": session.tts_provider,
            "tts_voice": session.tts_voice,
            "tts_enabled": session._settings.tts.enabled,
            "agent_tools_count": len(tool_names),
            "agent_tools": tool_names,
            "mcp_manager_active": _get_mcp_manager() is not None,
            "last_metrics": session.get_last_metrics(),
        }
        return json.dumps(info, indent=2, default=str)

    @mcp.tool()
    def list_agent_tools() -> str:
        """List all tools the assistant agent has access to, with descriptions."""
        agent = getattr(session, "_agent", None)
        if not agent or not hasattr(agent, "_tools"):
            return "No agent or tools available."
        entries: list[dict[str, str]] = []
        for t in agent._tools:
            entries.append({
                "name": getattr(t, "name", "?"),
                "description": getattr(t, "description", "")[:200],
            })
        return json.dumps(entries, indent=2)

    @mcp.tool()
    def get_last_response_detail() -> str:
        """Get the last assistant response with timing metrics."""
        metrics = session.get_last_metrics()
        return json.dumps(metrics, indent=2, default=str)

    @mcp.tool()
    def clear_history() -> str:
        """Clear the conversation history for the current session."""
        db = getattr(session, "_chat_db", None)
        if db is None:
            return "No chat database available."
        sid = getattr(session, "_session_id", "main")
        uid = getattr(session, "_user_id", "default")
        key = f"{uid}:{sid}" if uid else sid
        try:
            db.clear_messages(key, full_reset=True)
            return f"History cleared for session '{key}'."
        except Exception as exc:
            return f"Failed to clear history: {exc}"

    # ------------------------------------------------------------------
    # Tools: browser inspection (via Playwright MCP)
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_browser_snapshot() -> str:
        """Get the accessibility tree / text snapshot of the current browser page.

        This is the same view the agent sees when deciding what to click.
        """
        return _call_mcp_tool("browser_snapshot")

    @mcp.tool()
    def get_browser_screenshot() -> str:
        """Take a screenshot of the current browser page."""
        return _call_mcp_tool("browser_take_screenshot")

    @mcp.tool()
    def get_browser_url() -> str:
        """Get the current browser tabs and URLs."""
        return _call_mcp_tool("browser_tabs")

    @mcp.tool()
    def get_browser_console() -> str:
        """Get recent browser console messages (errors, warnings, debug)."""
        return _call_mcp_tool("browser_console_messages")

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    @mcp.resource("assistant://history")
    def get_history() -> str:
        """Recent conversation messages from the database."""
        db = getattr(session, "_chat_db", None)
        if db is None:
            return "No chat database."
        sid = getattr(session, "_session_id", "main")
        uid = getattr(session, "_user_id", "default")
        key = f"{uid}:{sid}" if uid else sid
        try:
            rows = db.get_messages(key, limit=40)
            entries = [{"role": r.role, "content": r.content[:500], "created_at": str(r.created_at)} for r in rows]
            return json.dumps(entries, indent=2, default=str)
        except Exception as exc:
            return f"Error reading history: {exc}"

    @mcp.resource("assistant://config")
    def get_config() -> str:
        """Current assistant configuration snapshot."""
        s = session._settings
        info = {
            "model": s.ollama.chat_model,
            "base_url": s.ollama.base_url,
            "temperature": s.ollama.temperature,
            "context_window": s.ollama.context_window,
            "tts_provider": s.tts.provider,
            "tts_voice": s.tts.voice,
            "tts_enabled": s.tts.enabled,
            "stt_model": s.stt.model,
            "stt_language": s.stt.language,
            "mcp_server_port": s.mcp_server.port,
        }
        return json.dumps(info, indent=2, default=str)

    agent = getattr(session, "_agent", None)
    tool_count = len(agent._tools) if agent and hasattr(agent, "_tools") else 0
    log.info("MCP server created with %d agent tools", tool_count)
    return mcp
