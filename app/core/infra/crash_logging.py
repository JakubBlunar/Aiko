from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import faulthandler
import json
import logging
import os
import sys
import threading
import traceback
from types import TracebackType
from typing import Any

from app.core.infra import native_crash
from app.core.infra.log_context import get_task_id, get_turn_id


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
CRASH_LOG_PATH = DATA_DIR / "crashlog.txt"

LOG_FORMAT = (
    "[%(asctime)s] %(levelname)s [%(name)s turn=%(turn)s task=%(task)s] %(message)s"
)
RING_BUFFER_CAPACITY = 1000

_lock = threading.Lock()
_fault_file = None
_logger: logging.Logger | None = None
_log_file_path: Path | None = None
# P44: the separate prompt-cache JSONL sink; None while it is disabled.
_prompt_cache_log_path: Path | None = None


class _SpamFilter(logging.Filter):
    """Suppress repetitive library errors that cannot be fixed upstream."""

    _SUPPRESSED = ("BrokenPipeError", "pipe has been ended", "poll_connection")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(tok in msg for tok in self._SUPPRESSED)


class _TurnIdFilter(logging.Filter):
    """Stamp every record with ``record.turn`` and ``record.task`` from
    the correlation contextvars.

    The format string references both ``%(turn)s`` and ``%(task)s`` so
    this filter MUST run before the record is emitted, otherwise the
    formatter raises ``KeyError``. We attach it directly to every
    handler we create, and set ``record.turn = "-"`` /
    ``record.task = "-"`` when no correlation id is active so
    unrelated lines (boot, shutdown, scheduler idle) stay clean.

    Name retained for backwards-compatibility — see
    :class:`_CorrelationFilter` alias below for new call sites.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "turn") or not record.turn:  # type: ignore[attr-defined]
            record.turn = get_turn_id() or "-"
        if not hasattr(record, "task") or not record.task:  # type: ignore[attr-defined]
            record.task = get_task_id() or "-"
        return True


_CorrelationFilter = _TurnIdFilter
"""Forward-looking alias. Existing callers (tests, infra) import
``_TurnIdFilter``; new code that touches the filter should reach for
the alias since the filter now handles two correlation ids."""


class _RingBufferHandler(logging.Handler):
    """Thread-safe in-process ring buffer for the most recent log lines.

    Records are stored as ``(level_no, name, turn, message, formatted)``
    tuples so :func:`tail` can filter by level and module substring
    cheaply without re-formatting. ``maxlen`` is fixed at
    :data:`RING_BUFFER_CAPACITY`; older entries fall off the back.
    """

    def __init__(self, capacity: int = RING_BUFFER_CAPACITY) -> None:
        super().__init__()
        self._buffer: deque[tuple[int, str, str, str, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            formatted = self.format(record)
        except Exception:
            formatted = record.getMessage()
        turn = getattr(record, "turn", None) or "-"
        entry = (
            int(record.levelno),
            str(record.name),
            str(turn),
            record.getMessage(),
            formatted,
        )
        with self._lock:
            self._buffer.append(entry)

    def snapshot(self) -> list[tuple[int, str, str, str, str]]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


_RING_HANDLER: _RingBufferHandler | None = None


def _ring_handler() -> _RingBufferHandler:
    """Return (lazily creating) the singleton ring-buffer handler."""
    global _RING_HANDLER
    if _RING_HANDLER is None:
        _RING_HANDLER = _RingBufferHandler()
    return _RING_HANDLER


def _set_log_file_path(path: Path) -> None:
    """Record the active rotating log file (used by :func:`read_log_file`)."""
    global _log_file_path
    _log_file_path = path


def get_log_file_path() -> Path | None:
    """Return the currently configured rotating log file path, if any."""
    return _log_file_path


def configure_logging(level_name: str | None = None) -> None:
    """Configure app logger: stderr handler with level from env LOG_LEVEL or
    argument. Call once at startup.

    For richer setups (rotating file, ring buffer, per-module overrides)
    use :func:`configure_logging_full`. This thin wrapper exists for
    backwards-compatibility with the existing ``__main__`` entrypoint.
    """
    configure_logging_full(level_name=level_name)


def configure_logging_full(
    *,
    level_name: str | None = None,
    module_levels: dict[str, str] | None = None,
    file_enabled: bool = False,
    file_path: str | os.PathLike[str] | None = None,
    file_max_bytes: int = 5 * 1024 * 1024,
    file_backup_count: int = 5,
    prompt_cache_log_enabled: bool = False,
    prompt_cache_log_path: str | os.PathLike[str] | None = None,
    prompt_cache_log_max_bytes: int = 2 * 1024 * 1024,
    prompt_cache_log_backup_count: int = 2,
) -> None:
    """Configure ``app.*`` logging: stderr + optional rotating file + ring buffer.

    Idempotent — clears existing handlers on the ``app`` and root loggers.
    The same formatter (``LOG_FORMAT``) and ``_TurnIdFilter`` are attached
    to every handler so log lines look identical wherever they end up.

    The P44 prompt-cache sink is the one exception: it is a *separate*
    file in a different format, configured last so ``module_levels``
    cannot accidentally re-enable it. See
    :func:`configure_prompt_cache_log`.
    """
    global _logger

    level = _coerce_level(level_name or os.environ.get("LOG_LEVEL"), default=logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)
    turn_filter = _TurnIdFilter()
    spam_filter = _SpamFilter()

    _logger = logging.getLogger("app")
    _logger.setLevel(level)
    _logger.handlers.clear()
    _logger.propagate = False

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(turn_filter)
    _logger.addHandler(stderr_handler)

    # In-process ring buffer always attached: cheap, instant access via MCP.
    ring = _ring_handler()
    ring.setLevel(logging.DEBUG)  # capture even DEBUG; tail() filters on read
    ring.setFormatter(formatter)
    ring.addFilter(turn_filter)
    _logger.addHandler(ring)

    if file_enabled:
        try:
            from logging.handlers import RotatingFileHandler

            resolved = Path(file_path) if file_path else (DATA_DIR / "app.log")
            if not resolved.is_absolute():
                resolved = (DATA_DIR.parent / resolved).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                resolved,
                maxBytes=max(64 * 1024, int(file_max_bytes)),
                backupCount=max(0, int(file_backup_count)),
                encoding="utf-8",
                delay=True,
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(turn_filter)
            _logger.addHandler(file_handler)
            _set_log_file_path(resolved)
        except Exception as exc:  # pragma: no cover - best-effort
            sys.stderr.write(
                f"[crash_logging] file logging disabled: {exc!r}\n"
            )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    root_handler = logging.StreamHandler(sys.stderr)
    root_handler.setLevel(logging.WARNING)
    root_handler.setFormatter(formatter)
    root_handler.addFilter(turn_filter)
    root_handler.addFilter(spam_filter)
    root.addHandler(root_handler)

    for noisy in ("RealtimeSTT", "audio_recorder", "multiprocessing"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    if module_levels:
        for name, lvl in module_levels.items():
            try:
                logging.getLogger(str(name)).setLevel(_coerce_level(str(lvl), default=level))
            except Exception:  # pragma: no cover
                pass

    configure_prompt_cache_log(
        enabled=prompt_cache_log_enabled,
        path=prompt_cache_log_path,
        max_bytes=prompt_cache_log_max_bytes,
        backup_count=prompt_cache_log_backup_count,
    )


def configure_prompt_cache_log(
    *,
    enabled: bool = False,
    path: str | os.PathLike[str] | None = None,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 2,
) -> None:
    """Point the P44 prompt-cache telemetry at its own JSONL file.

    One record per turn would be a meaningful share of ``app.log``, and
    the only consumer is ``scripts/prefix_break_report.py``, so it gets a
    separate file in a machine-readable format instead.

    Two details carry the whole design:

    * ``propagate = False`` — ``app.promptcache`` is a child of ``app``,
      so without this every record would *also* reach the stderr,
      rotating-file and ring-buffer handlers, which is exactly what the
      separate file exists to avoid.
    * a bare ``%(message)s`` formatter, so each line is valid JSON rather
      than JSON wrapped in ``LOG_FORMAT``'s timestamp/level preamble.
      Correlation ids ride *inside* the payload instead (the emitter
      reads ``get_turn_id()`` directly), so records still join back to
      the ``turn=<id>`` lines in ``app.log``.

    When disabled the logger is pinned above ``CRITICAL`` so the
    ``isEnabledFor`` check on the hot path is the only cost.
    """
    global _prompt_cache_log_path

    logger = logging.getLogger("app.promptcache")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best-effort
            pass
    logger.propagate = False
    _prompt_cache_log_path = None

    if not enabled:
        logger.setLevel(logging.CRITICAL + 1)
        return

    try:
        from logging.handlers import RotatingFileHandler

        resolved = Path(path) if path else (DATA_DIR / "prompt-cache.jsonl")
        if not resolved.is_absolute():
            resolved = (DATA_DIR.parent / resolved).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            resolved,
            maxBytes=max(64 * 1024, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        _prompt_cache_log_path = resolved
    except Exception as exc:  # pragma: no cover - best-effort
        logger.setLevel(logging.CRITICAL + 1)
        sys.stderr.write(
            f"[crash_logging] prompt-cache logging disabled: {exc!r}\n"
        )


def get_prompt_cache_log_path() -> Path | None:
    """Return the active prompt-cache JSONL path, if the sink is on."""
    return _prompt_cache_log_path


def _coerce_level(value: str | int | None, *, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        resolved = getattr(logging, value.strip().upper(), None)
        if isinstance(resolved, int):
            return resolved
    return default


def _write_line(entry: dict[str, object]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(entry)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(payload, ensure_ascii=False)
    with _lock:
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _write_native_crash(report: dict[str, Any]) -> None:
    """Persist a native-fault report. Called from a crashing process, so
    it stays minimal and swallows everything."""
    try:
        _write_line(report)
    except Exception:
        return
    try:
        if _logger is not None:
            _logger.error(
                "native crash: %s at %s in %s (thread %s) dump=%s",
                report.get("exception"),
                report.get("address"),
                report.get("module"),
                report.get("thread_id"),
                report.get("minidump") or "(none)",
            )
    except Exception:
        pass


def read_native_crashes(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ``native_crash`` records, newest first.

    Companion to :func:`read_ui_crashes`; backs the ``get_native_crashes``
    MCP tool so a fatal fault can be inspected without opening the file.
    """
    if limit <= 0:
        return []
    try:
        with CRASH_LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        text = line.strip()
        if not text or not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "native_crash":
            out.append(parsed)
            if len(out) >= limit:
                break
    return out


