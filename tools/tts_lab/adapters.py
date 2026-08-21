"""One seam every candidate engine implements, plus the incumbent.

Why a seam rather than a script per engine
------------------------------------------
The question this lab exists to answer is *not* "which engine sounds
best". It is the one in ``docs/tts-engine-options.md``: **which engine
exposes a control surface that :class:`ProsodyParams` can be mapped
onto?** Aiko already derives per-sentence pacing and emotion from
affect; today the speed channel is permanently dark because the only
mechanism available to express it (scaling the playback sample rate)
also detunes her by ~1.6 semitones per 10% of rate.

So the interesting output of an audition is two things at once -- how it
sounds, and what it can be *told*. A per-engine script answers the
first and quietly skips the second, which is how you end up adopting a
prettier engine that leaves the planner talking to itself. Hence
:class:`Caps`: every adapter declares its control surface, and the bench
prints the surface beside the audio.

The three channels that need engine support
-------------------------------------------
Of everything ``ProsodyParams`` carries, most is already solved in
post-processing and will survive any engine swap:

* ``gain_db`` -- a linear offset on the Int16 PCM. Engine-independent.
* ``pause_before_ms`` / ``pause_after_ms`` -- inserted silence. Same.
* ``prefix_text`` -- text, so it is the engine's problem only in that
  the engine has to pronounce it.

What genuinely needs the engine's cooperation is exactly three things,
and they are the three :class:`Caps` scores:

1. **rate** (``speed_hint``) -- the channel that is dark today.
2. **expressiveness** (``reaction``) -- a numeric dial or a
   natural-language instruction the reaction label can be mapped to.
3. **vocalisations** (``prefix_reaction``) -- native inline ``[laugh]``
   / ``[sigh]`` support, which would turn ``audio.earcons_enabled`` back
   on as sounds *in her own voice* instead of canned samples.
"""

from __future__ import annotations

import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Handed a mono float32 block the moment the engine produces it, so a
#: streaming engine can be timed on first audio rather than on the
#: finished clip. ``SessionController`` emits ~50 ms chunks over the
#: WebSocket, and an engine that cannot produce a first chunk inside a
#: few hundred ms forces a pipeline rewrite regardless of how it sounds.
ChunkSink = Callable[[np.ndarray], None]


@dataclass(frozen=True)
class Caps:
    """What an engine can be *told*, declared rather than discovered.

    Filled in from upstream documentation when an adapter is written,
    then corrected by whoever runs the bench -- the published numbers in
    this class are the ones most likely to be wrong, because they come
    from other people's machines. ``verified`` records whether anyone
    has actually confirmed the row here.
    """

    name: str
    params: str = "?"
    license: str = "?"
    sample_rate: int = 0
    #: No PyTorch in the audio path. The whole motivation for looking at
    #: alternatives is that Torch is a crash surface, so this is a
    #: first-class property rather than a footnote.
    torch_free: bool = False
    #: **Generation** streams, i.e. audio starts arriving before the
    #: utterance is finished. Deliberately not just "streaming": the
    #: incumbent chunks a *completed* clip for paced playback, which
    #: looks identical from the outside and is a different property.
    #: First-audio latency is the whole generation time when this is
    #: False, however small the chunks are afterwards.
    streams_generation: bool = False

    # ── cloning ──
    #: Seconds of reference audio the engine wants. 0 means it cannot
    #: clone at all, which rules it out however good it sounds: adopting
    #: it would lose Aiko's voice.
    clone_seconds: float = 0.0
    clone_needs_transcript: bool = False

    # ── the three channels that need engine support ──
    #: A real rate/duration control. This is the one that retires the
    #: playback-rate hack and lights up the cadence layer.
    native_rate: bool = False
    #: Name of a numeric expressiveness parameter, if any (Chatterbox's
    #: ``exaggeration``, for instance). Easy to drive from a float.
    numeric_expressiveness: str = ""
    #: Accepts a natural-language style instruction. Usable, but a
    #: reaction label has to be rendered into prose first.
    nl_instruct: bool = False
    #: Inline vocalisation tags the engine speaks natively.
    inline_tags: tuple[str, ...] = ()

    verified: bool = False
    notes: str = ""

    def prosody_coverage(self) -> dict[str, str]:
        """Which ``ProsodyParams`` channels this engine could carry.

        Deliberately returns *why* rather than a boolean, because the
        interesting answer for the incumbent is "yes, but only by
        detuning her", and a bool cannot say that.
        """
        if self.native_rate:
            rate = "native rate control"
        else:
            rate = "post-process time-stretch (WSOLA), or detune -- dark today"

        if self.numeric_expressiveness:
            expr = "numeric: " + self.numeric_expressiveness
        elif self.nl_instruct:
            expr = "natural-language instruction"
        else:
            expr = "none -- reaction label has nowhere to go"

        return {
            "speed_hint": rate,
            "reaction": expr,
            "prefix_reaction": (
                "native " + " ".join(self.inline_tags)
                if self.inline_tags else "sampled earcons only"
            ),
            "gain_db": "post-process (engine-independent)",
            "pause_ms": "post-process (engine-independent)",
        }


