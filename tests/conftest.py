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

The redirects are autouse so nobody has to remember them. User overrides
are cleared before each test, and attempts to access the live config
from this process fail without reading it (or mistaking external edits
for writes by the tests).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


_user_config_sandbox: tempfile.TemporaryDirectory[str] | None = None
_original_user_config_env: str | None = None
_protected_user_config_paths: set[str] = set()
_live_config_accesses: list[str] = []
_user_config_guard_active = False


def _reject_live_user_config_access(event: str, args: tuple[object, ...]) -> None:
    if not _user_config_guard_active:
        return
    if event == "open" or event in {"os.remove", "os.unlink"}:
        paths = args[:1]
    elif event == "os.rename":
        paths = args[:2]
    else:
        return
    for path in paths:
        if not isinstance(path, (str, bytes, os.PathLike)):
            continue
        normalized = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        if normalized in _protected_user_config_paths:
            _live_config_accesses.append(event)
            raise AssertionError(f"tests must not access the live user config ({event})")


def pytest_configure(config: pytest.Config) -> None:
    global _user_config_sandbox, _original_user_config_env, _user_config_guard_active
    _original_user_config_env = os.environ.get("AIKO_USER_CONFIG")
    live = (
        Path(_original_user_config_env).expanduser()
        if _original_user_config_env
        else Path(__file__).resolve().parents[1] / "config" / "user.json"
    )
    _protected_user_config_paths.update(
        os.path.normcase(os.path.abspath(os.fspath(path)))
        for path in (live, live.with_suffix(live.suffix + ".tmp"))
    )
    _user_config_sandbox = tempfile.TemporaryDirectory(prefix="aiko-tests-cfg-")
    os.environ["AIKO_USER_CONFIG"] = str(Path(_user_config_sandbox.name) / "user.json")
    _user_config_guard_active = True
    sys.addaudithook(_reject_live_user_config_access)
    config.addinivalue_line(
        "markers",
        "timing: asserts a wall-clock budget; skipped when running under -n",
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    global _user_config_guard_active
    _user_config_guard_active = False
    if _original_user_config_env is None:
        os.environ.pop("AIKO_USER_CONFIG", None)
    else:
        os.environ["AIKO_USER_CONFIG"] = _original_user_config_env
    if _user_config_sandbox is not None:
        _user_config_sandbox.cleanup()


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

    replacement = Path(os.environ["AIKO_USER_CONFIG"])
    originals = (settings_mod.USER_CONFIG_PATH, gate_tuning_store.USER_CONFIG_PATH)
    settings_mod.USER_CONFIG_PATH = replacement
    gate_tuning_store.USER_CONFIG_PATH = replacement
    try:
        yield replacement
    finally:
        settings_mod.USER_CONFIG_PATH = originals[0]
        gate_tuning_store.USER_CONFIG_PATH = originals[1]
        settings_mod._config_cache.pop(str(replacement), None)

    if _live_config_accesses:
        raise AssertionError(
            "tests accessed the live user config: " + ", ".join(_live_config_accesses)
        )


@pytest.fixture(autouse=True)
def _clear_test_user_config(_isolate_user_config: Path) -> None:
    from app.core.infra import settings as settings_mod

    _isolate_user_config.unlink(missing_ok=True)
    settings_mod._config_cache.pop(str(_isolate_user_config), None)


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