def log_native_runtime_inventory() -> None:
    """Log which OpenMP / BLAS runtimes this process ended up with.

    Call once boot is complete (after the STT/torch imports), so any
    future fatal fault can be read against a known-good or known-bad
    runtime configuration. Duplicate OpenMP runtimes are logged as a
    warning because the combination is undefined behaviour, not merely
    untidy: it produces random native faults that look like bad hardware.
    """
    try:
        from app.core.infra import native_runtimes

        found = native_runtimes.inventory()
    except Exception:
        return
    if not found.openmp and not found.blas:
        return
    target = logging.getLogger("app.native_runtimes")
    if found.hazardous:
        target.warning("%s -- unsupported: expect random native faults", found.describe())
    else:
        target.info("%s", found.describe())


def _stage_to_level(stage: str) -> int:
    if "error" in (stage or "").lower():
        return logging.ERROR
    return logging.INFO


def log_event(stage: str, message: str) -> None:
    stage_text = str(stage)
    message_text = str(message)
    if _logger is not None:
        level = _stage_to_level(stage_text)
        _logger.log(level, "[%s] %s", stage_text, message_text)
    if "error" in stage_text.lower():
        try:
            _write_line(
                {
                    "type": "event",
                    "stage": stage_text,
                    "message": message_text,
                }
            )
        except Exception:
            pass


