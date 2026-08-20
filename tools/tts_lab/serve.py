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
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tools.tts_lab import adapters, labeled, remote
from tools.tts_lab.adapters import REPO_ROOT, assess, read_audio, write_wav
from tools.tts_lab.page import INDEX_HTML
from tools.tts_lab.voicebank import PHRASES

VOICES_DIR = REPO_ROOT / "voices"
WORK_DIR = VOICES_DIR / "studio"

#: Subtrees of ``voices/`` that hold generated output rather than voices.
#: Without this the picker lists every bench render and every intermediate
#: from ``voicebank.py`` -- forty-odd rows to find the three real ones,
#: which is the same as not having a picker.
_SCRATCH = ("studio/", "datasets/", "audition/", "reference/parts/")

#: Engines are expensive to load (2 s for pocket-tts, up to 37 s for a
#: cold Chatterbox), so they are kept alive across requests. Guarded
#: because the engines themselves are not reentrant -- pocket-tts
#: serialises generation on its own lock and the sidecars are a single
#: pipe each.
_engines: dict[str, adapters.Adapter] = {}
_engine_lock = threading.Lock()
#: reference id -> wav path
_references: dict[str, Path] = {}
#: dataset clip id -> (decoded wav, original filename). Separate from
#: ``_references`` because these are training material, not conditioning
#: clips: dozens to hundreds of them, each carrying a transcript, and
#: none of them normalised individually (see ``normalise_set``).
_clips: dict[str, tuple[Path, str]] = {}


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
        """Saved voices, including the committed reference clip.

        Recursive on purpose. The one portable copy of Aiko's voice lives
        in ``voices/reference/``, and a flat glob left it invisible here
        -- so the studio offered no way to audition the voice it exists
        to protect without first re-recording something.
        """
        rows = []
        for path in sorted(VOICES_DIR.rglob("*")):
            if path.is_dir() or path.suffix not in (
                ".safetensors", ".wav", ".mp3", ".flac"
            ):
                continue
            rel = path.relative_to(VOICES_DIR).as_posix()
            if any(rel.startswith(p) for p in _SCRATCH) or "roundtrip" in rel:
                continue
            rows.append(
                {
                    "name": rel,
                    "kb": path.stat().st_size / 1024.0,
                    # A .safetensors is a pocket-tts speaker state and
                    # means nothing to an engine that clones from audio.
                    "audio": path.suffix != ".safetensors",
                }
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
        """Any format libsndfile reads -- mp3, flac, ogg, wav.

        Worth taking the original source material in whatever shape it
        already exists: for a voice that was cloned from mp3s, those mp3s
        are a generation closer to the truth than anything the current
        engine can regenerate.
        """
        raw = await request.body()
        # Suffix preserved where the browser sent one, since libsndfile
        # sniffs content but is happier with a hint.
        suffix = _safe_suffix(request.query_params.get("ext") or "")
        tmp = WORK_DIR / f"upload_{uuid.uuid4().hex[:8]}{suffix}"
        tmp.write_bytes(raw)
        try:
            audio, sample_rate = read_audio(tmp)
        except Exception as exc:
            return JSONResponse(
                {
                    "error": (
                        f"could not decode: {exc}. Supported: wav, mp3, "
                        "flac, ogg."
                    )
                }
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
        text = str(body.get("text") or "").strip()
        kwargs = body.get("kwargs") or {}
        if not text:
            return JSONResponse({"error": "nothing to say"})
        try:
            engine = _engine(name)
        except Exception as exc:
            return JSONResponse({"error": f"{name} unavailable: {exc}"})
        try:
            source = _resolve_voice_path(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)})
        try:
            with _engine_lock:
                if isinstance(engine, remote.Remote):
                    engine.overrides = dict(kwargs)
                voice = _load_voice(engine, source)
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
                state = _load_voice(engine, ref)
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

    # ── dataset panel ──

    @app.get("/api/dataset/asr")
    def api_dataset_asr() -> dict:
        from tools.tts_lab import transcribe

        t = transcribe.shared()
        return {
            "available": t.available,
            "model": t.model_name,
            "cached": transcribe.cached_models(),
        }

    @app.post("/api/dataset/add")
    async def api_dataset_add(request: Request) -> JSONResponse:
        """One training clip. Decoded, measured, kept as-is otherwise.

        Deliberately *not* run through ``_store_reference``: that trims
        and peak-normalises, which is right for a conditioning clip and
        wrong for training material. Level differences between a whisper
        and an exclamation are part of what a fine-tune should learn, so
        gain is applied once across the whole set at save time instead.
        """
        raw = await request.body()
        source = request.query_params.get("name") or "clip"
        ext = request.query_params.get("ext") or ""
        suffix = _safe_suffix(ext)
        tmp = WORK_DIR / f"in_{uuid.uuid4().hex[:8]}{suffix}"
        tmp.write_bytes(raw)
        try:
            audio, sample_rate = read_audio(tmp)
        except Exception as exc:
            return JSONResponse({"error": f"could not decode {source}: {exc}"})
        finally:
            tmp.unlink(missing_ok=True)

        clip_id = uuid.uuid4().hex[:12]
        path = write_wav(WORK_DIR / f"clip_{clip_id}.wav", audio, sample_rate)
        _clips[clip_id] = (path, Path(source).name)
        quality = assess(audio, sample_rate)
        notes = list(quality.warnings)
        if quality.duration_s > labeled.MAX_SECONDS:
            notes.append(
                f"{quality.duration_s:.0f}s is too long -- most trainers "
                f"window at {labeled.MAX_SECONDS:.0f}s, so cut it up"
            )
        elif quality.duration_s < labeled.MIN_SECONDS:
            notes.append("under a second, too short to learn prosody from")
        return JSONResponse(
            {
                "id": clip_id,
                "file": path.name,
                "source": Path(source).name,
                "sample_rate": sample_rate,
                "quality": asdict(quality),
                "notes": notes,
            }
        )

    @app.post("/api/dataset/transcribe")
    async def api_dataset_transcribe(request: Request) -> JSONResponse:
        """Draft one clip's transcript. One per call, so the page can
        show progress and the operator can stop a long run early."""
        body = await request.json()
        entry = _clips.get(str(body.get("id") or ""))
        if entry is None:
            return JSONResponse({"error": "unknown clip"})
        from tools.tts_lab.transcribe import shared

        transcriber = shared()
        if not transcriber.available:
            return JSONResponse(
                {
                    "error": (
                        "no faster-whisper model in the HuggingFace cache; "
                        "transcription would need a download"
                    )
                }
            )
        try:
            with _engine_lock:
                # Shares the lock with synthesis on purpose: both are
                # CPU-saturating, and letting them overlap would make
                # every latency number in the studio meaningless.
                line = transcriber.transcribe(entry[0])
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
        return JSONResponse(line.to_dict())

    @app.post("/api/dataset/save")
    async def api_dataset_save(request: Request) -> JSONResponse:
        body = await request.json()
        raw_name = str(body.get("name") or "").strip()
        stem = "".join(
            c for c in raw_name if c.isalnum() or c in ("-", "_")
        ).strip("-_")
        speaker = "".join(
            c for c in str(body.get("speaker") or "aiko")
            if c.isalnum() or c in ("-", "_")
        ) or "aiko"
        items = []
        for row in body.get("items") or []:
            entry = _clips.get(str(row.get("id") or ""))
            text = str(row.get("text") or "").strip()
            if entry is None or not text:
                continue
            items.append(labeled.Item(entry[0], text))
        if not items:
            return JSONResponse({"error": "nothing with both audio and text"})

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        out_dir = labeled.OUT_ROOT / (stem or f"{speaker}-labelled-{stamp}")
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            result, rate_info = labeled.build(items, out_dir)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
        if not result.samples:
            return JSONResponse(
                {
                    "error": "every clip was rejected",
                    "rejects": [
                        {"file": r.text, "reason": r.reason}
                        for r in result.rejects
                    ],
                }
            )
        gain = labeled.normalise_set(result, out_dir)
        labeled.write_manifests(result, out_dir, speaker=speaker)
        report = labeled.write_report(
            result, out_dir, rate_info=rate_info, gain=gain
        )
        return JSONResponse(
            {
                "path": str(out_dir.relative_to(REPO_ROOT)),
                "clips": len(result.samples),
                "minutes": report["audio"]["total_minutes"],
                "sample_rate": result.sample_rate,
                "rate_reason": rate_info["rate_reason"],
                "rejects": [
                    {"file": r.text, "reason": r.reason} for r in result.rejects
                ],
            }
        )

    return app


