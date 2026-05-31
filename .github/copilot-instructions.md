# Copilot Instructions

Aiko is a Python 3.11+ web app: FastAPI/WebSocket backend (`app/`) plus a React + Vite + PixiJS frontend (`web/`). Entry point: `python -m app.web` (or the `aiko-web` console script).

## MCP Server for Debugging

The running app exposes an MCP server at `http://localhost:6274/sse`. Use it to send messages, list available tools, and check turn timings programmatically. See `AGENTS.md` in the repo root for full tool documentation and setup.

Key tools: `send_message`, `get_status`, `list_agent_tools`, `get_last_response_detail`, `clear_history`.

## Code Style

- LLM calls go through `app/llm/ollama_client.py` (`chat`, `chat_stream`, `chat_with_tools`). No LangChain/LangGraph in `app/llm/`.
- Tool dispatch is a two-pass turn handled in `app/core/session/turn_runner.py`: a pre-stream `chat_with_tools` pass for tool calls, then a streaming reply pass.
- The tool registry (`app/llm/tools/`) is rebuilt per turn from `config.tools`. Built-in tools: `get_time`, `recall`, `web_search`.
- TTS text processing (`prepare_tts_text` in `app/core/session/session_text_utils.py`) applies to the spoken stream only, not the chat transcript.
- SQLite (`data/chat_sessions.db`) is the source of truth for messages, summaries, and memory metadata. LanceDB (`data/lancedb/`) mirrors `memories`, indexes `messages`, and holds chunked uploaded `documents`.
- Config: `config/default.json` (defaults) + `config/user.json` (overrides), loaded via `app/core/infra/settings.py`.
- Inline tags Aiko emits: `[[reaction:...]]` (mood) and `[[remember:...]]` / `[[remember:self:...]]` (memory writes). Both are stripped from the spoken/transcript output by the response-text service.