_UI_LOGGER_NAME = "app.ui"


def log_ui_event(
    entry: dict[str, Any],
    *,
    max_payload_bytes: int = 2048,
) -> bool:
    """Emit a UI-side debug event into the rotating ``app.log`` stream.

    The browser POSTs structured entries to ``/api/logs/ui`` and the
    handler hands each one to this helper. We render them as
    ``INFO [ui] {source} {kind} {payload_json}`` so the line interleaves
    with the existing backend events on the same logger. The payload is
    truncated to ``max_payload_bytes`` (JSON length) and replaced with
    ``{"truncated": true, "size": N}`` when oversized; this protects the
    log from a misbehaving client trying to dump an arbitrary blob.

    Returns ``True`` when a line was emitted, ``False`` when the entry
    failed validation (missing ``source``/``kind``).
    """
    if not isinstance(entry, dict):
        return False
    source = str(entry.get("source") or "").strip()
    kind = str(entry.get("kind") or "").strip()
    if not source or not kind:
        return False

    payload: Any = entry.get("payload")
    payload_text: str
    if payload is None:
        payload_text = ""
    else:
        try:
            rendered = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            rendered = json.dumps(
                {"unserializable": type(payload).__name__},
                ensure_ascii=False,
            )
        if max_payload_bytes > 0 and len(rendered) > max_payload_bytes:
            rendered = json.dumps(
                {"truncated": True, "size": len(rendered)},
                ensure_ascii=False,
            )
        payload_text = rendered

    ts = str(entry.get("ts") or "")
    ui_logger = logging.getLogger(_UI_LOGGER_NAME)
    if payload_text:
        ui_logger.info("[ui] %s %s %s ts=%s", source, kind, payload_text, ts or "-")
    else:
        ui_logger.info("[ui] %s %s ts=%s", source, kind, ts or "-")
    return True


