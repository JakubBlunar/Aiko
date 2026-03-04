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

For voice live mode, install AI extras at minimum:

```powershell
pip install -e .[ai]
```

## Configure

Edit `config/default.yaml` to match your model names.
Runtime UI preferences are saved automatically to `config/user.yaml` (device choices, source toggles, and VAD calibration).
Conversation memory is stored locally in `data/conversation_memory.jsonl` when `Remember Conversation` is enabled.

For Piper TTS, set `tts.voice` to your local Piper model path (example: `models/en_US-lessac-medium.onnx`) and ensure `piper` CLI is installed and available in `PATH`.

## Run

```powershell
python -m app.main
```

## Notes

- All processing is local by default.
- System audio capture on Windows uses WASAPI loopback device selection and may require manual device configuration.
- This initial slice keeps memory disabled intentionally.
- Live mode uses basic energy-based speech detection (VAD-like thresholding) and may need threshold tuning for noisy rooms.
- Live mode supports barge-in: when your speech starts, current assistant audio playback is stopped.
- Use `Refresh Devices` in the UI to pick a specific microphone or loopback source.
- System audio context is captured in short intervals and buffered into recent transcript snippets for prompt grounding.
- Tune `VAD Threshold` and `Silence Stop` from the UI calibration row to improve phrase detection in noisy or quiet rooms.
- The live input meter helps you pick a threshold where normal speech is above ~50% and room noise stays low.
- Personality profiles (`Friendly`, `Coach`, `Interviewer`) can be selected from the UI and are persisted to local user config.
- Use `Clear Memory` in the UI to wipe stored conversation history instantly.
