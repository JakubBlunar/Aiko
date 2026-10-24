"""Launch the isolated avatar playground with a chosen local Live2D model."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def build_profile(model_root: Path, name: str = "") -> dict:
    """Use the production capability detector without loading app settings."""
    from app.core.infra.settings import AvatarSettings
    from app.core.persona.avatar_profile import from_disk

    profile = from_disk(model_root, display_name=name or model_root.name).to_dict()
    profile.update(
        loaded=True,
        settings=asdict(AvatarSettings(
            root_dir=str(model_root), entry_filename=profile["entry_filename"], auto_outfit="day",
            scale_multiplier=1.1,
        )),
        resolved_outfit="day" if "day_clothes" in profile["outfits"] else "",
        circadian_period="day",
    )
    # Private preview aliases expose every loaded expression through the
    # existing ExpressionChannel, without altering production mappings.
    profile["playground_expressions"] = {
        f"preview_expression_{i}": expression["name"]
        for i, expression in enumerate(profile["expressions"])
    }
    profile["reaction_mapping"].update(profile["playground_expressions"])
    from app.core.affect.reactions import REACTIONS

    profile["playground_reactions"] = list(REACTIONS)
    return profile


def main(argv: list[str] | None = None, *, scratch_root: Path | None = None) -> int:
    from app.core.persona.avatar_profile import AvatarProfileError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=PROJECT_ROOT / "data/personas/active/Alexia")
    parser.add_argument("--name", default="", help="Optional model display name")
    parser.add_argument("--port", type=int, default=5174)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    model_root = args.model.expanduser().resolve()
    try:
        profile = build_profile(model_root, args.name)
    except AvatarProfileError as exc:
        parser.error(str(exc))
    node = shutil.which("node")
    vite = PROJECT_ROOT / "web/node_modules/vite/bin/vite.js"
    if node is None or not vite.is_file():
        parser.error("Node.js and web dependencies are required; run npm --prefix web install")
    scratch = scratch_root if scratch_root is not None else PROJECT_ROOT / ".tmp-avatar-playground"
    scratch.mkdir(exist_ok=True)
    # A process-owned manifest, never a copy of config/user.json or a DB.
    with TemporaryDirectory(prefix="run-", dir=scratch) as run_dir:
        manifest = Path(run_dir) / "profile.json"
        manifest.write_text(json.dumps(profile), encoding="utf-8", newline="\n")
        env = {
            **os.environ,
            "AIKO_PLAYGROUND_MODEL": str(model_root),
            "AIKO_PLAYGROUND_PROFILE": str(manifest),
        }
        print(f"Avatar playground: http://127.0.0.1:{args.port}/avatar-playground.html", flush=True)
        print("Press Ctrl+C to stop. Your chat backend is not started.", flush=True)
        try:
            process = subprocess.Popen(
                [node, str(vite), "--config", "vite.playground.config.ts",
                 "--port", str(args.port), "--strictPort"],
                cwd=PROJECT_ROOT / "web", env=env,
            )
            try:
                return process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
