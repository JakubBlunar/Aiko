# Aiko

**A local, private AI companion who remembers you, has her own moods, and grows alongside you.**

Under the hood, Aiko is an experimental persistent LLM-agent architecture focused on long-term memory, evolving concepts, behavioural modelling, and grounded, simulated experience.

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
- **Higher-order beliefs she forms *and revises*.** Beyond individual facts, Aiko can build durable **concepts** about you and about herself — traits, values, boundaries, aspirations, the shape of your relationship — each carrying a confidence she'll strengthen, weaken, or quietly retire as evidence accrues. Beliefs can be contradicted and re-formed, and refined when the truth turns out to be context-dependent, so over time she doesn't just remember *more*, she understands you *better*. (Opt-in and still maturing — enable `agent.concepts_enabled`.)
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

Under all the personality she can still tell the time, **search your own documents and memories**, search the web, check the real-world weather, look at images you share (optional vision model), and (for power users) drive background tasks and external tools — including your own MCP servers. She grounds answers in *your* uploaded files via local vector search.

---

## Architecture

Aiko is built around a persistent cognitive architecture rather than a single prompt:

- **Brain / LLM** — reasoning, language, expression, and action selection. Each turn runs a two-pass loop (a tool-decision pass, then a streaming reply), routed to local Ollama or any OpenAI-compatible provider.
- **Memory system** — episodic and semantic memories with lifecycle management: tiered storage (`scratchpad` → `long_term` → `archive`), wall-clock decay and revival, and RAG retrieval. SQLite is the source of truth; LanceDB mirrors it for vector search.
- **Concept system** — builds higher-order understanding from clustered evidence: durable beliefs about the user and herself, each with a confidence that promotes, drifts, contradicts, and refines over time. (Opt-in; see `agent.concepts_enabled`.)
- **Background workers** — the "slow cognition" that keeps Aiko growing between replies. A scheduler runs many small, single-purpose workers in idle gaps and in the pauses while she's speaking, so none of it blocks a turn. Roughly grouped:
  - *Memory maintenance* — tier promotion, wall-clock decay and revival, near-duplicate consolidation, and post-conversation fact/preference extraction.
  - *Reflection & summarisation* — rolling conversation summaries, between-session reflection and "mulling things over," and a dream/consolidation pass.
  - *Concept formation* — synthesising higher-order concepts from clustered evidence, then the lifecycle engine that promotes, decays, contradicts, and revises them.
  - *Curiosity & knowledge* — seeding things to get curious about, filling knowledge gaps (including background web look-ups), fact-checking claims, and learning your routines.
  - *Relationship & social* — theory-of-mind belief tracking, promise follow-through, shared-moment and milestone detection, and a periodic relationship "pulse."
  - *World & presence* — the room/world simulation, sensory anchoring, and optional real-world weather/season sync.
- **Presentation layer** — a FastAPI + WebSocket backend with a React / Vite / PixiJS frontend: the Live2D avatar, voice in/out (client-owned audio), gestures/touch, and the settings + memory UI.

## Under the hood

The concrete stack behind the architecture above. Everything runs on your machine by default — nothing about Aiko's memories, your conversations, or your documents leaves your computer.

- **Ollama** for chat (local, or any OpenAI-compatible endpoint — OpenAI / xAI / Groq / OpenRouter / DeepSeek / … — via the LLM provider routing layer).
- **RealtimeSTT** (faster-whisper + Silero VAD) for speech input.
- **Pocket-TTS** for low-latency speech output.
- **LanceDB** for vector RAG over long-term memories, recent chat messages, and user-uploaded documents.
- **SQLite** (`data/chat_sessions.db`) as the source of truth for messages, summaries, and memory.
- **FastAPI + React/Vite + PixiJS** for the web UI and Live2D avatar, with an optional **Tauri** desktop shell.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.11–3.13 (3.13 is what the dependency lock is built and tested against; **3.14 is not supported** — `ctranslate2`, behind faster-whisper, ships no 3.14 wheels and no sdist)
- Node.js 20+ (only for the React frontend dev server)
- Microphone and speakers
- An Ollama install (the first-run wizard downloads the models for you)

