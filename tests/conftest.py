"""Session-wide test guards.

Two jobs: keep the suite out of the real ``data/`` directory, and keep the
wall-clock budget tests from lying when the suite runs in parallel.

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

import os
from pathlib import Path
import tempfile

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "timing: asserts a wall-clock budget; skipped when running under -n",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip wall-clock budget tests inside xdist workers.

    ``-n auto`` cuts the suite from ~11 minutes to ~2, but it does it by
    saturating every core, and a test that asserts "50 iterations finish
    within 600 ms" is then measuring queueing delay rather than the code.
    The budgets are already loose enough to survive a slow machine; they
    cannot be made loose enough to survive 32 of themselves without
    ceasing to catch the 10x regressions they exist for. So they stay
    strict and simply don't run in parallel -- a serial ``python -m
    pytest`` still enforces every one of them.

    Marker, not a filename list, so the next timing test is covered by
    saying so at the point it's written.
    """
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        return
    skip = pytest.mark.skip(
        reason="wall-clock budget: unmeasurable while workers compete for cores",
    )
    for item in items:
        if "timing" in item.keywords:
            item.add_marker(skip)


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
