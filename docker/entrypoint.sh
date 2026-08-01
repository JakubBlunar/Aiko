#!/usr/bin/env sh
# Aiko container entrypoint.
#
# The data dir (/app/data) is a volume so chat history, memories, the
# LanceDB index and the active avatar survive container recreation. But a
# fresh/empty volume shadows two things baked into the image:
#
#   * the persona text (data/persona/*.txt) — seeded here from
#     /opt/aiko/seed/persona, per file and copy-if-absent so a user edit is
#     never clobbered.
#   * the Live2D avatar bundle — NOT seeded here: the app self-heals it on
#     boot from $AIKO_AVATAR_SEED_DIR/<name> (baked at
#     /opt/aiko/seed/personas-active, outside the volume) into
#     data/personas/active/<name> (see
#     SessionController._seed_avatar_root_if_empty).
#
# Host / port / Ollama URL come from env (AIKO_WEB_HOST, AIKO_WEB_PORT,
# AIKO_OLLAMA_BASE_URL) — see app/web/__main__._apply_env_overrides — so no
# config file mount is required for a normal run.
#
# Runtime settings (the name and model the first-run wizard collects, API
# keys, avatar tweaks) are written to $AIKO_USER_CONFIG, which points into
# the volume for the same reason: /app/config lives in the container's
# writable layer and is discarded on every recreate.
set -e

DATA_DIR="/app/data"
SEED_DIR="/opt/aiko/seed"
USER_CONFIG="${AIKO_USER_CONFIG:-${DATA_DIR}/user.json}"

# The hoisted handling notes were called cue_handling.txt for one release
# before growing past cues. Carry a volume's copy over under the new name
# rather than seeding beside it, or an edit made to the old file would go
# on sitting there doing nothing.
if [ -f "${DATA_DIR}/persona/cue_handling.txt" ] \
   && [ ! -e "${DATA_DIR}/persona/conditional_handling.txt" ]; then
  mv "${DATA_DIR}/persona/cue_handling.txt" \
     "${DATA_DIR}/persona/conditional_handling.txt"
  echo "[entrypoint] renamed persona/cue_handling.txt -> conditional_handling.txt"
fi

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

# A mounted /app/config/user.json used to be the only way to configure the
# container, so treat it as the seed for the volume copy — otherwise an
# existing deployment's settings would vanish the first time it runs an
# image that reads from the volume instead. Copy-if-absent, so the volume
# (which the app keeps writing to) always wins afterwards.
if [ ! -e "${USER_CONFIG}" ] && [ -f /app/config/user.json ]; then
  mkdir -p "$(dirname "${USER_CONFIG}")"
  cp /app/config/user.json "${USER_CONFIG}"
  echo "[entrypoint] seeded user.json into the data volume"
fi

exec "$@"
