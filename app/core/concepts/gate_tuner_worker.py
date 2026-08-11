"""L45 gate tuner -- the idle worker that measures the graph and moves the bars.

Once a day, when nothing else is happening, this reads the whole concept
store, builds the distributions each :class:`~app.core.concepts.gate_tuning
.GateSpec` names, solves them, appends a population snapshot, writes
``data/tuning/concept_gates.json``, and applies the handful of read-side gates
that are cleared to move. No LLM call, no writes to ``concepts``.

**Why the heartbeat is short and the cadence is long.** Three things in
:class:`~app.core.proactive.idle_worker_scheduler.IdleWorkerScheduler` decide
whether an expensive daily worker ever gets a tick:

- ``last_run_at`` persists in ``kv_meta``, so a nightly shutdown costs
  nothing: on the next boot this worker is overdue and sorts first.
- ``evaluate_admission`` charges the run against its lane budget using an EMA
  of past durations. The first run is always admitted, but once the EMA knows
  the worker is slow, a run that does not fit the current budget waits for
  ``_FIT_ESCAPE_HEARTBEATS`` -- *three of its own heartbeats*. At a 24-hour
  heartbeat that is a three-day worst case on a machine that is only on for
  part of each day.
- ``needs_llm=False`` puts the run in the compute lane, which scales with idle
  depth alone and never competes with Ollama for the GPU.

So the heartbeat is six hours and a ``kv_meta`` key enforces the real daily
spacing, the same split the L42 conduct pass uses. The scheduler ranks and
multiplies the *heartbeat*, so the fit-escape shrinks to eighteen hours of
uptime -- reachable in ordinary use -- while the work still happens about
once a day.

Two rules follow from an app that is not always running: catch up **once**
rather than backfilling a run per missed day, and never assume even spacing --
every snapshot line records ``hours_since_previous`` so a trend read over the
JSONL is honest about the gaps.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.concepts.concept_evidence_admission import load_fit_sample
from app.core.concepts.gate_measure import populations, snapshot
from app.core.concepts.gate_tuning import (
    GATE_SPECS,
    kind_floor_defaults,
    solve_all,
)
from app.core.infra import timephrase
from app.core.infra.gate_tuning_store import (
    append_population,
    apply_gates,
    build_document,
    load_gates,
    save_gates,
    user_memory_overrides,
)
from app.core.proactive.idle_worker import WorkSignal

log = logging.getLogger("app.gate_tuning")

#: Internal cadence key. Separate from the scheduler's
#: ``idle_worker.concept_gate_tuning.last_run_at`` on purpose: that one tracks
#: *ticks*, this one tracks completed tuning runs, and the whole point of the
#: short heartbeat is that those are different numbers.
LAST_RUN_KEY = "concept.gate_tuning.last_run"

#: Settings caps a ``pool_multiple`` objective can be a multiple of.
_CAP_SETTINGS = (
    "context_budget_core_cap",
    "concept_core_openness_slots",
    "profile_concept_max_lines",
)


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class ConceptGateTunerWorker:
    """Idle worker: measure the concept graph, calibrate its thresholds."""

    name = "concept_gate_tuning"

    def __init__(
        self,
        *,
        concept_store: Any,
        memory_settings: Any,
        agent_settings: Any = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        event_store: Any = None,
        surfacing_outcome_store_provider: Callable[[], Any] | None = None,
        topic_graph: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._concept_store = concept_store
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._event_store = event_store
        self._outcome_store_provider = surfacing_outcome_store_provider
        self._topic_graph = topic_graph
        # Every other concept worker takes its "now" from this seam, and
        # this one used to read ``datetime.now`` directly. That silently
        # exempted it from the DT1 debug clock: the cadence below and the
        # history span in ``_graph_mature`` could not be fast-forwarded
        # while the rest of the concept stack could.
        self._clock = clock or timephrase.utcnow

    # ── settings ──────────────────────────────────────────────────────

    def _f(self, name: str, default: float) -> float:
        return float(getattr(self._memory_settings, name, default))

    def _i(self, name: str, default: int) -> int:
        return int(getattr(self._memory_settings, name, default))

    @property
    def enabled(self) -> bool:
        return bool(
            getattr(self._memory_settings, "concept_gate_tuning_enabled", True)
        )

    @property
    def interval_seconds(self) -> float:
        """The scheduler heartbeat -- deliberately shorter than the cadence."""
        return float(
            max(600, self._i("concept_gate_tuning_heartbeat_seconds", 21600))
        )

    @property
    def cadence_seconds(self) -> float:
        return float(
            max(3600, self._i("concept_gate_tuning_cadence_seconds", 86400))
        )

    # ── scheduling ────────────────────────────────────────────────────

    def _last_run(self) -> datetime | None:
        if self._kv_get is None:
            return None
        try:
            return _parse_iso(self._kv_get(LAST_RUN_KEY))
        except Exception:
            return None

    def _due(self, now: datetime) -> bool:
        last = self._last_run()
        if last is None:
            return True
        return (now - last).total_seconds() >= self.cadence_seconds

    def _graph_mature(self) -> bool:
        """L21 cold start: don't calibrate to a graph that hasn't formed.

        A distribution over forty concepts from the first two days is not a
        distribution, it is a coincidence, and a bar solved from it would be
        worse than the shipped default.
        """
        try:
            rows = self._concept_store.list_by()
        except Exception:
            return False
        if len(rows) < max(1, self._i("concept_min_clusters", 6) * 10):
            return False
        oldest = min(
            (
                parsed
                for parsed in (
                    _parse_iso(getattr(row, "created_at", "")) for row in rows
                )
                if parsed is not None
            ),
            default=None,
        )
        if oldest is None:
            return False
        span_days = (
            self._clock() - oldest
        ).total_seconds() / 86_400.0
        return span_days >= self._f("concept_min_history_days", 3.0)

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        """Hard vetoes only; the timing lives in :meth:`demand`."""
        if not self.enabled:
            return False
        if self._concept_store is None:
            return False
        return self._graph_mature()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Invisible to the scheduler except on the day the cadence is due.

        ``needs_llm=False`` is what puts this in the compute lane. The
        pressure is modest but non-zero: nothing is waiting on this run, it
        just needs to happen eventually, and the staleness half of the urgency
        blend is what eventually carries it.
        """
        if not self._due(now):
            return None
        return WorkSignal(
            pressure=0.25,
            reason="daily concept gate calibration due",
            needs_llm=False,
        )

    # ── the run ───────────────────────────────────────────────────────

    def _cluster_engaged_rates(self) -> list[float]:
        """Per-cluster engaged rates from the L37 ledger, for the taste gate."""
        if self._outcome_store_provider is None:
            return []
        try:
            store = self._outcome_store_provider()
        except Exception:
            return []
        if store is None:
            return []
        try:
            affinity = store.engaged_rate_by_cluster(
                window_days=self._i("taste_affinity_window_days", 90),
                min_settled=self._i("taste_min_settled", 4),
            )
        except Exception:
            log.debug("gate tuning: cluster affinity read failed", exc_info=True)
            return []
        rates: list[float] = []
        for entry in (affinity or {}).values():
            rate = getattr(entry, "engaged_rate", None)
            if rate is not None:
                rates.append(float(rate))
        return rates

    def _event_delta(self, since: datetime | None) -> dict[str, int]:
        if self._event_store is None or since is None:
            return {}
        try:
            return self._event_store.counts_by_type_since(since.isoformat())
        except Exception:
            log.debug("gate tuning: event delta read failed", exc_info=True)
            return {}

    def _current_values(self) -> dict[str, float]:
        """Where each gate stands right now.

        The solver's step clamp is relative to this, so a hand-set value is
        respected as a *starting point* even for a gate whose key the user
        never touched. Per-kind floors read their module constants, since they
        have no settings field to read from.
        """
        current = dict(kind_floor_defaults())
        for spec in GATE_SPECS:
            if not spec.is_setting_field:
                continue
            current[spec.setting] = self._f(spec.setting, 0.0)
        return current

    def run(self) -> dict[str, Any] | None:
        now = self._clock()
        if not self._due(now):
            return None
        started = time.monotonic()

        try:
            rows = self._concept_store.list_by()
        except Exception:
            log.warning("gate tuning: concept load failed", exc_info=True)
            return None

        previous = load_gates()
        previous_at = _parse_iso(previous.get("updated_at"))
        pairs = self._i("concept_gate_tuning_cosine_pairs", 4000)
        pops = populations(
            rows,
            cluster_engaged_rates=self._cluster_engaged_rates(),
            evidence_fit=load_fit_sample(self._kv_get),
            cosine_pairs=pairs,
            rng=random.Random(),
        )
        caps = {name: self._i(name, 0) for name in _CAP_SETTINGS}
        solutions = solve_all(
            GATE_SPECS,
            pops,
            current=self._current_values(),
            caps=caps,
        )

        row = snapshot(
            rows,
            pops,
            now=now,
            previous_at=previous_at,
            event_counts=self._event_delta(previous_at),
        )
        append_population(row)

        document = build_document(
            solutions,
            now=now,
            previous=previous,
            user_overrides=user_memory_overrides(),
            population={
                "total": row["total"],
                "active": row["active"],
                "by_role": row["by_role"],
                "constraint_ratio": row["constraint_ratio"],
            },
        )
        save_gates(document)
        applied = apply_gates(self._memory_settings, document)

        # Stamped last, so a crash mid-run retries rather than skipping a day.
        if self._kv_set is not None:
            try:
                self._kv_set(LAST_RUN_KEY, now.isoformat())
            except Exception:
                log.debug("gate tuning: cadence stamp failed", exc_info=True)

        moved = {
            name: solution.proposed
            for name, solution in solutions.items()
            if solution.moved
        }
        stats = {
            "concepts": len(rows),
            "populations": len(pops),
            "solved": len(solutions),
            "moved": len(moved),
            "applied": applied,
            "cosine_pairs": len(pops.get("pair_cosine", ())),
            "hours_since_previous": row["hours_since_previous"],
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
        log.info("concept_gate_tuning run: %s", stats)
        if moved:
            log.debug("concept_gate_tuning proposals: %s", moved)
        return stats


__all__ = ["ConceptGateTunerWorker", "LAST_RUN_KEY"]
