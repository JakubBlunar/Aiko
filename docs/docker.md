# Running Aiko in Docker

The container runs the **Aiko backend** (FastAPI) and serves the built React
UI, so a single `docker compose up` gives you the full web experience at
`http://localhost:6275`. Chat, the Live2D avatar, memory and RAG all work
out of the box. Voice (server-side STT/TTS) is opt-in via a larger image.

Ollama is **not** baked into the Aiko image — you either run it on the host
(default) or as a sibling compose service (`--profile with-ollama`).

---

## TL;DR

```bash
# 1. Install Ollama on the host — https://ollama.com/download
#    (the app's first-run wizard downloads the models for you)

# 2. Build + start Aiko
docker compose -f docker-compose-slim.yaml up -d --build

# 3. Open the UI
#    http://localhost:6275
```

That's it. The container reaches your host's Ollama via
`host.docker.internal:11434`, and the setup wizard offers to pull anything
that's missing.

### Which compose file?

| File | Use it for |
|---|---|
| `docker-compose-slim.yaml` | Chat, avatar, memory, RAG, tools. **Start here.** |
| `docker-compose-full.yaml` | Same plus server-side voice. Bigger image, needs more RAM. |
| `docker-compose.yml` | The shared definition both of the above `include`. Runnable on its own; builds whatever `$AIKO_PROFILE` says. |

The two variants differ by exactly one build arg, and they share the same
data volume — switching from slim to full (or back) keeps your history,
memories and settings.

---

## Image profiles (size vs. voice)

The `PROFILE` build arg controls how big the image is:

| Profile | Size (approx) | What you get |
|---|---|---|
| `slim` (default) | ~1–1.5 GB | Text chat, Live2D avatar, memory, RAG, tools, proactivity |
| `full` | ~4–6 GB | Everything above **plus** server-side voice (RealtimeSTT + Pocket-TTS) |

The split is real: `realtimestt` + `pocket-tts` (and the PyTorch/whisper stack
they pull) live behind a `voice` extra in `pyproject.toml`. The slim image
never installs them; the app imports them defensively and simply reports voice
as unavailable.

```bash
# slim
docker compose -f docker-compose-slim.yaml up -d --build

# full voice
docker compose -f docker-compose-full.yaml up -d --build

# or without compose at all
docker build -t aiko:full --build-arg PROFILE=full .
```

The full image installs **CPU** PyTorch by default (keeps it from grabbing the
~2.5 GB CUDA wheel). STT on CPU is fine for a companion, just slower than GPU.
To build for GPU STT, point the build at a CUDA wheel index:

```bash
docker build -t aiko:full-cuda \
  --build-arg PROFILE=full \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121 .
```

(GPU STT then also needs the container to actually see the GPU — same
NVIDIA Container Toolkit story as the Ollama GPU section below.)

`stt.device` defaults to `auto`, so the same image picks CUDA when the container
can see a GPU and CPU when it can't. No config change is needed either way.

### Memory: the full profile needs real RAM

The default `stt.model` is `large-v1`, which is roughly a 3 GB model. Loading it
needs **well over 4 GB** available to the container — Docker Desktop's default
allocation (often ~2 GB) gets the container **OOM-killed** partway through boot,
which looks like `Exited (137)` with no error message in the log.

Either raise the limit (Docker Desktop → Settings → Resources → Memory, 8 GB is
comfortable), or run a smaller model by mounting a `config/user.json`:

```json
{ "stt": { "model": "small.en", "compute_type": "int8" } }
```

`int8` also cuts CPU memory and latency noticeably. `tiny` boots in seconds and
fits well under 2 GB if you just want to confirm the stack works.

---

## Dependency pinning

Python dependencies are pinned twice, on purpose:

- **`pyproject.toml`** holds readable floors *and ceilings* (`numpy>=2.2.3,<3`, …)
  so nothing can silently jump a breaking major.
- **`requirements.lock`** holds the exact, verified resolution of the whole
  transitive graph. The Dockerfile applies it as a pip **constraints** file
  (`pip install -c requirements.lock .`), so `pyproject.toml` still decides
  *what* is installed while the lock decides *which versions*.

Regenerate the lock after changing any dependency:

