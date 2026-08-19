"""Get Aiko's voice out of pocket-tts and into a portable reference clip.

Why this runs before any engine is installed
--------------------------------------------
Aiko's voice exists in exactly two places on this machine:
``voices/aiko1.safetensors`` and ``voices/aiko1_refined.safetensors``.
Both are pocket-tts speaker states -- the model's internal conditioning,
not audio. There is **no source recording anywhere in the repo**, so:

1. Every candidate engine clones from a *clip*, so without one there is
   nothing to audition against. The voice cannot move.
2. More pressing: if pocket-tts ever stops loading, the voice is gone.
   The entire reason for looking at other engines is that Torch is a
   crash surface, which makes "her voice is recoverable only by running
   the thing we are trying to replace" a poor place to be.

So the first artifact is a plain WAV. It is a backup and a universal
cloning source at once, and it makes the comparison fair: every engine
including pocket-tts itself gets cloned from the *same* clip, which
separates "how good is this engine" from "how much did the bootstrap
cost".

The round trip is measured, not assumed
---------------------------------------
Re-cloning pocket-tts from its own generated output loses something --
one generation of codec and sampling noise. ``--roundtrip`` renders a
holdout phrase three ways (original embedding, re-clone from the
reference, re-clone from a single part) so the cost is audible before
anyone builds on it. If that delta is large, the honest conclusion is
that the voice needs re-recording rather than bootstrapping, and it is
better to learn that now than after installing four engines.

Privacy note: the phrase set below is deliberately generic. A reference
clip may end up pasted into someone else's inference code, so nothing
here should be about him, her, or anything that happened.

Usage::

    python -m tools.tts_lab.voicebank                  # build the reference
    python -m tools.tts_lab.voicebank --roundtrip      # + hear the cost
    python -m tools.tts_lab.voicebank --voice aiko1.safetensors
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from tools.tts_lab.adapters import (
    REPO_ROOT,
    Adapter,
    PocketTts,
    assess,
    read_wav,
    write_wav,
)

OUT_DIR = REPO_ROOT / "voices" / "reference"

#: Phonetically broad and emotionally varied, because a reference clip
#: teaches the clone everything it knows about her: a set of flat
#: declaratives yields a clone that can only sound flat. Content is
#: deliberately dull -- see the privacy note in the module docstring.
#: Mixed lengths on purpose: a clone conditioned only on short bursts
#: tends to clip its own sentence endings.
PHRASES: tuple[str, ...] = (
    "So, where should I start with all of this?",
    "The quick brown fox jumps over the lazy dog, apparently.",
    "I think it went about as well as anyone could have hoped.",
    "Oh! I did not expect that at all.",
    "Um, hang on, let me actually think about it for a second.",
    "It is half past three in the afternoon, and nothing is finished.",
    "Would you mind saying that one more time, a little slower?",
    "Honestly? That is the funniest thing I have heard all week.",
    "Six or seven, maybe eight, but certainly no more than that.",
    "Right, okay, fine, I will admit you were probably correct.",
    "It is quiet now, and the light through the window has gone orange.",
    "Why would anyone choose to do it that way round?",
)

#: Held back from the reference so the round-trip comparison is not
#: measured on material the clone was conditioned on.
HOLDOUT = (
    "Hey, I was just thinking about something and I wanted to tell you."
)

#: Cloning wants a handful of seconds of clean speech. Long enough to
#: carry her range, short enough that engines with a hard reference
#: budget can take it whole.
TARGET_SECONDS = 24.0
#: Sentence gap in the concatenated reference. Real speech has pauses and
#: a clip butt-joined at zero teaches the clone to run sentences
#: together, but too much dead air trips the silence check.
GAP_MS = 220


def _trim_silence(
    audio: np.ndarray, sample_rate: int, *, floor: float = 0.004
) -> np.ndarray:
    """Drop leading and trailing dead air.

    Generated clips routinely carry a little of both, and both are worth
    removing before conditioning: an engine given a clip that opens on
    200 ms of nothing learns to open on nothing.
    """
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return flat
    loud = np.abs(flat) >= floor
    if not loud.any():
        return flat
    pad = int(sample_rate * 0.02)
    start = max(0, int(np.argmax(loud)) - pad)
    end = min(flat.size, flat.size - int(np.argmax(loud[::-1])) + pad)
    return flat[start:end]


def _normalise(audio: np.ndarray, *, peak: float = 0.89) -> np.ndarray:
    """Scale to a fixed peak, leaving headroom.

    Peak rather than loudness: this is a conditioning clip, not something
    anyone listens to for pleasure, and a true-peak-limited target is
    the one thing that reliably keeps an engine from being handed a
    clipped reference.
    """
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    current = float(np.abs(flat).max()) if flat.size else 0.0
    if current <= 1e-6:
        return flat
    return (flat * (peak / current)).astype(np.float32)


def build_reference(
    engine: Adapter,
    voice_id: str,
    *,
    out_dir: Path = OUT_DIR,
    target_s: float = TARGET_SECONDS,
) -> dict:
    """Render the phrase set, keep what passes, concatenate a reference."""
    out_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(exist_ok=True)

    voice = engine.voice_from_id(voice_id)
    print(f"rendering {len(PHRASES)} phrases with {voice_id}")

    kept: list[np.ndarray] = []
    manifest_parts: list[dict] = []
    sample_rate = 0
    total = 0.0

    for index, phrase in enumerate(PHRASES, start=1):
        try:
            result = engine.synth(phrase, voice)
        except Exception as exc:
            print(f"  {index:2}. FAILED {exc!r}")
            continue
        sample_rate = result.sample_rate
        clip = _trim_silence(result.audio, sample_rate)
        quality = assess(clip, sample_rate)
        part_path = write_wav(
            parts_dir / f"part{index:02d}.wav", _normalise(clip), sample_rate
        )
        flag = "ok " if quality.ok else "!! "
        print(
            f"  {index:2}. {flag}{quality.duration_s:5.2f}s "
            f"peak {quality.peak:.2f} rms {quality.rms:.3f} "
            f"sil {quality.silence_share:.0%}  {part_path.name}"
            + ("  <- " + ", ".join(quality.warnings) if quality.warnings else "")
        )
        manifest_parts.append(
            {
                "phrase": phrase,
                "file": part_path.name,
                "duration_s": round(quality.duration_s, 3),
                "warnings": list(quality.warnings),
            }
        )
        # A clip that fails its own check is kept on disk but excluded
        # from the reference: it is useful for diagnosis and actively
        # harmful as conditioning.
        if quality.ok and total < target_s:
            kept.append(clip)
            total += quality.duration_s

    if not kept:
        raise SystemExit("no usable clips; nothing to build a reference from")

    gap = np.zeros(int(sample_rate * GAP_MS / 1000.0), dtype=np.float32)
    joined = np.concatenate(
        [seg for clip in kept for seg in (clip, gap)][:-1]
    )
    reference = _normalise(joined)
    ref_quality = assess(reference, sample_rate, want_s=target_s)
    ref_path = write_wav(out_dir / "aiko_reference.wav", reference, sample_rate)

    print("")
    print(
        f"reference: {ref_quality.duration_s:.1f}s from {len(kept)} clips "
        f"at {sample_rate} Hz -> {ref_path.relative_to(REPO_ROOT)}"
    )
    if ref_quality.warnings:
        print("  WARNING: " + ", ".join(ref_quality.warnings))

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_engine": engine.caps.name,
        "source_voice": voice_id,
        "sample_rate": sample_rate,
        "reference": ref_path.name,
        "reference_duration_s": round(ref_quality.duration_s, 3),
        "reference_warnings": list(ref_quality.warnings),
        "clips_used": len(kept),
        "gap_ms": GAP_MS,
        "holdout": HOLDOUT,
        "parts": manifest_parts,
        "note": (
            "Generated from the pocket-tts embedding, not recorded. This "
            "is a bootstrap: see roundtrip.wav files for what it costs."
        ),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def roundtrip(
    engine: Adapter, voice_id: str, *, out_dir: Path = OUT_DIR
) -> None:
    """Render the holdout three ways so the bootstrap cost is audible."""
    ref_path = out_dir / "aiko_reference.wav"
    if not ref_path.exists():
        raise SystemExit(f"no reference at {ref_path}; build it first")

    print("")
    print("round trip on the holdout phrase:")
    print(f"  {HOLDOUT!r}")

    variants: list[tuple[str, object]] = [
        ("original", engine.voice_from_id(voice_id)),
        ("from_reference", engine.voice_from_reference(ref_path)),
    ]
    single = out_dir / "parts" / "part02.wav"
    if single.exists():
        # One 3-second clip against the full 24 s: if these are
        # indistinguishable, the reference is longer than it needs to be
        # and engines with a tight reference budget are not a problem.
        variants.append(
            ("from_one_part", engine.voice_from_reference(single))
        )

    for label, voice in variants:
        try:
            result = engine.synth(HOLDOUT, voice)
        except Exception as exc:
            print(f"  {label:16} FAILED {exc!r}")
            continue
        path = write_wav(
            out_dir / f"roundtrip_{label}.wav", result.audio, result.sample_rate
        )
        print(
            f"  {label:16} {result.duration_s:5.2f}s  "
            f"gen {result.total_ms:6.0f}ms  rtf {result.rtf:.2f}  "
            f"-> {path.name}"
        )

    print("")
    print(
        "Listen to these three before anything else. If 'from_reference' "
        "is clearly worse than 'original', the bootstrap is not good "
        "enough to clone other engines from and the voice wants "
        "re-recording."
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--voice",
        default="",
        help=(
            "voice embedding to render from (default: whatever "
            "config/user.json has live)"
        ),
    )
    p.add_argument("--out", type=Path, default=OUT_DIR)
    p.add_argument(
        "--target-seconds", type=float, default=TARGET_SECONDS,
        help=f"reference length to aim for (default {TARGET_SECONDS:.0f})",
    )
    p.add_argument(
        "--roundtrip", action="store_true",
        help="also render the holdout three ways to hear the bootstrap cost",
    )
    p.add_argument(
        "--temp", type=float, default=None,
        help=(
            "override pocket_tts_temp. Lower is more typical and usually "
            "the better reference; the live value is 0.6"
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    engine = PocketTts(temp=args.temp)
    try:
        engine.load()
    except Exception as exc:
        print(f"pocket-tts would not load: {exc}")
        return 1
    print(f"pocket-tts up in {engine.load_ms:.0f}ms")

    voice_id = args.voice
    if not voice_id:
        from app.core.infra.settings import load_settings

        voice_id = load_settings().tts.pocket_tts_voice
    print(f"voice: {voice_id}")

    build_reference(
        engine, voice_id, out_dir=args.out, target_s=args.target_seconds
    )
    if args.roundtrip:
        roundtrip(engine, voice_id, out_dir=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
