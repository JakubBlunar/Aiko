# Background workers

Shared scheduling for idle background jobs. G1 (`IdleWorkerScheduler`)
shipped as part of schema v8; G2 (schedule learning) and G3 (idle
curiosity) shipped on top of it. See [`shipped.md`](shipped.md) and
[`docs/memory-tiers.md`](../memory-tiers.md) for implementation
details.

No open G-series items at the moment. New background workers should
register with the existing
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py)
rather than spinning up their own threads, and should mirror the
INFO-level audit logging pattern established by
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
and [`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py).

For new worker ideas not yet committed to a section letter, see
[`patterns.md`](patterns.md) — several entries (K1 long-term goals,
K8 affect rupture, K10 persona regression, K14 engagement signals,
K21 fresh-eyes resummary) would naturally take the shape of an idle
worker.

---

## G-CLEANUP. `consolidator_state.last_cluster_index` is dead weight

Trivial cleanup item, parked here so it doesn't get forgotten.
The schema carries
[`consolidator_state.last_cluster_index`](../../app/core/memory/memory_consolidator.py)
but nothing reads it — the comment in the source flags it as
unused. Either wire incremental clustering (the original intent)
or drop the column in the next schema bump. Effort: trivial.

For perf / observability gaps that aren't workers in their own
right (turn-level embed budget, idle-worker queue visibility,
typed-mode prefetch, etc.), see
[`perf.md`](perf.md).