```bash
uv pip compile pyproject.toml --extra voice \
  --python-version 3.13 --python-platform x86_64-unknown-linux-gnu \
  --output-file requirements.lock
```

Two constraints worth knowing:

- **Python must be 3.11–3.13** (`requires-python = ">=3.11,<3.14"`). `ctranslate2`,
  which backs faster-whisper, publishes no 3.14 wheels *and* no sdist, so a 3.14
  install appears to succeed and then dies at runtime with
  `ModuleNotFoundError: No module named 'faster_whisper'`. The image is built on
  `python:3.13-slim-bookworm` to match the locked set.
- **The `wake-word` extra is excluded from the lock and the image.** `openwakeword`
  needs `tflite-runtime`, which has no Linux wheels past cp311.

---

## Build caching

Rebuilding after a code change takes **a few seconds**, not minutes. Two
things make that work, and both are easy to break by accident.

**Layer ordering.** The dependency install is keyed on `pyproject.toml` +
`requirements.lock` and nothing else — the app source is copied in as the
*last* step in the Dockerfile. Copying `app/` before the install (the
obvious-looking arrangement) means every edit to a Python file reinstalls
PyTorch. The install needs the `app` package to exist to build the project,
so the dependency layer creates an empty stub that the real tree overwrites
later; that's the only reason the split is possible.

**Cache mounts.** pip's wheel cache, npm's package cache and apt's `.deb`
archives live on BuildKit `--mount=type=cache` volumes. They persist across
builds and are shared between the `slim` and `full` profiles, so changing a
dependency re-*installs* without re-*downloading*, and the second profile
you build reuses the first one's downloads. Nothing from a cache mount ends
up in the image, which is why the Dockerfile no longer sets
`PIP_NO_CACHE_DIR` or deletes `/var/lib/apt/lists` — those would fight the
mounts for no benefit.

Measured on a warm cache:

| Change | slim | full |
|---|---|---|
| Nothing (no-op rebuild) | ~2 s | ~2 s |
| Python source under `app/` | ~4 s | ~5 s |
| A dependency in `pyproject.toml` | ~1 min | ~3 min |
| Frontend source under `web/` | ~1 min | ~1 min |

The frontend row is the odd one out, and it isn't a caching problem: `npm ci`
*is* cached, but `npm run build` runs `tsc -b` over the whole project and
then Vite, neither of which has a persistent cache to reuse. Dropping the
typecheck would roughly halve it at the cost of letting type errors ship, so
it stays.

Two caveats:

- **BuildKit is required.** It's the default in Docker 23+ and always used by
  `docker compose build`, but `DOCKER_BUILDKIT=0 docker build` fails outright
  on the `--mount` flags.
- **`--no-cache` and `docker builder prune` throw all of this away**, including
  the download caches. Reach for them only when you actually suspect a stale
  layer; `docker builder prune --filter type=exec.cachemount` clears *just*
  the download caches if that's what you're after.

---

## Configuration (no file editing needed)

Three env vars cover everything a fresh container needs — they override
`config/default.json` at startup (`app/web/__main__._apply_env_overrides`):

| Env var | Default (in image) | Purpose |
|---|---|---|
| `AIKO_WEB_HOST` | `0.0.0.0` | Bind address (must be `0.0.0.0` in a container) |
| `AIKO_WEB_PORT` | `6275` | Port the server listens on inside the container |
| `AIKO_OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Where Aiko finds Ollama (chat **and** embeddings) |

Compose reads `.env` for a few convenience knobs — copy and tweak:

```bash
cp .env.example .env
```

```ini
AIKO_PROFILE=slim                                  # slim | full
AIKO_PORT=6275                                     # host port
AIKO_OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_PORT=11434
```

`AIKO_PROFILE` only applies when you run `docker-compose.yml` directly — the
two variant files pin their profile so that a file called "slim" can't build
the voice image because of a stale `.env`.

If you'd rather use the full `config/user.json` mechanism (advanced LLM
routing, etc.), mount it read-only:

```yaml
    volumes:
      - aiko-data:/app/data
      - ./config/user.json:/app/config/user.json:ro