MAX_CRASH_BREADCRUMBS = 60


def _normalise_breadcrumbs(raw: Any, *, clip: Any) -> list[dict[str, Any]]:
    """Coerce the client's breadcrumb trail into a bounded, flat list.

    The payload is attacker-shaped by definition (any process can POST to
    the endpoint), so nothing here trusts a type: non-lists become empty,
    non-dict entries are dropped, and every field is clipped. Order is
    preserved — the trail only means anything read oldest-first.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:MAX_CRASH_BREADCRUMBS]:
        if not isinstance(item, dict):
            continue
        crumb: dict[str, Any] = {
            "t": item.get("t") if isinstance(item.get("t"), (int, float)) else 0,
            "cat": clip(item.get("cat")) or "app",
            "msg": clip(item.get("msg")),
        }
        detail = clip(item.get("detail"))
        if detail:
            crumb["detail"] = detail
        count = item.get("count")
        if isinstance(count, int) and count > 1:
            crumb["count"] = count
        out.append(crumb)
    return out


def _format_breadcrumbs(crumbs: list[dict[str, Any]]) -> str:
    """Render the trail as indented lines for the human-readable log."""
    if not crumbs:
        return "-"
    lines = []
    for crumb in crumbs:
        stamp = crumb.get("t") or 0
        repeat = f" x{crumb['count']}" if crumb.get("count") else ""
        detail = f" | {crumb['detail']}" if crumb.get("detail") else ""
        lines.append(
            f"  +{int(stamp):>7}ms [{crumb.get('cat', '?')}] "
            f"{crumb.get('msg', '')}{repeat}{detail}"
        )
    return "\n" + "\n".join(lines)


def log_ui_crash(report: dict[str, Any], *, max_field_bytes: int = 8192) -> bool:
    """Record a UI crash caught by the React error boundary.

    Unlike :func:`log_ui_event` (the opt-in debug firehose gated behind
    ``logging.ui_log_enabled``), a white-screen crash is **always**
    recorded — the whole point is to capture the cause the next time it
    happens, even when the user never turned debug logging on. Emits one
    ``ERROR [ui] crash …`` line on the ``app.ui`` logger (so it shows up
    in ``tail_logs(module_contains="ui", level="ERROR")`` and the
    rotating ``app.log``) and appends a structured entry to
    ``crashlog.txt`` so the full stack survives a log rotation. Each
    string field is clipped to ``max_field_bytes`` to keep a misbehaving
    client from dumping an unbounded blob. Returns ``True`` when a line
    was emitted, ``False`` on a malformed report.

    Three things beyond the bare message make a report actionable, and
    all are optional so an older client still logs fine:

    * ``breadcrumbs`` — what the UI was doing beforehand, usually the
      part that actually identifies the cause.
    * ``context`` — build id, viewport, socket state, voice mode, …
    * a **de-minified stack**. A production bundle's stack names neither
      the file nor the function, so it is mapped through
      :mod:`app.core.infra.sourcemap` against ``web/dist/assets``. The
      raw stack is still recorded alongside it: if ``dist`` has been
      rebuilt since the crash the mapping silently no-ops, and having
      both means that failure is visible rather than confusing.
    """
    if not isinstance(report, dict):
        return False

    def _clip(value: Any) -> str:
        text = str(value if value is not None else "").strip()
        if max_field_bytes > 0 and len(text) > max_field_bytes:
            return text[:max_field_bytes] + f"…(+{len(text) - max_field_bytes} more)"
        return text

    message = _clip(report.get("message")) or "(no message)"
    source = _clip(report.get("source")) or "unknown"
    url = _clip(report.get("url"))
    stack = _clip(report.get("stack"))
    component_stack = _clip(report.get("componentStack"))
    user_agent = _clip(report.get("userAgent"))
    ts = _clip(report.get("ts"))

    breadcrumbs = _normalise_breadcrumbs(report.get("breadcrumbs"), clip=_clip)
    raw_context = report.get("context")
    context: dict[str, str] = {}
    if isinstance(raw_context, dict):
        for key, value in list(raw_context.items())[:40]:
            context[_clip(key)[:64]] = _clip(value)[:256]

    mapped_stack = ""
    try:
        from app.core.infra import sourcemap

        candidate = sourcemap.symbolicate_stack(stack)
        if sourcemap.stack_is_symbolicated(stack, candidate):
            mapped_stack = candidate
    except Exception:  # pragma: no cover - symbolication is best-effort
        mapped_stack = ""

    context_text = (
        " ".join(f"{k}={v}" for k, v in context.items()) if context else "-"
    )

    ui_logger = logging.getLogger(_UI_LOGGER_NAME)
    ui_logger.error(
        "[ui] crash source=%s msg=%s url=%s ts=%s ua=%s\n"
        "context: %s\ncomponentStack: %s\nbreadcrumbs: %s\nstack: %s",
        source,
        message,
        url or "-",
        ts or "-",
        user_agent or "-",
        context_text,
        component_stack or "-",
        _format_breadcrumbs(breadcrumbs),
        mapped_stack or stack or "-",
    )
    try:
        entry: dict[str, object] = {
            "type": "ui_crash",
            "source": source,
            "message": message,
            "url": url,
            "user_agent": user_agent,
            "component_stack": component_stack,
            "stack": stack,
        }
        if mapped_stack:
            entry["stack_mapped"] = mapped_stack
        if context:
            entry["context"] = context
        if breadcrumbs:
            entry["breadcrumbs"] = breadcrumbs
        _write_line(entry)
    except Exception:
        pass
    return True


def read_ui_crashes(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ``ui_crash`` entries, newest first.

    Reads ``crashlog.txt`` back so a crash can be inspected without
    opening the file by hand — this backs the ``get_ui_crashes`` MCP
    tool. The file is append-only JSONL with other record types
    (``exception``, ``event``) interleaved, plus possible raw
    ``faulthandler`` output, so anything that isn't a well-formed
    ``ui_crash`` object is skipped rather than treated as an error.
    """
    if limit <= 0:
        return []
    try:
        with CRASH_LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    out: list[dict[str, Any]] = []
    for raw in reversed(lines):
        text = raw.strip()
        if not text.startswith("{") or '"ui_crash"' not in text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "ui_crash":
            out.append(parsed)
            if len(out) >= limit:
                break
    return out


