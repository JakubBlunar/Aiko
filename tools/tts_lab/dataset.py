"""Build a training dataset from her current voice.

Why this is the right move despite the caveats
----------------------------------------------
The audio her voice was originally cloned from is gone. The only surviving
copies are two pocket-tts speaker states, so generating from them is not a
shortcut past recording -- it is the only path that keeps *her* voice at
all. The alternative is a different voice.

What that costs is documented in ``docs/tts-engine-options.md`` and worth
restating once: the set inherits a 24 kHz ceiling and pocket-tts's own
habits, so a model fine-tuned on it cannot exceed pocket-tts in fidelity.
It can still be better in the ways that matter here -- native rate
control, emotion parameters, inline ``[laugh]`` -- because those are
architecture, not audio quality.

One genuine advantage over any recorded dataset: **the transcripts are
exact.** Recording means ASR transcription plus forced alignment, and both
introduce errors that are then trained on as if true. Here the text is the
input, so the label is correct by construction.

Quality filtering is the whole job
----------------------------------
For a *reference clip* one bad generation is obvious and gets deleted. For
a dataset of hundreds, bad samples are invisible and get trained on, and
one truncated clip teaches the model to stop early. So every sample is
checked and rejections are reported by reason rather than silently
dropped -- if 40% is being thrown away, the temperature is wrong and that
should be a visible fact, not a quiet one.

Levels are normalised **once across the whole set**, not per clip.
Per-clip peak normalisation flattens the difference between a whisper and
an exclamation, which is exactly the dynamic range worth keeping.

Usage::

    python -m tools.tts_lab.dataset                     # ~8 min, one temp
    python -m tools.tts_lab.dataset --temps 0.6 0.75    # more variety
    python -m tools.tts_lab.dataset --minutes 20
    python -m tools.tts_lab.dataset --text-file mine.txt
    python -m tools.tts_lab.dataset --dry-run           # inspect the corpus
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from tools.tts_lab.adapters import REPO_ROOT, PocketTts, assess, write_wav
from tools.tts_lab.corpus import build_corpus, coverage, load_corpus
from tools.tts_lab.voicebank import _trim_silence

OUT_ROOT = REPO_ROOT / "voices" / "datasets"

#: English speech runs roughly 14-17 characters per second at a
#: conversational rate. Used only to catch generations that came out
#: wildly wrong -- a clip at a third of the expected length was almost
#: certainly cut off, and one at triple has usually looped or started
#: babbling. Both failures are silent in the audio stats.
CHARS_PER_SECOND = 15.5
SHORT_RATIO = 0.45
LONG_RATIO = 2.2


@dataclass
class Sample:
    index: int
    text: str
    file: str
    duration_s: float
    peak: float
    rms: float
    #: Generation temperature, for synthesised sets. Meaningless for
    #: labelled real audio, which carries ``source`` instead.
    temp: float = 0.0
    #: Original filename, for labelled sets. Kept because provenance is
    #: the first question asked of a training set that misbehaves, and
    #: the answer is unrecoverable once clips are renamed to an index.
    source: str = ""


@dataclass
class Rejection:
    text: str
    temp: float
    reason: str


@dataclass
class Build:
    samples: list[Sample] = field(default_factory=list)
    rejects: list[Rejection] = field(default_factory=list)
    sample_rate: int = 0

    @property
    def total_seconds(self) -> float:
        return sum(s.duration_s for s in self.samples)


def _expected_seconds(text: str) -> float:
    return max(0.4, len(text) / CHARS_PER_SECOND)


def _screen(audio: np.ndarray, sample_rate: int, text: str) -> str:
    """Return a rejection reason, or "" if the sample is usable."""
    quality = assess(audio, sample_rate)
    if quality.duration_s <= 0.2:
        return "empty"
    if "clipped" in quality.warnings:
        return "clipped"
    if quality.rms < 0.008:
        return f"too quiet (rms {quality.rms:.4f})"
    if quality.silence_share > 0.45:
        return f"mostly silence ({quality.silence_share:.0%})"

    expected = _expected_seconds(text)
    ratio = quality.duration_s / expected
    if ratio < SHORT_RATIO:
        # The important one. A clip cut off mid-word looks perfectly
        # healthy by peak and RMS, and teaches the model to stop early.
        return f"truncated? {quality.duration_s:.1f}s vs ~{expected:.1f}s expected"
    if ratio > LONG_RATIO:
        return f"overran? {quality.duration_s:.1f}s vs ~{expected:.1f}s expected"
    return ""


def build_source(
    engine_name: str,
    temp: float,
    *,
    voice_ref: Path | None,
    voice_id: str = "",
) -> tuple[Any, Any, str]:
    """An engine at a given temperature, plus a voice. Returns a label too.

    Temperature is not one concept across engines. pocket-tts takes it at
    load time on the model, while the Chatterbox family takes
    ``temperature`` as a generate keyword -- so the same requested value
    has to be delivered two different ways, and an engine that has no
    such knob should simply not be told about it rather than be handed a
    kwarg it will discard politely.

    Worth doing at all because the fastest engine is not necessarily the
    one that should *make* the dataset. Generation is offline, so an
    engine at RTF 1.5 costs nothing here while being disqualifying for
    live conversation, and the teacher's quality is the ceiling of
    whatever gets trained on the result.
    """
    if engine_name == "pocket-tts":
        engine = PocketTts(temp=temp)
        engine.load()
        resolved = voice_id
        if not resolved:
            from app.core.infra.settings import load_settings

            resolved = load_settings().tts.pocket_tts_voice
        return engine, engine.voice_from_id(resolved), resolved

    from tools.tts_lab import adapters, remote

    remote.register()
    engine = adapters.build(engine_name)
    engine.load()
    if isinstance(engine, remote.Remote) and "temperature" in engine.accepts:
        engine.overrides = {"temperature": float(temp)}
    if voice_ref is None or not voice_ref.exists():
        raise SystemExit(
            f"{engine_name} clones from a clip; pass --voice-ref (or build "
            "one with 'python -m tools.tts_lab.voicebank')"
        )
    return engine, engine.voice_from_reference(voice_ref), voice_ref.name


def generate(
    prompts: list[str],
    temps: list[float],
    *,
    target_minutes: float,
    out_dir: Path,
    engine_name: str = "pocket-tts",
    voice_ref: Path | None = None,
    voice_id: str = "",
) -> Build:
    wav_dir = out_dir / "wavs"
    wav_dir.mkdir(parents=True, exist_ok=True)
    build = Build()
    target_seconds = target_minutes * 60.0
    index = 0

    for temp in temps:
        engine, voice, resolved = build_source(
            engine_name, temp, voice_ref=voice_ref, voice_id=voice_id
        )
        print(f"{engine_name} temp {temp:.2f}: voice {resolved}")

        for text in prompts:
            if build.total_seconds >= target_seconds:
                break
            index += 1
            try:
                result = engine.synth(text, voice)
            except Exception as exc:
                build.rejects.append(
                    Rejection(text, temp, f"synthesis failed: {exc!r}")
                )
                continue
            build.sample_rate = result.sample_rate
            clip = _trim_silence(result.audio, result.sample_rate)
            reason = _screen(clip, result.sample_rate, text)
            if reason:
                build.rejects.append(Rejection(text, temp, reason))
                print(f"  {index:4} reject: {reason}")
                continue

            # Written unnormalised; a single global gain is applied once
            # the whole set is known, so relative dynamics survive.
            name = f"aiko_{index:05d}.wav"
            write_wav(wav_dir / name, clip, result.sample_rate)
            quality = assess(clip, result.sample_rate)
            build.samples.append(
                Sample(
                    index=index,
                    text=text,
                    temp=temp,
                    file=name,
                    duration_s=round(quality.duration_s, 3),
                    peak=round(quality.peak, 4),
                    rms=round(quality.rms, 5),
                )
            )
            if len(build.samples) % 25 == 0:
                print(
                    f"  {len(build.samples)} kept, "
                    f"{build.total_seconds / 60.0:.1f} min"
                )
        close = getattr(engine, "close", None)
        if callable(close):
            # Sidecar engines hold a subprocess and a temp dir per pass.
            close()
        if build.total_seconds >= target_seconds:
            break
    return build


def normalise_set(build: Build, out_dir: Path, *, target_peak: float = 0.95) -> float:
    """Apply one gain to every clip, from the loudest sample in the set.

    Per-clip normalisation is the usual reflex and it is wrong here: it
    would erase the level difference between a whisper and an
    exclamation, which is part of what makes the voice worth training on.
    A single gain fixes headroom without touching relative dynamics.
    """
    from tools.tts_lab.adapters import read_wav

    wav_dir = out_dir / "wavs"
    peak = max((s.peak for s in build.samples), default=0.0)
    if peak <= 1e-6:
        return 1.0
    gain = target_peak / peak
    if abs(gain - 1.0) < 0.02:
        return 1.0
    for sample in build.samples:
        path = wav_dir / sample.file
        audio, rate = read_wav(path)
        write_wav(path, audio * gain, rate)
        sample.peak = round(float(min(1.0, sample.peak * gain)), 4)
        sample.rms = round(float(sample.rms * gain), 5)
    return float(gain)


def write_manifests(build: Build, out_dir: Path, *, speaker: str = "aiko") -> None:
    """Both formats worth having, since the trainer is not chosen yet.

    ``metadata.csv`` is LJSpeech layout, which most TTS training code
    reads. ``<speaker>.list`` is GPT-SoVITS's, the one engine in the
    options doc that would actually fine-tune. Cheap to emit both now
    versus regenerating the audio later for a format change.
    """
    lines_lj = []
    lines_gpt = []
    for sample in build.samples:
        stem = Path(sample.file).stem
        clean = sample.text.replace("|", " ").strip()
        lines_lj.append(f"{stem}|{clean}|{clean}")
        rel = (out_dir / "wavs" / sample.file).as_posix()
        lines_gpt.append(f"{rel}|{speaker}|EN|{clean}")

    (out_dir / "metadata.csv").write_text(
        "\n".join(lines_lj) + "\n", encoding="utf-8"
    )
    (out_dir / f"{speaker}.list").write_text(
        "\n".join(lines_gpt) + "\n", encoding="utf-8"
    )


def write_report(
    build: Build,
    out_dir: Path,
    *,
    prompts: list[str],
    temps: list[float],
    gain: float,
    voice_id: str,
    engine_name: str = "pocket-tts",
) -> dict:
    durations = [s.duration_s for s in build.samples]
    reasons: dict[str, int] = {}
    for reject in build.rejects:
        key = reject.reason.split("?")[0].split("(")[0].strip()
        reasons[key] = reasons.get(key, 0) + 1

    report = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "engine": engine_name,
            "voice": voice_id,
            "temps": temps,
            "generated_not_recorded": True,
            "caveat": (
                f"Generated by {engine_name} because the original source "
                "audio was lost, so this inherits that engine's ceiling "
                "and habits and a model trained on it cannot exceed them "
                "in fidelity. Transcripts are exact, since the text was "
                "the input."
            ),
        },
        "audio": {
            "sample_rate": build.sample_rate,
            "clips": len(build.samples),
            "total_minutes": round(build.total_seconds / 60.0, 2),
            "mean_seconds": round(float(np.mean(durations)), 2) if durations else 0,
            "min_seconds": round(min(durations), 2) if durations else 0,
            "max_seconds": round(max(durations), 2) if durations else 0,
            "global_gain_applied": round(gain, 4),
        },
        "rejected": {
            "total": len(build.rejects),
            "rate": (
                round(
                    len(build.rejects)
                    / max(1, len(build.rejects) + len(build.samples)),
                    3,
                )
            ),
            "by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        },
        "corpus": coverage(prompts),
        "samples": [
            {
                "file": s.file,
                "text": s.text,
                "temp": s.temp,
                "duration_s": s.duration_s,
            }
            for s in build.samples
        ],
        "rejects": [
            {"text": r.text, "temp": r.temp, "reason": r.reason}
            for r in build.rejects
        ],
    }
    (out_dir / "dataset.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--minutes", type=float, default=10.0,
        help="stop once this much audio is kept (default 10)",
    )
    p.add_argument(
        "--temps", type=float, nargs="+", default=[0.6],
        help=(
            "pocket_tts_temp values to render at. More than one adds "
            "variety at the cost of consistency; live value is 0.6"
        ),
    )
    p.add_argument(
        "--engine", default="pocket-tts",
        help=(
            "engine to generate with (default pocket-tts). Generation is "
            "offline, so a slow engine costs only wall time here -- use "
            "whichever sounds best, since the teacher is the ceiling"
        ),
    )
    p.add_argument("--voice", default="", help="embedding (default: live setting)")
    p.add_argument(
        "--voice-ref", type=Path,
        default=REPO_ROOT / "voices" / "reference" / "aiko_reference.wav",
        help="reference clip, required by engines that clone per call",
    )
    p.add_argument("--text-file", type=Path, default=None,
                   help="one prompt per line, replacing the built-in corpus")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--speaker", default="aiko")
    p.add_argument(
        "--dry-run", action="store_true",
        help="print corpus coverage and the time estimate, generate nothing",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    prompts = (
        load_corpus(args.text_file) if args.text_file else build_corpus()
    )
    if not prompts:
        print("empty corpus")
        return 1

    stats = coverage(prompts)
    est = sum(_expected_seconds(p) for p in prompts) / 60.0
    print(f"corpus: {stats['prompts']} prompts, ~{est:.1f} min per pass")
    total = max(1, stats["prompts"])
    print(
        f"  {stats['questions']} question-final "
        f"({stats['questions'] / total:.0%}), "
        f"{stats['exclamations']} exclamation-final "
        f"({stats['exclamations'] / total:.0%}), "
        f"{stats['has_digits']} with digits"
    )
    # Below ~8% of either and the trained voice has little to learn the
    # contour from. Terminal punctuation only -- a mid-sentence "!" still
    # ends on a falling tone.
    for label, count in (
        ("question-final", stats["questions"]),
        ("exclamation-final", stats["exclamations"]),
    ):
        if count / total < 0.08:
            print(
                f"  WARNING only {count} {label} prompts -- thin for "
                "learning that intonation contour"
            )
    if stats["missing_letters"]:
        print(f"  WARNING missing letters: {stats['missing_letters']}")
    if stats["rare_letters"]:
        print(f"  rare letters (<5 uses): {stats['rare_letters']}")
    thin = [d for d, n in stats["digraphs"].items() if n < 3]
    if thin:
        print(f"  thin digraphs: {thin}")

    if args.dry_run:
        passes = max(1, int(round(args.minutes / max(est, 0.1))))
        print(
            f"\n--minutes {args.minutes:.0f} needs ~{passes} pass(es); "
            f"you gave {len(args.temps)} temp(s)"
        )
        if passes > len(args.temps):
            print(
                "  Not enough passes for that target. Add temps, or a "
                "bigger --text-file: rendering the same prompt twice at "
                "the same temp mostly duplicates it."
            )
        return 0

    voice_id = args.voice
    if not voice_id and args.engine == "pocket-tts":
        from app.core.infra.settings import load_settings

        voice_id = load_settings().tts.pocket_tts_voice

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = args.out or (OUT_ROOT / f"{args.speaker}-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nwriting to {out_dir.relative_to(REPO_ROOT)}")

    build = generate(
        prompts,
        list(args.temps),
        target_minutes=args.minutes,
        out_dir=out_dir,
        engine_name=args.engine,
        voice_ref=args.voice_ref,
        voice_id=voice_id,
    )
    if not build.samples:
        print("nothing usable was generated")
        return 2

    gain = normalise_set(build, out_dir)
    write_manifests(build, out_dir, speaker=args.speaker)
    report = write_report(
        build, out_dir, prompts=prompts, temps=list(args.temps),
        gain=gain, voice_id=voice_id or args.voice_ref.name,
        engine_name=args.engine,
    )

    audio = report["audio"]
    rejected = report["rejected"]
    print("")
    print(
        f"{audio['clips']} clips, {audio['total_minutes']:.1f} min at "
        f"{audio['sample_rate']} Hz "
        f"(mean {audio['mean_seconds']:.1f}s, "
        f"{audio['min_seconds']:.1f}-{audio['max_seconds']:.1f}s)"
    )
    if gain != 1.0:
        print(f"global gain {gain:.2f}x applied across the set")
    print(
        f"rejected {rejected['total']} "
        f"({rejected['rate'] * 100:.0f}%)"
        + (f" -- {rejected['by_reason']}" if rejected["by_reason"] else "")
    )
    if rejected["rate"] > 0.2:
        print(
            "  High rejection rate. Try a lower --temps value: most of "
            "these are the sampler wandering, not the prompts."
        )
    print(f"manifests: metadata.csv (LJSpeech) and {args.speaker}.list (GPT-SoVITS)")
    print("")
    print(
        "Listen to a random handful before training on it. The screens "
        "here catch truncation and level faults, not mispronunciation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
