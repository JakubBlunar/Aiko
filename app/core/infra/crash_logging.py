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
    """Configure app logger: stderr handler with level from env LOG_LEVEL or argument. Call once at startup.

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
) -> None:
    """Configure ``app.*`` logging: stderr + optional rotating file + ring buffer.

    Idempotent — clears existing handlers on the ``app`` and root loggers.
    The same formatter (``LOG_FORMAT``) and ``_TurnIdFilter`` are attached
    to every handler so log lines look identical wherever they end up.
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

    ui_logger = logging.getLogger(_UI_LOGGER_NAME)
    ui_logger.error(
        "[ui] crash source=%s msg=%s url=%s ts=%s ua=%s\ncomponentStack: %s\nstack: %s",
        source,
        message,
        url or "-",
        ts or "-",
        user_agent or "-",
        component_stack or "-",
        stack or "-",
    )
    try:
        _write_line(
            {
                "type": "ui_crash",
                "source": source,
                "message": message,
                "url": url,
                "user_agent": user_agent,
                "component_stack": component_stack,
                "stack": stack,
            }
        )
    except Exception:
        pass
    return True


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
        _logger.error("[%s] %s: %s", context, exc_type.__name__, exc_value, exc_info=(exc_type, exc_value, exc_traceback))


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
