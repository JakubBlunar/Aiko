# syntax=docker/dockerfile:1
#
# Aiko backend image. The FastAPI server serves the built React UI and
# talks to Ollama over HTTP for chat + embeddings, so a single container
# gives you the full web experience. Ollama itself runs outside this image
# (on the host, or as a sibling compose service).
#
# Two size profiles via the PROFILE build arg:
#   PROFILE=slim  (default) — text chat + Live2D avatar + memory/RAG. No
#                  PyTorch/whisper, so the image stays small (~1-1.5 GB).
#   PROFILE=full  — adds server-side voice (RealtimeSTT + Pocket-TTS). Pulls
#                  in CPU PyTorch (override TORCH_INDEX_URL for CUDA), ffmpeg
#                  and build tools; multi-GB image.
#
#   docker build -t aiko .                         # slim
#   docker build -t aiko --build-arg PROFILE=full .  # full voice

# ── Stage 1: build the React/Vite frontend ───────────────────────────────
FROM node:20-bookworm-slim AS web-build
WORKDIR /web
# package*.json first so `npm ci` is cached unless deps change.
COPY web/package.json web/package-lock.json* ./
RUN npm ci
COPY web/ ./
RUN npm run build
# -> /web/dist (served by FastAPI in the runtime stage)

# ── Stage 2: python runtime ───────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ARG PROFILE=slim
# Where pip pulls torch from in the full profile. The CPU index keeps the
# image from grabbing the ~2.5 GB CUDA wheel. Point this at a CUDA index
# (e.g. https://download.pytorch.org/whl/cu121) if you intend to give the
# container a GPU for faster STT.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIKO_WEB_HOST=0.0.0.0 \
    AIKO_WEB_PORT=6275 \
    AIKO_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    AIKO_AVATAR_SEED_DIR=/opt/aiko/seed/personas-active

# System deps. libsndfile1 backs `soundfile` (a core dep) in both profiles;
# ffmpeg + build tools only matter for the voice stack in the full profile.
# tini gives us a real PID 1 for clean signal handling / no zombie procs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 ca-certificates curl tini \
 && if [ "$PROFILE" = "full" ]; then \
        apt-get install -y --no-install-recommends ffmpeg build-essential; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies. Copy only what the build needs first so the
# (slow) dependency layer is cached across source-only changes.
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --upgrade pip wheel setuptools \
 && if [ "$PROFILE" = "full" ]; then \
        pip install --index-url "${TORCH_INDEX_URL}" torch \
     && pip install ".[voice]"; \
    else \
        pip install .; \
    fi

# App assets that aren't part of the python package. The app is run from
# this source tree (WORKDIR=/app), so settings.py's parents[3] resolves the
# repo root to /app and data/ + config/ land under it.
COPY config ./config
COPY --from=web-build /web/dist ./web/dist
# Persona text + the Live2D avatar bundle are baked OUTSIDE the data
# volume and seeded into it on first boot (the volume would otherwise
# shadow anything baked under /app/data). Persona text is copied in by
# the entrypoint; the avatar bundle is self-healed by the app on boot
# from $AIKO_AVATAR_SEED_DIR (see SessionController._seed_avatar_root_if_empty).
COPY data/persona ./_seed-persona
COPY data/personas/active ./_seed-personas-active
COPY docker/entrypoint.sh /usr/local/bin/aiko-entrypoint
RUN chmod +x /usr/local/bin/aiko-entrypoint \
 && mkdir -p /opt/aiko/seed \
 && mv ./_seed-persona /opt/aiko/seed/persona \
 && mv ./_seed-personas-active /opt/aiko/seed/personas-active

EXPOSE 6275
VOLUME ["/app/data"]

# /api/health returns {"ok": true, ...} once the server is listening.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${AIKO_WEB_PORT}/api/health" || exit 1

ENTRYPOINT ["tini", "--", "aiko-entrypoint"]
CMD ["python", "-m", "app.web"]
