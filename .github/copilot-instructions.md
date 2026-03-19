# Copilot Instructions

This is a Python 3.11+ PySide6 desktop assistant with Ollama LLM, voice I/O, browser automation, and pluggable TTS.

## MCP Server for Debugging

The running app exposes an MCP server at `http://localhost:6274/sse`. Use it to send messages, inspect browser state, and check agent behavior programmatically. See `AGENTS.md` in the repo root for full tool documentation and setup.

Key tools: `send_message`, `get_status`, `get_browser_snapshot`, `get_last_response_detail`.

## Code Style

- Reuse agents — never create in loops.
- TTS text processing (`prepare_tts_text`) is for the spoken part only, not the chat transcript.
- Use `_extract_text()` for AI message content (handles both string and list formats).
- The retry mechanism in `_react_stream_with_retry` handles models that produce tool calls without text.
- SQLite for development, Postgres for production. Chat history uses a custom schema in `app/core/chat_database.py`.
- Config: `config/default.json` (defaults) + `config/user.json` (overrides), loaded via `app/core/settings.py`.
