#!/usr/bin/env sh
# Aiko container entrypoint.
#
# The data dir (/app/data) is a volume so chat history, memories, the
# LanceDB index and the active avatar survive container recreation. But a
# fresh/empty volume shadows two things baked into the image:
#
#   * the persona text (data/persona/aiko_companion.txt) — seeded here from
#     /opt/aiko/seed/persona, copy-if-absent so a user edit is never
#     clobbered.
#   * the Live2D avatar bundle — NOT seeded here: the app self-heals it on
#     boot from $AIKO_AVATAR_SEED_DIR/<name> (baked at
#     /opt/aiko/seed/personas-active, outside the volume) into
#     data/personas/active/<name> (see
#     SessionController._seed_avatar_root_if_empty).
#
# Host / port / Ollama URL come from env (AIKO_WEB_HOST, AIKO_WEB_PORT,
# AIKO_OLLAMA_BASE_URL) — see app/web/__main__._apply_env_overrides — so no
# config file mount is required for a normal run.
set -e

DATA_DIR="/app/data"
SEED_DIR="/opt/aiko/seed"

if [ -d "${SEED_DIR}/persona" ]; then
  mkdir -p "${DATA_DIR}/persona"
  for src in "${SEED_DIR}"/persona/*; do
    [ -e "${src}" ] || continue
    name="$(basename "${src}")"
    if [ ! -e "${DATA_DIR}/persona/${name}" ]; then
      cp -a "${src}" "${DATA_DIR}/persona/${name}"
      echo "[entrypoint] seeded persona/${name}"
    fi
  done
fi

exec "$@"