def log_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    *,
    context: str = "unhandled",
) -> None:
    formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    try:
        _write_line(
            {
                "type": "exception",
                "context": context,
                "exception_type": exc_type.__name__,
                "message": str(exc_value),
                "traceback": formatted,
            }
        )
    except Exception:
        pass
    if _logger is not None:
        _logger.error(
            "[%s] %s: %s",
            context,
            exc_type.__name__,
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )


def log_handled_exception(exc: BaseException, *, context: str) -> None:
    log_exception(type(exc), exc, exc.__traceback__, context=context)


def install_global_exception_hooks() -> None:
    global _fault_file

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if _fault_file is None:
        _fault_file = CRASH_LOG_PATH.open("a", encoding="utf-8")
        try:
            faulthandler.enable(file=_fault_file, all_threads=True)
        except Exception:
            pass

    # faulthandler gives the Python stack, which for a native fault is
    # only where the crash surfaced. This adds the faulting address, the
    # DLL it lives in, and a minidump.
    try:
        native_crash.install(dump_dir=DATA_DIR, record=_write_native_crash)
    except Exception:
        pass

    previous_sys_hook = sys.excepthook

    def _sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        try:
            log_exception(exc_type, exc_value, exc_traceback, context="sys.excepthook")
        except Exception:
            pass
        previous_sys_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def _thread_hook(args: threading.ExceptHookArgs) -> None:
            try:
                log_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                    context=f"thread:{args.thread.name if args.thread else 'unknown'}",
                )
            except Exception:
                pass
            previous_thread_hook(args)

        threading.excepthook = _thread_hook


