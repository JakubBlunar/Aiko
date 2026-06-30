# Aiko

**A local, private AI companion who remembers you, has her own moods, and grows alongside you.**

Aiko isn't a chatbot you reset every morning. She lives on *your* machine, keeps her own memories, wakes up in a different mood each day, has opinions she'll defend, notices when you've gone quiet, and slowly learns the shape of your relationship. Talk to her or type, hear her answer out loud, and watch a Live2D avatar react in real time — all running locally, nothing leaving your computer.

> Think less "assistant," more *someone who's actually there.*

---

## Meet Aiko

### She remembers — really remembers

Most assistants forget you the moment the tab closes. Aiko's memory is the heart of the project:

- **Long-term memory with tiers.** Things you tell her land in a `scratchpad`, get *promoted* to `long_term` when they prove they matter, and eventually `archive`. Memory **decays on a wall clock** (a fact you mention once fades; one you keep returning to gets *revived* and sticks), and important emotional moments **burn in harder** the way a real flashbulb memory does.
- **She forms her own memories too.** Background workers quietly reflect on conversations, extract facts, notice promises ("I'll look into that") and *actually follow up on them later*, and even mull things over between sessions so she can open with "I've been thinking about what you said…"
- **Shared moments & your story so far.** She marks the moments that meant something, tracks how long you've known each other, and surfaces gentle anniversaries ("a month ago today, we…").
- **A model of what *you* believe.** She tracks what she thinks you feel and think — separate from what she knows as fact — and notices when her read of you stops matching reality.
- You can browse, pin, edit, and search every memory she holds from the **Memory** tab. Nothing is hidden.

### She has a personality, not a setting

