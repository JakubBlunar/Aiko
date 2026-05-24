# Agent Instructions

## Project Overview

Aiko is a web-based AI companion built around:

- **Python 3.11+** backend (FastAPI + WebSocket) under `app/`. Entry point: `python -m app.web` (or the `aiko-web` console script).
- **React + Vite + PixiJS** frontend under `web/` (Live2D avatar, chat, voice controls, settings drawer, document upload).
- **Ollama** for chat (via `OllamaClient` directly, not LangChain). The `chat_llm` block can route to any OpenAI-compatible endpoint instead.
- **RealtimeSTT** + **Pocket-TTS** for voice in/out.
- **LanceDB** for vector RAG over memories, recent chat messages, and uploaded documents.
- **SQLite** (`data/chat_sessions.db`) as the source of truth for messages, summaries, and memory metadata.

There is no desktop / Qt / LangChain code. The web UI is the only UI.

## Embedded MCP Server

The app exposes an MCP server on `http://localhost:6274/sse` for development tooling. Start the app first, then connect any MCP client.

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
| `send_message` | `message: str`, `skip_tts: bool = false` | Assistant response text. The web UI updates live as the message streams. |
| `get_status` | — | JSON: model name, context window, TTS engine, agent tool count, recent metrics. |
| `list_agent_tools` | — | JSON array of `{name, description}` for every agent tool currently registered. |
| `get_last_response_detail` | — | JSON timing breakdown for the last turn (`llm_ms`, `tts_ms`, etc.). |
| `clear_history` | — | Clears the active session in `chat_sessions.db`. |

### Resources

| URI | Content |
|-----|---------|
| `assistant://history` | Recent conversation messages (JSON). |
| `assistant://config` | Current settings snapshot (JSON). |

### Debugging Workflow

1. **Confirm connection**: Call `get_status` — verify `model`, `tool_count`, and `tts.engine`.
2. **Test agent**: Call `send_message` with `skip_tts: true` to avoid audio playback during automated testing.
3. **Check timing**: Call `get_last_response_detail` — `llm_ms` is the model time, `tts_ms` is speech synthesis time.
4. **Read logs**: The app console prints tool registry rebuilds, `TurnRunner` two-pass execution, and proactive nudges at INFO level.

### Adding Custom MCP Tools

If the existing tools are not enough for your debugging scenario, add new ones directly in `app/mcp/server.py`. The server has full access to `SessionController` and all internal state.

```python
@mcp.tool()
def my_debug_tool(some_arg: str) -> str:
    """Description of what this tool does."""
    # Access any internal state via the `session` reference:
    #   session._settings, session._chat_db, session._memory_store,
    #   session._rag_store, session._tool_registry, etc.
    # The app must be restarted for new tools to take effect.
    return "result"
```

You are encouraged to add any MCP tool you need to debug a problem. Common examples: inspecting agent message history mid-turn, dumping the system prompt, reading TTS queue state, checking embedding search results, or triggering specific `SessionController` methods. After adding a tool, restart the app and it will appear automatically.

### Architecture Notes

- `app/mcp/server.py` — FastMCP server definition with all tools/resources. Add new tools here.
- `app/mcp/runner.py` — Runs uvicorn in a daemon thread; stops on app shutdown.
- `app/core/session_controller.py` — Starts the MCP server in `__init__`, stops in `shutdown()`. Message listeners notify the web UI of MCP-triggered messages over WebSocket.
- Config: `config/default.json` key `mcp_server` (`enabled`, `port`).

## Code Conventions

- Python 3.11+, dataclasses + slots for settings, FastAPI for the HTTP/WebSocket layer.
- LLM calls go through `app/llm/ollama_client.py` (`chat`, `chat_stream`, `chat_with_tools`). Tool dispatch happens in `app/core/turn_runner.py` via a pre-stream `chat_with_tools` pass; the streaming reply runs immediately after.
- The agent's available tools are built once per turn from `config.tools` by `SessionController.rebuild_tool_registry()` — see `app/llm/tools/builtins.py` for the live set (`get_time`, `recall`, `web_search`).
- TTS text processing (`prepare_tts_text` in `app/core/session_text_utils.py`) applies only to the spoken stream, not the chat transcript.
- SQLite (`data/chat_sessions.db`) is the source of truth; LanceDB (`data/lancedb/`) mirrors `memories`, asynchronously indexes `messages`, and holds chunked uploaded `documents`.
- Inline tags Aiko emits: `[[reaction:...]]` (mood, drives Live2D expression and TTS prosody) and `[[remember:...]]` / `[[remember:self:...]]` (writes a long-term memory). Both are stripped from the spoken/chat output.
- Frontend state lives in `web/src/store.ts` (Zustand). The WebSocket hook (`web/src/hooks/useAssistantSocket.ts`) is the single point that mutates store state from server events.
