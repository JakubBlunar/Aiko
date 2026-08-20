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

# Build caching: every expensive step below is keyed on the smallest set of
# files that can actually change it, and the slow downloads (apt, pip, npm)
# go through BuildKit cache mounts that survive across builds and across the
# two profiles. A source-only edit should rebuild in seconds. This needs
# BuildKit — the default since Docker 23 and always the case under
# `docker compose build`, but `DOCKER_BUILDKIT=0` will fail on the mounts.

# ── Stage 1: build the React/Vite frontend ───────────────────────────────
FROM node:20-bookworm-slim AS web-build
WORKDIR /web
# package*.json first so `npm ci` is cached unless deps change. The cache
# mount then covers the case where they DO change: npm ci always wipes
# node_modules, but it refetches from the local cache instead of the network.
COPY web/package.json web/package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build
# -> /web/dist (served by FastAPI in the runtime stage)

# ── Stage 2: python runtime ───────────────────────────────────────────────
# 3.13 matches the interpreter the dependency set is locked and tested against
# (see requirements.lock). Do NOT move to 3.14: ctranslate2 (via faster-whisper)
# ships no cp314 wheels and no sdist, so the voice stack cannot install there.
FROM python:3.13-slim-bookworm AS runtime

ARG PROFILE=slim
# Where pip pulls torch from in the full profile. The CPU index keeps the
# image from grabbing the ~2.5 GB CUDA wheel. Point this at a CUDA index
# (e.g. https://download.pytorch.org/whl/cu121) if you intend to give the
# container a GPU for faster STT.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# No PIP_NO_CACHE_DIR here on purpose: the pip cache lives on a BuildKit
# cache mount (see below), so it speeds up rebuilds without ever landing in
# a layer. Disabling it would make the mount useless.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIKO_WEB_HOST=0.0.0.0 \
    AIKO_WEB_PORT=6275 \
    AIKO_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    AIKO_AVATAR_SEED_DIR=/opt/aiko/seed/personas-active \
    AIKO_USER_CONFIG=/app/data/user.json

# Fall back to plain HTTPS for Hugging Face downloads instead of the newer Xet
# chunk backend (hf-xet), which ships with huggingface_hub and fails from inside
# the container's NAT on larger models:
#   "CAS Client Error: ... error sending request for url (us.aws.cdn.hf.co)"
# That aborts the whisper model fetch, the RealtimeSTT transcription worker
# retry-loops on it, and the app never reaches the point of serving HTTP.
# Slower but reliable; set HF_HUB_DISABLE_XET=0 to opt back in.
ENV HF_HUB_DISABLE_XET=1

# System deps. libsndfile1 backs `soundfile` (a core dep) in both profiles;
# ffmpeg + build tools only matter for the voice stack in the full profile.
# tini gives us a real PID 1 for clean signal handling / no zombie procs.
#
# portaudio19-dev is required, not optional: PyAudio (a hard dependency of
# RealtimeSTT) publishes NO Linux wheels at all, only an sdist, so pip must
# compile it and needs the PortAudio headers. Without this the full build fails
# with "fatal error: portaudio.h: No such file or directory".
#
# The .deb downloads go to a cache mount, which is why the usual
# `rm -rf /var/lib/apt/lists/*` is gone: /var/cache/apt and /var/lib/apt are
# mounts, so neither the archives nor the package lists end up in the layer.
# The base image ships a docker-clean hook that deletes every .deb right
# after install, which would empty the cache on each run — drop it first.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
 && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' \
      > /etc/apt/apt.conf.d/keep-cache
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 ca-certificates curl tini \
 && if [ "$PROFILE" = "full" ]; then \
        apt-get install -y --no-install-recommends \
            ffmpeg build-essential portaudio19-dev; \
    fi

WORKDIR /app

# Install python dependencies. This layer is keyed on pyproject.toml +
# requirements.lock ONLY, so editing anything under app/ leaves it cached —
# the app source is copied in near the end of the file for that reason.
#
# The stub package is what makes that split possible: `pip install .` has to
# build the project to learn its dependencies, and the setuptools backend
# needs the `app` package to exist. An empty one is enough, and the real
# tree is copied over it later.
#
# requirements.lock is applied as a *constraints* file: pyproject.toml decides
# what gets installed, the lock decides which versions, and constraints on
# packages we don't install (e.g. the CUDA nvidia-* wheels) are simply ignored.
#
# torch + torchaudio come from TORCH_INDEX_URL so we get the small +cpu builds
# instead of the multi-GB CUDA ones, and both are installed in one command so
# the pair stays ABI-matched (RealtimeSTT needs both). PyPI has to stay reachable
# as an --extra-index-url: --index-url *replaces* PyPI rather than adding to it,
# and the PyTorch index doesn't carry torch's own deps at the locked versions
# (the build fails on "no matching distribution ... filelock" without this).
# The CPU index stays primary so its "+cpu" local version wins over the plain
# CUDA wheel of the same version on PyPI.
COPY pyproject.toml README.md requirements.lock ./
RUN mkdir -p app && touch app/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip wheel setuptools \
 && if [ "$PROFILE" = "full" ]; then \
        pip install --index-url "${TORCH_INDEX_URL}" \
            --extra-index-url https://pypi.org/simple \
            -c requirements.lock torch torchaudio \
     && pip install -c requirements.lock ".[voice]"; \
    else \
        pip install -c requirements.lock .; \
    fi