@dataclass
class Synth:
    """One synthesis, with the timings the pipeline actually cares about."""

    audio: np.ndarray
    sample_rate: int
    #: Wall time to the first audio block. On a non-streaming engine this
    #: equals ``total_ms``, which is the honest answer rather than a
    #: missing value: a clip-at-a-time engine's first-audio latency *is*
    #: its whole generation time.
    first_chunk_ms: float
    total_ms: float
    chunks: int = 1

    @property
    def duration_s(self) -> float:
        return len(self.audio.reshape(-1)) / float(self.sample_rate or 1)

    @property
    def rtf(self) -> float:
        """Generation time over audio duration. Under 1.0 is realtime."""
        dur = self.duration_s
        return (self.total_ms / 1000.0 / dur) if dur > 0 else float("inf")


class Adapter(ABC):
    """A candidate engine, reduced to what the audition needs."""

    caps: Caps

    def __init__(self) -> None:
        self._loaded = False
        self.load_ms: float = 0.0

    # ── lifecycle ──

    def load(self) -> None:
        if self._loaded:
            return
        t0 = time.monotonic()
        self._load()
        self.load_ms = (time.monotonic() - t0) * 1000.0
        self._loaded = True

    @abstractmethod
    def _load(self) -> None:
        """Bring the model up. Raise to mark the engine unavailable."""

    # ── voices ──

    @abstractmethod
    def voice_from_reference(
        self, ref_wav: Path, *, transcript: str | None = None
    ) -> Any:
        """Clone from a reference clip, returning an opaque voice handle.

        Every candidate in the options doc clones from a clip, which is
        why this is the primary route rather than a named-voice lookup:
        an engine that cannot do this loses Aiko's voice on adoption.
        """

    def voice_from_id(self, ident: str) -> Any:
        """A pre-existing voice, where the engine has the concept."""
        raise NotImplementedError(f"{self.caps.name} has no named voices")

    # ── synthesis ──

    @abstractmethod
    def synth(
        self,
        text: str,
        voice: Any,
        *,
        rate: float = 1.0,
        sink: ChunkSink | None = None,
    ) -> Synth:
        """Speak ``text``.

        ``rate`` is a *request*. An engine without ``native_rate`` should
        ignore it and leave ``Caps.native_rate`` False rather than
        emulating it by detuning -- the bench compares engines on what
        they can genuinely do, and a silent emulation here is exactly the
        confusion this lab exists to remove.
        """


# ── the incumbent ────────────────────────────────────────────────────


