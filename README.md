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

For Agno built-in tools (Google Search, Wikipedia, Arxiv):

```powershell
pip install -e ".[agent]"
```

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

User overrides (model, voice, paths) go in `config/user.json`.

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
- **Record** using the Record button: speak, then stop; the transcript is sent to the agent and the reply is spoken.
- Conversation history is kept in the session (Agno storage). Use Clear history in settings to reset.

## Optional: MCP tools

To use MCP servers (e.g. windows-mcp) as agent tools:

1. Install prerequisites (e.g. `uv`, Python 3.13+ for windows-mcp).
2. Define servers in `config/mcp.servers.json` or `config/mcp.servers.user.json`.
3. In `config/tooling.user.json` set `tools.mcp.enabled` to `true`.

MCP tools are registered as Agno tools and called by the agent when needed.

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
- Default config (`config/default.json`) includes only assistant, ollama, audio, stt, tts, ui, and tooling; user overrides go in `config/user.json`.
