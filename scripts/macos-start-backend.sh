#!/usr/bin/env bash
# scripts/macos-start-backend.sh
#
# Tauri sidecar entrypoint: launches the FastAPI backend from the
# Application Support venv created by ``scripts/setup-macos.sh``.
#
# Tauri spawns this script as a child process when the .app starts.
# The Rust ``ensure_backend_running`` command waits for the resulting
# FastAPI server on http://127.0.0.1:6275 before connecting the WS.
#
# Exits with a non-zero status if the venv is missing so Tauri can
# surface a clear "run the setup script" message in the UI.

set -Eeuo pipefail

APP_NAME="Aiko"
SUPPORT_DIR="${HOME}/Library/Application Support/${APP_NAME}"
VENV_DIR="${SUPPORT_DIR}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
REPO_ROOT_FROM_ENV="${AIKO_REPO_ROOT:-}"
LOG_DIR="${SUPPORT_DIR}/logs"
LOG_FILE="${LOG_DIR}/backend.log"

mkdir -p "${LOG_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Aiko venv not found at ${VENV_DIR}." >&2
  echo "Run scripts/setup-macos.sh (or 'Aiko Setup.command' from the DMG)." >&2
  exit 2
fi

# Find a working directory that has app/ on its PYTHONPATH. Priority:
#   1. AIKO_REPO_ROOT env var (used by the Tauri sidecar)
#   2. The .app's Resources directory (one level up from this script)
#   3. The Application Support data dir
WORKDIR=""
if [[ -n "${REPO_ROOT_FROM_ENV}" && -d "${REPO_ROOT_FROM_ENV}/app" ]]; then
  WORKDIR="${REPO_ROOT_FROM_ENV}"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -d "${SCRIPT_DIR}/../app" ]]; then
    WORKDIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
  elif [[ -d "${SUPPORT_DIR}/app" ]]; then
    WORKDIR="${SUPPORT_DIR}"
  fi
fi

if [[ -z "${WORKDIR}" ]]; then
  echo "Cannot find Aiko's app/ package. Reinstall via scripts/setup-macos.sh." >&2
  exit 3
fi

cd "${WORKDIR}"

exec "${PYTHON_BIN}" -m app.web >> "${LOG_FILE}" 2>&1
