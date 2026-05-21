"""Fetch the Live2D Cubism Core JavaScript runtime and supporting libraries.

The Cubism Core (`live2dcubismcore.min.js`) is distributed by Live2D Inc. under
the Live2D Proprietary Software License (a.k.a. "Free Material License" for the
web SDK). It cannot be redistributed with this project, so this script downloads
it on demand after the user accepts the license.

Run:

    python scripts/fetch_live2d_core.py            # core + viewer libs
    python scripts/fetch_live2d_core.py --sample   # also fetch Hiyori sample model

The script is idempotent: existing files are kept unless ``--force`` is passed.
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

LICENSE_URL = "https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html"
CORE_URLS = [
    "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js",
    "https://cdn.jsdelivr.net/gh/Live2D/CubismWebSamples@develop/Core/live2dcubismcore.min.js",
    "https://cdn.jsdelivr.net/gh/Live2D/CubismWebSamples@master/Core/live2dcubismcore.min.js",
]
PIXI_URL = "https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js"
# Use the Cubism 4-only bundle. The default ``index.min.js`` ships both
# Cubism 2 and Cubism 4 runtimes and refuses to start when ``live2d.min.js``
# (the legacy Cubism 2 runtime) is absent. We only target model3.json
# (Cubism 4) models, so the 4-only bundle is enough.
PIXI_LIVE2D_URL = (
    "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"
)

SAMPLE_BASE = (
    "https://cdn.jsdelivr.net/gh/Live2D/CubismWebSamples@develop/Samples/Resources/Hiyori/"
)

# The Hiyori sample distributed by Live2D has no expression files. The motion
# list mirrors what ``Hiyori.model3.json`` references (Idle m01-m03 + m05-m10,
# TapBody m04). Files that 404 are skipped silently below.
SAMPLE_FILES = [
    "Hiyori.model3.json",
    "Hiyori.moc3",
    "Hiyori.physics3.json",
    "Hiyori.pose3.json",
    "Hiyori.cdi3.json",
    "Hiyori.userdata3.json",
    "Hiyori.2048/texture_00.png",
    "Hiyori.2048/texture_01.png",
    "motions/Hiyori_m01.motion3.json",
    "motions/Hiyori_m02.motion3.json",
    "motions/Hiyori_m03.motion3.json",
    "motions/Hiyori_m04.motion3.json",
    "motions/Hiyori_m05.motion3.json",
    "motions/Hiyori_m06.motion3.json",
    "motions/Hiyori_m07.motion3.json",
    "motions/Hiyori_m08.motion3.json",
    "motions/Hiyori_m09.motion3.json",
    "motions/Hiyori_m10.motion3.json",
]


ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "resources" / "vendor" / "live2d"
SAMPLE_DIR = ROOT / "data" / "avatars" / "hiyori"

# Default urllib User-Agent is rejected by some CDNs (notably cubism.live2d.com
# returns 403). Pretend to be a modern browser.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def _download(url: str, dest: Path, *, force: bool, quiet_404: bool = False) -> bool:
    if dest.exists() and not force:
        print(f"  skip (exists): {dest.relative_to(ROOT)}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetch {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and quiet_404:
            print(f"    not in this sample (skipped)")
        else:
            print(f"    failed: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"    failed: {exc}", file=sys.stderr)
        return False
    dest.write_bytes(data)
    print(f"    wrote {dest.relative_to(ROOT)} ({len(data)} bytes)")
    return True


def _download_first(urls: list[str], dest: Path, *, force: bool) -> bool:
    """Try each URL in order until one succeeds."""
    if dest.exists() and not force:
        print(f"  skip (exists): {dest.relative_to(ROOT)}")
        return True
    for url in urls:
        if _download(url, dest, force=True):
            return True
    return False


def _prompt_license() -> bool:
    print(
        "Live2D Cubism Core is distributed under the Live2D Proprietary Software\n"
        "License. By downloading it you agree to the terms at:\n"
        f"  {LICENSE_URL}\n"
    )
    answer = input("Accept and download? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Live2D web runtime")
    parser.add_argument("--force", action="store_true", help="re-download even if files exist")
    parser.add_argument(
        "--sample", action="store_true", help="also download the Hiyori sample model"
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip license prompt (you accept the EULA)"
    )
    args = parser.parse_args(argv)

    if not args.yes and not _prompt_license():
        print("Aborted.")
        return 1

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching Cubism Core + viewer libraries...")
    _download(PIXI_URL, VENDOR_DIR / "pixi.min.js", force=args.force)
    _download(PIXI_LIVE2D_URL, VENDOR_DIR / "pixi-live2d-display.min.js", force=args.force)
    _download_first(CORE_URLS, VENDOR_DIR / "live2dcubismcore.min.js", force=args.force)

    if args.sample:
        print("Fetching Hiyori sample model...")
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        for rel in SAMPLE_FILES:
            _download(SAMPLE_BASE + rel, SAMPLE_DIR / rel, force=args.force, quiet_404=True)
        # Hiyori has no expression files; that is normal.
        print(
            "Hiyori has no expression files; the expression_map in config will be\n"
            "ignored for this model. Use another model (e.g. from booth.pm) if you\n"
            "want reaction-driven expressions."
        )

    core = VENDOR_DIR / "live2dcubismcore.min.js"
    if core.exists():
        print(f"\nDone. Cubism core at {core.relative_to(ROOT)}.")
        return 0
    print(
        "\nCubism core was not downloaded automatically (CDN may have rejected\n"
        "the request). Download it manually from\n"
        "  https://www.live2d.com/en/sdk/download/web/\n"
        "and place 'live2dcubismcore.min.js' into:\n"
        f"  {(VENDOR_DIR).relative_to(ROOT)}/",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
