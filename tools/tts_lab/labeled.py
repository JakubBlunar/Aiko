"""Build a training set from real audio files plus hand-checked labels.

The other path, :mod:`tools.tts_lab.dataset`, synthesises its own audio
from the pocket-tts embedding and therefore knows every transcript
exactly. It exists because the original recordings were lost, and it
carries a permanent ceiling: a model trained on generated speech cannot
exceed the engine that generated it.

This module is the better path when real audio exists at all -- a handful
of mp3s, a recording session, anything actually spoken. Real audio has no
generational loss and no inherited habits, so it can produce a voice
*better* than the current one rather than a copy of it.

What it costs is labels, which is the whole design problem. A transcript
per clip has to match the sounds, and typing a few hundred by ear is the
step where this kind of project dies half-finished. So
:mod:`tools.tts_lab.transcribe` drafts them and the human corrects; see
that module for why the draft is never trusted on its own.

Three things here that are easy to get wrong and expensive to discover
after a training run:

* **One sample rate.** Trainers want a single rate, and found audio comes
  in several. Resampling happens through the anti-aliased path in
  :func:`~tools.tts_lab.adapters.resample`, and the chosen rate is the
  most common one present rather than the highest, so the majority of the
  set is untouched and only outliers are converted.
* **Length.** Most fine-tuners window at 10-15 s and either truncate or
  choke past that. A 40 s file is not one sample, and silently keeping it
  wastes the label that was typed for it.
* **Label/audio agreement.** A transcript that does not match its clip is
  worse than a missing one, because it actively teaches a
  mispronunciation. Duration against expected-from-text catches the big
  disagreements cheaply.

Usage::

    python -m tools.tts_lab.labeled --manifest labels.tsv
    python -m tools.tts_lab.labeled --dir clips/ --transcribe

``labels.tsv`` is ``path<TAB>transcript`` per line, which is also what
the studio's dataset panel writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from tools.tts_lab.adapters import (
    REPO_ROOT,
    assess,
    read_audio,
    resample,
    write_wav,
)
from tools.tts_lab.dataset import (
    OUT_ROOT,
    Build,
    Rejection,
    Sample,
    _screen,
    normalise_set,
    write_manifests,
)
from tools.tts_lab.voicebank import _trim_silence

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac")

#: Most open fine-tuners window at 10-15 s. Past this a clip is not a
#: sample, it is a file that needs cutting first, so it is rejected with
#: that as the reason rather than kept and quietly truncated by the
#: trainer.
MAX_SECONDS = 15.0
#: Under this there is not enough prosody to learn from, and the clip is
#: usually a fragment or a stray word.
MIN_SECONDS = 0.8


@dataclass
class Item:
    """One labelled file on its way into the set."""

    path: Path
    text: str


def read_manifest(path: Path) -> list[Item]:
    """``path<TAB>transcript`` per line, blanks and ``#`` skipped.

    Paths resolve relative to the manifest, not the working directory:
    a label file sitting beside its audio should keep working wherever
    it is invoked from.
    """
    items: list[Item] = []
    base = path.parent
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split("|")
        if len(parts) < 2:
            print(f"  skipping unparseable line: {line[:60]!r}")
            continue
        audio = Path(parts[0].strip())
        if not audio.is_absolute():
            audio = base / audio
        items.append(Item(audio, parts[1].strip()))
    return items


def scan_dir(path: Path) -> list[Item]:
    return [
        Item(p, "")
        for p in sorted(path.iterdir())
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ]


def choose_rate(rates: list[int], requested: int = 0) -> tuple[int, str]:
    """One rate for the whole set, plus why.

    The most common source rate, not the highest. Picking the highest
    would upsample the majority of the set to match one outlier, which
    invents no information and costs a resampling pass over almost every
    clip; picking the lowest would throw away real bandwidth. The mode
    leaves most files untouched. Ties go to the higher rate, since
    downsampling later is lossless in a way that upsampling is not.
    """
    if requested:
        return int(requested), "requested"
    if not rates:
        return 24000, "no input, defaulted"
    counts = Counter(rates)
    best = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if len(counts) == 1:
        return int(best[0]), "the only rate present"
    spread = ", ".join(f"{r} Hz x{n}" for r, n in sorted(counts.items()))
    return int(best[0]), f"most common of {spread}"


def build(
    items: list[Item],
    out_dir: Path,
    *,
    target_rate: int = 0,
    trim: bool = True,
) -> tuple[Build, dict]:
    wav_dir = out_dir / "wavs"
    wav_dir.mkdir(parents=True, exist_ok=True)
    result = Build()

    # Two passes. The first decodes and screens, so the rate decision is
    # made from the files that survive rather than from ones about to be
    # thrown out -- one unusable 48 kHz file should not set the rate for
    # the whole set.
    staged: list[tuple[Item, np.ndarray, int]] = []
    for item in items:
        if not item.text.strip():
            result.rejects.append(
                Rejection(item.path.name, 0.0, "no transcript")
            )
            continue
        if not item.path.exists():
            result.rejects.append(Rejection(item.path.name, 0.0, "missing file"))
            continue
        try:
            audio, rate = read_audio(item.path)
        except Exception as exc:
            result.rejects.append(
                Rejection(item.path.name, 0.0, f"could not decode: {exc}")
            )
            continue
        if trim:
            audio = _trim_silence(audio, rate)
        seconds = audio.size / float(rate or 1)
        if seconds < MIN_SECONDS:
            result.rejects.append(
                Rejection(item.path.name, 0.0, f"too short ({seconds:.1f}s)")
            )
            continue
        if seconds > MAX_SECONDS:
            result.rejects.append(
                Rejection(
                    item.path.name,
                    0.0,
                    f"too long ({seconds:.1f}s) -- cut it into "
                    f"clips under {MAX_SECONDS:.0f}s first",
                )
            )
            continue
        staged.append((item, audio, rate))

    rate_out, why = choose_rate([r for _, _, r in staged], target_rate)
    converted = 0
    seen: dict[str, str] = {}

    for index, (item, audio, rate) in enumerate(staged, start=1):
        if rate != rate_out:
            audio = resample(audio, rate, rate_out)
            converted += 1
        reason = _screen(audio, rate_out, item.text)
        if reason:
            result.rejects.append(Rejection(item.path.name, 0.0, reason))
            continue
        key = " ".join(item.text.lower().split())
        if key in seen:
            # Not fatal, but a duplicated line weights that phrase twice
            # and is usually a copy-paste in the label file rather than a
            # deliberate second take.
            print(f"  note: {item.path.name} repeats the text of {seen[key]}")
        seen.setdefault(key, item.path.name)

        quality = assess(audio, rate_out)
        name = f"{index:04d}.wav"
        write_wav(wav_dir / name, audio, rate_out)
        result.samples.append(
            Sample(
                index=index,
                text=item.text.strip(),
                file=name,
                duration_s=round(quality.duration_s, 3),
                peak=round(quality.peak, 4),
                rms=round(quality.rms, 5),
                source=item.path.name,
            )
        )

    result.sample_rate = rate_out
    return result, {
        "sample_rate": rate_out,
        "rate_reason": why,
        "resampled": converted,
    }


def write_report(
    result: Build, out_dir: Path, *, rate_info: dict, gain: float
) -> dict:
    durations = [s.duration_s for s in result.samples]
    reasons: dict[str, int] = {}
    for reject in result.rejects:
        key = reject.reason.split("(")[0].split("--")[0].strip()
        reasons[key] = reasons.get(key, 0) + 1

    report = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "kind": "labelled real audio",
            "generated_not_recorded": False,
            "note": (
                "Real audio with human-checked transcripts. No generational "
                "loss and no inherited engine habits, so unlike a "
                "synthesised set this has no ceiling at the current voice."
            ),
            **rate_info,
        },
        "audio": {
            "sample_rate": result.sample_rate,
            "clips": len(result.samples),
            "total_minutes": round(result.total_seconds / 60.0, 2),
            "mean_seconds": round(float(np.mean(durations)), 2) if durations else 0,
            "min_seconds": round(min(durations), 2) if durations else 0,
            "max_seconds": round(max(durations), 2) if durations else 0,
            "global_gain_applied": round(gain, 4),
        },
        "rejected": {
            "count": len(result.rejects),
            "by_reason": reasons,
            "detail": [
                {"file": r.text, "reason": r.reason} for r in result.rejects[:40]
            ],
        },
        "samples": [
            {
                "file": s.file,
                "source": s.source,
                "text": s.text,
                "duration_s": s.duration_s,
            }
            for s in result.samples
        ],
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", type=Path, help="path<TAB>transcript lines")
    src.add_argument("--dir", type=Path, help="a folder of audio files")
    p.add_argument("--transcribe", action="store_true",
                   help="draft missing transcripts with faster-whisper")
    p.add_argument("--name", default="", help="dataset folder name")
    p.add_argument("--speaker", default="aiko")
    p.add_argument("--rate", type=int, default=0,
                   help="target sample rate (default: most common present)")
    p.add_argument("--no-trim", action="store_true",
                   help="keep leading/trailing silence")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    items = (
        read_manifest(args.manifest) if args.manifest else scan_dir(args.dir)
    )
    if not items:
        print("no input files found")
        return 1
    print(f"{len(items)} file(s)")

    missing = [i for i in items if not i.text.strip()]
    if missing and args.transcribe:
        from tools.tts_lab.transcribe import shared

        transcriber = shared()
        if not transcriber.available:
            print("no cached faster-whisper model; cannot draft transcripts")
            return 1
        print(f"drafting {len(missing)} transcript(s) with "
              f"{transcriber.model_name} -- review them before training")
        for item in missing:
            try:
                line = transcriber.transcribe(item.path)
            except Exception as exc:
                print(f"  {item.path.name}: {exc}")
                continue
            item.text = line.text
            flag = " [REVIEW]" if line.needs_review else ""
            print(f"  {item.path.name}{flag}: {line.text[:70]}")
    elif missing:
        print(f"{len(missing)} file(s) have no transcript and will be "
              "skipped (pass --transcribe to draft them)")

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = OUT_ROOT / (args.name or f"{args.speaker}-labelled-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    result, rate_info = build(
        items, out_dir, target_rate=args.rate, trim=not args.no_trim
    )
    if not result.samples:
        print("\nnothing usable")
        for reject in result.rejects[:20]:
            print(f"  {reject.text}: {reject.reason}")
        return 1

    gain = normalise_set(result, out_dir)
    write_manifests(result, out_dir, speaker=args.speaker)
    report = write_report(result, out_dir, rate_info=rate_info, gain=gain)

    audio = report["audio"]
    print(f"\n{audio['clips']} clips, {audio['total_minutes']:.1f} min at "
          f"{audio['sample_rate']} Hz ({rate_info['rate_reason']})")
    if rate_info["resampled"]:
        print(f"  {rate_info['resampled']} file(s) resampled")
    if result.rejects:
        print(f"  {len(result.rejects)} rejected:")
        for reason, count in sorted(
            report["rejected"]["by_reason"].items(), key=lambda kv: -kv[1]
        ):
            print(f"    {count:3d}  {reason}")
    print(f"\n{out_dir.relative_to(REPO_ROOT)}")

    minutes = audio["total_minutes"]
    if minutes < 5:
        print(f"\n{minutes:.1f} min is thin. Zero-shot cloning needs only "
              "seconds, but a fine-tune wants 10-30 minutes to beat it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
