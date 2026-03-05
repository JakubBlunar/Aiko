# English Speaking Assistant (Python-only)

Local desktop assistant for speaking English practice using local Ollama models.

## MVP status

This is the first implementation slice:
- PySide6 desktop UI
- Local Ollama chat wiring
- Capture controls (microphone / system audio / screen context)
- Modular service layout for STT, OCR, and TTS

## Requirements

- Windows 10/11
- Python 3.11+
- Ollama installed and running
- A local Ollama model (example: `llama3.1:8b`)

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Optional dependencies for extended capabilities:

```powershell
pip install -e .[ai,tts]
```

For Llasa/Anime-Llasa provider, install provider extras (in addition to CUDA PyTorch):

```powershell
pip install -e .[llasa]
```

Install CUDA-enabled PyTorch first (recommended for large TTS models):

```powershell
# Example for CUDA 12.4 wheels; check pytorch.org for the latest command
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

For desktop action execution (click/type), install action extras:

```bash
pip install -e .[actions]
```

For voice live mode, install AI extras at minimum:

```powershell
pip install -e .[ai]
```

## Configure

Edit `config/default.json` to match your model names.
Runtime UI preferences are saved automatically to `config/user.json` (device choices, source toggles, and VAD calibration).
Conversation memory is stored locally in `data/conversation_memory.jsonl` when `Remember Conversation` is enabled.
You can override model per machine in `config/user.json`, for example:

```json
{
	"ollama": {
		"chat_model": "aqualaguna/gemma-3-27b-it-abliterated-GGUF:q4_k_m"
	}
}
```

Optional: configure a separate low-temperature `thinking_model` for decision tasks (like screen-capture gating):

```json
{
	"assistant": {
		"thinking_model": "aqualaguna/gemma-3-27b-it-abliterated-GGUF:q4_k_m"
	}
}
```

Optional: force speech recognition language to reduce wrong auto-detection (for example German mix-ups while speaking English):

```json
{
	"stt": {
		"language": "en"
	}
}
```

Optional: enable structured autonomy planning for more proactive turn-by-turn decision making:

```json
{
	"autonomy": {
		"enabled": true,
		"mode": "interactive",
		"agentic_narration_level": "summary",
		"proactive_conversation": true,
		"allow_action_suggestions": true,
		"allow_proactive_actions": false,
		"max_strategy_chars": 180,
		"auto_goal_switch": true,
		"default_goal": "general_conversation",
		"goal_switch_min_confidence": 0.6
	}
}
```

Optional: enable MCP tool integration (JSON `mcpServers` schema):

1. Install prerequisites (Python 3.13+ recommended for `windows-mcp`, and `uv`).
2. Verify server starts locally:

```powershell
uvx windows-mcp
```

3. Enable MCP loader in `config/tooling.user.json`:

```json
{
	"tools": {
		"mcp": {
			"enabled": true,
			"servers_user_json_path": "config/mcp.servers.user.json",
			"servers_json_path": "config/mcp.servers.json",
			"framing_mode": "newline-json",
			"auto_restart": true,
			"restart_backoff_seconds": 4.0,
			"max_restart_attempts": 5
		}
	}
}
```

4. Define servers in your local, git-ignored file `config/mcp.servers.user.json` (preferred):

```json
{
	"mcpServers": {
		"windows": {
			"transport": "stdio",
			"command": "uvx",
			"args": ["windows-mcp"],
			"env": {}
		},
		"remote-prod": {
			"transport": "http",
			"url": "https://your-host/mcp",
			"headers": {
				"Authorization": "Bearer <token>"
			}
		}
	}
}
```

Optional fallback: define shared defaults in tracked `config/mcp.servers.json`:

```json
{
	"mcpServers": {
		"windows": {
			"transport": "stdio",
			"command": "uvx",
			"args": ["windows-mcp"],
			"env": {}
		},
		"remote-prod": {
			"transport": "http",
			"url": "https://your-host/mcp",
			"headers": {
				"Authorization": "Bearer <token>"
			}
		}
	}
}
```

Notes:
- Loader precedence is: `servers_user_json_path` first, then `servers_json_path` fallback.
- `transport: "stdio"` uses local process supervision and restart attempts.
- `transport: "http"` uses reconnect attempts (same restart policy knobs) and expects a JSON-RPC-compatible MCP endpoint.
- `framing_mode` applies to stdio servers only.

If you keep a strict `enabled_tools` list in `config/tooling.user.json`, discovered MCP tools are auto-added at startup when `append_to_enabled_tools` is true (default in `tooling.default.json`).

When enabled, the assistant infers current goal from dialogue and can switch between goals like
`general_conversation`, `english_practice`, `coding_help`, `ui_automation`, `learning_coach`, and `troubleshooting`.

For Piper TTS, set `tts.voice` to your local Piper model path (example: `models/en_US-lessac-medium.onnx`) and ensure `piper` CLI is installed and available in `PATH`.

For Llasa TTS (including `NandemoGHS/Anime-Llasa-3B`), configure:

```json
{
	"tts": {
		"provider": "llasa",
		"enabled": true,
		"voice": "unused-for-llasa",
		"llasa_model": "NandemoGHS/Anime-Llasa-3B",
		"llasa_codec_model": "HKUSTAudio/xcodec2",
		"llasa_device": "cuda",
		"llasa_temperature": 0.8,
		"llasa_top_p": 0.95,
		"llasa_max_length": 2048
	}
}
```

Notes:
- Llasa models are not Piper-compatible `.onnx` voices; they use `transformers` + `xcodec2`.
- If `llasa_device` is `cuda` but CUDA is unavailable, runtime falls back to CPU.
- `Anime-Llasa-3B` is focused on Japanese voice style and is licensed under CC-BY-NC-4.0.
- This integration loads the codec model through Hugging Face `transformers` (`trust_remote_code=True`), so no separate `xcodec2` pip package is required.
- You can switch TTS providers directly in the app from the `TTS` dropdown (`PIPER` / `LLASA`), and the selection is saved to local user config.

### Llasa model downloading

- You do **not** need to manually download model files or place them in the project.
- On first Llasa use, `transformers` automatically downloads and caches:
	- `NandemoGHS/Anime-Llasa-3B`
	- `HKUSTAudio/xcodec2`
- Default Windows cache path is usually: `C:\Users\<your-user>\.cache\huggingface\hub`
- If you hit Hugging Face auth/rate-limit issues, run:

```powershell
huggingface-cli login
```

- Optional: set a custom Hugging Face cache directory before launching the app:

```powershell
setx HF_HOME D:\hf-cache
```

Then restart your terminal and run the app again.

If you see `model type xcodec2 not recognized` and Llasa TTS stays silent, run:

```powershell
pip install transformers==4.57.0
pip install xcodec2 --no-deps
pip install torchtune torchao vector-quantize-pytorch==1.17.8
```

The warning about unauthenticated HF requests is optional to fix (downloads still work). To increase rate limits:

```powershell
setx HF_TOKEN <your_hf_token>
```

## Run

```powershell
python -m app.main
```

## Notes

- All processing is local by default.
- System audio capture on Windows uses WASAPI loopback device selection and may require manual device configuration.
- Conversation memory can be toggled from the UI (`Remember Conversation`) and cleared on demand.
- Live mode uses basic energy-based speech detection (VAD-like thresholding) and may need threshold tuning for noisy rooms.
- Live mode supports barge-in: when your speech starts, current assistant audio playback is stopped.
- Use `Refresh Devices` in the UI to pick a specific microphone or loopback source.
- System audio context is captured in short intervals and buffered into recent transcript snippets for prompt grounding.
- Screen context is captured conditionally (not continuously): keyword-triggered and optionally model-decided.
- Tune `screen.decision_mode` (`model` or `keywords`) and `screen.decision_cooldown_seconds` in config for behavior/latency tradeoffs.
- Tune `screen.min_ocr_chars` to ignore low-information OCR captures and `screen.unchanged_reuse_seconds` to suppress repeated unchanged screen context in model-driven mode.
- Tune `VAD Threshold` and `Silence Stop` from the UI calibration row to improve phrase detection in noisy or quiet rooms.
- The live input meter helps you pick a threshold where normal speech is above ~50% and room noise stays low.
- Personality profiles (`Friendly`, `Coach`, `Interviewer`) can be selected from the UI and are persisted to local user config.
- Use `Clear Memory` in the UI to wipe stored conversation history instantly.
- Use `Memory Viewer` to inspect a long history window and refresh or clear stored entries.
- Use `Refresh Models` + `Model` dropdown to switch response models, and `Thinking Model` to choose a separate decision model (or `Use response model`) without editing config files manually.
- A latency strip shows per-turn `capture`, `stt`, `llm`, `tts`, and `total` timings for quick model comparisons.
- A second latency strip shows rolling averages over the last 10 turns for more stable model comparison.
- Use `Reset Latency` to clear current and average timing stats before testing another model.

## Autonomous actions (guarded)

You can enable model-planned desktop actions (currently `click` and `type_text`) with strict guardrails in config:

```json
{
	"actions": {
		"enabled": false,
		"dry_run": true,
		"require_confirmation": true,
		"decision_mode": "explicit_only",
		"max_actions_per_turn": 1,
		"min_confidence": 0.75,
		"min_action_interval_seconds": 1.0,
		"emergency_hotkey": "ctrl+alt+f12",
		"allowlist_window_titles": []
	}
}
```

- `dry_run: true` means the assistant will plan actions and report them, but never perform real input events.
- `require_confirmation: true` blocks execution and asks for confirmation (message shown in assistant output).
- `allowlist_window_titles` blocks actions unless the foreground window title contains one of the listed tokens.
- `decision_mode: explicit_only` limits planning to turns where your message includes action intent words (click/type/open/etc.).
- `min_confidence` blocks low-confidence actions before any click/type is attempted.
- `min_action_interval_seconds` enforces a cooldown between actions (default 1.0s).
- Press the global `emergency_hotkey` to instantly lock all actions; use `Reset E-Stop` in the app to unlock.
- You can tune cooldown at runtime from the app via `Action Cooldown (s)` + `Apply Guardrails` (saved to local user config).
- The UI separates controls into `Audio Calibration` and `Action Guardrails` rows for safer tuning during live use.
- When confirmation is enabled, pending actions appear in the guardrails status line and can be handled with `Approve Action` or `Reject Action`.
- Use `Action/Thinking Log` in the UI to inspect decision traces (screen decision YES/NO, planned action type/confidence/reason, execution result, confirmations).
- The log is a compact decision trace for debugging, not raw hidden chain-of-thought output.
- The trace dialog includes stage filters so you can view only `autonomy.plan`, `autonomy.goal`, `screen.decision`, `screen.capture`, `action.plan`, `action.execute`, or `action.confirmation` entries.

## Session and mode steering

You can switch behavior in-chat at runtime:

- `@mode manual`
- `@mode interactive`
- `@mode automatic`
- `@session chat`
- `@session reading`
- `@session agentic`
- `@stop session`

The Settings tab also exposes `Autonomy Mode` and `Session Type` controls.
