# English Speaking Assistant

Local speech-to-speech + type-in assistant: talk or type to the agent, get text and spoken response in real time. Uses RealtimeSTT (Whisper + Silero VAD), Kokoro TTS, and Agno with Ollama.

## Requirements

- Windows 10/11 (or macOS/Linux)
- Python 3.11+
- Ollama installed and running
- A local Ollama model (e.g. `llama3.1:8b`)
- Microphone and speakers

The app uses **Ollama** for the LLM; Agno still needs the `openai` package for internal types (installed automatically with the project).

## Setup

### 1. Install Ollama

- **Windows:** Download from [ollama.com/download/windows](https://ollama.com/download/windows)
- **macOS:** `brew install ollama` or download from [ollama.com/download/mac](https://ollama.com/download/mac)
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`

Then pull the model:

```powershell
ollama pull llama3.1:8b
```

### 2. Python environment

```powershell
python -m venv .venv
.\.venv\Activate.ps1
pip install -e .
```

For Agno toolkits (search, calculator, weather, etc.), see [Agno toolkits](#agno-toolkits-config-and-deps) below. Quick start:

```powershell
pip install -e ".[agent]"
# or install optional toolkit deps (DuckDuckGo, YouTube, OpenWeather, YFinance):
pip install -e ".[agno-tools]"
```

**If your `.venv` is corrupted or misbehaving**, recreate it from the project root (close the app first):

```powershell
Remove-Item -Recurse -Force .venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
.\.venv\Scripts\python -m spacy download en_core_web_sm
```

Then run the app with `.\.venv\Scripts\python -m app.main`.

### 3. Kokoro TTS dependencies

If you see "Missing TTS dependencies" or "Missing kokoro_onnx, misaki, sounddevice, or numpy", install the project (which includes them) or the TTS packages explicitly:

```powershell
pip install -e .
```

Or only the TTS stack:

```powershell
pip install numpy sounddevice kokoro-onnx misaki
```

You also need **espeak-ng** installed on your system for misaki G2P (see step 5 below).

### 4. Kokoro TTS model files

Download and place in the `models/` folder (default paths):

1. [kokoro-v1.0.onnx](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) → `models/kokoro-v1.0.onnx`
2. [voices-v1.0.bin](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) → `models/voices-v1.0.bin`

To use a different location, set paths in `config/user.json`:

```json
{
  "tts": {
    "kokoro_model_path": "models/kokoro-v1.0.onnx",
    "kokoro_voices_path": "models/voices-v1.0.bin"
  }
}
```

### 5. RealtimeSTT / Whisper

RealtimeSTT will download Whisper `large-v1` on first use. No manual download required.

### 6. espeak-ng (for Kokoro G2P)

- **Windows:** Download from [eSpeak NG releases](https://github.com/espeak-ng/espeak-ng/releases) (e.g. `.msi`) and install; add to PATH if needed.
- **macOS:** `brew install espeak-ng`
- **Linux:** `sudo apt-get install espeak-ng`

## Configure

- **Ollama:** `config/default.json` → `ollama.base_url`, `ollama.chat_model`, `ollama.temperature`
- **STT:** `stt.provider` (realtime_stt), `stt.model` (large-v1), `stt.language` (en)
- **TTS:** `tts.provider` (kokoro), `tts.voice` (e.g. af_heart), `tts.kokoro_model_path`, `tts.kokoro_voices_path`
- **MCP:** Optional. See `config/tooling.default.json` and `config/mcp.servers.json`; enable in `config/tooling.user.json` under `tools.mcp.enabled`
- **Logging:** All app events and errors are logged to the console (stderr). Filter by level with the `LOG_LEVEL` environment variable (e.g. `DEBUG`, `INFO`, `WARNING`, `ERROR`) or set `logging.level` in `config/default.json` or `config/user.json`. Use `DEBUG` to see all pipeline/tool events for diagnosis; use `ERROR` to reduce noise.

User overrides (model, voice, paths) go in `config/user.json`.

## Customizing the persona

**User persona** (what the agent knows about you) is handled by **Agno Learning**: User Profile and User Memory are stored in the same database as session history (`data/agno_sessions.db` by default) and injected into the agent automatically.

Configure the assistant in `config/default.json` or `config/user.json` under `assistant`:

| Key | Description |
|-----|-------------|
| `background` | Inline description of the assistant’s role (single line in JSON). Used when `background_path` is not set or the file cannot be read. |
| `background_path` | Path to a **text file** (relative to project root) with multiline instructions, e.g. `data/assistant_background.txt`. If set and the file exists, its content is used instead of `background`. |
| `user_id` | Optional; default `"default"`. Scopes Agno Learning per user. |
| `response_style` | Optional; one of `balanced`, `concise`, `detailed`. Affects reply length. Default `balanced`. |
| `tts_length_scale` | Optional; float in 0.65–1.35. Higher = slower TTS. Default `1.0`. |

## Run

Use the project’s virtual environment so that `agno` and other dependencies are found. From the project root:

```powershell
.\.venv\Scripts\python -m app.main
```

Or activate the venv first, then run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.main
```

## Usage

- **Type** in the input field and press Enter or click Send to get a text + spoken reply.
- **Live** — Click **Start Live** to use voice detection or push-to-talk: the app listens for your speech, transcribes it, sends it to the agent, and speaks the reply. Sentence chunks are spoken as they are generated (stream-to-speak). Use **Stop Live** when done.
- Conversation history is kept in the session (Agno storage). Use Clear history in settings to reset.

### Live mode: streaming, barge-in, and mood

- **Streaming:** In Live mode the agent reply is streamed; each sentence is spoken as soon as it is ready, so you hear the start of the answer sooner.
- **Mood:** The agent starts each reply with a mood tag (e.g. `[[reaction:cheerful]]`). TTS uses this to slightly adjust speaking speed (e.g. more energetic for “excited”, slower for “sad”).
- **Barge-in:** In Settings → Audio you can enable **Allow barge-in**. When on, you can interrupt while the assistant is speaking: your new utterance stops playback and is processed as a correction or follow-up in the same conversation, so the agent keeps context.

## Agno toolkits (config and deps)

Which Agno toolkits the agent can use is set in **`config/tooling.default.json`** (and overrides in `config/tooling.user.json`) under **`agno_toolkits`**. Enabling a toolkit in config requires **installing its Python packages** (and setting any env vars) for it to load; if a toolkit fails, the app logs a copy-pastable `pip install ...` line.

- **Packages and env vars:** See [docs/agno-toolkits-deps.md](docs/agno-toolkits-deps.md) for a table of toolkit id → pip packages and env vars (e.g. `openweather` needs `OPENWEATHER_API_KEY`; `youtube` needs `pip install youtube_transcript_api`).
- **Optional install:** `pip install -e ".[agno-tools]"` installs deps for DuckDuckGo, YouTube, OpenWeather, and YFinance. Or install per-toolkit extras: `.[agno-duckduckgo]`, `.[agno-youtube]`, etc.

**Toolkit parameters:** Some toolkits accept constructor options (e.g. YouTube: `languages`, `enable_get_video_captions`). You can pass them in config in two ways:

1. **Option A — list of entries with optional params:**  
   In `config/tooling.default.json` set `agno_toolkits` to a list where each entry is either a string (toolkit id) or an object with `id` and `params`:

   ```json
   "agno_toolkits": [
     "calculator",
     "duckduckgo",
     { "id": "youtube", "params": { "languages": ["en"], "enable_get_video_captions": true } }
   ]
   ```

2. **Option B — ids list + params map:**  
   Set `agno_toolkits` to a list of id strings and `agno_toolkit_params` to a map from toolkit id to params:

   ```json
   "agno_toolkits": ["calculator", "youtube", "duckduckgo"],
   "agno_toolkit_params": {
     "youtube": { "languages": ["en"], "enable_get_video_captions": true }
   }
   ```

   User overrides in `config/tooling.user.json` are merged (e.g. you can add or override params per toolkit).

## Optional: MCP tools

To use MCP servers (e.g. windows-mcp) as agent tools:

1. Install prerequisites (e.g. `uv`, Python 3.13+ for windows-mcp).
2. Define servers in `config/mcp.servers.json` or `config/mcp.servers.user.json`.
3. In `config/tooling.user.json` set `tools.mcp.enabled` to `true`.

MCP tools are registered as Agno tools and called by the agent when needed.

### Coding tools (read/edit files in chosen folders)

To let the agent read and edit files under folders you choose:

1. Install the coding MCP dependency: `pip install -e ".[coding-mcp]"`
2. Open **Settings → Coding**, check **Enable coding tools**, click **Add folder…** and pick one or more workspace roots. Save (OK).
3. Restart the app. The agent will have `coding_read_file`, `coding_list_dir`, `coding_search`, and `coding_apply_patch`; all paths are restricted to your chosen roots.

Context and history are kept in check: tool-result compression and history length are configurable in `config/default.json` under `agent` (`num_history_runs`, `compress_tool_results`, etc.).

## Testing

Install dev dependencies and run tests:

```powershell
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Notes

- All processing is local (Ollama, Whisper, Kokoro).
- Ensure Ollama is running before starting the app (`ollama serve` or start from tray).
- If Kokoro fails to load, check that `kokoro-v1.0.onnx` and `voices-v1.0.bin` are in the configured path and that espeak-ng is installed.
- Default config (`config/default.json`) includes only assistant, agent, ollama, audio, stt, tts, ui, and tooling; user overrides go in `config/user.json`.
- **Agent context:** `config/default.json` → `agent.num_history_runs` (how many past turns to send), `agent.compress_tool_results` (shrink large tool outputs). Lower history or enable compression if the agent slows down with long chats or many tool calls.