## Quick start with Docker

The fastest way to run the web version on any machine: install [Ollama](https://ollama.com/download), then bring up the container.

```bash
docker compose -f docker-compose-slim.yaml up -d --build
# open http://localhost:6275
```

On first launch a setup wizard asks for your name, then shows which models the
default config needs (`qwen3.5:9b` for chat, `qwen3-embedding:0.6b` for RAG),
which of them Ollama already has, and offers to pull the missing ones with a
progress bar. You can also pick a different installed model there instead. To
skip the wizard's download step, pull them yourself beforehand:

```bash
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b
```

For server-side voice (STT/TTS), swap in `docker-compose-full.yaml` — same command, bigger image, and it shares the data volume so nothing is lost when you switch. Add `--profile with-ollama` to either file to run Ollama as a sibling container instead of on the host. Full guide — image sizes, GPU, model management, build caching, desktop — in [`docs/docker.md`](docs/docker.md).

For a from-source / development install, follow the steps below.

## Setup

### 1. Install Ollama and pull models

- **Windows:** [ollama.com/download/windows](https://ollama.com/download/windows)
- **macOS:** `brew install ollama`
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`

The first-run wizard can pull the models for you, so this step is optional. To
do it up front (defaults match `config/default.json`):

```powershell
ollama pull qwen3.5:9b                     # ~6.6 GB, fits 12 GB VRAM at 64k context
ollama pull qwen3-embedding:0.6b
```

`jaahas/qwen3.5-uncensored:9b` is a drop-in alternative for the chat role if you
want fewer refusals; pick it in the wizard or in **Settings → Chat → Role
assignments**.

### 2. Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[voice]" -c requirements.lock
```

The `[voice]` extra pulls in RealtimeSTT + Pocket-TTS (the PyTorch/whisper speech stack). Omit it (`pip install -e . -c requirements.lock`) for a lighter, text-only install — the app boots fine without voice. The console script `aiko-web` is installed as a shortcut for `python -m app.web`.

`-c requirements.lock` pins the entire dependency graph to the exact versions this project is tested against. It's optional but recommended: without it pip is free to pick newer releases within the ranges in `pyproject.toml`. See [`docs/docker.md`](docs/docker.md#dependency-pinning) for how to regenerate the lock.

On Linux, install the PortAudio and libsndfile system packages first, otherwise PyAudio (a RealtimeSTT dependency with no Linux wheel) can't compile:

```bash
sudo apt-get install -y python3-dev portaudio19-dev libsndfile1 ffmpeg
```

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

User-editable defaults live in `config/default.json`; personal overrides go in `config/user.json` and are deep-merged on top (the Settings drawer in the UI writes `config/user.json` for you). Every knob — LLM routing, voice, memory tiers, the personality/worker toggles, tools, weather, avatar — is documented in the **[configuration reference](docs/configuration.md)**, which stays in lock-step with `app/core/infra/settings.py`.

## Tools

Aiko calls tools via the LLM's native function-calling (time, memory recall, web search, weather, her room, her goals, plus bundled plugins and external MCP servers). See the **[tools catalog](docs/tools.md)** for the full list and [`docs/configuration.md`](docs/configuration.md#tools--toolssettings) for the per-family toggles.

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
- Providers with `kind == "openai_compatible"` route through the hand-rolled `OpenAICompatibleClient` (plain `requests`, no vendor SDK) and work with OpenAI / xAI Grok / Groq / OpenRouter / DeepSeek / Together / Mistral (with per-provider `api_style` + `reasoning_effort` controls). Add one from **Settings → Chat → Providers** and point any role at it — see [`docs/llm-providers.md`](docs/llm-providers.md).
- The embedded MCP *server* is opt-in (default on) and is intended for development tooling — see `AGENTS.md` for the available tools and how to add new ones.
- Aiko can also act as an MCP *client*: point `mcp_clients.servers` at external MCP servers to expose their tools to her, and load local capability `plugins` (e.g. the bundled `filesystem` plugin) from `config.plugins`.
