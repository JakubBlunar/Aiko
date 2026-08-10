"""Idle-time background-worker contract (schema v8 / G1 scaffold).

The :class:`IdleWorkerScheduler` (see :mod:`app.core.proactive.idle_worker_scheduler`)
periodically wakes during quiet windows -- no Live mode, no recent
user activity -- and asks each registered worker whether it's due. Due
workers run one at a time so CPU stays predictable and so a slow
worker can't stack on top of the next tick.

Initial workers:
  * :class:`MemoryPromotionWorker` -- promote scratchpad rows to
    long_term, demote stale long_term rows to archive, delete dead
    scratchpad rows. See :mod:`app.core.memory.memory_promotion_worker`.
  * :class:`MemoryDecayWorker` -- wall-clock-driven salience decay
    + revival_score rebate. See :mod:`app.core.memory.memory_decay_worker`.

Future workers (mentioned in the backlog as F1 / G2 / G3 / etc.)
implement the same :class:`IdleWorker` Protocol and register at boot.

Demand-driven scheduling (P36)
------------------------------
``interval_seconds`` alone conflates two questions: "how long since the
last run" and "is there anything to do". A worker may additionally
implement :meth:`DemandAwareWorker.demand`, a probe far cheaper than
``run()`` that reports a :class:`WorkSignal` -- how much work is
pending, and whether servicing it will call the worker LLM. The
scheduler ranks by :func:`compute_urgency` rather than by age, and
charges the run to the compute or LLM lane per ``needs_llm``.

Migrating a worker means splitting its ``is_ready()`` in two: the hard
vetoes (feature flags, cold-start guards, per-hour caps) stay, and the
``default_is_ready(self.interval_seconds, ...)`` timing check moves
into ``demand()``. Leaving the timing check in ``is_ready()`` would
veto the worker before its pressure is ever consulted, defeating the
mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from app.core.infra import timephrase


def _utcnow() -> datetime:
    return timephrase.utcnow()


# Budget lanes. Non-LLM work is charged to the compute lane, which scales
# with idle depth alone; LLM work is charged to the LLM lane, which is
# additionally sized by how badly the worker route contends with the chat
# route for a GPU. See app/core/proactive/llm_contention.py.
LANE_COMPUTE = "compute"
LANE_LLM = "llm"

# Urgency blend. Pressure dominates -- the whole point is to serve real
# backlog first -- but staleness keeps a persistently low-pressure worker
# from being starved forever by noisier neighbours.
PRESSURE_WEIGHT = 0.7
STALENESS_WEIGHT = 0.3


@dataclass(frozen=True, slots=True)
class WorkSignal:
    """A worker's cheap answer to "do you have work, and will it cost a GPU".

    ``pressure`` is clamped to ``[0.0, 1.0]``: 0.0 means nothing is
    pending and the worker should not be admitted at all, 1.0 means
    saturated.

    ``needs_llm`` is deliberately per-*run* rather than per-worker,
    because that is where the knowledge lives: ``ConceptSynthesisWorker``
    only calls the LLM when its signature diff found dirty clusters, and
    ``IdleAwayActivityWorker`` rolls ``away_activities_llm_ratio`` per
    beat. A static per-worker flag would be wrong for both.
    """

    pressure: float
    reason: str = ""
    needs_llm: bool = False

    def __post_init__(self) -> None:
        clamped = max(0.0, min(1.0, float(self.pressure)))
        object.__setattr__(self, "pressure", clamped)

    @property
    def lane(self) -> str:
        return LANE_LLM if self.needs_llm else LANE_COMPUTE


@dataclass(frozen=True, slots=True)
class Admission:
    """The scheduler's verdict on one worker for one tick."""

    admit: bool
    urgency: float
    reason: str
    lane: str


@runtime_checkable
class IdleWorker(Protocol):
    """Anything the :class:`IdleWorkerScheduler` can run during idle time.

    Implementations supply a stable ``name`` (used for logging,
    ``force_run``, and the :class:`IdleWorkerRecord` key), a target
    ``interval_seconds`` between successful runs, and a ``run()``
    method that does the actual work.

    The default :meth:`is_ready` checks elapsed time since the last
    run; override it for richer gating (e.g. "only run while
    scratchpad has rows").
    """

    @property
    def name(self) -> str:
        ...

    @property
    def interval_seconds(self) -> float:
        ...

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        ...

    def run(self) -> dict[str, Any] | None:
        ...


