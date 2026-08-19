"""Provision a candidate engine into its own venv, via ``uv``.

Why the isolation is not optional
---------------------------------
``pip install --dry-run chatterbox-tts`` against the app's venv wants to
install **torch 2.6.0 over our 2.10.0**, plus ``torchaudio 2.6.0``,
``safetensors 0.5.3`` (down from 0.7.0), ``transformers 5.2.0``,
``pandas``, ``numba``, ``onnx`` and ``gradio``. Upstream says so plainly
-- "the versions of the dependencies are pinned in ``pyproject.toml`` to
ensure consistency" -- which is reasonable of them and fatal to us: a
torch downgrade takes pocket-tts, RealtimeSTT and the embedder with it.

Every remaining candidate has the same shape. Qwen3-TTS's CPU path is a
C runtime, PocketTTS.cpp wants ONNX Runtime, MOSS-TTS-Nano likewise. So
auditioning engines in-process was never going to work, and the fix is
one venv per engine plus a subprocess seam (:mod:`tools.tts_lab.remote`).

The venvs live in ``.venvs/<engine>/`` and are disposable. Nothing in
``app/`` or the main venv is touched, and deleting the directory
uninstalls the experiment completely -- which matters when the candidate
list is this long and most of it will be rejected.

Usage::

    python -m tools.tts_lab.envs list
    python -m tools.tts_lab.envs install chatterbox
    python -m tools.tts_lab.envs remove chatterbox
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_ROOT = REPO_ROOT / ".venvs"


@dataclass(frozen=True)
class EngineEnv:
    """How to build one candidate's environment."""

    name: str
    packages: tuple[str, ...]
    #: Upstream tested on 3.11 and pins aggressively. 3.12 is the
    #: compromise: new enough for current wheels, old enough that a
    #: pinned dependency graph from a few months ago still resolves.
    #: uv downloads it if the machine does not have it.
    python: str = "3.12"
    notes: str = ""

    @property
    def path(self) -> Path:
        return VENV_ROOT / self.name

    @property
    def interpreter(self) -> Path:
        if sys.platform == "win32":
            return self.path / "Scripts" / "python.exe"
        return self.path / "bin" / "python"

    @property
    def installed(self) -> bool:
        return self.interpreter.exists()


ENGINES: dict[str, EngineEnv] = {
    "chatterbox": EngineEnv(
        name="chatterbox",
        # setuptools<81 is not cosmetic. chatterbox depends on
        # resemble-perth for watermarking, perth/perth_net/__init__.py
        # does 'from pkg_resources import resource_filename', and
        # setuptools 81 removed pkg_resources. perth's __init__ catches
        # the ImportError and leaves PerthImplicitWatermarker = None, so
        # the failure surfaces six frames later inside model
        # construction as "TypeError: 'NoneType' object is not callable"
        # -- almost certainly the "CPU loading bugs" this family is known
        # for. Pinning setuptools keeps the watermarker working, which is
        # the honest fix; stubbing perth out would also "work" and would
        # be us disabling someone's responsible-AI feature to save a pin.
        packages=("chatterbox-tts", "setuptools<81"),
        notes=(
            "PyPI 0.1.7: Turbo (350M) and the original (500M). Pulls "
            "torch 2.6 + transformers 5.2, hence the isolation. Weights "
            "download from HuggingFace on first load. No Nano -- the "
            "README documents from_pretrained(nano=True) but the wheel "
            "has no such parameter, so use chatterbox-git for that."
        ),
    ),
    "chatterbox-git": EngineEnv(
        name="chatterbox-git",
        packages=(
            "chatterbox-tts @ git+https://github.com/resemble-ai/chatterbox.git",
            "setuptools<81",  # same pkg_resources trap as above
        ),
        notes=(
            "Master, for Chatterbox-Nano (110M) -- upstream's CPU "
            "recommendation, documented but not yet released to PyPI. "
            "Separate env from the wheel so a broken master does not "
            "cost us the working Turbo."
        ),
    ),
}


def _uv() -> str:
    found = shutil.which("uv")
    if not found:
        raise SystemExit(
            "uv not found on PATH. Install it, or create the venv by hand "
            "and point --python at it."
        )
    return found


def install(env: EngineEnv, *, force: bool = False) -> None:
    if env.installed and not force:
        print(f"{env.name}: already at {env.path.relative_to(REPO_ROOT)}")
        return
    uv = _uv()
    VENV_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"{env.name}: creating venv on python {env.python}")
    subprocess.run(
        [uv, "venv", str(env.path), "--python", env.python],
        check=True,
        cwd=REPO_ROOT,
    )
    print(f"{env.name}: installing {' '.join(env.packages)}")
    subprocess.run(
        [uv, "pip", "install", "--python", str(env.interpreter), *env.packages],
        check=True,
        cwd=REPO_ROOT,
    )
    print(f"{env.name}: ready at {env.interpreter.relative_to(REPO_ROOT)}")


def remove(env: EngineEnv) -> None:
    if not env.path.exists():
        print(f"{env.name}: nothing to remove")
        return
    shutil.rmtree(env.path, ignore_errors=True)
    print(f"{env.name}: removed")


def _list() -> None:
    width = max([len(n) for n in ENGINES] + [6]) + 2
    print(f"{'engine':{width}}{'installed':11}packages")
    print("-" * 72)
    for env in ENGINES.values():
        mark = "yes" if env.installed else "no"
        print(f"{env.name:{width}}{mark:11}{' '.join(env.packages)}")
        if env.notes:
            print(f"{'':{width + 11}}{env.notes}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for verb in ("install", "remove"):
        q = sub.add_parser(verb)
        q.add_argument("engine", choices=sorted(ENGINES))
        if verb == "install":
            q.add_argument("--force", action="store_true")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.cmd == "list":
        _list()
        return 0
    env = ENGINES[args.engine]
    if args.cmd == "install":
        install(env, force=bool(getattr(args, "force", False)))
    else:
        remove(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