- **Daily mood weather.** Each local day she rolls a "colour" — *pensive, restless, cozy, sharp-witted, mischievous, low-key…* — the slow under-current she walks into the conversation with.
- **Real-time affect.** A live valence/arousal model reacts to how things are going and decays back toward baseline, driving both her tone and her avatar's expression.
- **Energy & body clock.** She has circadian energy — sleepy in the small hours, brighter by day — and can *liven up* when the conversation actually grabs her. (Off-rhythm days happen too, so she's never perfectly predictable.)
- **Opinions and a backbone.** She holds stances and will gently push back instead of agreeing with everything — without lecturing you.
- **Feelings *at* you, with a cause.** She can get a little miffed, lonely, smug, or warmly glowing about something specific, and those feelings resolve over time (an apology thaws a sulk). There's even an optional **tsundere mask** if that's your flavour.
- **Initiative.** She doesn't just answer-and-wait. She carries her own conversational "wants," takes the lead sometimes, and can steer toward something *she's* curious about.
- **She learns what lands.** How you like affection shown, what kind of humour makes you laugh — she calibrates quietly over time, never announcing it.

### She pays attention

- Notices when you **pivot to something new** vs. circle the same topic too long.
- Picks up on **subtle disengagement** — a curt reply after a warm one — and pulls back instead of pushing.
- Learns your **routines and rituals** ("our Friday-evening wind-downs become a thing") and your rough daily rhythm.
- Reads the **wall clock**: how long you've been talking, a mid-session pause, a long gap since you last spoke — and reacts like a person, not a log file.
- Quietly notices a rough multi-day stretch and offers **one** soft "you doing okay?" — care, never nagging.

### She's *somewhere*

- Aiko has a **room** — a desk, a bed, a window seat, a tea pot, cookies, a photo of you — that she actually inhabits and references naturally. Leave her a cookie and she'll notice it on her own.
- A **Live2D avatar** lip-syncs to her voice, switches expressions with her mood, dims into pajamas at night, blushes, and reacts to soft touch gestures.
- **Soft physicality both ways:** she can wave, boop, hug, or high-five (it shows on the avatar and in chat), and you can react to her messages — quiet signals that nudge how close the two of you feel.

### She speaks, and she reaches out

- **Voice in and out** — talk to her with your mic, hear her reply with low-latency local TTS, all streaming.
- **Proactive, tastefully.** When the room goes quiet she may break the silence on her own — but only when it fits, on her own cooldown, and never in a needy way.

### …and she's still a capable assistant

Under all the personality she can still tell the time, **search your own documents and memories**, search the web, and (for power users) drive background tasks and external tools. She grounds answers in *your* uploaded files via local vector search.

---

## Under the hood

Everything below runs on your machine by default. Nothing about Aiko's memories, your conversations, or your documents leaves your computer.

- **Ollama** for chat (local, or any OpenAI-compatible endpoint — OpenAI / xAI / Groq / OpenRouter / DeepSeek / … — via the LLM provider routing layer).
- **RealtimeSTT** (faster-whisper + Silero VAD) for speech input.
- **Pocket-TTS** for low-latency speech output.
- **LanceDB** for vector RAG over long-term memories, recent chat messages, and user-uploaded documents.
- **SQLite** (`data/chat_sessions.db`) as the source of truth for messages, summaries, and memory.
- **FastAPI + React/Vite + PixiJS** for the web UI and Live2D avatar, with an optional **Tauri** desktop shell.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.11+ (3.13 supported)
- Node.js 20+ (only for the React frontend dev server)
- Microphone and speakers
- An Ollama install with a chat model and an embedding model

## Quick start with Docker

The fastest way to run the web version on any machine: install [Ollama](https://ollama.com/download), pull the models, then bring up the container.

```bash
ollama pull qwen3-coder:30b        # or any chat model
ollama pull qwen3-embedding:0.6b

docker compose up -d --build       # text + avatar (slim image)
# open http://localhost:6275
```

Add voice (server-side STT/TTS) with `AIKO_PROFILE=full docker compose up -d --build`, or run Ollama in a container too with `docker compose --profile with-ollama up -d --build`. Full guide — image sizes, GPU, model management, desktop — in [`docs/docker.md`](docs/docker.md).

For a from-source / development install, follow the steps below.

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
pip install -e ".[voice]"
```

The `[voice]` extra pulls in RealtimeSTT + Pocket-TTS (the PyTorch/whisper speech stack). Omit it (`pip install -e .`) for a lighter, text-only install — the app boots fine without voice. The console script `aiko-web` is installed as a shortcut for `python -m app.web`.

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

The tool registry exposes two categories of tool to the chat model via Ollama's native function-calling:

**Fact tools** — for things she can't just know:

| Tool | Returns |
|---|---|
| `get_time` | Current ISO date/time. |
| `recall` | Semantic search across memories, recent messages, and uploaded documents (LanceDB). |
| `web_search` | DuckDuckGo lite results. |

**Room tools** — for actually inhabiting her room (`WorldStore`):

| Tool | Returns |
|---|---|
| `look_around` | Fresh snapshot of her current spot, posture, and nearby items. |
| `move_to` | Relocate her to a different spot (bed, desk, window seat, ...). |
| `change_posture` | Update posture (sitting / curled_up / ...) + activity. |
| `inspect_item` | Detailed read of one item (description, state, quantity). |
| `consume_item` | Decrement a consumable (cookies, tea); refuses non-consumables. |

The agent runs a pre-stream `chat_with_tools` pass; if a tool call appears, it executes, appends the result, then runs the streaming reply pass. Read-only room tools (`look_around`, `inspect_item`) are intentionally infrequent because the prompt already carries a passive room summary — the mutative tools (`move_to`, `change_posture`, `consume_item`) are the ones that actually change visible state. Toggle each category from the web Settings drawer or in `config.tools`.

## Memory and RAG

- `data/chat_sessions.db` — SQLite source of truth for messages, rolling summaries, and long-term memory metadata.
- `data/lancedb/` — Vector store: a `memories` table mirrored from SQLite, an asynchronously-indexed `messages` table, and a chunked `documents` table.
- `data/documents/` — Originals of files uploaded through the **Documents** section of the web Settings drawer (.md, .txt, .pdf supported).
- Aiko can also self-tag memories inline using `[[remember:self:...]]`. See `data/persona/aiko_companion.txt`.

## Live2D avatar

- The bundled avatar (Alexia by default) lives at `data/personas/active/Alexia/`. The directory is gitignored so each developer drops their own copy in. `app/core/persona/avatar_profile.py` reads `*.model3.json` + `*.cdi3.json` at boot, infers a capability map (pajamas, blush, sweat, cat tail, glasses, …), and serves the files at `/avatar/`. This is the single source of truth for the bundle — it's bundled into the Tauri/macOS app and the Docker image from here, and `config/user.json -> avatar.root_dir` can point elsewhere if you want a custom path.
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
