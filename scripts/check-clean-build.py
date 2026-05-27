#!/usr/bin/env python3
"""Pre-build sanity check for the macOS distribution.

Run automatically by the ``tauri:build:macos`` npm script. Fails the
build (non-zero exit) if a developer artefact would end up in the .app
bundle Resources or if a known-required asset is missing.

Checks:
  1. ``data/chat_sessions.db`` must NOT exist. Friend should start with
     a fresh chat database.
  2. ``data/persona/aiko_companion.txt`` must NOT contain the literal
     word ``"Jacob"`` (case-insensitive). It should template the user's
     name via ``{user_name}``.
  3. ``data/persona/aiko_companion_backup.txt`` must NOT exist (stale
     backup that still contains "Jacob").
  4. ``data/personas/active/`` must contain at least one persona
     subdirectory with a Live2D ``*.model3.json`` file.
  5. ``config/default.json -> assistant.user_display_name`` must be
     blank so first-run onboarding actually fires for the friend.

The script is intentionally repository-relative and platform-agnostic;
the only thing macOS-specific is the npm script that calls it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ERRORS: list[str] = []


def fail(msg: str) -> None:
    ERRORS.append(msg)


def check_no_dev_db() -> None:
    db = REPO_ROOT / "data" / "chat_sessions.db"
    if db.exists():
        fail(
            f"developer chat database is present: {db}\n"
            "  delete it (or move it elsewhere) before building the bundle."
        )


def check_persona_template() -> None:
    persona = REPO_ROOT / "data" / "persona" / "aiko_companion.txt"
    if not persona.exists():
        fail(f"persona file missing: {persona}")
        return
    text = persona.read_text(encoding="utf-8")
    if "jacob" in text.lower():
        fail(
            f"persona still mentions 'Jacob': {persona}\n"
            "  every occurrence must be templated as '{user_name}'."
        )


def check_backup_removed() -> None:
    backup = REPO_ROOT / "data" / "persona" / "aiko_companion_backup.txt"
    if backup.exists():
        fail(
            f"stale persona backup is present: {backup}\n"
            "  delete it before building."
        )


def check_avatar_bundle() -> None:
    """The bundle takes its Live2D source from either the new
    ``data/personas/active/<name>/`` location or the legacy
    ``live-2d-models/<name>/`` location. The destination inside the
    .app's Resources is always ``data/personas/active/<name>/``, so a
    build is fine as long as one of the two source dirs has a
    ``*.model3.json`` file we can bundle.
    """
    candidate_roots = [
        REPO_ROOT / "data" / "personas" / "active",
        REPO_ROOT / "live-2d-models",
    ]
    for root in candidate_roots:
        if not root.exists():
            continue
        for sub in root.iterdir():
            if not sub.is_dir():
                continue
            if list(sub.glob("*.model3.json")):
                return
    fail(
        "no *.model3.json found under data/personas/active/<name>/ or "
        "live-2d-models/<name>/.\n"
        "  drop the Live2D bundle into one of those directories."
    )


def check_default_identity_blank() -> None:
    cfg_path = REPO_ROOT / "config" / "default.json"
    if not cfg_path.exists():
        fail(f"config file missing: {cfg_path}")
        return
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"config file is not valid JSON ({cfg_path}): {exc}")
        return
    assistant = data.get("assistant", {})
    name = (assistant.get("user_display_name") or "").strip()
    if name:
        fail(
            f"config/default.json -> assistant.user_display_name is set "
            f"to {name!r}.\n  it must be blank so first-run onboarding "
            "triggers for the friend."
        )


def main() -> int:
    check_no_dev_db()
    check_persona_template()
    check_backup_removed()
    check_avatar_bundle()
    check_default_identity_blank()

    if ERRORS:
        print("clean-build check FAILED:\n", file=sys.stderr)
        for err in ERRORS:
            print(f"  - {err}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1

    print("clean-build check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