def _safe_suffix(ext: str) -> str:
    """A filename hint for libsndfile, with nothing of the caller's in it."""
    cleaned = "".join(c for c in ext.lstrip(".").lower() if c.isalnum())[:5]
    return f".{cleaned}" if cleaned else ".bin"


def _resolve_voice_path(body: dict) -> Path:
    """Where the voice for this request comes from.

    Two routes, and having only the first was the bug: the studio
    required a freshly recorded or uploaded reference before it would
    synthesise anything, which made auditioning an already-saved voice
    impossible -- including the committed reference clip that is the only
    portable copy of Aiko's voice. A saved voice is a perfectly good
    starting point, so it is now one.
    """
    ref_id = str(body.get("reference") or "")
    if ref_id:
        ref = _references.get(ref_id)
        if ref is None or not ref.exists():
            raise ValueError("that reference has expired -- upload it again")
        return ref

    saved = str(body.get("voice") or "").strip()
    if saved:
        # Resolved under voices/ and checked to still be inside it, so a
        # crafted name cannot read elsewhere on disk.
        path = (VOICES_DIR / saved).resolve()
        if not path.is_relative_to(VOICES_DIR.resolve()) or not path.exists():
            raise ValueError(f"no saved voice named {saved!r}")
        return path

    raise ValueError("pick a saved voice, or record or upload a reference")


def _load_voice(engine: adapters.Adapter, source: Path) -> Any:
    """Turn a path into an engine voice handle.

    A ``.safetensors`` is a pocket-tts speaker state, not audio, so it
    goes through the named-voice route; everything else is a clip to
    clone from. Engines that cannot read an embedding say so plainly
    rather than failing inside a tensor load.
    """
    if source.suffix == ".safetensors":
        try:
            return engine.voice_from_id(str(source))
        except NotImplementedError:
            raise ValueError(
                f"{engine.caps.name} clones from audio and cannot read a "
                "pocket-tts embedding -- pick a .wav, or make one with "
                "'python -m tools.tts_lab.voicebank'"
            ) from None
    return engine.voice_from_reference(source)


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
