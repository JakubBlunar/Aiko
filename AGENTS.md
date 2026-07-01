# Agent Instructions

Aiko is a local-first, web-based AI companion. This file is the **lean entry
point** — a short overview plus the hard rules that always apply. Detailed
references live in [`rules/`](rules/); read the relevant file **on demand**
instead of loading everything up front (see the index at the bottom).

## Project Overview

Aiko is built around:

- **Python 3.11+** backend (FastAPI + WebSocket) under `app/`. Entry point: `python -m app.web` (or the `aiko-web` console script).
- **React + Vite + PixiJS** frontend under `web/` (Live2D avatar, chat, voice controls, settings drawer, document upload).
- **Ollama** for chat (via `OllamaClient` directly, not LangChain). The `chat_llm` block can route to any OpenAI-compatible endpoint instead.
- **RealtimeSTT** + **Pocket-TTS** for voice in/out, with **client-owned audio I/O**: the browser / Tauri shell captures the microphone (48 kHz Int16 mono, browser DSP) and plays back TTS, streaming raw PCM frames over the existing WebSocket. See [`docs/voice-mode.md`](docs/voice-mode.md) for the binary frame protocol and the voice-ownership lock used when multiple windows are open.
- **LanceDB** for vector RAG over memories, recent chat messages, and uploaded documents.
- **SQLite** (`data/chat_sessions.db`) as the source of truth for messages, summaries, and memory metadata.

There is no desktop / Qt / LangChain code. The web UI is the only UI.

## Core rules (always apply)

- **No LangChain / LangGraph in `app/llm/`.** The chat path is direct HTTP to Ollama; `langchain-openai`'s `ChatOpenAI` is used *only* for the `openai_compatible` provider router.
- **No PySide6 / Qt.** The web UI is the only UI.
- **Don't use f-strings** for print/log lines that have no interpolated variables.
- **Don't add emojis to source files** unless the user explicitly asks.
- **TTS text processing** (`prepare_tts_text`) applies to the spoken stream only, never the chat transcript.
- **Long-term memory writes go through `MemoryStore.add(...)`** (SQLite is the source of truth); the LanceDB mirror is handled by `MemoryStore` itself.
- **Inline tags Aiko emits** — `[[reaction:…]]`, `[[remember:…]]` / `[[remember:self:…]]`, `[[prosody:…]]`, `[[arc:…]]`, `[[goal:…]]`, `[[predict:…]]`, `[[touch:…]]`, `[[conflict:…]]`, stage-direction earcons, … — are stripped from the spoken/transcript output before TTS / persistence.
- **The tool registry is built per-turn** from settings; never instantiate tools inside loops. When adding a tool, put its "what/when/sync-vs-async" description in its `schema()` (not the persona) and add its name to `_TOOL_FAMILY` in `app/core/session/tool_pass_gate.py`.
- **When adding a prompt block**, pick the tier matching its lifetime, append it inside that tier's cluster, and add its name to `_PROMPT_BLOCK_TIERS` (`app/core/session/prompt_assembler.py`) — the T0→T6 prefix-stability ladder protects the OpenAI prompt cache.
- **File size**: keep Python files below ~1,500 lines and React/TS components below ~1,000; split via feature mixins (`app/core/<area>/*_mixin.py`) or feature folders (`web/src/components/<feature>/`) before a file passes ~2,500 lines.
- **Persona** (`data/persona/aiko_companion.txt`) is user-editable; every user-name reference must stay the literal `{user_name}` placeholder (any other `{…}` token crashes `.format()`).

For the *why* behind any of these — and for anything not listed here — read the
matching reference file below.

## Reference index (`rules/`)

Read on demand; don't load all of it up front:

- [`rules/mcp-server.md`](rules/mcp-server.md) — the embedded **MCP debug server** (`http://localhost:6274/sse`): how to connect, the core tools, and adding your own. **First stop for interacting with / debugging the live app.**
- [`rules/code-conventions.md`](rules/code-conventions.md) — the **subsystem reference catalogue**: architecture conventions plus per-feature design notes (LLM providers & prompt cache, memory tiers & RAG, the K-series personality/affect/relationship features, avatar / Live2D, Tauri shell, tasks / brain orchestration, external MCP clients, …). Grep it for the area you're about to change.
- [`rules/debugging.md`](rules/debugging.md) — the **log stream**: where to look, line shape + canonical fields, the symptom → grep-target table, level cheat sheet, and workflow.

Deeper design docs live under [`docs/`](docs/) (linked from the files above).
The Cursor-specific short ruleset is in [`.cursorrules`](.cursorrules).
