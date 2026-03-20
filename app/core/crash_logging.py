from __future__ import annotations

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


DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CRASH_LOG_PATH = DATA_DIR / "crashlog.txt"

_lock = threading.Lock()
_fault_file = None
_logger: logging.Logger | None = None


class _SpamFilter(logging.Filter):
    """Suppress repetitive library errors that cannot be fixed upstream."""

    _SUPPRESSED = ("BrokenPipeError", "pipe has been ended", "poll_connection")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(tok in msg for tok in self._SUPPRESSED)


def configure_logging(level_name: str | None = None) -> None:
    """Configure app logger: console (stderr) with level from env LOG_LEVEL or argument. Call once at startup."""
    global _logger
    level = level_name or os.environ.get("LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    _logger = logging.getLogger("app")
    _logger.setLevel(level)
    _logger.handlers.clear()
    _logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s [app] %(message)s"))
    _logger.addHandler(handler)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    root_handler = logging.StreamHandler(sys.stderr)
    root_handler.setLevel(logging.WARNING)
    root_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s] %(message)s"))
    root_handler.addFilter(_SpamFilter())
    root.addHandler(root_handler)

    for noisy in ("RealtimeSTT", "audio_recorder", "multiprocessing"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)


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
