"""Session-wide test guards.

Currently one job: keep the suite out of the real ``data/`` directory.

``crash_logging`` resolves ``CRASH_LOG_PATH`` from the module's own
location, so any test that exercises the crash path — directly, or via
``POST /api/logs/ui-crash``, or by tripping ``log_exception`` — appends to
the developer's actual ``data/crashlog.txt``. That is not a hypothetical:
it happened, and it left the file full of ``"boom"`` and ``"user_agent":
"vitest"`` entries that buried the real crashes the file exists to
preserve. Redirect the module global for the whole session so a crash the
suite provokes is written somewhere disposable.

This is autouse and session-scoped precisely so nobody has to remember
it. A future test that logs a crash is covered without opting in.
"""
from __future__ import annotations

from pathlib import Path
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_crash_log() -> object:
    from app.core.infra import crash_logging

    with tempfile.TemporaryDirectory(prefix="aiko-tests-") as tmp:
        original = crash_logging.CRASH_LOG_PATH
        crash_logging.CRASH_LOG_PATH = Path(tmp) / "crashlog.txt"
        try:
            yield crash_logging.CRASH_LOG_PATH
        finally:
            crash_logging.CRASH_LOG_PATH = original
