"""P29 -- process-memory observability.

"Why is the server using 6 GB?" used to be answerable only by reading
code and guessing. This module turns it into one call: total resident
memory, every child process with its command line, and a best-effort
per-subsystem attribution for the things that are actually big (STT
weights, TTS weights, the in-memory memory mirror, the LanceDB files,
the embedder's LRU).

Two deliberate design notes:

- **Attribution is not measurement.** Python gives no per-object RSS, so
  the subsystem rows report *what is loaded* plus a size estimate from
  row counts and vector widths. Trust ``process.rss_mb`` as the ground
  truth and the rows as "which of these is plausibly responsible".
- **Children matter more than they look.** On Windows RealtimeSTT runs
  its transcription worker in a separate interpreter, so the Whisper
  weights are largely *not* in the parent's RSS. A breakdown that only
  looked at ``self`` would report a suspiciously small number and send
  the next investigation down the wrong path.

``psutil`` is optional: without it the subsystem rows still work and
only the process/children numbers degrade.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")

try:  # optional dependency -- see module docstring
    import psutil
except Exception:  # pragma: no cover - exercised by the no-psutil test
    psutil = None  # type: ignore[assignment]

_MB = 1024.0 * 1024.0
_CMDLINE_CHARS = 240


def _mb(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / _MB, 1)


def _dir_bytes(path: Path) -> tuple[int, int]:
    """Return ``(total_bytes, file_count)`` for a directory tree."""
    total = 0
    files = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
                    files += 1
            except OSError:
                continue
    except OSError:
        pass
    return total, files


def process_snapshot() -> dict[str, Any]:
    """Resident memory for this process, plus every descendant."""
    out: dict[str, Any] = {"pid": os.getpid(), "executable": sys.executable}
    if psutil is None:
        out["psutil"] = False
        out["note"] = (
            "psutil not installed -- process numbers unavailable; "
            "install it (pip install psutil) for RSS + child enumeration"
        )
        return out
    out["psutil"] = True
    try:
        me = psutil.Process()
        info = me.memory_info()
        out["rss_mb"] = _mb(getattr(info, "rss", None))
        out["vms_mb"] = _mb(getattr(info, "vms", None))
        out["threads"] = me.num_threads()
        out["create_time"] = me.create_time()
    except Exception as exc:
        out["error"] = f"self inspection failed: {exc}"
        return out

    children: list[dict[str, Any]] = []
    total_child_rss = 0.0
    try:
        for child in me.children(recursive=True):
            row: dict[str, Any] = {"pid": child.pid}
            # Each accessor can raise independently once a child exits
            # mid-walk, so they're probed one at a time rather than in
            # one try block that would drop the whole row.
            for key, fn in (
                ("name", child.name),
                ("status", child.status),
            ):
                try:
                    row[key] = fn()
                except Exception:
                    row[key] = None
            try:
                rss = child.memory_info().rss
                row["rss_mb"] = _mb(rss)
                total_child_rss += float(rss)
            except Exception:
                row["rss_mb"] = None
            try:
                cmd = " ".join(child.cmdline())
            except Exception:
                cmd = ""
            row["cmdline"] = cmd[:_CMDLINE_CHARS]
            row["python"] = "python" in (row.get("name") or "").lower()
            children.append(row)
    except Exception as exc:
        out["children_error"] = str(exc)
    out["children"] = children
    out["children_count"] = len(children)
    out["children_rss_mb"] = _mb(total_child_rss)
    if out.get("rss_mb") is not None:
        out["tree_rss_mb"] = round(
            float(out["rss_mb"]) + float(out.get("children_rss_mb") or 0.0), 1,
        )
    return out


def stt_snapshot(session: "SessionController") -> dict[str, Any]:
    stt = getattr(session, "_realtime_stt", None)
    settings = getattr(getattr(session, "_settings", None), "stt", None)
    out: dict[str, Any] = {
        "service_constructed": stt is not None,
        "enabled_setting": bool(getattr(settings, "enabled", True)),
        "configured_model": getattr(settings, "model", None),
        "configured_device": getattr(settings, "device", None),
        "configured_compute_type": getattr(settings, "compute_type", None),
    }
    if stt is None:
        return out
    out["weights_loaded"] = getattr(stt, "is_loaded", None)
    out["loaded_model"] = getattr(stt, "_loaded_model", "") or None
    out["loaded_device"] = getattr(stt, "_loaded_device", "") or None
    out["last_error"] = getattr(stt, "_last_error", None)
    out["note"] = (
        "on Windows the weights live in RealtimeSTT's transcription "
        "child process -- look for it under process.children"
    )
    return out


def tts_snapshot(session: "SessionController") -> dict[str, Any]:
    # ``_tts`` is the queue; ``_tts_engine`` is the thing holding weights.
    engine = getattr(session, "_tts_engine", None)
    settings = getattr(getattr(session, "_settings", None), "tts", None)
    out: dict[str, Any] = {
        "engine_constructed": engine is not None,
        "enabled_setting": bool(getattr(settings, "enabled", True)),
        "provider": getattr(settings, "provider", None),
        "voice": getattr(settings, "voice", None),
    }
    if engine is None:
        return out
    out["engine_class"] = type(engine).__name__
    # P28: a null engine means the PyTorch import was skipped entirely,
    # which is a bigger win than merely having no model loaded -- worth
    # distinguishing in the report, since both show weights_loaded=false.
    out["torch_runtime_avoided"] = bool(getattr(engine, "is_null_engine", False))
    out["weights_loaded"] = getattr(engine, "_model", None) is not None
    cache = getattr(engine, "_audio_cache", None)
    if isinstance(cache, dict):
        out["audio_cache_entries"] = len(cache)
    return out


def mirror_snapshot(session: "SessionController") -> dict[str, Any]:
    store = getattr(session, "_memory_store", None)
    if store is None:
        return {"attached": False}
    mirror = getattr(store, "_mirror", None)
    if not isinstance(mirror, dict):
        return {"attached": True, "rows": None}
    # One locked pass: row count, how many carry a vector, and the
    # vector width (assumed uniform -- a dim change rebuilds the table).
    rows = 0
    with_vector = 0
    dim = 0
    vector_bytes = 0
    content_chars = 0
    try:
        lock = getattr(store, "_lock", None)
        snapshot = None
        if lock is not None:
            with lock:
                snapshot = list(mirror.values())
        else:
            snapshot = list(mirror.values())
        for mem in snapshot:
            rows += 1
            content_chars += len(getattr(mem, "content", "") or "")
            emb = getattr(mem, "embedding", None)
            if emb is None:
                continue
            with_vector += 1
            nbytes = getattr(emb, "nbytes", None)
            if nbytes is not None:
                vector_bytes += int(nbytes)
            if not dim:
                try:
                    dim = int(getattr(emb, "shape", (0,))[0])
                except Exception:
                    dim = 0
    except Exception as exc:
        return {"attached": True, "error": str(exc)}
    return {
        "attached": True,
        "rows": rows,
        "rows_with_vector": with_vector,
        "vector_dim": dim or None,
        "vector_mb": _mb(vector_bytes),
        "content_mb": _mb(content_chars),
        "cap_max_memories": getattr(store, "_max", None),
        "tier_caps": dict(getattr(store, "_tier_caps", {}) or {}),
        "note": (
            "the effective ceiling is the sum of the per-tier caps, not "
            "max_memories alone (see perf backlog P30)"
        ),
    }


def lancedb_snapshot(session: "SessionController") -> dict[str, Any]:
    rag = getattr(session, "_rag_store", None)
    raw = getattr(rag, "_root", None)
    if raw is None:
        raw = Path("data") / "lancedb"
    path = Path(str(raw))
    if not path.exists():
        return {"path": str(path), "exists": False}
    total, files = _dir_bytes(path)
    tables: dict[str, float | None] = {}
    try:
        for child in sorted(path.iterdir()):
            if child.is_dir():
                sub_total, _ = _dir_bytes(child)
                tables[child.name] = _mb(sub_total)
    except OSError:
        pass
    return {
        "path": str(path),
        "exists": True,
        "on_disk_mb": _mb(total),
        "files": files,
        "tables_mb": tables,
        "note": "on disk, not resident -- Lance memory-maps on demand",
    }


def embedder_snapshot(session: "SessionController") -> dict[str, Any]:
    embedder = getattr(session, "_embedder", None)
    if embedder is None:
        return {"attached": False}
    cache = getattr(embedder, "_cache", None)
    entries = len(cache) if hasattr(cache, "__len__") else None
    return {
        "attached": True,
        "model": getattr(embedder, "_model", None),
        "cache_entries": entries,
        "cache_capacity": getattr(embedder, "_cache_size", None),
    }


def build_breakdown(session: "SessionController") -> dict[str, Any]:
    """Assemble the full report. Every section fails independently."""
    out: dict[str, Any] = {}
    for key, fn in (
        ("process", lambda: process_snapshot()),
        ("stt", lambda: stt_snapshot(session)),
        ("tts", lambda: tts_snapshot(session)),
        ("memory_mirror", lambda: mirror_snapshot(session)),
        ("lancedb", lambda: lancedb_snapshot(session)),
        ("embedder", lambda: embedder_snapshot(session)),
    ):
        try:
            out[key] = fn()
        except Exception as exc:
            out[key] = {"error": str(exc)}
    out["reading_guide"] = (
        "process.tree_rss_mb is the number Task Manager shows for the "
        "whole tree. If it is large and stt.weights_loaded is true, STT "
        "is the first suspect (P27); if tts.weights_loaded is true while "
        "tts.enabled_setting is false, that is the P28 bug. The LLM "
        "context window is NOT in these numbers -- it lives in Ollama."
    )
    return out


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_memory_breakdown() -> str:
        """P29 -- where the process's resident memory actually went.

        Returns total RSS for this process, every child process with its
        command line and RSS (the RealtimeSTT transcription child is the
        usual explanation for a second large ``python.exe``), and a
        per-subsystem attribution: whether the STT and TTS weights are
        loaded and which models, the in-memory memory mirror's row count
        and vector bytes, the LanceDB on-disk size per table, and the
        embedder's LRU occupancy.

        First stop for "why is the server this big?". Note the subsystem
        rows are estimates from row counts and vector widths, not real
        per-object measurements -- ``process.rss_mb`` is the only hard
        number here. Requires ``psutil`` for the process section; the
        rest works without it.
        """
        try:
            return json.dumps(build_breakdown(session), indent=2, default=str)
        except Exception as exc:
            return f"get_memory_breakdown failed: {exc}"
