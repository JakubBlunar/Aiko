"""Transcription for the dataset builder, via faster-whisper.

Exists because the expensive part of building a training set from found
audio is not the audio, it is the labels. A fine-tune wants a transcript
per clip that matches what was said closely enough that the model learns
pronunciation rather than noise, and typing a few hundred of those by ear
is hours of work that people abandon halfway through -- leaving a
half-labelled set, which is worth nothing.

So the transcript arrives already filled in and the human corrects it.
That inverts the effort: reading a line and fixing a word is seconds, and
crucially it is *bounded*, so the job gets finished.

Whisper's own errors are the reason this is a draft and not the answer.
It normalises numbers ("twenty twenty six" becomes "2026"), it guesses at
proper nouns, and it punctuates to taste. Every one of those is wrong for
TTS training, where the transcript must match the *sounds*. Hence
:func:`transcribe` returns text plus its own confidence, the UI shows the
weak ones first, and nothing is saved without a human having looked.

Never downloads. The app already carries three Systran conversions in the
HuggingFace cache, and a tool that silently pulls three gigabytes because
a default named a model that was not there is a tool that gets run once
on a metered connection and then never again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Preference order among cached models, best-first. small is the default
#: pick rather than large-v1 because this is a draft for correction: on
#: clean single-speaker English the two disagree mostly on punctuation,
#: which is being hand-fixed anyway, and small is roughly 6x faster on
#: CPU. large-v1 is there for anyone who would rather wait.
PREFERRED = ("small", "base", "large-v1", "medium", "tiny")

_CACHE_ENV = ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME")


def _cache_roots() -> list[Path]:
    roots: list[Path] = []
    for var in _CACHE_ENV:
        raw = os.environ.get(var)
        if not raw:
            continue
        path = Path(raw)
        roots.append(path / "hub" if var == "HF_HOME" else path)
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return [r for r in roots if r.exists()]


def cached_models() -> list[str]:
    """Which faster-whisper sizes are on disk, in preference order.

    Read off the cache directory rather than by trying to load one,
    because a miss costs a download and the whole point is to not do
    that by accident.
    """
    found: set[str] = set()
    for root in _cache_roots():
        for entry in root.glob("models--Systran--faster-whisper-*"):
            found.add(entry.name.rsplit("faster-whisper-", 1)[-1])
    ordered = [m for m in PREFERRED if m in found]
    return ordered + sorted(found - set(ordered))


def default_model() -> str:
    models = cached_models()
    return models[0] if models else ""


@dataclass
class Line:
    """One transcribed clip, with enough to triage it."""

    text: str
    language: str = ""
    language_probability: float = 0.0
    duration_s: float = 0.0
    #: Mean per-token logprob, roughly -0.1 (confident) to -1.0 (guessing).
    logprob: float = 0.0
    #: Whisper's own no-speech estimate for the clip.
    no_speech: float = 0.0
    #: Fraction of the clip covered by speech segments. A long clip with
    #: a short transcript is the signature of a truncated or partly
    #: silent label, which is worse than no label.
    coverage: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """Should this be pushed to the top of the human's queue?

        Thresholds are deliberately loose. A false "review this" costs a
        glance; a false "this is fine" puts a wrong label into the
        training set, where it teaches a mispronunciation.
        """
        return bool(self.warnings) or self.logprob < -0.55

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "duration_s": round(self.duration_s, 2),
            "logprob": round(self.logprob, 3),
            "no_speech": round(self.no_speech, 3),
            "coverage": round(self.coverage, 3),
            "warnings": list(self.warnings),
            "needs_review": self.needs_review,
        }


class Transcriber:
    """Lazily-loaded faster-whisper, held open across calls."""

    def __init__(self, model: str = "", *, threads: int = 0) -> None:
        self.model_name = model or default_model()
        self.threads = int(threads)
        self._model: Any = None
        self.load_ms = 0.0

    @property
    def available(self) -> bool:
        if not self.model_name:
            return False
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            return False
        return True

    def load(self) -> None:
        if self._model is not None:
            return
        import time

        from faster_whisper import WhisperModel

        if not self.model_name:
            raise RuntimeError(
                "no faster-whisper model in the HuggingFace cache; "
                "transcription would require a download"
            )
        t0 = time.monotonic()
        self._model = WhisperModel(
            self.model_name,
            device="cpu",
            # int8 rather than float32: this is a draft for correction,
            # and the quantisation error is far below the transcript
            # errors a human is here to fix anyway.
            compute_type="int8",
            cpu_threads=self.threads or 0,
            local_files_only=True,
        )
        self.load_ms = (time.monotonic() - t0) * 1000.0

    def transcribe(self, path: Path, *, language: str = "en") -> Line:
        self.load()
        segments, info = self._model.transcribe(
            str(path),
            language=language or None,
            # Beam search over greedy: this runs once per clip offline,
            # so the extra time buys labels that need less correcting.
            beam_size=5,
            vad_filter=False,
            # Each clip is independent. Carrying context between them
            # would let one bad label poison the next, and there is no
            # narrative continuity in a phrase list to exploit.
            condition_on_previous_text=False,
        )
        parts: list[str] = []
        logprobs: list[float] = []
        no_speech: list[float] = []
        spoken = 0.0
        for seg in segments:
            parts.append(seg.text.strip())
            logprobs.append(float(getattr(seg, "avg_logprob", 0.0) or 0.0))
            no_speech.append(float(getattr(seg, "no_speech_prob", 0.0) or 0.0))
            spoken += float(seg.end) - float(seg.start)

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        line = Line(
            text=" ".join(p for p in parts if p).strip(),
            language=str(getattr(info, "language", "") or ""),
            language_probability=float(
                getattr(info, "language_probability", 0.0) or 0.0
            ),
            duration_s=duration,
            logprob=(sum(logprobs) / len(logprobs)) if logprobs else 0.0,
            no_speech=max(no_speech) if no_speech else 0.0,
            coverage=(spoken / duration) if duration > 0 else 0.0,
        )
        line.warnings = self._warn(line)
        return line

    @staticmethod
    def _warn(line: Line) -> list[str]:
        out: list[str] = []
        if not line.text:
            out.append("nothing transcribed")
            return out
        if line.coverage < 0.6 and line.duration_s > 1.5:
            out.append(
                f"speech covers only {line.coverage * 100:.0f}% of the clip"
            )
        if line.no_speech > 0.5:
            out.append("whisper suspects this is not speech")
        if line.logprob < -0.7:
            out.append("low confidence, read it carefully")
        if line.language and line.language != "en":
            out.append(f"detected {line.language}, not English")
        if any(ch.isdigit() for ch in line.text):
            # Not a transcription error, a format one, and the single
            # most common way a whisper-drafted TTS set goes wrong: the
            # model must be told the sounds, and "2026" is not sounds.
            out.append("digits present -- spell them as spoken")
        return out


_shared: Transcriber | None = None


def shared() -> Transcriber:
    global _shared
    if _shared is None:
        _shared = Transcriber()
    return _shared


if __name__ == "__main__":
    import sys

    models = cached_models()
    print(f"cached models: {', '.join(models) or 'none'}")
    if len(sys.argv) > 1:
        t = shared()
        print(f"using {t.model_name}")
        for arg in sys.argv[1:]:
            line = t.transcribe(Path(arg))
            print(f"\n{Path(arg).name}  ({line.duration_s:.1f}s)")
            print(f"  {line.text}")
            print(f"  logprob {line.logprob:.2f} coverage {line.coverage:.2f}")
            for w in line.warnings:
                print(f"  ! {w}")
