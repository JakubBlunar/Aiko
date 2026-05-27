# Aiko

Local, private, web-based AI companion. Talk or type, get streaming text + spoken responses, watch a Live2D avatar react, and ground replies in your own documents — all running on your machine.

Built around:

- **Ollama** for chat (local, or any OpenAI-compatible endpoint via the `chat_llm` block).
- **RealtimeSTT** (faster-whisper + Silero VAD) for speech input.
- **Pocket-TTS** for low-latency speech output.
- **LanceDB** for vector RAG over long-term memories, recent chat messages, and user-uploaded documents.
- **FastAPI + React/Vite + PixiJS** for the web UI and Live2D avatar.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.11+ (3.13 supported)
- Node.js 20+ (only for the React frontend dev server)
- Microphone and speakers
- An Ollama install with a chat model and an embedding model

## Setup

### 1. Install Ollama and pull models

- **Windows:** [ollama.com/download/windows](https://ollama.com/download/windows)
- **macOS:** `brew install ollama`
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`

Pull a chat model and the embedding model used for RAG (defaults below match `config/default.json`):

```powershell
ollama pull jaahas/qwen3.5-uncensored:9b   # or any chat model you prefer
ollama pull qwen3-embedding:0.6b
```

### 2. Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

The console script `aiko-web` is installed as a shortcut for `python -m app.web`.

### 3. Frontend dependencies

```powershell
cd web
npm install
cd ..
```

For day-to-day development the top-level `npm run dev` script (in `package.json`) starts both the Python web server and Vite together.

## Run

```powershell
# Backend + frontend in one shot (recommended for development)
npm run dev

# Or backend only:
python -m app.web
# (then open http://127.0.0.1:6275)
```

The Python process boots:

- The `SessionController` (chat, memory, RAG, tools).
- The FastAPI/WebSocket app on `http://127.0.0.1:6275`.
- The embedded MCP server on `http://127.0.0.1:6274/sse` (used for debugging — see `AGENTS.md`).

In dev mode Vite proxies `/api` and `/ws` to the Python server.

## Configure

User-editable defaults live in `config/default.json`. Personal overrides go in `config/user.json` and are deep-merged on top.

| Block | Purpose |
|---|---|
| `assistant` | `name`, `remember_history`, `user_id`, `tts_length_scale` |
| `ollama` | `base_url`, `chat_model`, `embedding_model`, `temperature`, `context_window`, `timeout` |
| `chat_llm` | Routes the chat call. `provider: "ollama"` (default) or `"openai_compatible"` for OpenAI / xAI / Groq / OpenRouter / DeepSeek / etc. |
| `audio` | Sample rate, microphone/output device, VAD thresholds, push-to-talk |
| `stt` | `model` (e.g. `large-v1`), `language` |
| `tts` | `provider` (`pocket-tts`), `voice`, `enabled`, `pocket_tts_voice`, `pocket_tts_temp` |
| `agent` | `proactive_silence_seconds`, `proactive_cooldown_seconds` |
| `memory` | `enabled`, `top_k`, `score_threshold`, `max_memories`, `dedupe_threshold`, `extractor_enabled`, `self_tagged_salience` |
| `tools` | `enabled` plus per-tool flags: `get_time`, `recall`, `web_search` |
| `web_server` | `host`, `port` (default `127.0.0.1:6275`) |
| `mcp_server` | `enabled`, `port` (default `6274`) |

Set `LOG_LEVEL=DEBUG` (env var) or `logging.level` in config to control verbosity.

## Tools

The lean v1 tool registry exposes three tools to the chat model via Ollama's native function-calling:

| Tool | Returns |
|---|---|
| `get_time` | Current ISO date/time. |
| `recall` | Semantic search across memories, recent messages, and uploaded documents (LanceDB). |
| `web_search` | DuckDuckGo lite results. |

The agent runs a pre-stream `chat_with_tools` pass; if a tool call appears, it executes, appends the result, then runs the streaming reply pass. Toggle each tool from the web Settings drawer or in `config.tools`.

## Memory and RAG

- `data/chat_sessions.db` — SQLite source of truth for messages, rolling summaries, and long-term memory metadata.
- `data/lancedb/` — Vector store: a `memories` table mirrored from SQLite, an asynchronously-indexed `messages` table, and a chunked `documents` table.
- `data/documents/` — Originals of files uploaded through the **Documents** section of the web Settings drawer (.md, .txt, .pdf supported).
- Aiko can also self-tag memories inline using `[[remember:self:...]]`. See `data/persona/aiko_companion.txt`.

## Live2D avatar

- The bundled avatar (Alexia by default) lives at `data/personas/active/Alexia/`. The directory is gitignored so each developer drops their own copy in. `app/core/avatar_profile.py` reads `*.model3.json` + `*.cdi3.json` at boot, infers a capability map (pajamas, blush, sweat, cat tail, glasses, …), and serves the files at `/avatar/`. (The legacy `live-2d-models/Alexia/` path is still honoured if you point `config/user.json -> avatar.root_dir` at it.)
- The avatar plays an idle motion loop, syncs lip movement to TTS audio amplitude, switches expressions based on `[[reaction:...]]` tags, and supports Tier-3 auto-driven effects: pajamas at night, auto-blush on tender moods, auto-sweat on concerned reactions, and a cat-tail wag whose frequency tracks the current arousal. The LLM can also fire transient overlays via `[[overlay:sweat]]` / `[[overlay:blush]]` / etc. — only those whose capability is detected on the loaded model are advertised in the system prompt.
- User-tunable knobs (scale, auto-outfit mode) live in `config.avatar` and on the Avatar tab of the Settings drawer.

## Voice

- Pocket-TTS uses the `voice` set in `config.tts` (e.g. `aiko1_refined.safetensors` from `voices/`). Drop `.safetensors` files into `voices/` and they show up automatically.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest tests/
```

The suite covers the live surface end-to-end: `TurnRunner`, `RagStore`, `MessageIndexer`, `DocumentIngestor`, `MemoryStore`, `ChatDatabase`, `AvatarProfile`, `OllamaClient` tool calls, the response-text service, and the tool registry.

## Notes

- Everything runs locally by default — Ollama, faster-whisper, Pocket-TTS, LanceDB.
- The `chat_llm.provider == "openai_compatible"` path routes through `langchain-openai`'s `ChatOpenAI` and works with OpenAI / xAI Grok / Groq / OpenRouter / DeepSeek / Together / Mistral.
- The MCP server is opt-in (default on) and is intended for development tooling — see `AGENTS.md` for the available tools and how to add new ones.
