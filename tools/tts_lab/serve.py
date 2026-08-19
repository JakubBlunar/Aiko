"""A local voice studio: record a reference, clone it, audition, save.

Rebuilds the voice-cloning dialog that died with the Qt app. The service
side of it never went away -- ``PocketTtsService.get_model()`` and
``export_voice()`` are still there, hanging off nothing since the dialog
was deleted -- so this is mostly wiring plus a page.

Deliberately a standalone tool rather than a panel in the app's settings
drawer. Cloning wants to load candidate engines that live in their own
venvs with their own torch, and a prototype that can break should not
be able to break the thing Aiko talks through. If an engine wins the
audition, the *narrow* version of this (pick from saved voices, no
cloning) is what belongs in the drawer.

Usage::

    python -m tools.tts_lab.serve            # http://127.0.0.1:6280
    python -m tools.tts_lab.serve --port 7000 --open

Bound to loopback: it takes microphone audio and writes files into
``voices/``, neither of which should be reachable from the network.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tools.tts_lab import adapters, remote
from tools.tts_lab.adapters import REPO_ROOT, assess, read_wav, write_wav
from tools.tts_lab.page import INDEX_HTML
from tools.tts_lab.voicebank import PHRASES

VOICES_DIR = REPO_ROOT / "voices"
WORK_DIR = VOICES_DIR / "studio"

#: Engines are expensive to load (2 s for pocket-tts, up to 37 s for a
#: cold Chatterbox), so they are kept alive across requests. Guarded
#: because the engines themselves are not reentrant -- pocket-tts
#: serialises generation on its own lock and the sidecars are a single
#: pipe each.
_engines: dict[str, adapters.Adapter] = {}
_engine_lock = threading.Lock()
#: reference id -> wav path
_references: dict[str, Path] = {}


def _engine(name: str) -> adapters.Adapter:
    with _engine_lock:
        existing = _engines.get(name)
        if existing is not None:
            return existing
        engine = adapters.build(name)
        engine.load()
        _engines[name] = engine
        return engine


def _saves_as(name: str) -> str:
    """What a "saved voice" is for this engine.

    pocket-tts has a real speaker embedding to export. Everything else in
    the candidate list clones per call from a clip, so the clip *is* the
    voice and saving means keeping it somewhere stable.
    """
    return "safetensors" if name == "pocket-tts" else "wav"


def build_app() -> FastAPI:
    app = FastAPI(title="Aiko voice studio", docs_url=None, redoc_url=None)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/engines")
    def api_engines() -> dict:
        remote.register()
        out = []
        for name in adapters.available():
            caps = adapters.REGISTRY[name]().caps
            row = asdict(caps)
            row["available"] = _availability(name)
            row["saves_as"] = _saves_as(name)
            out.append(row)
        # Installed first, then by name, so the dropdown opens on
        # something that will actually work.
        out.sort(key=lambda r: (not r["available"], r["name"]))
        return {"engines": out}

    @app.get("/api/script")
    def api_script() -> dict:
        return {"phrases": list(PHRASES)}

    @app.get("/api/voices")
    def api_voices() -> dict:
        rows = []
        for path in sorted(VOICES_DIR.glob("*")):
            if path.is_dir() or path.suffix not in (".safetensors", ".wav", ".mp3"):
                continue
            rows.append(
                {"name": path.name, "kb": path.stat().st_size / 1024.0}
            )
        return {"voices": rows}

    @app.post("/api/reference")
    async def api_reference(request: Request) -> JSONResponse:
        """Raw Int16 mono PCM from the browser's Web Audio capture."""
        sample_rate = int(request.query_params.get("sample_rate") or 24000)
        raw = await request.body()
        if len(raw) < 2:
            return JSONResponse({"error": "no audio received"})
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return JSONResponse(_store_reference(audio, sample_rate))

    @app.post("/api/reference/wav")
    async def api_reference_wav(request: Request) -> JSONResponse:
        raw = await request.body()
        tmp = WORK_DIR / f"upload_{uuid.uuid4().hex[:8]}.wav"
        tmp.write_bytes(raw)
        try:
            audio, sample_rate = read_wav(tmp)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            return JSONResponse(
                {"error": f"could not read WAV: {exc}. 16-bit PCM only."}
            )
        finally:
            tmp.unlink(missing_ok=True)
        return JSONResponse(_store_reference(audio, sample_rate))

    @app.get("/api/audio/{name}")
    def api_audio(name: str) -> Response:
        # Name-only lookup inside the work dir: the browser never needs to
        # name a path, so it should not be able to.
        path = WORK_DIR / Path(name).name
        if not path.exists():
            return Response(status_code=404)
        return Response(path.read_bytes(), media_type="audio/wav")

    @app.post("/api/synth")
    async def api_synth(request: Request) -> JSONResponse:
        body = await request.json()
        name = str(body.get("engine") or "")
        ref_id = str(body.get("reference") or "")
        text = str(body.get("text") or "").strip()
        kwargs = body.get("kwargs") or {}
        if not text:
            return JSONResponse({"error": "nothing to say"})
        ref = _references.get(ref_id)
        if ref is None or not ref.exists():
            return JSONResponse({"error": "record or upload a reference first"})
        try:
            engine = _engine(name)
        except Exception as exc:
            return JSONResponse({"error": f"{name} unavailable: {exc}"})
        try:
            with _engine_lock:
                if isinstance(engine, remote.Remote):
                    engine.overrides = dict(kwargs)
                voice = engine.voice_from_reference(ref)
                result = engine.synth(text, voice)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
        out = WORK_DIR / f"synth_{uuid.uuid4().hex[:8]}.wav"
        write_wav(out, result.audio, result.sample_rate)
        return JSONResponse(
            {
                "file": out.name,
                "duration_s": result.duration_s,
                "total_ms": result.total_ms,
                "rtf": result.rtf,
                "sample_rate": result.sample_rate,
            }
        )

    @app.post("/api/save")
    async def api_save(request: Request) -> JSONResponse:
        body = await request.json()
        name = str(body.get("engine") or "")
        ref_id = str(body.get("reference") or "")
        raw_name = str(body.get("name") or "").strip()
        # Filename only, no traversal, and no surprises in voices/.
        stem = "".join(
            c for c in raw_name if c.isalnum() or c in ("-", "_")
        ).strip("-_")
        if not stem:
            return JSONResponse({"error": "name must be alphanumeric"})
        ref = _references.get(ref_id)
        if ref is None or not ref.exists():
            return JSONResponse({"error": "no reference to save"})

        if _saves_as(name) == "safetensors":
            try:
                engine = _engine(name)
                state = engine.voice_from_reference(ref)
                dest = VOICES_DIR / f"{stem}.safetensors"
                # export_voice is a staticmethod on the *service*, not on
                # the adapter wrapping it. This is the call the deleted Qt
                # dialog used to make, and the only reason the exporter is
                # still in the service at all.
                engine.service.export_voice(state, dest)
            except Exception as exc:
                return JSONResponse(
                    {"error": f"export failed: {type(exc).__name__}: {exc}"}
                )
            if not dest.exists():
                return JSONResponse(
                    {
                        "error": (
                            "export_voice wrote nothing -- the installed "
                            "pocket-tts may not expose the exporter"
                        )
                    }
                )
        else:
            dest = VOICES_DIR / f"{stem}.wav"
            shutil.copy2(ref, dest)
        return JSONResponse(
            {
                "path": str(dest.relative_to(REPO_ROOT)),
                "kb": dest.stat().st_size / 1024.0,
            }
        )

    return app


