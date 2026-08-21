"""Build a cloning reference out of several real recordings.

Why this is not just "upload a file"
------------------------------------
Her incumbent reference was *generated*: rendered from the pocket-tts
embedding, one generation of loss away from the recordings the embedding
itself came from. When those recordings turn up, the best reference is
built from them directly -- but a cloning reference is one clip, and
real source material arrives as dozens of one-second files. So the job
is selection and ordering, not upload.

Ordering matters more than it looks, and this is the whole reason for
the module. Chatterbox truncates the reference rather than summarising
it::

    ENC_COND_LEN = 15 * S3_SR      # 15 s -> the tokenizer prompt
    DEC_COND_LEN = 10 * S3GEN_SR   # 10 s -> the decoder conditioning

(``chatterbox/tts_turbo.py``, which Nano subclasses.) Ten seconds reach
the part that reconstructs waveforms and fifteen the part that primes
articulation; everything after that is read from disk and thrown away.
So a 27-second reference is not "more thorough", it is a ten-second
reference plus twelve seconds of decoration plus five wasted -- and
because the cut lands wherever the concatenation happens to be, *part
order silently decides which clips condition the clone at all*. This
module reports where those two boundaries fall so the choice is visible.

Two deliberate differences from :mod:`tools.tts_lab.voicebank`, which
builds the same shape of artifact out of generated audio:

- **Parts are not individually normalised.** Level differences between
  real takes are the speaker, not an error, and flattening them per clip
  throws away range that the clone would otherwise inherit. Gain is
  applied once, to the joined reference.
- **Phrases are never guessed.** The manifest's ``phrase`` fields are
  what the app measures her target tempo from, so a wrong transcript
  does not degrade gracefully -- it aims her pacing at a number derived
  from words nobody said. Filenames in a found voice pack routinely
  carry an English *gloss* of Japanese audio, which is exactly the
  plausible-looking wrong answer, so transcripts are left to the
  operator and omitted parts simply do not count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
from tools.tts_lab.voicebank import GAP_MS, _normalise, _trim_silence

#: Everything the browser is allowed to name. The studio is loopback-only
#: but this is still the difference between "pick a clip" and "read any
#: file on the machine", and the check is one line.
CLIP_ROOT = REPO_ROOT / "voices"

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a")

#: Subtrees that are output rather than source material.
_SKIP = ("studio/", "datasets/", "audition/", "speed_ab/")

#: The one exception: clips uploaded through the studio. They live under
#: the scratch tree so that neither voice picker lists them as voices,
#: but they are source material for a reference and have to be pickable.
UPLOAD_REL = "studio/uploads"

#: Chatterbox's two conditioning windows, in seconds. Read off the
#: installed code rather than a README -- see the module docstring.
DEC_WINDOW_S = 10.0
ENC_WINDOW_S = 15.0

#: The app needs at least this many measurable parts before it will take
#: a tempo target from a manifest (``_adopt_rate_target``). Mirrored here
#: so the studio can say whether a reference will get one *before* it is
#: saved, rather than leaving it to be discovered in the app's log.
MIN_RATE_PARTS = 3

#: Below this, a clip is an isolated word rather than connected speech.
#:
#: Measured, and the most expensive thing in this module to have got
#: wrong. **Chatterbox clones pacing along with timbre.** A first real
#: reference built from the brightest available clips came out of ten
#: parts with a median length of 0.92 s -- single game-pack words, each
#: drawled and followed by a gap -- and the clone delivered 5.50
#: syllables per second against 7.36 from her incumbent reference, whose
#: parts are full sentences at a 3.04 s median. That is a 34% drawl, it
#: is audible immediately, and nothing downstream saves it: an
#: unlabelled manifest means the app's tempo correction is switched off
#: entirely, and even switched on it caps at 15%.
#:
#: So brightness alone is the wrong thing to select on, and 1.4 s is
#: where this pack stops being one word.
MIN_CONNECTED_S = 1.4


@dataclass
class Clip:
    """One candidate source recording, measured."""

    rel: str
    duration_s: float
    sample_rate: int
    peak: float
    rms: float
    silence_share: float
    #: Frequency below which 99.9% of the energy sits. The reason to
    #: prefer a real recording at all: her generated reference carries
    #: content to 7.4 kHz where the 24 kHz decoder path can use 12.
    bandwidth_hz: float
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "rel": self.rel,
            "name": Path(self.rel).name,
            "duration_s": round(self.duration_s, 3),
            "sample_rate": self.sample_rate,
            "peak": round(self.peak, 4),
            "rms": round(self.rms, 5),
            "silence_share": round(self.silence_share, 4),
            "bandwidth_hz": round(self.bandwidth_hz, 1),
            "warnings": list(self.warnings),
        }


def bandwidth_hz(audio: np.ndarray, sample_rate: int) -> float:
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size < 32:
        return 0.0
    spec = np.abs(np.fft.rfft(flat * np.hanning(flat.size))) ** 2
    freqs = np.fft.rfftfreq(flat.size, 1.0 / sample_rate)
    cum = np.cumsum(spec)
    if cum[-1] <= 0:
        return 0.0
    return float(freqs[int(np.searchsorted(cum, cum[-1] * 0.999))])


def resolve_clip(rel: str) -> Path:
    """A caller-supplied relative path, confined to :data:`CLIP_ROOT`."""
    path = (CLIP_ROOT / rel).resolve()
    if not path.is_relative_to(CLIP_ROOT.resolve()):
        raise ValueError(f"{rel!r} is outside voices/")
    if not path.is_file():
        raise ValueError(f"no such clip: {rel}")
    return path


def folders() -> list[dict]:
    """Directories under ``voices/`` holding source audio, with counts."""
    out: list[dict] = []
    for path in sorted(CLIP_ROOT.rglob("*")):
        if not path.is_dir():
            continue
        rel = path.relative_to(CLIP_ROOT).as_posix()
        if rel != UPLOAD_REL and any(
            rel.startswith(s.rstrip("/")) for s in _SKIP
        ):
            continue
        count = sum(
            1
            for f in path.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES
        )
        if count:
            out.append({"rel": rel, "clips": count})
    # Found packs first, then uploads, then the rest. ``reference/parts``
    # stays listed on purpose: rebuilding from the existing parts in a
    # different order is a legitimate use, given the order is what
    # decides which of them the engine hears at all.
    def key(row: dict) -> tuple:
        rel = str(row["rel"])
        rank_ = 0 if rel.startswith("sounds/") else 1 if rel == UPLOAD_REL else 2
        return (rank_, rel)

    out.sort(key=key)
    return out


def scan(rel_dir: str) -> list[Clip]:
    """Measure every clip directly inside one folder."""
    base = (CLIP_ROOT / rel_dir).resolve()
    if not base.is_relative_to(CLIP_ROOT.resolve()) or not base.is_dir():
        raise ValueError(f"no such folder: {rel_dir}")
    clips: list[Clip] = []
    for path in sorted(base.iterdir()):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        try:
            audio, rate = read_audio(path)
        except Exception as exc:
            clips.append(
                Clip(
                    rel=path.relative_to(CLIP_ROOT).as_posix(),
                    duration_s=0.0,
                    sample_rate=0,
                    peak=0.0,
                    rms=0.0,
                    silence_share=1.0,
                    bandwidth_hz=0.0,
                    warnings=(f"could not decode: {exc}",),
                )
            )
            continue
        trimmed = _trim_silence(audio, rate)
        quality = assess(trimmed, rate)
        clips.append(
            Clip(
                rel=path.relative_to(CLIP_ROOT).as_posix(),
                duration_s=quality.duration_s,
                sample_rate=rate,
                peak=quality.peak,
                rms=quality.rms,
                silence_share=quality.silence_share,
                bandwidth_hz=bandwidth_hz(trimmed, rate),
                warnings=quality.warnings,
            )
        )
    return clips


def rank(
    clips: list[Clip],
    *,
    seconds: float = DEC_WINDOW_S,
    gap_ms: int = GAP_MS,
) -> list[str]:
    """Pick clips that fill the decoder window with the best material.

    A starting selection rather than an answer. **Connected speech
    first**, then brightness within it: the engine clones pacing as well
    as timbre, so ten seconds of the brightest available one-word clips
    produces a clone that drawls (see :data:`MIN_CONNECTED_S` for the
    measurement). Sorting on bandwidth alone was the first version of
    this and it was wrong in exactly that way.

    Short clips are used only to top up a budget that connected speech
    could not fill, since some of a reference is better than a third of
    one.

    The gap counts toward the budget. Ignoring it looks like a rounding
    detail and is not: seven inter-clip gaps are a second and a half, so
    a selection totalling ten seconds of speech is nearly twelve of
    reference and the tail of it falls past the cut this is trying to
    fill exactly.
    """
    usable = [c for c in clips if not c.warnings and c.duration_s >= 0.5]
    connected = [c for c in usable if c.duration_s >= MIN_CONNECTED_S]
    short = [c for c in usable if c.duration_s < MIN_CONNECTED_S]
    for group in (connected, short):
        group.sort(key=lambda c: (-c.bandwidth_hz, -c.duration_s))
    gap_s = gap_ms / 1000.0
    picked: list[str] = []
    total = 0.0
    for clip in connected + short:
        step = clip.duration_s + (gap_s if picked else 0.0)
        if picked and total + step > seconds:
            continue
        picked.append(clip.rel)
        total += step
    return picked


@dataclass
class Part:
    """One clip's place in a built reference."""

    rel: str
    phrase: str = ""


