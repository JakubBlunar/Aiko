"""Render a phrase at each reaction speed, both ways, so you can hear it.

Layer 5 ear-test helper. For every reaction in
``app.tts.pocket_tts_service._REACTION_SPEED`` it writes **two** clips at
that reaction's production-clamped speed:

``..._stretch.wav``
    The pitch-preserving time-stretch that now ships -- duration in the
    sample count, true rate declared.

``..._varispeed.wav``
    The old trick of declaring a scaled sample rate, kept because "does
    the stretch actually sound better on *her* voice" is a listening
    question. The tests prove pitch is held; only an ear says whether the
    overlap-add left an artefact worth caring about.

Each row prints a spectral centroid ratio, so the listen comes with a
number. Varispeed resamples and so scales every frequency by the speed
factor; a time-stretch leaves the spectrum alone. The stretch column
should therefore read 1.00x while the varispeed column tracks the speed.

Usage::

    python -m tools.tts_speed_ab                       # default phrase
    python -m tools.tts_speed_ab --text "your phrase"
    python -m tools.tts_speed_ab --out ./speed_ab
    python -m tools.tts_speed_ab --reactions cry sad   # only some
    python -m tools.tts_speed_ab --speeds 0.88 1.0 1.12  # bypass reactions

Uses the same sub-cap table as the runtime
(:data:`app.tts.pocket_tts_service._REACTION_SPEED_CAPS`) so the clips
reflect what would actually be emitted. Manual aid, not run by CI: if you
change those tables, run this, listen at the new clamp edges, and back
off anything that sounds wrong.
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

from app.audio.timestretch import time_stretch
from app.core.infra.settings import load_settings
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


def _centroid(audio: np.ndarray, sample_rate: int) -> float:
    """Spectral centroid in Hz -- where the energy sits, on average.

    Not pitch, and deliberately so. Estimating F0 on real speech needs
    proper tracking (YIN or better): a plain autocorrelation makes octave
    errors, and attempts at one here reported her voice at 318 Hz one row
    and 222 Hz the next from the *same* waveform. Numbers that
    unreliable are worse than no numbers.

    The centroid answers the question actually being asked. Varispeed
    resamples, which multiplies **every** frequency by the speed factor,
    so it scales the centroid by exactly that factor. A time-stretch
    changes only the time axis and leaves the spectrum where it was. So a
    centroid ratio of 1.00 versus one that tracks the speed is a direct,
    robust discriminator between the two -- no voicing decisions, no
    octaves to get wrong.
    """
    signal = np.asarray(audio, dtype=np.float64).reshape(-1)
    if signal.size < 256:
        return 0.0
    magnitude = np.abs(np.fft.rfft(signal * np.hanning(signal.size)))
    freqs = np.fft.rfftfreq(signal.size, 1.0 / sample_rate)
    total = magnitude.sum()
    return float((freqs * magnitude).sum() / total) if total > 0 else 0.0


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """Write a mono Int16 WAV file at ``sample_rate``."""
    flat = audio.reshape(-1) if audio.ndim > 1 else audio
    pcm16 = (np.clip(flat, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())


def _render_pair(
    audio: np.ndarray,
    sample_rate: int,
    label: str,
    speed: float,
    out_dir: Path,
) -> tuple[float, float]:
    """Write both renderings at ``speed``; return their spectral centroids.

    Takes already-synthesised audio rather than synthesising, so that
    every row of the report comes from the *same* waveform. Pocket-TTS is
    stochastic at temperature 0.7 and re-synthesising per row changed the
    voice between rows, which made the pitch column incomparable and hid
    the effect being measured behind the model's own variation. It is
    also faithful to production, where ``speed`` never reaches synthesis
    at all -- it only ever tags the cache key.
    """
    stretched = time_stretch(audio, speed, sample_rate)
    _write_wav(out_dir / f"ab_{label}_{speed:.3f}_stretch.wav", stretched, sample_rate)

    # Varispeed's whole mechanism is the declared rate, so the samples go
    # out untouched and the scaled rate goes in the header. Resampling
    # them here instead would produce the same audible result but would
    # not be the thing production used to do.
    fast_rate = (
        int(sample_rate * speed) if abs(speed - 1.0) > 1e-3 else sample_rate
    )
    _write_wav(out_dir / f"ab_{label}_{speed:.3f}_varispeed.wav", audio, fast_rate)

    return (
        _centroid(stretched, sample_rate),
        # Varispeed is a pure relabelling -- the samples are untouched
        # and only the declared rate changes -- so its spectral shift is
        # measured the same way: same samples, scaled rate. That makes it
        # exact rather than approximate.
        #
        # Resampling the audio and measuring at the native rate would be
        # the equivalent operation in principle and was wrong in
        # practice: linear-interpolation decimation aliases the top of
        # the band downward, which at speed 1.06 reported the spectrum
        # moving *down* by 0.94x when varispeed plainly moves it up. The
        # artefact was larger than the effect.
        _centroid(audio, int(sample_rate * speed)),
    )


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
    p.add_argument(
        "--speeds",
        nargs="+",
        type=float,
        default=None,
        help=(
            "render these speed factors directly instead of going through "
            "the reaction table -- the quickest way to judge the stretch "
            "itself at the edges of the band"
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    # The configured voice, not a bare TtsSettings() -- which cannot even
    # be constructed (provider/voice/enabled have no defaults) and would
    # have picked the stock voice rather than Aiko's if it could. The
    # whole question is how this sounds on *her* voice.
    service = PocketTtsService(load_settings().tts)
    if not service.warmup_sync():
        status, message = service.get_status()
        print(f"TTS engine unavailable: status={status} message={message}")
        return 1

    if args.speeds:
        jobs = [(f"{s:.2f}".replace(".", "p"), float(s)) for s in args.speeds]
    else:
        names = list(args.reactions or _REACTION_SPEED.keys())
        unknown = [n for n in names if n not in _REACTION_SPEED]
        for name in unknown:
            print(f"  ! unknown reaction: {name}")
        jobs = [
            (n, _resolve_clamped_speed(n)) for n in names if n not in unknown
        ]

    # Synthesised once for every row, so the rate treatment is the only
    # thing that differs between clips and between rows.
    result = service.generate_audio(args.text, 1.0)
    if result is None:
        print("synthesis failed")
        return 1
    audio, sample_rate = result
    baseline = _centroid(audio, sample_rate)

    print(f"Rendering {len(jobs)} pair(s) -> {out_dir}")
    print(f"source: {audio.size / sample_rate:.2f}s at {sample_rate} Hz, "
          f"centroid {baseline:.0f} Hz")
    print(
        f"\n{'label':<10} {'speed':>6}  {'stretch':>9} {'varispeed':>11}"
        f"  {'spectrum moved':>19}"
    )
    for label, speed in jobs:
        held, drifted = _render_pair(
            audio, sample_rate, label, speed, out_dir
        )
        # The ratio is the finding, not the absolute figure: whether the
        # treatment moved the spectrum, and by how much against the
        # untouched source.
        held_ratio = held / baseline if baseline else 0.0
        drift_ratio = drifted / baseline if baseline else 0.0
        print(
            f"{label:<10} {speed:6.3f}  {held:6.0f} Hz {drifted:8.0f} Hz"
            f"   {held_ratio:5.3f}x vs {drift_ratio:5.3f}x"
        )
    print(
        "\nThe stretch column should hold at 1.000x; varispeed should track "
        "the speed factor. Then listen to a matching pair back to back -- "
        "the numbers only show the spectrum stayed put, not that the "
        "overlap-add is clean on her voice."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
