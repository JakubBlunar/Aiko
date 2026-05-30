"""Render a fixed phrase at every reaction-speed value to WAV files.

Layer 5 ear-test helper: writes one ``.wav`` per reaction in
``app.tts.pocket_tts_service._REACTION_SPEED`` so you can listen at
the new clamp edges and back off any reaction that sounds off
(chipmunk on the high end / underwater on the low end).

Usage::

    python -m tools.tts_speed_ab                       # default phrase
    python -m tools.tts_speed_ab --text "your phrase"  # custom phrase
    python -m tools.tts_speed_ab --out ./speed_ab      # custom output dir
    python -m tools.tts_speed_ab --reactions cry sad   # only some reactions

Output files are named ``ab_<reaction>_<speed>.wav``. The script uses
the same per-reaction sub-cap table as the production engine
(:data:`app.tts.pocket_tts_service._REACTION_SPEED_CAPS`) so the
generated clips reflect what the runtime would actually emit.

This script is a manual ear-test aid -- it is *not* run by CI. If
you change ``_REACTION_SPEED`` or ``_REACTION_SPEED_CAPS`` in
production, run this script, listen to the deltas at the new clamp
floors / ceilings, and back off any reaction that sounds wrong.
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

from app.core.settings import TtsSettings
from app.tts.pocket_tts_service import (
    PocketTtsService,
    _REACTION_SPEED,
    _resolve_speed_caps,
)


_DEFAULT_PHRASE = (
    "Okay, let me tell you what I was thinking about this morning."
)


def _resolve_clamped_speed(reaction: str) -> float:
    """Return the production-clamped speed for ``reaction``.

    Mirrors :meth:`PocketTtsService.speak_async` -- the per-reaction
    sub-cap is applied first, then the global outer envelope. The
    returned value is what the engine would actually feed into the
    samplerate trick in ``_speak_worker``.
    """
    base = _REACTION_SPEED.get(reaction, 1.0)
    sub_min, sub_max = _resolve_speed_caps(reaction)
    clamped = max(sub_min, min(sub_max, base))
    # The global ``[_SPEED_MIN, _SPEED_MAX]`` band is wider than every
    # sub-cap, so a sub-clamped value never needs further clamping.
    return float(round(clamped, 4))


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """Write a mono Int16 WAV file at ``sample_rate``."""
    flat = audio.reshape(-1) if audio.ndim > 1 else audio
    pcm16 = (np.clip(flat, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())


def _render_one(
    service: PocketTtsService,
    text: str,
    reaction: str,
    out_dir: Path,
) -> Path | None:
    """Render ``text`` at the production-clamped speed for ``reaction``.

    Returns the WAV path on success, ``None`` if the engine refused
    (unloaded model, unsupported numpy, etc.).
    """
    speed = _resolve_clamped_speed(reaction)
    result = service.generate_audio(text, speed)
    if result is None:
        return None
    audio, sample_rate = result
    # Mirror the samplerate-only pitch shift the engine uses at
    # playback so the WAV plays at the correct effective speed in
    # any audio player.
    playback_rate = (
        int(sample_rate * speed) if abs(speed - 1.0) > 1e-3 else sample_rate
    )
    out_path = out_dir / f"ab_{reaction}_{speed:.3f}.wav"
    _write_wav(out_path, audio, playback_rate)
    return out_path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--text",
        default=_DEFAULT_PHRASE,
        help="phrase to render (default: a calibration sentence)",
    )
    p.add_argument(
        "--out",
        default="speed_ab",
        type=Path,
        help="output directory for WAV files (default: ./speed_ab)",
    )
    p.add_argument(
        "--reactions",
        nargs="+",
        default=None,
        help=(
            "render only the named reactions (default: every entry in "
            "_REACTION_SPEED). Useful when iterating on a single clamp."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = TtsSettings()
    service = PocketTtsService(settings)
    if not service.warmup_sync():
        status, message = service.get_status()
        print(f"TTS engine unavailable: status={status} message={message}")
        return 1

    reactions = list(args.reactions or _REACTION_SPEED.keys())
    print(f"Rendering {len(reactions)} reaction(s) -> {out_dir}")
    failures: list[str] = []
    for reaction in reactions:
        if reaction not in _REACTION_SPEED:
            print(f"  ! unknown reaction: {reaction}")
            failures.append(reaction)
            continue
        path = _render_one(service, args.text, reaction, out_dir)
        if path is None:
            print(f"  ! synthesis failed: {reaction}")
            failures.append(reaction)
            continue
        speed = _resolve_clamped_speed(reaction)
        print(f"  - {reaction:<13} speed={speed:.3f}  -> {path.name}")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
