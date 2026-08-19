"""Adapters for engines that live in their own venv.

:class:`~tools.tts_lab.adapters.Adapter` on this side, a subprocess on
the other. The bench cannot tell the difference, which is the point: an
engine's packaging hostility is an installation problem, not a reason
for a second comparison harness.

What the isolation costs, and why it is worth paying anyway
----------------------------------------------------------
Two honest caveats, both reported rather than hidden:

* **A per-call file round trip.** The sidecar writes a WAV and the
  parent reads it. On an NVMe that is well under a millisecond against
  generation times in the hundreds, so it does not distort the RTF
  comparison. It would distort a first-audio measurement in the tens of
  ms, which no engine in this class is close to.
* **No streaming.** The protocol is request/response, so a sidecar
  engine's ``first_chunk_ms`` equals its ``total_ms`` even if the engine
  streams natively. That is a limitation of the harness and not of the
  engine, so ``Caps.streams_generation`` stays honest and the bench's
  first-audio column should be read as "clip latency" for these rows.
  Worth fixing only for an engine that survives the first audition.

The alternative -- installing candidates into the app's venv -- is not a
tradeoff, it is a broken app: Chatterbox alone wants torch 2.6 over our
2.10. See :mod:`tools.tts_lab.envs`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from tools.tts_lab.adapters import (
    REPO_ROOT,
    Adapter,
    Caps,
    ChunkSink,
    Synth,
    read_wav,
)
from tools.tts_lab.envs import ENGINES

SIDECAR = Path(__file__).resolve().parent / "sidecar.py"


class SidecarError(RuntimeError):
    pass


class Remote(Adapter):
    """Drives :mod:`tools.tts_lab.sidecar` in another interpreter."""

    #: Which entry in ``envs.ENGINES`` owns the venv, and which engine
    #: key inside the sidecar's own registry to ask for. Split because
    #: one venv usually hosts several model sizes.
    env_name: str = ""
    sidecar_engine: str = ""

    def __init__(self, *, threads: int = 0) -> None:
        super().__init__()
        self.threads = int(threads)
        self._proc: subprocess.Popen | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._calls = 0
        #: Filled from the sidecar's ``load`` reply -- the engine's real
        #: ``generate`` keywords, read off the installed code.
        self.accepts: list[str] = []
        #: The installed engine's own generate() defaults. Used in
        #: preference to anything hardcoded here, so an engine is
        #: auditioned as its authors shipped it.
        self.defaults: dict[str, Any] = {}
        #: Explicit overrides, for sweeping a knob. Empty means "as
        #: shipped", which is the only defensible starting point.
        self.overrides: dict[str, Any] = {}
        #: Interpreter and thread facts from the sidecar, so a CPU RTF
        #: comes with the conditions it was measured under.
        self.runtime: dict[str, Any] = {}

    # ── process ──

    def _interpreter(self) -> Path:
        env = ENGINES.get(self.env_name)
        if env is None:
            raise SidecarError(f"no env registered as {self.env_name!r}")
        if not env.installed:
            raise SidecarError(
                f"{self.env_name} venv missing -- run "
                f"'python -m tools.tts_lab.envs install {self.env_name}'"
            )
        return env.interpreter

    def _load(self) -> None:
        python = self._interpreter()
        self._tmp = tempfile.TemporaryDirectory(prefix="ttslab-")
        self._proc = subprocess.Popen(
            [
                str(python),
                str(SIDECAR),
                "--engine",
                self.sidecar_engine,
                "--threads",
                str(self.threads),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Inherited, so download bars and loader banners land in the
            # terminal where they are useful instead of being swallowed.
            stderr=None,
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        reply = self._call({"op": "load"}, timeout_note="model load")
        self.accepts = list(reply.get("accepts") or [])
        self.defaults = dict(reply.get("defaults") or {})
        self.runtime = {
            k: reply[k]
            for k in ("threads", "interop_threads", "torch", "python")
            if k in reply
        }
        if self.runtime.get("threads"):
            print(
                f"  {self.caps.name}: torch {self.runtime.get('torch')} on "
                f"{self.runtime['threads']} threads, "
                f"python {self.runtime.get('python')}"
            )
        # The declared caps are the thing most likely to be wrong -- they
        # come from a README -- so reconcile against the installed code
        # and say so rather than silently trusting either side. Rebound on
        # the instance, leaving the class's declared values intact as the
        # record of what upstream claimed.
        self.caps = self._reconciled(reply)

    def _reconciled(self, reply: dict) -> Caps:
        changes: dict[str, Any] = {"verified": True}
        rate = int(reply.get("sample_rate") or 0)
        if rate and rate != self.caps.sample_rate:
            print(
                f"  note: {self.caps.name} reports {rate} Hz, caps said "
                f"{self.caps.sample_rate}"
            )
            changes["sample_rate"] = rate
        declared = self.caps.numeric_expressiveness
        if self.accepts and declared and declared not in self.accepts:
            print(
                f"  note: {self.caps.name} caps claimed '{declared}' but "
                f"generate() accepts {self.accepts}"
            )
            changes["numeric_expressiveness"] = ""
        if self.accepts and not self.caps.native_rate:
            # A real rate knob would retire the time-stretch stage, so it
            # is worth noticing if one turns up that the docs omitted.
            found = next(
                (k for k in self.accepts if k in ("speed", "rate", "length_scale",
                                                  "speaking_rate", "duration")),
                "",
            )
            if found:
                print(
                    f"  note: {self.caps.name} accepts '{found}' -- "
                    "undocumented rate control, worth a look"
                )
                changes["native_rate"] = True
        return replace(self.caps, **changes)

    def _call(self, msg: dict, *, timeout_note: str = "") -> dict:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise SidecarError("sidecar not running")
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            code = proc.poll()
            raise SidecarError(
                f"sidecar died during {timeout_note or msg.get('op')} "
                f"(exit {code}); see stderr above"
            )
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise SidecarError(f"unparseable reply: {line[:200]!r}") from exc
        if not reply.get("ok"):
            raise SidecarError(str(reply.get("error") or "unknown error"))
        return reply

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._call({"op": "quit"})
            except Exception:
                self._proc.kill()
            self._proc = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ── Adapter ──

    def voice_from_reference(
        self, ref_wav: Path, *, transcript: str | None = None
    ) -> Any:
        self.load()
        reply = self._call(
            {"op": "clone", "ref": str(Path(ref_wav).resolve())}
        )
        return int(reply["voice"])

    def synth(
        self,
        text: str,
        voice: Any,
        *,
        rate: float = 1.0,
        sink: ChunkSink | None = None,
    ) -> Synth:
        self.load()
        assert self._tmp is not None
        self._calls += 1
        out = Path(self._tmp.name) / f"synth{self._calls:04d}.wav"
        t0 = time.monotonic()
        reply = self._call(
            {
                "op": "synth",
                "text": text,
                "voice": int(voice) if voice is not None else 0,
                "out": str(out),
                "kwargs": self.synth_kwargs(rate),
            }
        )
        wall = (time.monotonic() - t0) * 1000.0
        audio, sample_rate = read_wav(out)
        if sink is not None:
            sink(audio)
        # The parent's wall clock, not the sidecar's ``total_ms``: it
        # includes the pipe and the file round trip, which is what the
        # app would actually feel if it drove the engine this way. The
        # engine-only figure is printed when the two diverge enough to
        # matter, so the harness overhead never hides inside the result.
        engine_ms = float(reply.get("total_ms") or 0.0)
        if engine_ms and wall - engine_ms > 50.0:
            print(
                f"  note: {self.caps.name} harness overhead "
                f"{wall - engine_ms:.0f}ms on top of {engine_ms:.0f}ms"
            )
        return Synth(
            audio=audio,
            sample_rate=int(sample_rate or reply.get("sample_rate") or 0),
            first_chunk_ms=wall,
            total_ms=wall,
            chunks=1,
        )

    def synth_kwargs(self, rate: float) -> dict:
        """Generation options for one call.

        Only explicit overrides are sent. Everything else is left to the
        engine's own defaults, which is deliberate: Turbo ships
        ``exaggeration=0.0, cfg_weight=0.0`` while every published tip
        quotes 0.5 / 0.5 -- those tips are about the *original* model.
        Hardcoding 0.5 here would have auditioned Turbo in a
        configuration its authors did not pick, and the resulting verdict
        would have been about our guess rather than about the engine.
        """
        return dict(self.overrides)


class ChatterboxTurbo(Remote):
    """350M. The one actually shipped on PyPI today.

    Nano (110M) is what upstream recommends for CPU, but the released
    wheel has no ``nano`` option -- see :class:`ChatterboxNano`. Turbo is
    the same architecture and the same paralinguistic tags at 3x the
    parameters, so it is the honest stand-in until the smaller weights
    are installable.
    """

    env_name = "chatterbox"
    sidecar_engine = "chatterbox-turbo"

    caps = Caps(
        name="chatterbox-turbo",
        params="350M",
        license="MIT (weights: see model card)",
        sample_rate=24000,
        torch_free=False,
        # Single-step decoder, but the harness is request/response, so
        # this is about the engine rather than what the bench measures.
        streams_generation=False,
        clone_seconds=10.0,
        clone_needs_transcript=False,
        native_rate=False,
        numeric_expressiveness="exaggeration",
        nl_instruct=False,
        inline_tags=("[laugh]", "[chuckle]", "[cough]", "[sigh]"),
        verified=False,
        notes=(
            "Paralinguistic tags are native to the Turbo family, which is "
            "the interesting part for us: earcons in her own voice rather "
            "than sampled clips. No rate parameter, but upstream notes "
            "exaggeration speeds speech up and cfg_weight slows it, so "
            "pacing is reachable indirectly. All output carries an "
            "imperceptible Perth watermark."
        ),
    )


class ChatterboxNano(ChatterboxTurbo):
    """110M, upstream's CPU recommendation. Not on PyPI yet.

    Kept registered on purpose even though it cannot load from the
    released wheel: the bench lists it under "would not run" with the
    reason, which is more useful than it quietly not appearing.
    Points at the git env, which is where ``nano=True`` exists.
    """

    env_name = "chatterbox-git"
    sidecar_engine = "chatterbox-nano"
    caps = replace(
        ChatterboxTurbo.caps, name="chatterbox-nano", params="110M"
    )


class ChatterboxFull(ChatterboxTurbo):
    """The original 500M model. Slower, and the reference for quality.

    Different defaults from Turbo (``exaggeration=0.5, cfg_weight=0.5``)
    and no paralinguistic tags, so it is here as a quality ceiling rather
    than a deployment candidate.
    """

    sidecar_engine = "chatterbox-full"
    caps = replace(
        ChatterboxTurbo.caps,
        name="chatterbox-full",
        params="500M",
        inline_tags=(),
        notes=(
            "The original model: CFG and exaggeration tuning, no "
            "paralinguistic tags. Here as the quality bar, since at 500M "
            "on CPU it is unlikely to meet the latency budget."
        ),
    )


def register() -> None:
    """Add the sidecar engines to the bench registry."""
    from tools.tts_lab import adapters

    adapters.REGISTRY.setdefault("chatterbox-turbo", ChatterboxTurbo)
    adapters.REGISTRY.setdefault("chatterbox-nano", ChatterboxNano)
    adapters.REGISTRY.setdefault("chatterbox-full", ChatterboxFull)


if __name__ == "__main__":
    # Smoke test: clone from the reference clip and speak one line.
    register()
    ref = REPO_ROOT / "voices" / "reference" / "aiko_reference.wav"
    engine = ChatterboxTurbo()
    engine.load()
    print(f"loaded in {engine.load_ms:.0f}ms")
    print(f"  accepts:  {engine.accepts}")
    print(f"  defaults: {engine.defaults}")
    voice = engine.voice_from_reference(ref)
    result = engine.synth(
        "Hey, I was just thinking about you. [chuckle] How did it go?", voice
    )
    out = REPO_ROOT / "voices" / "audition" / "_smoke_chatterbox.wav"
    from tools.tts_lab.adapters import write_wav

    write_wav(out, result.audio, result.sample_rate)
    print(
        f"{result.duration_s:.2f}s at {result.sample_rate} Hz in "
        f"{result.total_ms:.0f}ms (rtf {result.rtf:.2f}) -> {out.name}"
    )
    engine.close()
    sys.exit(0)
