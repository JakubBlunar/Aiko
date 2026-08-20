"""Session-wide test guards.

Three jobs: keep the suite out of the real ``data/`` directory, keep it out
of the real ``config/user.json``, and keep the wall-clock budget tests from
lying when the suite runs in parallel.

``crash_logging`` resolves ``CRASH_LOG_PATH`` from the module's own
location, so any test that exercises the crash path — directly, or via
``POST /api/logs/ui-crash``, or by tripping ``log_exception`` — appends to
the developer's actual ``data/crashlog.txt``. That is not a hypothetical:
it happened, and it left the file full of ``"boom"`` and ``"user_agent":
"vitest"`` entries that buried the real crashes the file exists to
preserve. Redirect the module global for the whole session so a crash the
suite provokes is written somewhere disposable.

``user.json`` is the same story with a worse ending. It is the live
install's runtime state, and the tests write to it: any test that drives a
real ``SessionController`` through a turn reaches
``_touch_last_active_session``, which persists ``session.last_active_id``
by design. Fourteen tests did so on the last full run, and since some of
them use plausible session ids, the developer's restore pointer was left
naming ``main`` (8 messages, last used in May) or ``s2`` (157 messages,
last used on the 12th) — both real conversations, so the app dutifully
reopened one of them on next launch and the bug presented as "it always
puts me in an old chat". That was mis-diagnosed once already, as
``switch_session`` recording intent; the pointer logic was fine and the
tests were writing over it.

Both fixtures are autouse and session-scoped precisely so nobody has to
remember them. A future test that logs a crash, or takes a turn, is
covered without opting in.
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
def _isolate_user_config() -> object:
    """Point ``USER_CONFIG_PATH`` at a throwaway file for the whole run.

    Starts empty rather than as a copy of the real file: a test that needs
    a setting present should write it, and inheriting the developer's
    install would make results depend on whose machine it ran on.

    ``gate_tuning_store`` is redirected too. It binds the path with
    ``from ... import USER_CONFIG_PATH``, so it holds a *copy* and is
    unaffected by patching the settings module — the trap that makes
    per-test ``mock.patch.object(settings, "USER_CONFIG_PATH", ...)``
    look sufficient when it is not.
    """
    from app.core.infra import gate_tuning_store, settings as settings_mod

    real = settings_mod.USER_CONFIG_PATH
    before = real.read_bytes() if real.is_file() else None

    with tempfile.TemporaryDirectory(prefix="aiko-tests-cfg-") as tmp:
        replacement = Path(tmp) / "user.json"
        originals = (real, gate_tuning_store.USER_CONFIG_PATH)
        settings_mod.USER_CONFIG_PATH = replacement
        gate_tuning_store.USER_CONFIG_PATH = replacement
        settings_mod._config_cache.pop(str(real), None)
        try:
            yield replacement
        finally:
            settings_mod.USER_CONFIG_PATH = originals[0]
            gate_tuning_store.USER_CONFIG_PATH = originals[1]
            settings_mod._config_cache.pop(str(replacement), None)

    # Tripwire. Redirecting the two known globals covers the paths that
    # exist today; this covers the ones that don't yet. Anything that
    # reaches the live file by another route -- a third module copying the
    # value, a hardcoded path, a subprocess -- turns into a loud teardown
    # error here instead of quietly rewriting the developer's install and
    # being discovered weeks later from the symptom.
    after = real.read_bytes() if real.is_file() else None
    if before is not None and after != before:
        raise AssertionError(
            f"the test run modified {real}. Something wrote the live user "
            "config despite the redirect in this fixture; find it with a "
            "Path.replace/write_text guard and give it the same treatment."
        )


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
