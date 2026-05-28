# Background workers

Shared scheduling for idle background jobs. G1 (`IdleWorkerScheduler`)
shipped as part of schema v8; G2 (schedule learning) and G3 (idle
curiosity) shipped on top of it. See [`shipped.md`](shipped.md) and
[`docs/memory-tiers.md`](../memory-tiers.md) for implementation
details.

No open G-series items at the moment. New background workers should
register with the existing
[`IdleWorkerScheduler`](../../app/core/idle_worker_scheduler.py)
rather than spinning up their own threads, and should mirror the
INFO-level audit logging pattern established by
[`app/core/idle_fact_checker.py`](../../app/core/idle_fact_checker.py)
and [`app/core/idle_curiosity_worker.py`](../../app/core/idle_curiosity_worker.py).

For new worker ideas not yet committed to a section letter, see
[`patterns.md`](patterns.md) — several entries (K1 long-term goals,
K8 affect rupture, K10 persona regression) would naturally take the
shape of an idle worker.