@runtime_checkable
class DemandAwareWorker(IdleWorker, Protocol):
    """An :class:`IdleWorker` that can report pending work cheaply.

    Kept separate from :class:`IdleWorker` so the ~40 unmigrated workers
    still satisfy the base Protocol. The scheduler probes with
    ``getattr(worker, "demand", None)`` and falls back to legacy
    interval behaviour when it is absent or returns ``None``.

    The probe must be *far* cheaper than ``run()`` -- a ``COUNT``, a
    ``kv_meta`` read, or an in-memory mirror filter. Returning ``None``
    means "no opinion, schedule me the old way".
    """

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        ...


# Smoothing factor for the per-worker rolling average duration. 0.3 means a
# fresh measurement contributes 30% and the existing EMA carries 70%, so
# ~5 runs are enough to converge while a one-off slow run can't
# permanently push a worker over the per-tick budget.
_DURATION_EMA_ALPHA: float = 0.3


@dataclass(slots=True)
class IdleWorkerRecord:
    """Per-worker state tracked by the scheduler.

    Persisted to ``kv_meta`` (see :meth:`ChatDatabase.kv_set`) so the
    next process boot doesn't immediately re-fire a worker that just
    completed. ``last_error`` is reset on successful runs and surfaced
    via the ``inspect_idle_workers`` MCP debug tool.

    Duration accounting (P8): every successful run pushes its wall time
    through an EMA so the scheduler can decide whether the worker fits
    in the remaining per-tick budget. ``error_count`` keeps a separate
    cumulative error counter so a flapping worker is visible in
    ``get_status()`` even after a successful retry clears
    ``last_error``.
    """

    name: str
    last_run_at: datetime | None = None
    last_error: str | None = None
    run_count: int = 0
    last_result: dict[str, Any] | None = field(default=None)
    last_duration_ms: float | None = None
    avg_duration_ms: float | None = None
    total_duration_ms: float = 0.0
    error_count: int = 0
    # Demand-driven bookkeeping (P36). All None for a worker that has
    # never been probed, which is how the status view distinguishes
    # "unmigrated" from "probed and found idle".
    last_pressure: float | None = None
    last_urgency: float | None = None
    last_admit_reason: str | None = None
    last_lane: str | None = None
    last_probe_reason: str | None = None
    avg_probe_ms: float | None = None

    def update_after_run(self, duration_ms: float) -> None:
        """Fold a successful run's wall time into the EMA + totals."""
        ms = max(0.0, float(duration_ms))
        self.last_duration_ms = ms
        self.total_duration_ms += ms
        prev = self.avg_duration_ms
        if prev is None:
            self.avg_duration_ms = ms
        else:
            self.avg_duration_ms = (
                _DURATION_EMA_ALPHA * ms + (1.0 - _DURATION_EMA_ALPHA) * prev
            )

    def update_after_error(self) -> None:
        """Bump the cumulative error counter (last_error is set elsewhere)."""
        self.error_count += 1

    def update_after_probe(self, duration_ms: float) -> None:
        """Fold a ``demand()`` probe's wall time into its own EMA.

        Tracked separately from ``avg_duration_ms`` because the whole
        premise is that probing is orders of magnitude cheaper than
        running; mixing them would hide a probe that has quietly become
        expensive.
        """
        ms = max(0.0, float(duration_ms))
        prev = self.avg_probe_ms
        if prev is None:
            self.avg_probe_ms = ms
        else:
            self.avg_probe_ms = (
                _DURATION_EMA_ALPHA * ms + (1.0 - _DURATION_EMA_ALPHA) * prev
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "run_count": int(self.run_count),
            "last_result": dict(self.last_result) if self.last_result else None,
            "last_duration_ms": (
                round(self.last_duration_ms, 2)
                if self.last_duration_ms is not None
                else None
            ),
            "avg_duration_ms": (
                round(self.avg_duration_ms, 2)
                if self.avg_duration_ms is not None
                else None
            ),
            "total_duration_ms": round(self.total_duration_ms, 2),
            "error_count": int(self.error_count),
            "last_pressure": (
                round(self.last_pressure, 4)
                if self.last_pressure is not None
                else None
            ),
            "last_urgency": (
                round(self.last_urgency, 4)
                if self.last_urgency is not None
                else None
            ),
            "last_admit_reason": self.last_admit_reason,
            "last_lane": self.last_lane,
            "last_probe_reason": self.last_probe_reason,
            "avg_probe_ms": (
                round(self.avg_probe_ms, 3)
                if self.avg_probe_ms is not None
                else None
            ),
        }


