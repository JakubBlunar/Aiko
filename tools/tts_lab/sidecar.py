"""Engine worker, run *inside* a candidate's own venv. Speaks JSON.

This file is executed by an interpreter that knows nothing about this
repo: ``.venvs/<engine>/Scripts/python.exe tools/tts_lab/sidecar.py``.
So it must import **nothing** from ``tools.tts_lab`` or ``app`` -- only
the standard library, numpy, and the engine under test. Adding an import
from the lab package here is the one change that will break every
sidecar at once.

Protocol
--------
Line-delimited JSON on stdin, one JSON object per line on stdout.

    {"op": "load"}                        -> {"ok": true, "sample_rate": …,
                                              "accepts": [...], "load_ms": …}
    {"op": "clone", "ref": "path.wav"}    -> {"ok": true, "voice": 0}
    {"op": "synth", "text": …,
     "voice": 0, "out": "path.wav",
     "kwargs": {...}}                     -> {"ok": true, "total_ms": …, …}
    {"op": "quit"}                        -> exits

Audio goes via files rather than down the pipe. Piping a few hundred KB
of PCM through stdout would mean framing binary alongside JSON, and the
bench wants the WAV on disk anyway.

Two things this does that matter
--------------------------------
**stdout is protocol, so everything else is pushed to stderr.** Model
loaders print banners, HuggingFace prints download bars, and one stray
``print`` inside a dependency would corrupt the stream and look like a
parse bug in the parent. ``load`` runs under
``redirect_stdout(sys.stderr)`` for that reason.

**The control surface is introspected, not assumed.** ``accepts`` is the
keyword list of the engine's own ``generate``, read off the installed
code with :mod:`inspect`. That is what lets ``Caps`` be *verified*
rather than copied from a README -- the docs for this family describe
``exaggeration`` and ``cfg_weight`` for the original model and are quiet
about whether Turbo and Nano kept them, and guessing wrong would mean
silently benchmarking an engine with its expressiveness dial ignored.
Unknown kwargs are dropped with a warning rather than passed through,
because several of these engines accept ``**kwargs`` and discard
politely.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
import traceback
import wave
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import numpy as np


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _note(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> int:
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm16 = (np.clip(flat, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())
    return int(flat.size)


def _to_numpy(wav: Any) -> np.ndarray:
    """Coerce whatever the engine returned into mono float32.

    Engines in this family return a torch tensor shaped ``(1, N)``, but
    some return ``(N,)`` and some return numpy already. Handled here
    rather than per-engine so a new adapter does not have to care.
    """
    if hasattr(wav, "detach"):
        wav = wav.detach().cpu().numpy()
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim > 1:
        # (channels, samples) for this family, so average channels.
        arr = arr.mean(axis=0) if arr.shape[0] <= 2 else arr.reshape(-1)
    return arr.reshape(-1)


class Engine:
    """Base for a sidecar-hosted engine."""

    def load(self) -> None:
        raise NotImplementedError

    @property
    def sample_rate(self) -> int:
        raise NotImplementedError

    def accepts(self) -> list[str]:
        return []

    def defaults(self) -> dict[str, Any]:
        return {}

    def clone(self, ref: Path) -> Any:
        raise NotImplementedError

    def synth(self, text: str, voice: Any, kwargs: dict) -> np.ndarray:
        raise NotImplementedError

    def metadata(self) -> dict[str, Any]:
        """Anything engine-specific the parent should know after loading.

        Read off the installed package rather than declared, for the same
        reason ``accepts`` is: a language list in a README is a claim
        about some release, not about this one.
        """
        return {}


class Chatterbox(Engine):
    """Resemble AI Chatterbox: Turbo (350M), Nano (110M), or the original.

    Cloning is per-call rather than a prepared handle -- ``generate``
    takes ``audio_prompt_path`` directly -- so ``clone`` just validates
    the clip and remembers the path.

    ``nano`` is handled by introspection rather than by passing it and
    hoping. The published README documents
    ``from_pretrained(device, nano=True)``, and the release on PyPI
    (0.1.7) has no such parameter: the docs are ahead of the wheel. Given
    that, passing it blind produces a ``TypeError`` from inside a
    subprocess, which is a confusing way to learn a packaging fact.
    """

    def __init__(
        self,
        *,
        variant: str = "turbo",
        nano: bool = False,
        device: str = "cpu",
    ) -> None:
        self._variant = variant
        self._nano = nano
        self._device = device
        self._model: Any = None
        self._accepts: list[str] = []

    def load(self) -> None:
        if self._variant == "full":
            from chatterbox.tts import ChatterboxTTS as cls
        else:
            from chatterbox.tts_turbo import ChatterboxTurboTTS as cls

        kwargs: dict[str, Any] = {"device": self._device}
        if self._nano:
            try:
                takes_nano = "nano" in inspect.signature(
                    cls.from_pretrained
                ).parameters
            except (TypeError, ValueError):
                takes_nano = False
            if not takes_nano:
                raise RuntimeError(
                    "this chatterbox release has no 'nano' option "
                    "(README is ahead of PyPI 0.1.7). Install the git "
                    "version: python -m tools.tts_lab.envs install "
                    "chatterbox-git"
                )
            kwargs["nano"] = True

        self._model = cls.from_pretrained(**kwargs)
        self._introspect()

    def _introspect(self) -> None:
        try:
            sig = inspect.signature(self._model.generate)
            self._accepts = [
                name
                for name, p in sig.parameters.items()
                if p.kind
                in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                and name not in ("self", "text")
            ]
        except (TypeError, ValueError):
            self._accepts = []

    def defaults(self) -> dict[str, Any]:
        """The engine's own generate() defaults.

        Reported so the bench can run an engine *as shipped* rather than
        at numbers copied from a README written about a sibling model:
        Turbo defaults to ``exaggeration=0.0, cfg_weight=0.0`` while the
        original model and every published tip use 0.5 / 0.5. Forcing
        0.5 onto Turbo would have auditioned it in a configuration its
        authors did not choose.
        """
        if self._model is None:
            return {}
        try:
            sig = inspect.signature(self._model.generate)
        except (TypeError, ValueError):
            return {}
        return {
            name: p.default
            for name, p in sig.parameters.items()
            if p.default is not inspect.Parameter.empty
            and isinstance(p.default, (int, float, bool))
        }

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._model, "sr", 24000) or 24000)

    def accepts(self) -> list[str]:
        return list(self._accepts)

    def clone(self, ref: Path) -> Any:
        if not ref.exists():
            raise FileNotFoundError(str(ref))
        return str(ref)

    def synth(self, text: str, voice: Any, kwargs: dict) -> np.ndarray:
        call: dict[str, Any] = {}
        if voice:
            call["audio_prompt_path"] = voice
        for key, value in (kwargs or {}).items():
            if not self._accepts or key in self._accepts:
                call[key] = value
            else:
                _note(f"sidecar: dropping unsupported kwarg {key!r}")
        return _to_numpy(self._model.generate(text, **call))


class ChatterboxMultilingual(Chatterbox):
    """The 23-language model, for cross-lingual cloning.

    The point of it here is not localisation. Voice identity and
    linguistic content are separate paths in this architecture, so a
    reference clip in one language can speak text in another -- which
    matters when the available candidate voices for a character are
    overwhelmingly Japanese and the character speaks English.

    Accent comes along with the timbre, because the speaker encoder
    captures phonetic habit as well as vocal tract. For most products
    that is a defect to engineer around; for an anime-styled companion it
    may be the desired result, so nothing here tries to suppress it.

    ``language_id`` is a *required* positional on this model's
    ``generate`` -- no default, unlike every other knob -- so it is
    injected rather than left to the caller. Forgetting it is a
    ``TypeError`` from inside a subprocess, which is a poor way to find
    out. Note also that the defaults here are ``exaggeration=0.5,
    cfg_weight=0.5``, matching the original model rather than Turbo's
    ``0.0 / 0.0``.
    """

    #: What to speak when the caller does not say. English because that
    #: is what Aiko speaks; the reference clip's language is a separate
    #: question and deliberately not tied to this.
    DEFAULT_LANGUAGE = "en"

    def load(self) -> None:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS as cls

        if self._nano:
            raise RuntimeError("there is no Nano variant of the multilingual model")
        self._model = cls.from_pretrained(device=self._device)
        self._introspect()

    def metadata(self) -> dict[str, Any]:
        languages: list[str] = []
        try:
            from chatterbox.mtl_tts import SUPPORTED_LANGUAGES

            languages = sorted(SUPPORTED_LANGUAGES)
        except Exception:
            getter = getattr(self._model, "get_supported_languages", None)
            if callable(getter):
                try:
                    languages = sorted(getter())
                except Exception:
                    languages = []
        return {"languages": languages, "default_language": self.DEFAULT_LANGUAGE}

    def defaults(self) -> dict[str, Any]:
        base = super().defaults()
        # Surfaced as a default even though generate() has none, so the
        # parent can show and override it like any other knob.
        base["language_id"] = self.DEFAULT_LANGUAGE
        return base

    def synth(self, text: str, voice: Any, kwargs: dict) -> np.ndarray:
        call = dict(kwargs or {})
        call.setdefault("language_id", self.DEFAULT_LANGUAGE)
        return super().synth(text, voice, call)


REGISTRY = {
    "chatterbox-turbo": lambda device="cpu": Chatterbox(
        variant="turbo", device=device
    ),
    "chatterbox-nano": lambda device="cpu": Chatterbox(
        variant="turbo", nano=True, device=device
    ),
    "chatterbox-full": lambda device="cpu": Chatterbox(
        variant="full", device=device
    ),
    "chatterbox-multilingual": lambda device="cpu": ChatterboxMultilingual(
        device=device
    ),
}


def _device_report(requested: str) -> dict:
    """Check the device is real *before* loading, and say what it is.

    A CUDA request that silently ran on CPU would be the worst outcome
    available: Turbo needs a GPU to reach real time, so a quiet
    downgrade produces an engine that stutters for a reason nobody can
    see from the setting. Worse, the venvs here were installed with
    CPU-only torch wheels, so this is the likely case rather than an
    exotic one -- and on a 5090 there is a second trap behind it, since
    Blackwell is sm_120 and needs a cu128 build.

    So this refuses up front with a message naming the fix, rather than
    letting ``from_pretrained`` fail six frames deep in a foreign venv.
    """
    want = (requested or "cpu").strip().lower()
    try:
        import torch
    except Exception as exc:
        return {"error": f"torch is not importable in this venv: {exc}"}

    build = getattr(torch.version, "cuda", None)
    report = {
        "device": want,
        "device_requested": want,
        "torch": torch.__version__,
        "cuda_build": build,
    }
    if want != "cuda":
        return report

    if not build:
        report["error"] = (
            f"cuda requested but this venv has a CPU-only torch "
            f"({torch.__version__}); reinstall the env with a CUDA wheel"
        )
        return report
    if not torch.cuda.is_available():
        report["error"] = (
            f"cuda requested but torch {torch.__version__} "
            f"(cuda {build}) cannot see a device"
        )
        return report
    try:
        major, minor = torch.cuda.get_device_capability()
        report["compute_capability"] = f"{major}.{minor}"
        report["gpu"] = torch.cuda.get_device_name(0)
        if f"sm_{major}{minor}" not in torch.cuda.get_arch_list():
            report["error"] = (
                f"{report['gpu']} is sm_{major}{minor} but this torch "
                f"({torch.__version__}, cuda {build}) was built for "
                f"{','.join(torch.cuda.get_arch_list())}"
            )
    except Exception as exc:
        report["error"] = f"cuda probe failed: {exc}"
    return report


def _set_threads(count: int) -> dict:
    """Pin torch's thread count, and report what actually took effect.

    Upstream's CPU claim for Nano is "3x realtime on 8 cores", so any
    comparison that does not state its thread count is not a
    measurement. Torch's default is one thread per physical core, which
    on a 16-core part is not obviously optimal for a small autoregressive
    model -- memory bandwidth and sync overhead can make fewer threads
    faster. Returned rather than logged so the bench can record it beside
    the RTF.
    """
    info: dict[str, Any] = {}
    try:
        import torch

        if count > 0:
            torch.set_num_threads(int(count))
        info["threads"] = int(torch.get_num_threads())
        info["interop_threads"] = int(torch.get_num_interop_threads())
        info["torch"] = torch.__version__
    except Exception as exc:
        info["threads_error"] = f"{type(exc).__name__}: {exc}"
    return info


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="tts_lab engine sidecar")
    p.add_argument("--engine", required=True, choices=sorted(REGISTRY))
    p.add_argument(
        "--threads",
        type=int,
        default=0,
        help="torch thread count; 0 leaves the library default alone",
    )
    p.add_argument(
        "--device",
        default="cpu",
        help="cpu or cuda; the reply to 'load' reports what was actually got",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    thread_info = _set_threads(args.threads)
    engine = REGISTRY[args.engine](device=args.device)
    voices: list[Any] = []

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            _reply({"ok": False, "error": f"bad json: {exc}"})
            continue
        op = msg.get("op")

        try:
            if op == "quit":
                return 0

            if op == "load":
                device_info = _device_report(args.device)
                if device_info.get("error"):
                    _reply({"ok": False, "error": device_info["error"]})
                    continue
                t0 = time.monotonic()
                # Loaders and download bars must not touch stdout.
                with redirect_stdout(sys.stderr):
                    engine.load()
                _reply(
                    {
                        "ok": True,
                        "load_ms": (time.monotonic() - t0) * 1000.0,
                        "sample_rate": engine.sample_rate,
                        "accepts": engine.accepts(),
                        "defaults": engine.defaults(),
                        "python": sys.version.split()[0],
                        **device_info,
                        **engine.metadata(),
                        **thread_info,
                    }
                )
                continue

            if op == "clone":
                with redirect_stdout(sys.stderr):
                    voices.append(engine.clone(Path(msg["ref"])))
                _reply({"ok": True, "voice": len(voices) - 1})
                continue

            if op == "synth":
                voice = voices[int(msg.get("voice", 0))] if voices else None
                out = Path(msg["out"])
                t0 = time.monotonic()
                with redirect_stdout(sys.stderr):
                    audio = engine.synth(
                        msg["text"], voice, msg.get("kwargs") or {}
                    )
                total_ms = (time.monotonic() - t0) * 1000.0
                rate = engine.sample_rate
                samples = _write_wav(out, audio, rate)
                _reply(
                    {
                        "ok": True,
                        "total_ms": total_ms,
                        "sample_rate": rate,
                        "samples": samples,
                        "out": str(out),
                    }
                )
                continue

            _reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as exc:  # noqa: BLE001 -- must not die on one bad call
            # The parent only gets one line, and a bare "TypeError:
            # 'NoneType' object is not callable" from six frames inside a
            # foreign venv is not a debuggable message. Full traceback to
            # stderr, which is inherited by the terminal.
            _note("".join(traceback.format_exception(exc)))
            _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