def build(
    parts: list[Part],
    out_dir: Path,
    *,
    gap_ms: int = GAP_MS,
    name: str = "reference.wav",
    target_syl_s: float = 0.0,
) -> dict:
    """Concatenate an ordered selection into a reference plus manifest.

    Writes the layout the app reads back: the joined wav, a ``parts/``
    directory, and a ``manifest.json`` pairing each part file with its
    phrase. That shape is a contract rather than a convention --
    ``ChatterboxTtsService._adopt_rate_target`` looks for
    ``manifest.json`` *beside* the reference and the part wavs *under*
    it, and quietly disables tempo matching when either is missing.
    """
    if not parts:
        raise ValueError("nothing selected")
    out_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(exist_ok=True)
    for stale in parts_dir.glob("part*.wav"):
        stale.unlink()

    loaded: list[tuple[Part, np.ndarray, int]] = []
    for part in parts:
        audio, rate = read_audio(resolve_clip(part.rel))
        trimmed = _trim_silence(audio, rate)
        if trimmed.size:
            loaded.append((part, trimmed, rate))
    if not loaded:
        raise ValueError("every selected clip was empty after trimming")

    # Converge on the most common rate rather than the highest: it leaves
    # the majority of the selection untouched and only converts outliers.
    rates = [rate for _, _, rate in loaded]
    target_rate = max(set(rates), key=rates.count)

    manifest_parts: list[dict] = []
    segments: list[np.ndarray] = []
    cumulative = 0.0
    for index, (part, audio, rate) in enumerate(loaded, start=1):
        clip = audio if rate == target_rate else resample(audio, rate, target_rate)
        # Parts land on disk unnormalised on purpose -- see the module
        # docstring. The joined reference gets the one gain pass.
        file_name = f"part{index:02d}.wav"
        write_wav(parts_dir / file_name, clip, target_rate)
        duration = clip.size / float(target_rate)
        segments.append(clip)
        manifest_parts.append(
            {
                "phrase": part.phrase,
                "file": file_name,
                "source": part.rel,
                "duration_s": round(duration, 3),
                "starts_at_s": round(cumulative, 3),
                "sample_rate_in": rate,
            }
        )
        cumulative += duration + gap_ms / 1000.0

    gap = np.zeros(int(target_rate * gap_ms / 1000.0), dtype=np.float32)
    joined = np.concatenate(
        [seg for clip in segments for seg in (clip, gap)][:-1]
    )
    reference = _normalise(joined)
    ref_path = write_wav(out_dir / name, reference, target_rate)
    quality = assess(reference, target_rate)

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_engine": "none (real recordings)",
        "source_voice": "",
        "sample_rate": target_rate,
        "reference": ref_path.name,
        "reference_duration_s": round(quality.duration_s, 3),
        "reference_warnings": list(quality.warnings),
        "clips_used": len(manifest_parts),
        "gap_ms": gap_ms,
        "parts": manifest_parts,
        "note": (
            "Built from real recordings, not generated. Parts are stored "
            "unnormalised so relative level survives; only the joined "
            "reference is gain-staged."
        ),
    }
    if target_syl_s > 0.0:
        # Declared rather than measured, which the app honours in
        # preference to scanning the parts. The escape hatch for source
        # audio in another language: the clips are her, and no honest
        # English transcript exists to measure a rate from.
        manifest["target_syl_s"] = round(float(target_syl_s), 3)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def windows(manifest: dict) -> dict:
    """Where the engine's two conditioning cuts land in this reference.

    The number the studio exists to show. Parts wholly past
    :data:`ENC_WINDOW_S` were read off disk and discarded, and a part
    straddling :data:`DEC_WINDOW_S` is half-heard, which is worth knowing
    before concluding an audition says something about the clips.
    """
    total = float(manifest.get("reference_duration_s") or 0.0)
    decoder: list[str] = []
    encoder: list[str] = []
    wasted: list[str] = []
    cut: list[str] = []
    for part in manifest.get("parts") or []:
        start = float(part.get("starts_at_s") or 0.0)
        end = start + float(part.get("duration_s") or 0.0)
        label = str(part.get("source") or part.get("file") or "")
        if start < DEC_WINDOW_S:
            decoder.append(label)
            if end > DEC_WINDOW_S:
                # Half-heard by the decoder: worth naming, because a
                # clip cut mid-word conditions on a word that has no
                # ending, and it looks fine in the player.
                cut.append(label)
        if start < ENC_WINDOW_S:
            encoder.append(label)
        else:
            wasted.append(label)
    return {
        "total_s": round(total, 2),
        "decoder_s": round(min(total, DEC_WINDOW_S), 2),
        "encoder_s": round(min(total, ENC_WINDOW_S), 2),
        "in_decoder": decoder,
        "in_encoder": encoder,
        "straddling": cut,
        "discarded": wasted,
        "over_budget_s": round(max(0.0, total - ENC_WINDOW_S), 2),
    }