class PocketTts(Adapter):
    """Kyutai pocket-tts, as production runs it.

    Wraps :class:`PocketTtsService` rather than ``TTSModel`` directly, on
    the same reasoning ``cue_reach_report.py`` imports its predicate from
    production: a second copy of the generation path is how the bench and
    the app come to disagree, and both numbers would look plausible.
    """

    caps = Caps(
        name="pocket-tts",
        params="100M",
        license="MIT",
        sample_rate=24000,
        torch_free=False,
        # Measured, not assumed, and it was worth measuring: the service
        # emits ~50 ms chunks, which reads as streaming from the
        # WebSocket end. But _speak_worker runs generate_audio to
        # completion and only then paces the finished array out through
        # _emit_pcm, so first audio waits for the last token. It stays
        # responsive in production only because text arrives a sentence
        # at a time -- the 8.7 s phrase in the bench takes 2.2 s to
        # start, and that is the real number for a single long utterance.
        streams_generation=False,
        clone_seconds=10.0,
        clone_needs_transcript=False,
        native_rate=False,
        numeric_expressiveness="",
        nl_instruct=False,
        inline_tags=(),
        verified=True,
        notes=(
            "Speed exists only as playback-rate scaling, which couples "
            "pitch at ~1.6 semitones per 10% of rate. temp is the one "
            "real knob and it varies stability, not expression. Chunked "
            "playback is not chunked generation."
        ),
    )

    def __init__(self, *, temp: float | None = None) -> None:
        super().__init__()
        self._temp_override = temp
        self._service: Any = None

    def _load(self) -> None:
        from app.core.infra.settings import load_settings
        from app.tts.pocket_tts_service import PocketTtsService

        settings = load_settings().tts
        if self._temp_override is not None:
            settings.pocket_tts_temp = float(self._temp_override)
        service = PocketTtsService(settings)
        if not service.warmup_sync():
            status, message = service.get_status()
            raise RuntimeError(f"pocket-tts unavailable: {status}: {message}")
        self._service = service
        model = service.get_model()
        rate = int(getattr(model, "sample_rate", 24000) or 24000)
        if rate != self.caps.sample_rate:
            # Declared caps are the thing most likely to be stale, so
            # disagreement with the live model is worth saying out loud
            # rather than silently trusting either one.
            print(f"  note: pocket-tts sample_rate is {rate}, caps say "
                  f"{self.caps.sample_rate}")

    @property
    def service(self) -> Any:
        self.load()
        return self._service

    def voice_from_reference(
        self, ref_wav: Path, *, transcript: str | None = None
    ) -> Any:
        model = self.service.get_model()
        if model is None:
            raise RuntimeError("pocket-tts model not loaded")
        # ``_resolve_voice`` accepts .safetensors / .wav / .mp3 and hands
        # any of them to the same call, so a reference clip and a saved
        # embedding are the same operation here.
        return model.get_state_for_audio_prompt(str(ref_wav))

    def voice_from_id(self, ident: str) -> Any:
        model = self.service.get_model()
        if model is None:
            raise RuntimeError("pocket-tts model not loaded")
        path = Path(ident)
        if not path.is_absolute():
            candidate = REPO_ROOT / "voices" / ident
            if candidate.exists():
                path = candidate
        return model.get_state_for_audio_prompt(
            str(path) if path.exists() else ident
        )

    def synth(
        self,
        text: str,
        voice: Any,
        *,
        rate: float = 1.0,
        sink: ChunkSink | None = None,
    ) -> Synth:
        service = self.service
        # The service memoises clips by text, so a repeated phrase would
        # be timed at ~0 ms and the bench would report a fictional
        # latency. ``ClipCache`` carries its own lock; this used to take
        # a ``_cache_lock`` off the service, which the cache absorbed --
        # leaving pocket-tts auditioning broken with an AttributeError,
        # visible only on the engine nobody was auditioning.
        service._audio_cache.clear()  # noqa: SLF001 -- prototype, see docstring

        prior = getattr(service, "_voice_state", None)
        with service._lock:  # noqa: SLF001
            service._voice_state = voice  # noqa: SLF001
        t0 = time.monotonic()
        try:
            result = service.generate_audio(text, 1.0)
        finally:
            with service._lock:  # noqa: SLF001
                service._voice_state = prior  # noqa: SLF001
        elapsed = (time.monotonic() - t0) * 1000.0
        if result is None:
            raise RuntimeError("pocket-tts returned no audio")
        audio, sample_rate = result
        flat = np.asarray(audio, dtype=np.float32).reshape(-1)
        if sink is not None:
            sink(flat)
        # generate_audio hands back a finished clip, so first audio and
        # total are the same number. The streaming path exists in the
        # service (_emit_pcm) but is driven by speak_async, which owns
        # playback -- not something to route through a bench.
        return Synth(
            audio=flat,
            sample_rate=int(sample_rate),
            first_chunk_ms=elapsed,
            total_ms=elapsed,
            chunks=1,
        )


# ── registry ─────────────────────────────────────────────────────────

#: Adding an engine is a factory here plus a module beside this one.
#: Kept lazy so a missing optional dependency costs an entry in the
#: bench's "unavailable" list rather than an import error at startup.
REGISTRY: dict[str, Callable[[], Adapter]] = {
    "pocket-tts": PocketTts,
}


def available() -> list[str]:
    return sorted(REGISTRY)


def build(name: str) -> Adapter:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise SystemExit(
            f"unknown engine {name!r}; have: {', '.join(available())}"
        ) from None
    return factory()