def default_is_ready(
    interval_seconds: float,
    *,
    now: datetime,
    last_run_at: datetime | None,
) -> bool:
    """Default readiness predicate: due when ``interval_seconds`` elapsed.

    Workers that never ran (``last_run_at is None``) are always ready
    on first scheduler tick. Negative deltas (clock skew) count as
    ready too -- better to fire once spuriously than to silently
    starve.
    """
    if last_run_at is None:
        return True
    delta = (now - last_run_at).total_seconds()
    return delta >= float(interval_seconds)


# Idle depth tiers: (name, upper bound in seconds, budget multiplier).
# The longer the user has been gone, the less a long tick costs them and
# the more there is to catch up on. ``just_left`` deliberately sits at
# 1.0 so shallow idle behaves exactly as it does today.
DEPTH_TIERS: tuple[tuple[str, float, float], ...] = (
    ("just_left", 300.0, 1.0),
    ("away", 900.0, 3.0),
    ("long_away", 3600.0, 6.0),
    ("overnight", float("inf"), 10.0),
)


def classify_depth(
    idle_seconds: float,
    *,
    max_multiplier: float = 10.0,
) -> tuple[str, int, float]:
    """Map seconds-since-last-user-activity to ``(name, index, multiplier)``.

    ``max_multiplier`` clamps the result, so setting it to 1.0 disables
    depth scaling entirely without touching the tier table.
    """
    secs = max(0.0, float(idle_seconds))
    cap = max(1.0, float(max_multiplier))
    for index, (name, upper, mult) in enumerate(DEPTH_TIERS):
        if secs < upper:
            return name, index, min(mult, cap)
    name, _upper, mult = DEPTH_TIERS[-1]
    return name, len(DEPTH_TIERS) - 1, min(mult, cap)


def derive_min_interval_s(
    interval_seconds: float,
    *,
    wake_seconds: float,
    ratio: float,
) -> float:
    """Anti-thrash floor, derived rather than configured per worker.

    ``max(wake_seconds, interval_seconds * ratio)``. The wake floor
    exists because nothing can run more often than one tick anyway; the
    ratio term is what makes a single knob serve intervals spanning
    three orders of magnitude. At ``wake=15`` / ``ratio=0.1`` a 30s
    ``gap_resolver`` floors at one tick while an 86400s
    ``topic_graph_rebuild`` floors at 2.4 hours, so a rare expensive
    worker gets proportional protection without its own config key.
    """
    interval = max(0.0, float(interval_seconds))
    wake = max(0.0, float(wake_seconds))
    scaled = interval * max(0.0, float(ratio))
    return max(wake, scaled)


def pressure_from_count(count: int, *, saturation: int) -> float:
    """Map a backlog count to pressure in ``[0.0, 1.0]``.

    Zero backlog is zero pressure -- the worker is not admitted at all.
    Any backlog starts at 0.5 so a single pending item still clears the
    default urgency threshold on its own; ``saturation`` is the count at
    which the worker is considered fully loaded, purely for ranking
    against its neighbours.
    """
    n = max(0, int(count))
    if n <= 0:
        return 0.0
    sat = max(1, int(saturation))
    return max(0.5, min(1.0, n / sat))


