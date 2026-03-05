from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import faulthandler
import json
import sys
import threading
import traceback
from types import TracebackType


DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CRASH_LOG_PATH = DATA_DIR / "crashlog.txt"

_lock = threading.Lock()
_fault_file = None


def _write_line(entry: dict[str, object]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(entry)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(payload, ensure_ascii=False)
    with _lock:
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def log_event(stage: str, message: str) -> None:
    _write_line(
        {
            "type": "event",
            "stage": str(stage),
            "message": str(message),
        }
    )


def log_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    *,
    context: str = "unhandled",
) -> None:
    formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    _write_line(
        {
            "type": "exception",
            "context": context,
            "exception_type": exc_type.__name__,
            "message": str(exc_value),
            "traceback": formatted,
        }
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
