"""Which TTS engines exist, which are actually usable, and how to build one.

Why a registry rather than an if-else in the factory
---------------------------------------------------
``_build_tts_service`` used to hardcode ``PocketTtsService`` and
``list_tts_providers()`` returned the literal ``["pocket-tts"]``, so the
provider setting was stored, switchable, and ignored. Adding a second
engine by extending that if-else would have spread three separate pieces
of knowledge -- what exists, whether it is installed, and how to
construct it -- across the session mixins, the REST layer and the web UI.

So all three live here. The session code asks "what can I offer the
user", the REST layer asks the same thing to populate a dropdown, and the
factory asks "build me this one". None of them need to know that
pocket-tts is an in-process import while Chatterbox is a subprocess in a
foreign virtualenv.

Nothing heavy is imported at module scope
-----------------------------------------
This is the load-bearing property, not a nicety. Two reasons:

**Cost.** Importing an engine pulls its whole runtime -- for pocket-tts
that is PyTorch, roughly 0.6-1 GB resident before a single word is
spoken. A registry that imported every candidate to see if it worked
would make every engine's cost unconditional, which is precisely
backwards.

**Safety.** Chatterbox requires ``torch==2.6.0`` and this app runs
``2.10.0``. Those cannot coexist in one interpreter, which is why
Chatterbox runs as a subprocess in its own venv. Importing it here would
not be slow, it would be impossible -- so availability is answered by
looking at the filesystem, never by trying.

Both engines are therefore imported inside :func:`build`, at the moment
one is actually chosen.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the per-engine virtualenvs live. Shared with
#: ``tools/tts_lab/envs.py``, which is what creates them -- installing an
#: engine stays a deliberate developer action, so the app only ever reads
#: this and never installs anything itself.
VENV_ROOT = REPO_ROOT / ".venvs"

#: The engine worker script, run by a foreign interpreter. It imports
#: nothing from this repo by design, so invoking it by path is a
#: subprocess call rather than a code dependency on ``tools/``.
SIDECAR = REPO_ROOT / "tools" / "tts_lab" / "sidecar.py"

#: Fallback when the configured provider cannot be built. Chosen over
#: raising because the alternative to a working voice is not a clear
#: error message, it is a companion who has gone mute.
DEFAULT_PROVIDER = "pocket-tts"


@dataclass(frozen=True, slots=True)
class Provider:
    """Everything about an engine that can be known without loading it."""

    name: str
    label: str
    #: What a "voice" means here. ``embedding`` is a saved pocket-tts
    #: speaker state; ``clip`` is a reference wav the engine clones from
    #: per call. The distinction has to reach the UI, because offering a
    #: ``.safetensors`` to an engine that clones from audio is an error
    #: the user cannot diagnose.
    voice_kind: str
    #: Devices this engine can actually run on, best-first. pocket-tts is
    #: CPU-only, so exposing a device picker for it would be a lie.
    devices: tuple[str, ...]
    #: Which ``.venvs/`` entry owns the interpreter, empty for in-process
    #: engines. One venv hosts several model sizes, which is why this is
    #: separate from :attr:`sidecar_engine`.
    venv: str = ""
    #: The key the sidecar's own registry knows this model by.
    sidecar_engine: str = ""
    notes: str = ""


#: Ordered as offered to the user: the incumbent first, then candidates
#: cheapest-first, since that is also fastest-first.
CATALOGUE: tuple[Provider, ...] = (
    Provider(
        name="pocket-tts",
        label="Pocket-TTS (in-process)",
        voice_kind="embedding",
        devices=("cpu",),
        notes=(
            "The incumbent. RTF 0.24 and ~570 ms to first audio, which no "
            "candidate has beaten on CPU. No native rate, pitch or emotion "
            "control -- everything about how she sounds is entangled in the "
            "speaker embedding."
        ),
    ),
    Provider(
        name="chatterbox-nano",
        label="Chatterbox Nano (110M)",
        voice_kind="clip",
        devices=("cpu", "cuda"),
        venv="chatterbox-git",
        sidecar_engine="chatterbox-nano",
        notes=(
            "The only Chatterbox variant under RTF 1.0 on CPU (0.59-0.87), "
            "so the only one that can ship without a GPU. Supports inline "
            "[laugh] / [sigh] tags. ~1900 ms to first audio, which is a "
            "real regression against pocket-tts, and only ~15% headroom "
            "before it cannot keep up at all."
        ),
    ),
    Provider(
        name="chatterbox-turbo",
        label="Chatterbox Turbo (350M)",
        voice_kind="clip",
        devices=("cuda", "cpu"),
        venv="chatterbox",
        sidecar_engine="chatterbox-turbo",
        notes=(
            "Preserves her voice well and pronounces more naturally than "
            "the incumbent, but RTF 1.37-1.66 on CPU means playback can "
            "never keep up. Needs a GPU to be a live engine; excellent as "
            "an offline dataset teacher either way."
        ),
    ),
    Provider(
        name="chatterbox-multilingual",
        label="Chatterbox Multilingual (500M, 23 languages)",
        voice_kind="clip",
        devices=("cuda",),
        venv="chatterbox",
        sidecar_engine="chatterbox-multilingual",
        notes=(
            "Cross-lingual: a Japanese reference clip can speak English "
            "text, accent included. RTF ~3.0 on CPU, so CPU is not offered "
            "-- it would stutter rather than merely lag. No inline tags."
        ),
    ),
)

_BY_NAME = {p.name: p for p in CATALOGUE}


def get(name: str) -> Provider | None:
    return _BY_NAME.get((name or "").strip().lower())


def _venv_interpreter(venv: str) -> Path:
    root = VENV_ROOT / venv
    if sys.platform == "win32":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def availability(name: str) -> tuple[bool, str]:
    """Can this engine be built right now? Answered without importing it.

    Returns ``(usable, reason)``, where ``reason`` explains a ``False``
    and is empty otherwise. The reason text is user-facing: it ends up in
    the settings drawer next to a greyed-out option, so it says what to
    *do*, not merely what is missing.
    """
    provider = get(name)
    if provider is None:
        return False, f"unknown TTS provider {name!r}"

    if not provider.venv:
        # ``find_spec`` locates without executing the module, so this
        # stays cheap -- the point of the whole exercise.
        for module in ("pocket_tts", "numpy"):
            try:
                if importlib.util.find_spec(module) is None:
                    return False, f"{module} is not installed"
            except (ImportError, ValueError):
                return False, f"{module} is not importable"
        return True, ""

    # Sidecar before venv, because the container image is the common case
    # for its absence and the venv message is actively misleading there:
    # the Docker build copies ``app/`` and ``config/`` but not ``tools/``,
    # so telling someone to run a module out of a directory that is not in
    # the image sends them looking for a bug that is a packaging decision.
    if not SIDECAR.exists():
        return False, (
            "not available in this install -- the engine worker "
            f"({SIDECAR.name}) ships with the source tree, not the "
            "container image"
        )
    interpreter = _venv_interpreter(provider.venv)
    if not interpreter.exists():
        return False, (
            f"engine venv missing -- run 'python -m tools.tts_lab.envs "
            f"install {provider.venv}'"
        )
    return True, ""


def resolve_device(name: str, requested: str) -> str:
    """Pick a device this engine can actually use.

    ``auto`` prefers whatever the provider lists first, which encodes the
    engine's own economics: pocket-tts is CPU-only, Nano is fine on CPU
    and there is no reason to spend VRAM on it, Turbo needs a GPU to
    reach real time. A request the engine cannot honour is downgraded
    with a warning rather than refused -- a device typo should not cost
    the user her voice.

    Note this does *not* check whether CUDA is present. That would mean
    importing torch, which is exactly what this module refuses to do; the
    engine reports what it actually got once it has loaded.
    """
    provider = get(name)
    if provider is None:
        return "cpu"
    want = (requested or "auto").strip().lower()
    if want in ("", "auto"):
        return provider.devices[0]
    if want in provider.devices:
        return want
    fallback = provider.devices[0]
    log.warning(
        "TTS provider %s cannot run on %s; using %s",
        provider.name, want, fallback,
    )
    return fallback


def _per_provider(tts_settings: Any, name: str) -> Any:
    """Per-provider settings, tolerating a settings object without them.

    Settings are duck-typed all over this app, and the factory must not
    be the one place that insists on the real dataclass.
    """
    getter = getattr(tts_settings, "for_provider", None)
    if callable(getter):
        return getter(name)
    from app.core.infra.settings import TtsProviderSettings

    return TtsProviderSettings()


def describe() -> list[dict[str, Any]]:
    """The catalogue plus live availability, shaped for the settings API."""
    rows: list[dict[str, Any]] = []
    for provider in CATALOGUE:
        usable, reason = availability(provider.name)
        rows.append(
            {
                "name": provider.name,
                "label": provider.label,
                "available": usable,
                "reason": reason,
                "voice_kind": provider.voice_kind,
                "devices": list(provider.devices),
                "notes": provider.notes,
            }
        )
    return rows


def usable_names() -> list[str]:
    return [p.name for p in CATALOGUE if availability(p.name)[0]]


def build(name: str, tts_settings: Any) -> Any:
    """Construct an engine, importing only what that engine needs.

    Raises on failure. Callers that must not fail -- the session factory
    -- catch and fall back; see :func:`build_with_fallback`.
    """
    provider = get(name)
    if provider is None:
        raise ValueError(f"unknown TTS provider {name!r}")
    usable, reason = availability(provider.name)
    if not usable:
        raise RuntimeError(f"{provider.name} unavailable: {reason}")

    if not provider.venv:
        from app.tts.pocket_tts_service import PocketTtsService

        # Deliberately not reading per-provider settings on this path.
        # pocket-tts has no device choice and reads its own voice from
        # the flat fields, so touching ``for_provider`` here would make
        # the incumbent engine depend on a method it never uses -- and it
        # did, briefly: any duck-typed settings object without it fell
        # all the way through to the null engine and silently muted her.
        return PocketTtsService(tts_settings)

    per_provider = _per_provider(tts_settings, provider.name)
    device = resolve_device(provider.name, per_provider.device)

    from app.tts.chatterbox_service import ChatterboxTtsService

    return ChatterboxTtsService(
        tts_settings,
        interpreter=_venv_interpreter(provider.venv),
        sidecar=SIDECAR,
        engine_key=provider.sidecar_engine,
        device=device,
        voice=per_provider.voice,
        threads=(
            per_provider.threads
            if per_provider.threads > 0
            else default_threads()
        ),
    )


def default_threads() -> int:
    """CPU threads for a sidecar engine when nothing is configured.

    Not simply left to torch, whose default is one thread per core.
    Measured on a 16-core 9950X3D, Nano synthesises at RTF 0.78 on 8
    threads and 0.93 on 16 -- a small autoregressive model hits memory
    bandwidth and sync overhead well before it runs out of cores, so the
    extra threads cost time rather than saving it.

    Half the cores, capped at 8, is therefore both the faster choice and
    the neighbourly one: it leaves the rest of the machine to whatever
    else is running, which is the whole reason TTS is on the CPU instead
    of the GPU in the first place.
    """
    import os

    cores = os.cpu_count() or 4
    return max(1, min(8, cores // 2))


def build_with_fallback(name: str, tts_settings: Any) -> Any:
    """Build ``name``, or the default, or a null engine -- but always return.

    The session controller constructs TTS during boot and has nowhere
    useful to put an exception: a stack trace at startup because a
    provider name was misspelled, or because an experimental venv was
    deleted, would take the whole app down over a component that is
    supposed to be optional. So this degrades in steps and says so
    loudly.
    """
    for candidate in (name, DEFAULT_PROVIDER):
        if not candidate:
            continue
        try:
            engine = build(candidate, tts_settings)
        except Exception as exc:
            log.warning("TTS provider %s unavailable: %s", candidate, exc)
            continue
        if candidate != name:
            log.warning(
                "TTS falling back from %s to %s", name, candidate,
            )
        return engine

    from app.tts.null_tts_service import NullTtsService

    log.error("no TTS provider could be built; voice disabled")
    return NullTtsService(tts_settings)