def _availability(name: str) -> bool:
    """Can this engine load without a download or an install?

    Cheap check only -- whether the venv exists -- because the honest
    answer needs a model load, and loading every candidate to render a
    dropdown would cost a minute.
    """
    try:
        engine = adapters.REGISTRY[name]()
    except Exception:
        return False
    if isinstance(engine, remote.Remote):
        from tools.tts_lab.envs import ENGINES

        env = ENGINES.get(engine.env_name)
        return bool(env and env.installed)
    return True


def _store_reference(audio: np.ndarray, sample_rate: int) -> dict:
    from tools.tts_lab.voicebank import _normalise, _trim_silence

    trimmed = _trim_silence(audio, sample_rate)
    if trimmed.size < sample_rate // 2:
        return {"error": "clip is under half a second of audible audio"}
    clip = _normalise(trimmed)
    quality = assess(clip, sample_rate)
    ref_id = uuid.uuid4().hex[:12]
    path = write_wav(WORK_DIR / f"ref_{ref_id}.wav", clip, sample_rate)
    _references[ref_id] = path
    return {
        "id": ref_id,
        "file": path.name,
        "sample_rate": sample_rate,
        "quality": asdict(quality),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6280)
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    import uvicorn

    url = f"http://{args.host}:{args.port}/"
    print(f"voice studio on {url}")
    print("record 20-30s, clone into any installed engine, save to voices/")
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        build_app(), host=args.host, port=args.port, log_level="warning"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