def pressure_from_deficit(have: int, *, want: int) -> float:
    """Pressure rises as stock runs down. A full shelf means zero.

    The inverse of :func:`pressure_from_count`, and the shape every cue
    worker needs. Those workers are not draining a backlog -- they are
    *producing* inventory, so the thing that should wake them is running
    out rather than piling up. A worker holding its target number of
    unspent cues reports 0.0 and is never admitted; one that is empty
    reports 1.0 and jumps the queue.

    The interesting case is the middle. Deficit is scaled so that being
    one short of target already clears the default urgency threshold on
    its own: restocking is cheap and being caught empty when the
    conversation opens a seam is the expensive failure, so the curve is
    deliberately eager. Everything between is linear in the shortfall,
    which is all the ranking needs.
    """
    stock = max(0, int(have))
    target = max(1, int(want))
    if stock >= target:
        return 0.0
    deficit = target - stock
    return max(0.5, min(1.0, deficit / target))


def compute_staleness(elapsed_s: float, heartbeat_s: float) -> float:
    """How overdue a worker is, clamped to ``[0.0, 1.0]``.

    A non-positive heartbeat means "no timing opinion", which reads as
    fully stale so such a worker rides entirely on its pressure.
    """
    hb = float(heartbeat_s)
    if hb <= 0.0:
        return 1.0
    return max(0.0, min(1.0, float(elapsed_s) / hb))


def compute_urgency(pressure: float, staleness: float) -> float:
    """Blend pending work with overdueness into a single rank key."""
    p = max(0.0, min(1.0, float(pressure)))
    s = max(0.0, min(1.0, float(staleness)))
    return PRESSURE_WEIGHT * p + STALENESS_WEIGHT * s


def evaluate_admission(
    *,
    elapsed_s: float | None,
    heartbeat_s: float,
    min_interval_s: float,
    signal: WorkSignal | None,
    threshold: float,
) -> Admission:
    """Decide whether a ready worker runs this tick, and how urgently.

    Assumes the caller has already applied ``is_ready()`` as a hard
    veto. ``elapsed_s is None`` means the worker has never run.

    Workers with no signal take the legacy path: already ready means
    already admitted, ranked by staleness, charged to the LLM lane
    because we cannot know that they are cheap. That keeps the ~40
    unmigrated workers behaving as they do today.
    """
    if signal is None:
        stale = (
            1.0 if elapsed_s is None
            else compute_staleness(elapsed_s, heartbeat_s)
        )
        return Admission(
            admit=True, urgency=stale, reason="legacy", lane=LANE_LLM,
        )

    lane = signal.lane
    if elapsed_s is None:
        return Admission(
            admit=True, urgency=1.0, reason="first_run", lane=lane,
        )
    if elapsed_s < float(min_interval_s):
        return Admission(
            admit=False, urgency=0.0, reason="floor", lane=lane,
        )

    stale = compute_staleness(elapsed_s, heartbeat_s)
    urgency = compute_urgency(signal.pressure, stale)

    # Heartbeat guarantees liveness even if a probe is broken or a
    # worker's pressure never rises above the threshold on its own.
    if heartbeat_s > 0.0 and elapsed_s >= float(heartbeat_s):
        return Admission(
            admit=True, urgency=urgency, reason="heartbeat", lane=lane,
        )
    if signal.pressure <= 0.0:
        return Admission(
            admit=False, urgency=urgency, reason="idle", lane=lane,
        )
    if urgency >= float(threshold):
        return Admission(
            admit=True, urgency=urgency, reason="pressure", lane=lane,
        )
    return Admission(
        admit=False, urgency=urgency, reason="below_threshold", lane=lane,
    )


__all__ = [
    "Admission",
    "DEPTH_TIERS",
    "DemandAwareWorker",
    "IdleWorker",
    "IdleWorkerRecord",
    "LANE_COMPUTE",
    "LANE_LLM",
    "PRESSURE_WEIGHT",
    "STALENESS_WEIGHT",
    "WorkSignal",
    "classify_depth",
    "compute_staleness",
    "compute_urgency",
    "default_is_ready",
    "derive_min_interval_s",
    "evaluate_admission",
    "pressure_from_count",
    "pressure_from_deficit",
]
