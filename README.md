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

## Configure

Edit `config/default.yaml` to match your model names.

## Run

```powershell
python -m app.main
```

## Notes

- All processing is local by default.
- System audio capture on Windows uses WASAPI loopback device selection and may require manual device configuration.
- This initial slice keeps memory disabled intentionally.
