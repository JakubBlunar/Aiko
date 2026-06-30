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
# 1. Install Ollama on the host and pull the models (defaults match config/default.json)
ollama pull qwen3-coder:30b
ollama pull qwen3-embedding:0.6b

# 2. Build + start Aiko (text + avatar, slim image)
docker compose up -d --build

# 3. Open the UI
#    http://localhost:6275
```

That's it. The container reaches your host's Ollama via
`host.docker.internal:11434`.

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
# slim (default)
docker compose up -d --build

# full voice — pick ONE of:
AIKO_PROFILE=full docker compose up -d --build       # via .env / inline env
docker build -t aiko:full --build-arg PROFILE=full . # plain docker build
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

If you'd rather use the full `config/user.json` mechanism (advanced LLM
routing, etc.), mount it read-only:

```yaml
    volumes:
      - aiko-data:/app/data
      - ./config/user.json:/app/config/user.json:ro
```

---

## Data persistence

The `aiko-data` named volume is mounted at `/app/data` and holds everything
that should survive a rebuild:

- `data/chat_sessions.db` — messages, memories, world, tasks, beliefs, …
- `data/lancedb/` — the vector index
- `data/documents/`, `data/attachments/` — uploads
- `data/personas/active/Alexia/` — the active avatar bundle
- `data/persona/` — the persona text

Two things are baked into the image and seeded into that volume on first run
so an empty volume doesn't blank them out:

- **Persona text** — the entrypoint copies `data/persona/*` from the image
  into the volume **only if absent** (your edits are never clobbered).
- **Live2D avatar** — baked at `/opt/aiko/seed/personas-active` (outside the
  volume) via `AIKO_AVATAR_SEED_DIR`; the app self-heals it on boot into
  `data/personas/active/Alexia`.

> The avatar bundle is gitignored, so it ships in the image only if it's
> present on your machine at build time (under `data/personas/active/Alexia/`).
> If you build on a machine without it, drop the bundle into the volume
> yourself or mount it in.

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

docker compose --profile with-ollama up -d --build

# pull models INTO the container (stored in the ollama-models volume, so
# this is a one-time cost — they survive restarts and recreation)
docker compose exec ollama ollama pull qwen3-coder:30b
docker compose exec ollama ollama pull qwen3-embedding:0.6b
```

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
| Avatar doesn't load | The Live2D bundle wasn't in the build context. Ensure `data/personas/active/Alexia/` exists at build time, or drop the bundle into the `aiko-data` volume at `personas/active/Alexia/`. |
| Voice controls do nothing | You're on the `slim` image. Rebuild with `AIKO_PROFILE=full`. |
| Want a clean slate | `docker compose down -v` then `up -d --build`. |
