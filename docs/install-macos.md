# Installing Aiko on macOS

Aiko ships as a hybrid macOS bundle:

- **Aiko.app** — a Tauri shell that hosts the React UI and spawns the
  Python backend on demand.
- **Aiko Setup.command** — a one-shot installer that lays down a
  dedicated Python venv, pulls the chat model, and copies the bundled
  config + persona files into Application Support.

You only run the setup script once. Everything afterwards is
double-click-Aiko, do-your-thing, close-Aiko.

## Requirements

- macOS 12 (Monterey) or newer. Apple Silicon and Intel are both fine.
- About 10 GB of free disk (mostly the Ollama chat model and the
  Python ML dependencies).
- An internet connection on the first run (Homebrew, pip, Ollama
  model). After that Aiko is fully offline.

## Step 1 — Install

1. Download `Aiko-macos.dmg` from the share link your installer sent you.
2. Open the DMG. Drag **Aiko.app** to your **Applications** folder.
3. Drag **Aiko Setup.command** next to it (or anywhere you can find it
   again — Desktop is fine).

## Step 2 — Run setup (once)

4. Double-click **Aiko Setup.command**.
5. A Terminal window opens. The first time the script runs it will:
   - Install Homebrew (if you don't have it).
   - Install Python 3.11, ffmpeg, portaudio, and Ollama.
   - Create a venv at `~/Library/Application Support/Aiko/venv` and
     install all Python dependencies.
   - Ask whether to pull the default chat model (`qwen2.5:7b-instruct`,
     ~4 GB). If you'd rather save space, accept the smaller
     `qwen2.5:3b-instruct` (~2 GB) when prompted.
   - Seed `~/Library/Application Support/Aiko/` with the bundled
     config, persona text, and Live2D avatar files.

The script is **idempotent**: if you re-run it nothing breaks, it just
verifies each piece is in place and pulls any missing dependency.

If anything fails the script prints a path to its log file
(`~/Library/Application Support/Aiko/logs/setup.log`). Send that file
to whoever shared the build with you.

## Step 3 — Launch

6. **Right-click → Open** on **Aiko.app** the first time. macOS shows a
   "this is from an unidentified developer" warning because the DMG is
   not code-signed yet. Click **Open** once and macOS remembers your
   choice — subsequent launches behave like a normal app.
7. On first launch Aiko asks for **your display name**. Type whatever
   you want her to call you. You can change it anytime from
   *Settings → Chat → Your display name*.

That's it. The Tauri shell starts the Python backend in the background
the moment you open the app and shuts it down when you close it.

## What lives where

| Path | What |
|------|------|
| `/Applications/Aiko.app` | The Tauri shell + bundled React UI |
| `~/Library/Application Support/Aiko/venv` | Python virtualenv |
| `~/Library/Application Support/Aiko/config/` | App config (default + your overrides) |
| `~/Library/Application Support/Aiko/data/` | Chat history, memories, persona, avatar |
| `~/Library/Application Support/Aiko/logs/` | `setup.log`, `backend.log` |

To completely uninstall Aiko: delete `Aiko.app` and the
`~/Library/Application Support/Aiko/` folder. (Optionally also remove
the chat model via `ollama rm qwen2.5:7b-instruct` and uninstall
Ollama itself with `brew uninstall ollama`.)

## Troubleshooting

- **"Aiko backend did not answer http://127.0.0.1:6275/api/health"** —
  the Tauri shell couldn't start the Python sidecar. Open
  `~/Library/Application Support/Aiko/logs/backend.log` and look for
  the first traceback. Re-running **Aiko Setup.command** usually fixes
  a stale dependency.
- **"venv not found"** — you haven't run the setup script yet, or it
  failed mid-install. Run it again; it picks up where it left off.
- **Ollama model download stuck** — the model lives in
  `~/.ollama/models`. Free up disk or run `ollama pull qwen2.5:7b-instruct`
  manually from Terminal.
- **The avatar appears as a blank window** — make sure
  `~/Library/Application Support/Aiko/data/personas/active/Alexia/`
  contains the `*.model3.json` and texture files. The setup script
  seeds them from the DMG; if you customised the path in
  `config/user.json`, point it back at this default.

## Updating

When you receive a newer DMG: replace `Aiko.app` in Applications and
re-run **Aiko Setup.command**. The setup script will install any new
Python dependencies and reseed updated persona / config files. Your
chat history and memories under `data/` are preserved.