def shape(manifest: dict) -> dict:
    """Is this reference connected speech or a list of words?

    The question a player cannot answer. A reference of ten one-second
    clips and a reference of three three-second ones sound equally fine
    played back -- they are the same voice saying the same things -- and
    they clone to noticeably different speaking rates, because pacing
    transfers. So the shape gets stated as numbers next to the audio.
    """
    parts = manifest.get("parts") or []
    lengths = sorted(float(p.get("duration_s") or 0.0) for p in parts)
    if not lengths:
        return {}
    total = float(manifest.get("reference_duration_s") or sum(lengths))
    gap_s = float(manifest.get("gap_ms") or 0) / 1000.0
    gap_total = gap_s * max(0, len(lengths) - 1)
    median = lengths[len(lengths) // 2]
    out = {
        "parts": len(lengths),
        "median_part_s": round(median, 2),
        "shortest_s": round(lengths[0], 2),
        "longest_s": round(lengths[-1], 2),
        "gap_share": round(gap_total / total, 3) if total > 0 else 0.0,
        "connected": median >= MIN_CONNECTED_S,
        "warning": "",
    }
    if not out["connected"]:
        out["warning"] = (
            f"median part is {median:.2f}s -- isolated words, not "
            "connected speech. Pacing clones along with timbre, and a "
            "reference this chopped drawls: measured 5.50 syllables per "
            "second against 7.36 from her sentence-length reference. "
            "Prefer a few clips over 1.4s."
        )
    elif out["gap_share"] > 0.15:
        out["warning"] = (
            f"{out['gap_share']:.0%} of the reference is inter-clip "
            "silence, which teaches the clone to pause. Use fewer, "
            "longer clips."
        )
    return out


def app_targets(out_dir: Path, manifest: dict) -> dict:
    """What the running app will aim at once this reference is in use.

    Computed with the app's own code rather than an approximation of it,
    so the studio cannot report a brightness or tempo target that the
    engine then disagrees with. Both are optional in the app -- a missing
    target disables that correction -- so "off" is a legitimate answer
    and is reported as one.

    The tempo warning is the reason this reports rather than just
    computes. A reference built from a found voice pack is made of
    one-word interjections, and transcribing them yields a target near
    2.5 syllables per second against her established 6.55 -- so filling
    in those transcripts helpfully would tell the app to slow every
    sentence she speaks by the full correction limit, permanently, on
    evidence from clips that are single words. Short interjections are
    exactly where tempo is legitimately unusual, which the app's own
    module says; the studio's job is to notice when a well-meant
    transcript is about to be believed.
    """
    from app.audio.speech_rate import (
        DEFAULT_TARGET_SYL_S,
        MAX_CORRECTION,
        MIN_SYLLABLES,
        is_measurable,
        measured_rate,
        syllables,
    )
    from app.audio.timbre import spectral_tilt_db
    from tools.tts_lab.adapters import read_wav

    out: dict = {
        "tilt_db": None,
        "rate_syl_s": None,
        "rate_parts": 0,
        "rate_incumbent": DEFAULT_TARGET_SYL_S,
        "rate_skipped": [],
        "rate_warning": "",
    }
    ref = out_dir / str(manifest.get("reference") or "")
    if ref.is_file():
        audio, rate = read_wav(ref)
        tilt = spectral_tilt_db(audio, rate)
        out["tilt_db"] = round(float(tilt), 2) if tilt else None

    rates: list[float] = []
    for part in manifest.get("parts") or []:
        phrase = str(part.get("phrase") or "")
        clip = out_dir / "parts" / str(part.get("file") or "")
        label = str(part.get("source") or part.get("file") or "")
        if not phrase or not clip.is_file():
            continue
        if not is_measurable(phrase):
            out["rate_skipped"].append(
                {"part": label, "why": "not enough letters to count"}
            )
            continue
        if syllables(phrase) < MIN_SYLLABLES:
            out["rate_skipped"].append(
                {
                    "part": label,
                    "why": (
                        f"{syllables(phrase)} syllables, needs "
                        f"{MIN_SYLLABLES}"
                    ),
                }
            )
            continue
        audio, rate = read_wav(clip)
        measured = measured_rate(audio, rate, phrase)
        if measured <= 0.0:
            out["rate_skipped"].append(
                {"part": label, "why": "too little voiced speech"}
            )
            continue
        rates.append(measured)
    out["rate_parts"] = len(rates)
    if len(rates) >= MIN_RATE_PARTS:
        target = float(np.median(rates))
        out["rate_syl_s"] = round(target, 2)
        drift = abs(target - DEFAULT_TARGET_SYL_S) / DEFAULT_TARGET_SYL_S
        if drift > MAX_CORRECTION:
            out["rate_warning"] = (
                f"{target:.1f} syl/s is {drift:.0%} off her established "
                f"{DEFAULT_TARGET_SYL_S:.2f}, so every sentence would be "
                f"stretched to the {MAX_CORRECTION:.0%} limit. Isolated "
                "words measure slow. Clear the transcripts unless these "
                "clips are full sentences."
            )
    return out
