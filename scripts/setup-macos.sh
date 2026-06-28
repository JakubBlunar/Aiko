#!/usr/bin/env bash
# scripts/setup-macos.sh
#
# One-shot installer for Aiko on macOS.
#
# Run once per machine:
#
#   bash scripts/setup-macos.sh
#
# Or via the "Aiko Setup.command" shim shipped beside the .app bundle.
#
# Idempotent: re-running picks up the latest pinned dependencies and
# verifies the Application Support layout without re-downloading Homebrew
# or Ollama models that already exist.

set -Eeuo pipefail

APP_NAME="Aiko"
SUPPORT_DIR="${HOME}/Library/Application Support/${APP_NAME}"
VENV_DIR="${SUPPORT_DIR}/venv"
CONFIG_DIR="${SUPPORT_DIR}/config"
DATA_DIR="${SUPPORT_DIR}/data"
LOG_DIR="${SUPPORT_DIR}/logs"
LOG_FILE="${LOG_DIR}/setup.log"
PYTHON_BIN="${VENV_DIR}/bin/python"
MIN_MACOS_VERSION="12"
DEFAULT_CHAT_MODEL="${AIKO_CHAT_MODEL:-qwen2.5:7b-instruct}"
SMALL_CHAT_MODEL="qwen2.5:3b-instruct"

# Resolve the bundle/repo root: this script lives in <root>/scripts/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

mkdir -p "${SUPPORT_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${LOG_DIR}"

# ── helpers ────────────────────────────────────────────────────────────
log() {
  local msg
  msg="[$(date '+%H:%M:%S')] $*"
  echo "${msg}"
  echo "${msg}" >> "${LOG_FILE}"
}

die() {
  log "✖ $*"
  exit 1
}

confirm() {
  # confirm "Question" -> 0 if yes, 1 if no. Defaults to yes on Enter.
  local prompt="$1"
  local reply
  read -r -p "${prompt} [Y/n] " reply
  case "${reply}" in
    ""|y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || die "This installer only supports macOS."
  local ver_major
  ver_major="$(sw_vers -productVersion | cut -d. -f1)"
  if (( ver_major < MIN_MACOS_VERSION )); then
    die "macOS ${MIN_MACOS_VERSION}+ is required (you have $(sw_vers -productVersion))."
  fi
}

ensure_homebrew() {
  if command -v brew >/dev/null 2>&1; then
    log "Homebrew already installed."
    return
  fi
  log "Installing Homebrew (this can take a few minutes)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Apple Silicon brew prefix
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew >/dev/null 2>&1 || die "Homebrew installation failed."
}

ensure_brew_packages() {
  log "Ensuring Homebrew packages: python@3.11, ffmpeg, portaudio, ollama."
  brew update >/dev/null
  for pkg in python@3.11 ffmpeg portaudio ollama; do
    if brew list "${pkg}" >/dev/null 2>&1; then
      log "  ✓ ${pkg} already installed."
    else
      log "  → installing ${pkg}..."
      brew install "${pkg}"
    fi
  done
}

ensure_venv() {
  local py
  py="$(brew --prefix python@3.11)/bin/python3.11"
  if [[ ! -x "${py}" ]]; then
    py="$(command -v python3.11 || true)"
  fi
  [[ -x "${py}" ]] || die "python3.11 binary not found after brew install."

  if [[ ! -x "${PYTHON_BIN}" ]]; then
    log "Creating virtualenv at ${VENV_DIR}."
    "${py}" -m venv "${VENV_DIR}"
  else
    log "Virtualenv already exists at ${VENV_DIR}."
  fi

  log "Upgrading pip + installing Python dependencies (this can take a few minutes)..."
  "${PYTHON_BIN}" -m pip install --upgrade pip wheel setuptools >> "${LOG_FILE}" 2>&1
  # The repo ships dependencies via pyproject.toml. The ``[voice]`` extra
  # pulls in RealtimeSTT + Pocket-TTS (the heavy PyTorch/whisper stack) so
  # the macOS install keeps full speech in/out -- those moved out of the
  # core deps so a slim text-only Docker image can skip them.
  "${PYTHON_BIN}" -m pip install "${REPO_ROOT}[voice]" >> "${LOG_FILE}" 2>&1 \
    || die "pip install failed. Tail ${LOG_FILE} for details."
}

