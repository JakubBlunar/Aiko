"""K10-followup — background persona-regression worker.

K10 shipped the whole golden-turn eval engine as an **on-demand** path: the
MCP ``run_persona_regression()`` tool, the "Run check" button in
Settings -> Diagnostics, and ``POST /api/persona-drift/run`` all call the
same :meth:`SessionController.run_persona_regression` core. The one thing
missing was a clock — nothing noticed drift unless somebody went looking.

This worker is that clock and nothing more. On a slow cadence (daily by
default), during a quiet window, it calls the same core, and compares the
result against the previous snapshot so an *unattended* run can say
"something that used to pass now doesn't" instead of quietly filing a
number. A regression logs at WARNING with the newly-failing turn ids; a
recovery logs at INFO. The snapshot itself already persists to ``kv_meta``
and renders in the existing panel, so there is no new surface here.

**Off by default.** A run replays every golden turn through the worker LLM
(six turns today), and K10's whole reason for staying on-demand was to
avoid unattended background spend. ``persona_regression_auto_enabled``
opts in; ``persona_regression_enabled`` (the K10 master switch) still
gates it, so turning the harness off turns this off too.

Every failure path is swallowed and logged — the worst case is a missed
eval, never a crashed tick.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from app.core.proactive.idle_worker import default_is_ready


log = logging.getLogger("app.persona_regression_worker")


def failing_ids(snapshot: dict[str, Any]) -> set[str]:
    """Ids of the golden turns that failed in ``snapshot``.

    Pure, and tolerant of a partial / legacy snapshot shape: anything that
    isn't a well-formed result row is ignored rather than raising, because
    the only consumer is a log line.
    """
    out: set[str] = set()
    results = snapshot.get("results")
    if not isinstance(results, list):
        return out
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get("passed"):
            continue
        turn_id = str(row.get("id") or "").strip()
        if turn_id:
            out.add(turn_id)
    return out


class PersonaRegressionWorker:
    """IdleWorker that replays the K10 golden turns on a slow cadence."""

    name = "persona_regression"

    def __init__(
        self,
        *,
        run_regression: Callable[[], dict[str, Any]],
        snapshot_provider: Callable[[], dict[str, Any]],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 86400.0,
    ) -> None:
        self._run_regression = run_regression
        self._snapshot_provider = snapshot_provider
        self._enabled_provider = enabled_provider
        # Floor of an hour: this is a multi-LLM-call worker running on the
        # shared scheduler thread, and nothing about persona drift moves
        # faster than that.
        self._interval_seconds = max(3600.0, float(interval_seconds))

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"ran": 0, "disabled": True}

        # Read the baseline *before* the run overwrites it, so the
        # comparison is against the last recorded state whether that came
        # from a previous auto-run or from someone pressing "Run check".
        before = failing_ids(self._safe_snapshot())

        try:
            snapshot = self._run_regression()
        except Exception:
            log.warning("persona-regression auto-run failed", exc_info=True)
            return {"ran": 0, "error": "exception"}
        if not isinstance(snapshot, dict):
            return {"ran": 0, "error": "bad_snapshot"}
        error = snapshot.get("error")
        if error:
            log.debug("persona-regression auto-run skipped: %s", error)
            return {"ran": 0, "error": str(error)}

        after = failing_ids(snapshot)
        regressed = sorted(after - before)
        recovered = sorted(before - after)
        passed = int(snapshot.get("passed", 0) or 0)
        total = int(snapshot.get("total", 0) or 0)

        if regressed:
            log.warning(
                "persona-regression auto-run: %d/%d passed, NEWLY FAILING: %s",
                passed, total, ", ".join(regressed),
            )
        elif recovered:
            log.info(
                "persona-regression auto-run: %d/%d passed, recovered: %s",
                passed, total, ", ".join(recovered),
            )
        else:
            log.info(
                "persona-regression auto-run: %d/%d passed, no change",
                passed, total,
            )

        return {
            "ran": 1,
            "passed": passed,
            "total": total,
            "failed": int(snapshot.get("failed", 0) or 0),
            "regressed": regressed,
            "recovered": recovered,
        }

    # ── helpers ──────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return False

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            snapshot = self._snapshot_provider()
        except Exception:
            log.debug("persona-regression baseline read failed", exc_info=True)
            return {}
        return snapshot if isinstance(snapshot, dict) else {}


__all__ = ["PersonaRegressionWorker", "failing_ids"]