# ── wav helpers, shared by the lab ───────────────────────────────────


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> Path:
    """Write mono Int16 at ``sample_rate``."""
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm16 = (np.clip(flat, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())
    return path


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read 16-bit PCM WAV as mono float32, averaging extra channels.

    Stdlib only, for the hot path: the bench reads back one of these per
    synthesis and does not want a decoder in the loop. Use
    :func:`read_audio` for anything a human supplied.
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: expected 16-bit, got {width * 8}-bit")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, int(rate)


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    """Read any format libsndfile knows, as mono float32.

    Exists because the highest-quality reference material available is
    whatever the voice was *originally* cloned from, and that arrives as
    whatever the person happened to have -- mp3, flac, 24-bit wav. Making
    them convert first is how a good source clip gets replaced by a
    convenient bad one. libsndfile 1.2.2 covers MP3, FLAC, OGG and WAV
    at any bit depth; the stdlib fallback keeps 16-bit WAV working if
    soundfile is ever missing.
    """
    try:
        import soundfile as sf

        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        return np.asarray(data, dtype=np.float32).mean(axis=1), int(rate)
    except ImportError:
        return read_wav(path)


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Rate-convert, anti-aliased where scipy is available.

    Polyphase (``resample_poly``) rather than the linear interpolation
    this used to do, because linear is only defensible upward.
    Downsampling with it aliases: every component above the new Nyquist
    folds back down as inharmonic noise, and speech has plenty up there
    -- fricatives are broadband to 16 kHz and beyond. On a conditioning
    clip that is a subtle wrongness you would blame on the engine; baked
    into a *training* set it is permanent, because the model learns the
    folded noise as part of the voice.

    scipy is a hard dependency of the app, so the linear path below is a
    fallback for a stripped environment rather than a real branch. It
    warns instead of failing: a rate conversion refusing to happen would
    stop a dataset build, and a noisy conversion the operator was told
    about is the better of two bad options.
    """
    if src_rate == dst_rate or audio.size == 0:
        return np.asarray(audio, dtype=np.float32).reshape(-1)
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    try:
        from math import gcd

        from scipy.signal import resample_poly

        divisor = gcd(int(src_rate), int(dst_rate))
        return np.asarray(
            resample_poly(flat, dst_rate // divisor, src_rate // divisor),
            dtype=np.float32,
        )
    except ImportError:
        if dst_rate < src_rate:
            print(
                f"  warning: downsampling {src_rate} -> {dst_rate} without "
                "scipy; expect aliasing"
            )
    count = int(round(flat.size * dst_rate / float(src_rate)))
    if count <= 1:
        return flat
    src_idx = np.linspace(0.0, flat.size - 1.0, count, dtype=np.float64)
    return np.interp(src_idx, np.arange(flat.size), flat).astype(np.float32)


@dataclass
class Quality:
    """Cheap sanity numbers for a clip about to be used as a reference.

    A bad reference poisons every clone made from it, and the failure is
    silent -- the clone just sounds subtly wrong and you spend the day
    blaming the new engine. Peak, RMS and silence share catch the three
    ways a generated reference actually goes wrong: clipped, too quiet
    to condition on, or padded with dead air.
    """

    duration_s: float
    peak: float
    rms: float
    silence_share: float
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.warnings


def assess(audio: np.ndarray, sample_rate: int, *, want_s: float = 0.0) -> Quality:
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return Quality(0.0, 0.0, 0.0, 1.0, ("empty clip",))
    duration = flat.size / float(sample_rate)
    peak = float(np.abs(flat).max())
    rms = float(np.sqrt(np.mean(flat.astype(np.float64) ** 2)))
    # 20 ms frames under -50 dBFS. Coarse, but it separates "sentence
    # gaps" from "the model stopped and we kept recording".
    frame = max(1, int(sample_rate * 0.02))
    usable = flat[: (flat.size // frame) * frame].reshape(-1, frame)
    quiet = float((np.abs(usable).max(axis=1) < 0.003).mean()) if usable.size else 1.0

    warnings: list[str] = []
    if peak >= 0.999:
        warnings.append("clipped")
    elif peak < 0.1:
        warnings.append(f"very quiet (peak {peak:.3f})")
    if rms < 0.01:
        warnings.append(f"low RMS ({rms:.4f})")
    if quiet > 0.35:
        warnings.append(f"{quiet:.0%} silence")
    if want_s and duration < want_s * 0.6:
        warnings.append(f"short: {duration:.1f}s for a {want_s:.0f}s target")
    return Quality(duration, peak, rms, quiet, tuple(warnings))
