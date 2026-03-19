# Agent Instructions

## Project Overview

Python 3.11+ PySide6 desktop assistant with voice I/O, LLM chat (Ollama), browser automation (Playwright MCP), and pluggable TTS. Entry point: `python -m app.main`.

## Embedded MCP Server

The app exposes an MCP server on `http://localhost:6274/sse` for programmatic interaction and debugging. Start the app first, then connect any MCP client.

### Cursor Setup

Already configured in `.cursor/mcp.json`. The tools appear as native MCP tools — call them directly, no wrapper scripts needed.

### VSCode / Copilot Setup

Add to your MCP settings (`.vscode/mcp.json` or user settings):

```json
{
  "servers": {
    "assistant": {
      "type": "sse",
      "url": "http://localhost:6274/sse"
    }
  }
}
```

### Tools

| Tool | Args | Returns |
|------|------|---------|
| `send_message` | `message: str`, `skip_tts: bool = false` | Assistant response text. UI updates live. |
| `get_status` | — | JSON: model, context window, tool count, TTS, metrics. |
| `list_agent_tools` | — | JSON array of `{name, description}` for all agent tools. |
| `get_last_response_detail` | — | JSON timing breakdown of the last turn. |
| `clear_history` | — | Clears conversation session in the DB. |
| `get_browser_snapshot` | — | Accessibility tree of the current browser page. |
| `get_browser_screenshot` | — | Screenshot of the browser page. |
| `get_browser_url` | — | Current tab URLs. |
| `get_browser_console` | — | Recent console messages. |

### Resources

| URI | Content |
|-----|---------|
| `assistant://history` | Recent conversation messages (JSON). |
| `assistant://config` | Current settings snapshot (JSON). |

### Debugging Workflow

1. **Confirm connection**: Call `get_status` — verify model name, tool count, and `mcp_manager_active: true`.
2. **Test agent**: Call `send_message` with `skip_tts: true` to avoid audio playback during testing.
3. **Inspect browser**: After a browser task, call `get_browser_snapshot` to see what the agent saw, or `get_browser_screenshot` for a visual.
4. **Check performance**: Call `get_last_response_detail` — `llm_ms` is the model time, `tts_ms` is speech synthesis time.
5. **Read logs**: The app console prints `Stream pass complete:` (tool call counts, AI text length) and `Nudging agent:` (retry trigger) at INFO level.

### Adding Custom MCP Tools

If the existing tools are not enough for your debugging scenario, add new ones directly in `app/mcp/server.py`. The server has full access to `SessionController` and all internal state.

```python
@mcp.tool()
def my_debug_tool(some_arg: str) -> str:
    """Description of what this tool does."""
    # Access any internal state via the `session` reference:
    #   session._agent, session._settings, session._chat_db, etc.
    # Call Playwright MCP tools via _call_mcp_tool("browser_*", ...)
    # The app must be restarted for new tools to take effect.
    return "result"
```

You are encouraged to add any MCP tool you need to debug a problem. Common examples: inspecting agent message history mid-turn, dumping the system prompt, reading TTS queue state, checking embedding search results, or triggering specific SessionController methods. After adding a tool, restart the app and it will appear automatically.

### Architecture Notes

- `app/mcp/server.py` — FastMCP server definition with all tools/resources. Add new tools here.
- `app/mcp/runner.py` — Runs uvicorn in a daemon thread; stops on app shutdown.
- `app/core/session_controller.py` — Starts the MCP server in `__init__`, stops in `shutdown()`. Message listeners notify the UI of MCP-triggered messages.
- Browser tools proxy through the existing `_mcp_manager` in `app/llm/langchain_agent.py` which holds the persistent Playwright session.
- Config: `config/default.json` key `mcp_server` (`enabled`, `port`).

## Code Conventions

- Python 3.11+, PySide6 for UI, LangChain/LangGraph for agents.
- Agent is created once and reused (`create_react_agent`). Never recreate in loops.
- TTS text processing (`prepare_tts_text`) applies only to the spoken part, not the chat transcript.
- SQLite for chat history (`data/chat_sessions.db`), with token-aware trimming and rolling summaries.
- The react agent retry mechanism (`_react_stream_with_retry`) nudges the model when it calls tools without producing text.