```

The mount is a **seed**, not the live file. On first boot the entrypoint
copies it to `/app/data/user.json` (the volume) if that file doesn't exist
yet, and from then on the app reads and writes the volume copy — see
"Where settings are stored" below. Editing the mounted file after that
first boot has no effect; edit the volume copy or `docker compose down -v`
to re-seed.

---

## Data persistence

The `aiko-data` named volume is mounted at `/app/data` and holds everything
that should survive a rebuild:

- `data/chat_sessions.db` — messages, memories, world, tasks, beliefs, …
- `data/lancedb/` — the vector index
- `data/documents/`, `data/attachments/` — uploads
- `data/personas/active/Alexia/` — the active avatar bundle
- `data/persona/` — the persona text
- `data/user.json` — your settings (see below)

### Where settings are stored

Everything you configure at runtime — the name and chat model the first-run
wizard collects, provider API keys with no OS keychain behind them, avatar
tweaks — is written to `data/user.json` **inside the volume**, because
`AIKO_USER_CONFIG=/app/data/user.json` is set in the image.

This matters: `/app/config` lives in the container's writable layer, which
Docker discards on every `docker compose up --build` or image update. Before
the override existed, a rebuild silently reset you to the setup wizard.
`AIKO_USER_CONFIG` works outside Docker too if you want the file somewhere
other than the repo (e.g. `~/.config/aiko/user.json`).

Two things are baked into the image and seeded into that volume on first run
so an empty volume doesn't blank them out:

- **Persona text** — the entrypoint copies `data/persona/*` from the image
  into the volume **only if absent** (your edits are never clobbered).
- **Live2D avatar** — baked at `/opt/aiko/seed/personas-active` (outside the
  volume) via `AIKO_AVATAR_SEED_DIR`; the app self-heals it on boot into
  `data/personas/active/Alexia`.

> The avatar bundle is gitignored, so it ships in the image only if it's
> present on your machine at build time under **`data/personas/active/Alexia/`**
> (that exact path in the repo root — not `data/persona/`, and not nested one
> level deeper). The build no longer fails when it's absent: you just get an
> avatar-less UI until you drop the bundle into the `aiko-data` volume at
> `personas/active/Alexia/` (or rebuild on a machine that has it).

To wipe and start fresh: `docker compose down -v` (removes the volumes too).

---

## Option A — host Ollama (default, matches the README install)

You already run Ollama natively (best on macOS, and the simplest way to use
your GPU on any OS). The container connects out to it. Nothing extra to do
beyond the TL;DR above.

If host networking ever fails on Linux, confirm the gateway alias resolves —
compose sets `extra_hosts: host.docker.internal:host-gateway` for you.

## Option B — Ollama in a container (`--profile with-ollama`)

No host Ollama install at all:

```bash
# tell Aiko to use the sibling service instead of the host
echo 'AIKO_OLLAMA_BASE_URL=http://ollama:11434' >> .env

docker compose -f docker-compose-slim.yaml --profile with-ollama up -d --build

# pull models INTO the container (stored in the ollama-models volume, so
# this is a one-time cost — they survive restarts and recreation)
docker compose exec ollama ollama pull qwen3.5:9b
docker compose exec ollama ollama pull qwen3-embedding:0.6b
```

The `with-ollama` profile is defined on the shared file, so it works with
any of the three (swap in `docker-compose-full.yaml` for voice).

**Changing models later** is just another `ollama pull` (or `ollama rm`) via
`docker compose exec ollama ...`; the `ollama-models` volume keeps them
cached. You can also change the chat model in the Settings drawer once it's
pulled.

### GPU for the containerised Ollama

Yes — but only on Linux/Windows hosts with an NVIDIA GPU and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
installed. Uncomment the `deploy.resources.reservations.devices` block on the
`ollama` service in `docker-compose.yml`:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Caveats:

- **macOS:** Docker Desktop has **no** GPU passthrough. Run Ollama natively
  (Option A) to use Apple Silicon acceleration — the containerised Ollama
  there would be CPU-only.
- **Windows:** works through WSL2 with an NVIDIA GPU + the toolkit.

---

## Health & logs

- Health check: `GET http://localhost:6275/api/health` → `{"ok": true, ...}`
  (compose marks the container healthy once this passes).
- Logs: `docker compose logs -f aiko`. The same stream lands in
  `data/app.log` inside the volume.
- The embedded MCP debug server binds `127.0.0.1:6274` **inside** the
  container and is not published by default. To reach it, add a
  `- "6274:6274"` port mapping (note: the MCP runner hardcodes `127.0.0.1`,
  so you'd also need it to bind `0.0.0.0` — debug-only, left off by design).

---

## Desktop app + Dockerised backend

The Tauri desktop shell is just a client of the backend. Run the backend in
Docker (above), then point the desktop build at it — it talks to the same
`http://localhost:6275` over HTTP/WebSocket. The macOS packaged app normally
auto-spawns its own Python sidecar; when you're running the backend in Docker
you don't want that sidecar, so use the plain dev shell (`npm run tauri:dev`)
or a build configured against the container URL. See
[`docs/tauri-shell.md`](tauri-shell.md) for how `backendBase()` resolves the
backend address.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| UI loads but chat errors / "connection refused" to Ollama | Ollama isn't reachable. Host Ollama: is it running and listening on `0.0.0.0`/all interfaces? Try `AIKO_OLLAMA_BASE_URL=http://host.docker.internal:11434`. In-compose: did you set it to `http://ollama:11434` and pull the models? |
| "model not found" on first message | `ollama pull <chat_model>` and `ollama pull qwen3-embedding:0.6b` (host or `docker compose exec ollama ...`). |
| `COPY data/personas/active ... not found` during build | Old Dockerfile behaviour. Pull latest — the avatar COPY is now optional. If you're on an older checkout, create the folder (`mkdir -p data/personas/active/Alexia`) or drop the bundle in before building. |
| Avatar doesn't load | The Live2D bundle wasn't in the build context. Put it at exactly `data/personas/active/Alexia/` (repo root) at build time, or drop it into the `aiko-data` volume at `personas/active/Alexia/`. A bundle copied to the wrong folder (e.g. `data/persona/`) won't be picked up. |
| `No module named 'faster_whisper'` | You're on `slim`, or installed bare `realtimestt`. RealtimeSTT 1.x moved the ASR engines behind extras — the `voice` extra requests `realtimestt[faster-whisper,silero-onnx-cpu]`. Rebuild with `docker-compose-full.yaml`. |
| `snakers4/silero-vad ... not in the list of trusted repositories (y/N)` | Something is using RealtimeSTT's legacy Torch Hub VAD path, which auto-answers *no* when nothing is attached to stdin. Install the `silero-onnx-cpu` extra (the `voice` extra does) and don't pass `silero_use_onnx` — setting it to *either* `True` or `False` pins the backend to the legacy Torch Hub path. |
| `fatal error: portaudio.h: No such file or directory` during build | `portaudio19-dev` is missing from the build. PyAudio (a hard RealtimeSTT dependency) ships no Linux wheel, so it compiles from source and needs the PortAudio headers. The `full` profile installs it. |
| `Exited (137)` partway through boot, no error logged | OOM-killed. The container ran out of memory loading `stt.model` (`large-v1` ≈ 3 GB). Raise Docker Desktop's memory limit or use a smaller model — see "Memory" above. `docker inspect <name> --format "{{.State.OOMKilled}}"` confirms it. |
| `CAS Client Error ... us.aws.cdn.hf.co` while fetching a model | The `hf-xet` transfer backend failing through the container's NAT. The image sets `HF_HUB_DISABLE_XET=1` to use plain HTTPS instead; make sure it isn't overridden to `0`. |
| `/usr/bin/env: 'sh\r': No such file or directory` (exit 127 at boot) | `docker/entrypoint.sh` was copied in with CRLF line endings. The Dockerfile strips CRs now; if you hit it on an older checkout, re-checkout the file so `.gitattributes` (`*.sh text eol=lf`) applies: `rm docker/entrypoint.sh && git checkout -- docker/entrypoint.sh`. |
| Voice controls do nothing | You're on the `slim` image. Rebuild with `docker compose -f docker-compose-full.yaml up -d --build`. |
| Want a clean slate | `docker compose down -v` then `up -d --build` with your chosen file. Removes the data volume, so history, memories and settings go too. |
| `unknown keyword: include` / the variant file isn't understood | Compose v1 or a v2 older than 2.20. Either update Docker Compose or run the base file with `AIKO_PROFILE=full docker compose up -d --build`. |