# ── public helpers (used by MCP tools and tests) ──────────────────────────


def tail(
    n: int = 200,
    *,
    level: str | int = "INFO",
    module_contains: str | None = None,
) -> list[str]:
    """Return up to ``n`` most recent log lines from the in-process ring.

    ``level`` filters by minimum severity (case-insensitive name or numeric).
    ``module_contains`` is a substring matched against the logger name
    (e.g. ``"prompt"`` matches ``app.core.session.prompt_assembler``).
    """
    handler = _RING_HANDLER
    if handler is None:
        return []
    min_level = _coerce_level(level if isinstance(level, str) else int(level), default=logging.INFO)
    needle = module_contains.lower() if module_contains else None
    out: list[str] = []
    for level_no, name, _turn, _msg, formatted in handler.snapshot():
        if level_no < min_level:
            continue
        if needle and needle not in name.lower():
            continue
        out.append(formatted)
    if n > 0 and len(out) > n:
        out = out[-n:]
    return out


def read_log_file(
    lines: int = 500,
    *,
    level: str | int = "INFO",
    grep: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Tail the rotating ``data/app.log`` (and rolled siblings if needed).

    Reads the active file plus ``.1``, ``.2`` … in reverse order until at
    least ``lines`` candidate lines have been collected. Filters by
    ``level`` (minimum severity, parsed from the formatted line) and an
    optional case-insensitive substring ``grep``.
    """
    target = Path(path) if path is not None else _log_file_path
    if target is None:
        return []
    target = Path(target)
    if not target.exists() and not any(
        Path(f"{target}.{i}").exists() for i in range(1, 10)
    ):
        return []

    min_level = _coerce_level(level if isinstance(level, str) else int(level), default=logging.INFO)
    needle = grep.lower() if grep else None

    candidate_paths: list[Path] = []
    if target.exists():
        candidate_paths.append(target)
    for i in range(1, 10):
        rolled = Path(f"{target}.{i}")
        if rolled.exists():
            candidate_paths.append(rolled)

    collected: list[str] = []
    for candidate in candidate_paths:
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as fh:
                file_lines = fh.readlines()
        except OSError:
            continue
        # Walk newest-first so we can early-exit when we have enough.
        for raw in reversed(file_lines):
            line = raw.rstrip("\n")
            if needle and needle not in line.lower():
                continue
            level_no = _parse_level_from_line(line)
            if level_no < min_level:
                continue
            collected.append(line)
            if lines > 0 and len(collected) >= lines:
                break
        if lines > 0 and len(collected) >= lines:
            break

    collected.reverse()
    return collected


def _parse_level_from_line(line: str) -> int:
    """Best-effort extraction of the severity from a formatted log line."""
    try:
        # Expected shape: "[ts] LEVEL [name turn=…] message"
        right = line.split("] ", 1)[1] if "] " in line else line
        token = right.split(" ", 1)[0].upper()
        resolved = getattr(logging, token, None)
        if isinstance(resolved, int):
            return resolved
    except Exception:
        pass
    return logging.INFO


def set_module_level(module: str, level: str | int) -> str:
    """Bump a single logger to the requested level. Returns the resolved name."""
    target = logging.getLogger(str(module))
    target.setLevel(_coerce_level(level, default=logging.INFO))
    return logging.getLevelName(target.level)


def clear_ring_buffer() -> None:
    """Test helper: drop everything from the in-process ring."""
    if _RING_HANDLER is not None:
        _RING_HANDLER.clear()