# App assets that aren't part of the python package. The app is run from
# this source tree (WORKDIR=/app), so settings.py's parents[3] resolves the
# repo root to /app and data/ + config/ land under it. Note that
# AIKO_USER_CONFIG moves the *writable* overrides file out of config/ and
# into the data volume — config/ here is the read-only default.json plus
# whatever a user chooses to mount.
COPY config ./config
COPY --from=web-build /web/dist ./web/dist
# Her voice: ~10 MB of Pocket-TTS speaker embeddings, which the shipped
# config/default.json already names. Without them the full profile boots,
# works, and speaks as "alba" -- Pocket-TTS resolves a missing voice file
# by substituting a stock speaker, so the symptom was a companion with the
# wrong voice and, until the warning added alongside this, nothing in the
# log to explain it.
#
# Globbed to the embeddings rather than copying the directory. `voices/`
# also accumulates audition renders, generated fine-tuning datasets and
# studio takes -- 34 MB of untracked scratch at the time of writing, and
# growing with every experiment. Copying the directory swept all of it
# into the image. The reference wav is deliberately left out too: its only
# consumer is a cloning engine, and none ships here (see docs/docker.md).
#
# Copied in both profiles because 10 MB is not worth a conditional, and a
# slim install later switched to full then already has what it needs.
COPY voices/*.safetensors ./voices/
# Persona text + the Live2D avatar bundle are baked OUTSIDE the data
# volume and seeded into it on first boot (the volume would otherwise
# shadow anything baked under /app/data). Persona text is copied in by
# the entrypoint; the avatar bundle is self-healed by the app on boot
# from $AIKO_AVATAR_SEED_DIR (see SessionController._seed_avatar_root_if_empty).
COPY data/persona ./_seed-persona
# The Live2D avatar bundle is a gitignored third-party model, so it may be
# absent on a fresh clone. Anchor the COPY on README.md (always present) and
# use a bracket-glob for the optional bundle path so the build NEVER fails
# when it's missing (a lone "not found" source aborts the build). When the
# bundle is present its contents land alongside the anchor; the app then
# self-heals it into the /app/data volume on boot. When it's absent the app
# just boots avatar-less until a bundle is dropped into the volume. The
# README anchor is deleted right after so it can't leak into the seed dir.
COPY README.md data/personas/activ[e] ./_seed-personas-active/
COPY docker/entrypoint.sh /usr/local/bin/aiko-entrypoint
# Strip CRs before anything tries to exec it. COPY takes the *working tree*
# copy, so a checkout that predates .gitattributes (or any tooling that
# rewrites line endings) bakes in CRLF -- and then the `#!/usr/bin/env sh`
# shebang resolves to `sh\r`, so the container dies at boot with
# "/usr/bin/env: 'sh\r': No such file or directory" and exit code 127.
RUN sed -i 's/\r$//' /usr/local/bin/aiko-entrypoint \
 && chmod +x /usr/local/bin/aiko-entrypoint \
 && mkdir -p /opt/aiko/seed ./_seed-personas-active \
 && rm -f ./_seed-personas-active/README.md \
 && mv ./_seed-persona /opt/aiko/seed/persona \
 && mv ./_seed-personas-active /opt/aiko/seed/personas-active

# The app source goes last: it's what changes on almost every build, and
# everything above it is expensive. Reinstalling the project over the stub
# keeps the installed metadata (version, console script) matching the real
# tree; --no-deps because the dependency layer already resolved everything,
# and --no-build-isolation to reuse the setuptools installed above instead
# of fetching a fresh build environment. The build artifacts setuptools
# leaves behind are removed in the same layer so they don't ship.
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation . \
 && rm -rf build ./*.egg-info

EXPOSE 6275
VOLUME ["/app/data"]

# /api/health returns {"ok": true, ...} once the server is listening.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${AIKO_WEB_PORT}/api/health" || exit 1

ENTRYPOINT ["tini", "--", "aiko-entrypoint"]
CMD ["python", "-m", "app.web"]
