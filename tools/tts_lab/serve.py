"""A local voice studio: pick clips, build a reference, clone, audition.

Rebuilds the voice-cloning dialog that died with the Qt app. The service
side of it never went away -- ``PocketTtsService.get_model()`` and
``export_voice()`` are still there, hanging off nothing since the dialog
was deleted -- so this is mostly wiring plus a page.

Microphone capture was removed rather than kept as an option. Her voice
is not something anyone here can perform, so recording could only ever
produce a *different* voice, and the real source material is a folder of
found recordings. Offering a mic button alongside that implied a choice
where there was none, and the raw-PCM capture path existed only to serve
it. Clips arrive from ``voices/`` or by upload; both land in the same
pool and the only route to a voice is to select from it.

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
import json
import shutil
import sys
import threading
import uuid
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tools.tts_lab import adapters, labeled, refset, remote
from tools.tts_lab.adapters import REPO_ROOT, assess, read_audio, write_wav
from tools.tts_lab.page import INDEX_HTML

VOICES_DIR = REPO_ROOT / "voices"
WORK_DIR = VOICES_DIR / "studio"

#: Subtrees of ``voices/`` that hold generated output rather than voices.
#: Without this the picker lists every bench render and every intermediate
#: from ``voicebank.py`` -- forty-odd rows to find the three real ones,
#: which is the same as not having a picker.
#:
#: ``sounds/`` is source material, not voices: it is where found packs go,
#: and this machine's two hold 98 clips between them. A voice built from
#: them is a *reference set*, and that is what belongs in the picker.
_SCRATCH = ("studio/", "datasets/", "audition/", "speed_ab/", "sounds/")


def _is_scratch(rel: str) -> bool:
    """Is this path output or source material rather than a voice?

    The ``parts/`` rule is a segment match rather than a prefix one. It
    used to be the literal ``reference/parts/``, which was correct for
    exactly one reference; every reference set built since has its own
    ``<name>/parts/`` holding a dozen fragments, and prefix matching
    would have put all of them in the dropdown as though each were a
    voice. Same failure as the 279-entry list, one directory later.
    """
    if any(rel.startswith(p) for p in _SCRATCH) or "roundtrip" in rel:
        return True
    return "parts" in Path(rel).parent.parts

#: What the pace check speaks. Fixed, because the number is only
#: comparable between references if the words are identical, and long
#: enough that one syllable of estimation error is not a verdict.
PACE_PROBE = "I was just thinking about you, and I wondered how the build went."

#: Engines are expensive to load (2 s for pocket-tts, up to 37 s for a
#: cold Chatterbox), so they are kept alive across requests. Guarded
#: because the engines themselves are not reentrant -- pocket-tts
#: serialises generation on its own lock and the sidecars are a single
#: pipe each.
_engines: dict[str, adapters.Adapter] = {}
_engine_lock = threading.Lock()
#: reference id -> wav path
_references: dict[str, Path] = {}
#: reference id -> the directory holding the wav, its ``parts/`` and its
#: ``manifest.json``. Tracked separately because saving a *set* has to
#: copy all three: the app reads the manifest beside the reference to
#: find her tempo target, and a bare wav silently loses it.
_refsets: dict[str, Path] = {}
#: dataset clip id -> (decoded wav, original filename). Separate from
#: ``_references`` because these are training material, not conditioning
#: clips: dozens to hundreds of them, each carrying a transcript, and
#: none of them normalised individually (see ``normalise_set``).
_clips: dict[str, tuple[Path, str]] = {}


def _engine(name: str) -> adapters.Adapter:
    """Get or build an engine, by name, from any endpoint.

    Registers the sidecar-hosted engines first. They used to be
    registered only by ``/api/engines``, which made every other endpoint
    quietly depend on the page having rendered its dropdown: a client
    that posted before fetching the engine list was told
    ``unknown engine 'chatterbox-nano'``, naming the engine it had just
    been offered. Ordering like that survives every manual test, because
    a browser always loads the list first.
    """
    remote.register()
    with _engine_lock:
        existing = _engines.get(name)
        if existing is not None:
            return existing
        if name not in adapters.REGISTRY:
            # ``adapters.build`` raises SystemExit for an unknown name,
            # which is right for the CLI it was written for and wrong
            # here: SystemExit is not an Exception, so it walks past
            # every handler in this file and becomes a 500 with no
            # message. Checked rather than caught.
            raise ValueError(
                f"unknown engine {name!r}; have: "
                f"{', '.join(adapters.available())}"
            )
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
        remote.register()  # idempotent; also done in _engine
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
            if _is_scratch(rel):
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

    # ── reference sets ──

    @app.get("/api/clips/folders")
    def api_clip_folders() -> dict:
        return {"folders": refset.folders(), "upload_rel": refset.UPLOAD_REL}

    @app.get("/api/clips")
    def api_clips(dir: str = "") -> JSONResponse:
        try:
            clips = refset.scan(dir)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)})
        return JSONResponse(
            {
                "dir": dir,
                "clips": [c.to_dict() for c in clips],
                "suggested": refset.rank(clips),
                "decoder_window_s": refset.DEC_WINDOW_S,
                "encoder_window_s": refset.ENC_WINDOW_S,
            }
        )

    @app.post("/api/clips/upload")
    async def api_clip_upload(request: Request) -> JSONResponse:
        """Take source audio in whatever shape it already exists.

        Decoded here rather than trusted, and written into the scratch
        tree as a wav so it joins the same pool as everything else --
        there is exactly one route to a reference, and it starts with
        picking clips.
        """
        raw = await request.body()
        name = Path(request.query_params.get("name") or "clip").name
        suffix = _safe_suffix(request.query_params.get("ext") or "")
        tmp = WORK_DIR / f"upload_{uuid.uuid4().hex[:8]}{suffix}"
        tmp.write_bytes(raw)
        try:
            audio, sample_rate = read_audio(tmp)
        except Exception as exc:
            return JSONResponse(
                {
                    "error": (
                        f"could not decode {name}: {exc}. Supported: wav, "
                        "mp3, flac, ogg."
                    )
                }
            )
        finally:
            tmp.unlink(missing_ok=True)
        stem = "".join(
            c for c in Path(name).stem if c.isalnum() or c in ("-", "_", " ")
        ).strip() or "clip"
        dest = VOICES_DIR / refset.UPLOAD_REL / f"{stem}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_wav(dest, audio, sample_rate)
        return JSONResponse(
            {
                "rel": dest.relative_to(VOICES_DIR).as_posix(),
                "dir": refset.UPLOAD_REL,
            }
        )

    @app.post("/api/reference/build")
    async def api_reference_build(request: Request) -> JSONResponse:
        """Concatenate an ordered selection into one conditioning clip."""
        body = await request.json()
        parts = [
            refset.Part(
                rel=str(row.get("rel") or ""),
                phrase=str(row.get("phrase") or "").strip(),
            )
            for row in body.get("parts") or []
            if str(row.get("rel") or "")
        ]
        if not parts:
            return JSONResponse({"error": "select at least one clip"})
        ref_id = uuid.uuid4().hex[:12]
        out_dir = WORK_DIR / f"refset_{ref_id}"
        try:
            manifest = refset.build(
                parts,
                out_dir,
                gap_ms=int(body.get("gap_ms") or 220),
                target_syl_s=float(body.get("target_syl_s") or 0.0),
            )
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
        ref_path = out_dir / str(manifest["reference"])
        _references[ref_id] = ref_path
        _refsets[ref_id] = out_dir
        try:
            targets = refset.app_targets(out_dir, manifest)
        except Exception as exc:
            targets = {"error": str(exc)}
        audio, rate = read_audio(ref_path)
        return JSONResponse(
            {
                "id": ref_id,
                # Served from the work dir by name, so the player needs
                # the path relative to it rather than a bare filename.
                "file": f"refset_{ref_id}/{ref_path.name}",
                "sample_rate": manifest["sample_rate"],
                "quality": asdict(assess(audio, rate)),
                "bandwidth_hz": round(refset.bandwidth_hz(audio, rate), 1),
                "manifest": manifest,
                "windows": refset.windows(manifest),
                "shape": refset.shape(manifest),
                "targets": targets,
                "min_rate_parts": refset.MIN_RATE_PARTS,
            }
        )

    @app.post("/api/pace")
    async def api_pace(request: Request) -> JSONResponse:
        """Speak a probe sentence and measure how fast it came out.

        The one check a listener cannot do by ear on a reference clip and
        can do instantly on a generated one -- by which point the
        reference is saved and in use. Chatterbox clones pacing along
        with timbre, so a reference of drawled single words yields a
        clone that drawls, and it is a property of the *selection* rather
        than of anything downstream. Worth one synthesis to find out
        before saving.
        """
        from app.audio.speech_rate import (
            DEFAULT_TARGET_SYL_S,
            MAX_CORRECTION,
            measured_rate,
        )
        from tools.tts_lab.adapters import read_wav

        body = await request.json()
        name = str(body.get("engine") or "")
        try:
            engine = _engine(name)
            source = _resolve_voice_path(body)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
        try:
            with _engine_lock:
                voice = _load_voice(engine, source)
                result = engine.synth(PACE_PROBE, voice)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
        out = WORK_DIR / f"pace_{uuid.uuid4().hex[:8]}.wav"
        write_wav(out, result.audio, result.sample_rate)
        audio, rate = read_wav(out)
        delivered = measured_rate(audio, rate, PACE_PROBE)
        if delivered <= 0.0:
            return JSONResponse(
                {"error": "could not measure that clip", "file": out.name}
            )
        wanted = DEFAULT_TARGET_SYL_S / delivered
        return JSONResponse(
            {
                "file": out.name,
                "text": PACE_PROBE,
                "delivered_syl_s": round(delivered, 2),
                "her_pace_syl_s": DEFAULT_TARGET_SYL_S,
                "needs": round(wanted, 3),
                # The app corrects toward her pace but only within this,
                # and only when the manifest gave it a target at all --
                # so a reference needing more than the cap is slow for
                # good, whatever the settings say.
                "app_limit": MAX_CORRECTION,
                "fixable_in_app": abs(wanted - 1.0) <= MAX_CORRECTION,
            }
        )

    @app.get("/api/audio/{name:path}")
    def api_audio(name: str) -> Response:
        # Resolved under the work dir and checked to still be inside it.
        # A path rather than a bare name because a built reference set is
        # a directory, so its player needs one level of nesting -- but
        # the containment check is what makes that safe, not the shape.
        path = (WORK_DIR / name).resolve()
        if not path.is_relative_to(WORK_DIR.resolve()) or not path.is_file():
            return Response(status_code=404)
        return Response(path.read_bytes(), media_type="audio/wav")

    @app.get("/api/clip/{rel:path}")
    def api_clip(rel: str) -> Response:
        """Audition one source clip, straight out of ``voices/``.

        Decoded on the way out so the browser gets a wav whatever the
        source format was: a picker you cannot listen to is a list of
        filenames, and choosing ten seconds out of seventy clips by name
        is not choosing.
        """
        try:
            audio, rate = read_audio(refset.resolve_clip(rel))
        except Exception:
            return Response(status_code=404)
        tmp = WORK_DIR / f"aud_{uuid.uuid4().hex[:8]}.wav"
        try:
            write_wav(tmp, audio, rate)
            return Response(tmp.read_bytes(), media_type="audio/wav")
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/knobs")
    async def api_knobs(request: Request) -> JSONResponse:
        """The engine's real ``generate()`` keywords and its own defaults.

        Loads the engine, which is the point: these are read off the
        installed code by the sidecar rather than copied from a model
        card. The docs for this family describe ``exaggeration`` and
        ``cfg_weight`` for the original 500M model and say nothing about
        whether Turbo and Nano kept them -- and Turbo ships 0.0/0.0 where
        every published tip quotes 0.5/0.5. A knob panel built from the
        README would therefore have offered dials that do nothing on the
        one variant fast enough to ship.
        """
        body = await request.json()
        name = str(body.get("engine") or "")
        try:
            engine = _engine(name)
        except Exception as exc:
            return JSONResponse({"error": f"{name} unavailable: {exc}"})
        if not isinstance(engine, remote.Remote):
            return JSONResponse(
                {
                    "accepts": [],
                    "defaults": {},
                    "note": (
                        f"{name} runs in-process and takes its settings at "
                        "load time, so there are no per-call knobs"
                    ),
                }
            )
        return JSONResponse(
            {
                "accepts": list(engine.accepts),
                "defaults": dict(engine.defaults),
                "languages": list(engine.languages),
                "runtime": dict(engine.runtime),
            }
        )

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
            source_dir = _refsets.get(ref_id)
            if source_dir is not None and source_dir.is_dir():
                # A built set is saved whole. Copying only the wav would
                # look like it worked and quietly cost her tempo target:
                # the app reads ``manifest.json`` *beside* the reference
                # and the part wavs under it, and falls back to no
                # correction when either is missing.
                dest_dir = VOICES_DIR / stem
                dest_dir.mkdir(parents=True, exist_ok=True)
                for stale in (dest_dir / "parts").glob("part*.wav"):
                    stale.unlink()
                manifest_path = dest_dir / "manifest.json"
                # Tuning already saved against this voice, rescued across
                # the copy below. The build's own manifest has no
                # ``generate`` block, so copying it over the destination
                # wipes whatever earlier engines contributed -- which is
                # exactly the case per-engine keying exists to support,
                # and it survived a unit test of the merge because the
                # merge was never the part that was wrong.
                carried = _tuned_knobs(manifest_path)
                shutil.copytree(source_dir, dest_dir, dirs_exist_ok=True)
                dest = dest_dir / ref.name
                voice_id = dest.relative_to(VOICES_DIR).as_posix()
                for engine_key, knobs in carried.items():
                    _record_knobs(manifest_path, engine_key, knobs)
                tuned = _record_knobs(manifest_path, name, body.get("kwargs"))
                stored = _tuned_engines(manifest_path)
                return JSONResponse(
                    {
                        "path": str(dest_dir.relative_to(REPO_ROOT)),
                        "voice_id": voice_id,
                        "kb": dest.stat().st_size / 1024.0,
                        "parts": len(list((dest_dir / "parts").glob("*.wav"))),
                        "tuned": tuned,
                        "engines_tuned": stored,
                    }
                )
            dest = VOICES_DIR / f"{stem}.wav"
            shutil.copy2(ref, dest)
        return JSONResponse(
            {
                "path": str(dest.relative_to(REPO_ROOT)),
                "voice_id": dest.relative_to(VOICES_DIR).as_posix(),
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
            raise ValueError("that reference has expired -- build it again")
        return ref

    saved = str(body.get("voice") or "").strip()
    if saved:
        # Resolved under voices/ and checked to still be inside it, so a
        # crafted name cannot read elsewhere on disk.
        path = (VOICES_DIR / saved).resolve()
        if not path.is_relative_to(VOICES_DIR.resolve()) or not path.exists():
            raise ValueError(f"no saved voice named {saved!r}")
        return path

    raise ValueError("pick a saved voice, or build a reference from clips")


def _record_knobs(
    manifest: Path, engine: str, kwargs: Any
) -> dict[str, float]:
    """Write the audition's knob overrides into the saved manifest.

    The point of tuning them. Until this existed the app sent no
    generation kwargs at all, so every voice spoke on its engine's shipped
    defaults and a value found here -- a colder temperature that clears a
    reproducible artifact, say -- could not be carried anywhere. Stored
    beside the tempo and brightness targets, which travel with the voice
    for the same reason.

    **Keyed by engine, and merged rather than replaced.** One reference
    gets auditioned on several engines in a sitting, which is the whole
    point of having them side by side, and each needs its own numbers:
    these are absolute values chosen against defaults that are not
    shared, so Nano's ``min_p=0.05`` is a real intervention where the
    full model already ships it. A single block per voice would have let
    the second save quietly discard the first engine's afternoon.
    """
    if not isinstance(kwargs, dict) or not manifest.is_file():
        return {}
    tuned = {
        str(k): float(v)
        for k, v in kwargs.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    try:
        body = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    block = body.get("generate")
    if not isinstance(block, dict):
        block = {}
    if tuned:
        block[engine] = tuned
    else:
        # An emptied panel means "back to this engine's defaults", and
        # leaving the old entry behind would make that unsayable.
        block.pop(engine, None)
    if block:
        body["generate"] = block
    else:
        body.pop("generate", None)
    manifest.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return tuned


def _tuned_knobs(manifest: Path) -> dict[str, dict[str, float]]:
    """The whole per-engine ``generate`` block, or nothing."""
    try:
        body = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    block = body.get("generate")
    if not isinstance(block, dict):
        return {}
    return {
        str(engine): dict(knobs)
        for engine, knobs in block.items()
        if isinstance(knobs, dict) and knobs
    }


def _tuned_engines(manifest: Path) -> list[str]:
    """Every engine this voice carries knobs for, for the save message."""
    return sorted(_tuned_knobs(manifest))


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6280)
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    import uvicorn

    url = f"http://{args.host}:{args.port}/"
    print(f"voice studio on {url}")
    print("pick clips, build a reference, clone it, save to voices/")
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        build_app(), host=args.host, port=args.port, log_level="warning"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