ensure_ollama_service() {
  log "Starting Ollama service via brew services (if not already running)."
  if ! brew services list | grep -E '^ollama\s+started' >/dev/null 2>&1; then
    brew services start ollama >/dev/null 2>&1 || true
  fi
  # Wait briefly for the daemon socket.
  for _ in $(seq 1 10); do
    if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      log "Ollama is responding on :11434."
      return
    fi
    sleep 1
  done
  log "⚠ Ollama did not become reachable on :11434 within 10s. You can"
  log "  start it manually with 'brew services start ollama' and re-run."
}

ensure_chat_model() {
  local target="${DEFAULT_CHAT_MODEL}"
  if curl -sf "http://127.0.0.1:11434/api/tags" 2>/dev/null \
      | grep -q "\"name\":\"${target}\""; then
    log "Ollama model '${target}' already pulled."
    return
  fi
  log "Aiko will need to download the chat model '${target}'."
  log "Default model is ~4-7 GB. A smaller option is '${SMALL_CHAT_MODEL}' (~2 GB)."
  if ! confirm "Pull '${target}' now?"; then
    if confirm "Pull the smaller '${SMALL_CHAT_MODEL}' instead?"; then
      target="${SMALL_CHAT_MODEL}"
    else
      log "Skipping model pull. Aiko will not work until you run:"
      log "  ollama pull ${DEFAULT_CHAT_MODEL}"
      return
    fi
  fi
  log "Pulling ${target} via Ollama (this can take a while)."
  ollama pull "${target}" || die "ollama pull ${target} failed."
}

copy_bundle_assets() {
  # The .app bundle Resources directory mirrors the repo's data/ and
  # config/default.json. When this script is shipped via the DMG, those
  # files sit next to scripts/ in the Resources dir. When the friend
  # runs it directly from the source checkout, REPO_ROOT already has
  # everything in place. Either way, make sure Application Support has
  # a copy of the templates so the running .app can read them.

  if [[ ! -f "${CONFIG_DIR}/default.json" ]]; then
    if [[ -f "${REPO_ROOT}/config/default.json" ]]; then
      cp "${REPO_ROOT}/config/default.json" "${CONFIG_DIR}/default.json"
      log "Seeded ${CONFIG_DIR}/default.json."
    fi
  fi

  for sub in persona personas; do
    if [[ -d "${REPO_ROOT}/data/${sub}" && ! -d "${DATA_DIR}/${sub}" ]]; then
      cp -R "${REPO_ROOT}/data/${sub}" "${DATA_DIR}/${sub}"
      log "Seeded ${DATA_DIR}/${sub}."
    fi
  done

  # Live2D bundle: prefer the new location, fall back to legacy.
  local avatar_target="${DATA_DIR}/personas/active/Alexia"
  if [[ ! -d "${avatar_target}" || -z "$(ls -A "${avatar_target}" 2>/dev/null)" ]]; then
    mkdir -p "${avatar_target}"
    local avatar_src=""
    if [[ -d "${REPO_ROOT}/data/personas/active/Alexia" ]]; then
      avatar_src="${REPO_ROOT}/data/personas/active/Alexia"
    elif [[ -d "${REPO_ROOT}/live-2d-models/Alexia" ]]; then
      avatar_src="${REPO_ROOT}/live-2d-models/Alexia"
    fi
    if [[ -n "${avatar_src}" ]]; then
      cp -R "${avatar_src}/." "${avatar_target}/"
      log "Seeded Live2D bundle into ${avatar_target}."
    else
      log "⚠ No Live2D bundle found in the source tree. Drop the model"
      log "  files into ${avatar_target}/ before launching Aiko."
    fi
  fi
}

print_summary() {
  log ""
  log "──────────────────────────────────────────"
  log "Aiko is ready."
  log ""
  log "  venv:    ${VENV_DIR}"
  log "  config:  ${CONFIG_DIR}"
  log "  data:    ${DATA_DIR}"
  log "  logs:    ${LOG_DIR}"
  log ""
  log "Launch the desktop app (Aiko.app). On first run it will ask for"
  log "your display name -- that name is woven into everything Aiko"
  log "says about you."
  log "──────────────────────────────────────────"
}

# ── main ───────────────────────────────────────────────────────────────
log "Aiko setup starting. Logging to ${LOG_FILE}."
require_macos
ensure_homebrew
ensure_brew_packages
ensure_venv
ensure_ollama_service
ensure_chat_model
copy_bundle_assets
print_summary
