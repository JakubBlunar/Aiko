# Idle workers

Aiko does most of her thinking when you are not looking. Roughly fifty
background workers grow the garden, decay memory salience, synthesise
concepts, resummarise threads, and pick what she was doing while you were
away. They all run on one scheduler thread, and the interesting question
is not *what* they do but *when* they are allowed to.

This document describes the scheduler as it works after P36. For the
config values themselves see [`configuration.md`](configuration.md); for
the backlog history see [P36](personality-backlog/perf.md#p36-idle-worker-llm-pile-up-under-a-6-s-soft-budget).

- [The problem the old design had](#the-problem-the-old-design-had)
- [The quiet gate](#the-quiet-gate)
- [Anatomy of a tick](#anatomy-of-a-tick)
- [Pressure: what a worker reports](#pressure-what-a-worker-reports)
- [Urgency: how the scheduler ranks](#urgency-how-the-scheduler-ranks)
- [The two lanes](#the-two-lanes)
- [Idle depth](#idle-depth)
- [LLM contention grades](#llm-contention-grades)
- [Fit rules and anti-starvation](#fit-rules-and-anti-starvation)
- [Legacy workers](#legacy-workers)
- [Writing or migrating a worker](#writing-or-migrating-a-worker)
- [Observability](#observability)
- [Configuration](#configuration)
- [Turning it off](#turning-it-off)
- [Known gaps](#known-gaps)

## The problem the old design had

Every worker declared an `interval_seconds` and the scheduler ran it when
that much time had passed. That conflates two different questions: *how
long since this last ran* and *is there anything for it to do*. Plant
growth swept every plant hourly to usually promote none. Concept
synthesis waited out its interval while a hundred dirty clusters piled
up. Neither the worker's actual backlog nor the user's actual absence
entered the decision.

The budget had the same problem. `idle_worker_tick_budget_ms` was 6000
because, at the time it was written, one local Ollama served both the
chat path and the workers: a background generation stole the GPU, so the
budget was really a *contention* limit wearing a *time* limit's clothes.
It applied identically to a worker doing integer arithmetic on SQLite
rows and to one waiting on a 9B model, and it did not change when you
moved the worker route to a second backend and there was nothing left to
contend over.

So the current design asks workers what they need, and sizes the budget
by what it is actually protecting.

## The quiet gate

Before any of the below matters, the scheduler has to be allowed to run
at all. `SessionController._is_user_idle` says no when:

- Live (voice) mode is on. The speaking-window scheduler is already
  using that time.
- A turn is in progress.
- Less than `idle_worker_quiet_threshold_seconds` (default 20s) has
  passed since the last user activity.

The gate is checked at the top of each tick *and* between workers inside
a tick, so if you come back mid-tick the scheduler stops admitting new
work immediately. The worker already running is **not** cancelled — it
finishes, and your message queues behind it. That is a deliberate
trade: killing a half-written concept synthesis costs more than making
you wait a moment, and bounding that wait is what the fit rules below
are for.

## Anatomy of a tick

The scheduler thread wakes every `idle_worker_wake_seconds` (default
30s). One tick, in order:

1. **Quiet check.** Bail if the user is around.
2. **Classify depth and contention.** How long has the user been gone,
   and does background LLM work fight the chat path? These two set the
   size of the two lane budgets for this tick.
3. **For each registered worker:** apply `is_ready()` as a hard veto,
   then call its `demand()` probe, then run `evaluate_admission` to get
   a verdict — admit or not, at what urgency, in which lane.
4. **Rank the admitted set** lane-major, urgency descending, oldest
   `last_run_at` breaking ties.
5. **Drain** in that order, charging each run's actual wall time against
   its lane, stopping a lane when it runs dry and the whole tick if the
   user returns.

Everything is sequential on the one thread. Multiple workers per tick
comes from fitting them into the budget, not from concurrency — see
[Known gaps](#known-gaps).

## Pressure: what a worker reports

A worker may implement `demand()`, returning a `WorkSignal`:

```python
@dataclass(frozen=True, slots=True)
class WorkSignal:
    pressure: float      # clamped to [0.0, 1.0]
    reason: str = ""     # free text, shown in the status tool
    needs_llm: bool = False
```

`pressure` is "how much work is pending": 0.0 means nothing, and the
worker is not admitted at all. `pressure_from_count(n, saturation=k)` is
the usual way to produce it — zero backlog gives 0.0, and *any* backlog
starts at 0.5 so a single pending item still clears the default
threshold on its own, with `saturation` setting where the worker counts
as fully loaded for ranking purposes.

`needs_llm` is per **run**, not per worker, because that is where the
knowledge lives. `ConceptSynthesisWorker` only calls a model when its
signature diff found dirty clusters. `IdleAwayActivityWorker` rolls
`away_activities_llm_ratio` per beat and is often pure template. A
static per-worker flag would be wrong for both.

The probe must be far cheaper than `run()` — a `COUNT`, a `kv_meta`
read, an in-memory mirror scan. The scheduler keeps an EMA of each
probe's wall time, warns when a single probe passes 50ms, and once the
EMA passes 50ms stops probing that worker entirely and drops it back to
interval scheduling. Probing that is not much cheaper than doing turns a
scheduling win into a per-tick tax.

Returning `None` means "no opinion, schedule me the old way".

## Urgency: how the scheduler ranks

```
urgency = 0.7 * pressure + 0.3 * staleness
staleness = clamp(seconds_since_last_run / interval_seconds, 0, 1)
```

Pressure dominates, because serving real backlog first is the whole
point. Staleness is the minority term that keeps a persistently
low-pressure worker from being starved forever by noisier neighbours.

`interval_seconds` still exists but has been **reinterpreted as a
heartbeat**, not a cadence. It does two jobs now: it is the denominator
of staleness, and it is a liveness backstop — once a worker is a full
heartbeat overdue it is admitted regardless of pressure, so a broken
probe degrades to the old behaviour instead of silencing the worker.

Underneath sits an anti-thrash floor, `min_interval_s = max(wake_seconds,
interval_seconds * min_interval_ratio)`. Below that floor a worker is
refused no matter how much pressure it reports. Deriving it from the
heartbeat is what lets one ratio serve intervals spanning three orders of
magnitude: at the defaults, a 30-second `gap_resolver` floors at one
tick while an 86400-second `topic_graph_rebuild` floors at 2.4 hours.

The admission verdict carries a reason, which is what the status tool
reports: `first_run`, `pressure`, `heartbeat`, `legacy` (admitted), or
`idle`, `floor`, `below_threshold`, `lane_full` (not).

## The two lanes

Each admitted worker is charged to one of two budgets:

| Lane | Budget key | Scaled by |
| --- | --- | --- |
| `compute` — no LLM call | `idle_worker_compute_budget_ms` | idle depth |
| `llm` — will call a model | `idle_worker_tick_budget_ms` | idle depth **and** contention grade |

The split exists because the original 6000ms was protecting a GPU, and a
worker doing arithmetic is not competing for one. Compute drains first
within a tick, so cheap work is never stuck behind a multi-second
generation or pushed to a later tick by one. That ordering costs the LLM
lane almost nothing, since compute work is milliseconds.

This is also the answer to "can we run the non-LLM ones in parallel" —
lane-major ordering gets most of that benefit without any of the
thread-safety problems. See [Known gaps](#known-gaps).

## Idle depth

The longer you have been gone, the less a long tick costs you and the
more there is to catch up on. Both lanes scale by a multiplier keyed on
seconds since your last activity:

| Tier | Up to | Multiplier |
| --- | --- | --- |
| `just_left` | 5 min | 1x |
| `away` | 30 min | 3x |
| `long_away` | 4 h | 6x |
| `overnight` | — | 10x |

`just_left` is deliberately 1x, so shallow idle behaves exactly as it
did before P36 and the risk of the new machinery is confined to windows
where you are demonstrably not there.

Depth is the max of the in-process monotonic elapsed time and the
wall-clock gap since a timestamp persisted to `kv_meta` on every user
activity. The monotonic value is the trustworthy one while the process
lives; the persisted one is what rescues depth after a restart
mid-absence, which would otherwise reset an overnight gap to zero and
throw away exactly the window worth using.

`idle_worker_depth_max_multiplier` caps the whole table; set it to 1.0 to
disable depth scaling without touching anything else.

## LLM contention grades

Whether the LLM lane may take its depth multiplier depends on how badly
worker LLM calls actually fight the chat path. The grade is derived per
tick by comparing the `main_chat` and `worker_default` routes, so
editing the route table in the settings drawer widens or narrows the lane
on the next tick rather than the next restart.

| Grade | Topology | Effect |
| --- | --- | --- |
| `none` | Different backends, or either side is not local Ollama | Full depth multiplier |
| `queueing` | Same local Ollama, same model | Full depth multiplier |
| `swapping` | Same local Ollama, **different** model | Pinned to 1x through `just_left` and `away` |

`swapping` is the destructive case: Ollama evicts the chat model to load
the worker model, so one background call can cost your *next* turn a full
model reload even with a 30-minute `keep_alive`. Pinning it at shallow
depth means a returning user never pays that. From `long_away` on the
reload amortises against a long absence and the lane opens up.

`none` and `queueing` currently behave identically. They are kept as
separate grades because they are diagnostically different — the status
tool tells you which one you are in — and inventing a behavioural
difference just to justify the third name would be worse than admitting
there is not one yet.

A missing `worker_default` route reads as `queueing`, because the
controller then serves workers from the chat client itself — literally
the same model on the same endpoint.

Auto-detection normalises loopback aliases (`localhost` and `127.0.0.1`
are the same GPU) and errs toward the stricter grade whenever the
comparison is ambiguous, such as a missing route or an unrecognised
provider. A wrong guess in that direction costs some background
throughput; the opposite mistake costs your first token.
`idle_worker_contention_override` forces a grade for the cases where the
topology lies, such as a "remote" endpoint that is really the same box.

## Fit rules and anti-starvation

A worker starts only if its estimated cost (an EMA of its own past runs,
or 250ms if it has never run) fits what is left of its lane. The
exceptions matter, because the pre-P36 rule exempted the first worker of
every tick from the budget entirely — which meant a worker averaging 45
seconds was admitted on every single tick, and `tick_budget_ms` bounded
only slots two onward. Since a returning message queues behind whatever
is running rather than cancelling it, that unbounded first run was the
real worst-case wait.

So the first-slot exemption now applies only from the `away` tier on,
with two escape valves so the tightened rule does not become a trap:

- **A worker that has never run is always exempt.** It has no measured
  cost, and refusing it on the strength of a guessed estimate would mean
  never measuring it — it could be excluded permanently.
- **Past three heartbeats, admit regardless.** Someone who checks in
  every few minutes pins depth at `just_left` indefinitely, and without
  this a long worker would never see a tick it fits in.

## Legacy workers

Eighteen workers implement `demand()` today: five world (`plant_growth`,
`garden_visit`, `circadian_settle`, `room_evolution`, `away_activity`),
four thinking (`concept_lifecycle`, `concept_synthesis`,
`concept_consolidation`, `memory_decay`), eight cue producers
(`curiosity_seed`, `forward_curiosity`, `associative_wander`,
`curiosity_gradient`, `interest_drift`, `knowledge_gap_notice`,
`dormant_interest`, `self_callback`) and the `mood_drift` sampler.

The cue producers all report the same shape of pressure — the deficit
between the pending rows on their shelf and `CuePolicy.inventory_target`
— and get it from [`CueProducer`](../app/core/proactive/cue_producer.py)
rather than writing it out; see [`cue-pool.md`](cue-pool.md). The
`mood_drift` sampler is the other end of the range: a pure heartbeat,
full pressure until today's sample lands and nothing after.

The rest — around thirty — do not, and are handled by an explicit legacy
path in `evaluate_admission`: already ready means already admitted,
ranked by staleness alone, and charged to the **LLM lane**, because
without a signal we cannot know they are cheap. That reproduces
pre-P36 behaviour for them. Migration is tracked as
[P44](personality-backlog/perf.md#p44-migrate-the-remaining-idle-workers-to-demand).

## Writing or migrating a worker

Implement `demand()`, and split the existing `is_ready()` in two. Hard
vetoes stay: feature flags, cold-start guards, rate limiters, anything
that means "must not run, full stop". The
`default_is_ready(self.interval_seconds, …)` timing check **moves into
the probe**. Leaving it in `is_ready()` vetoes the worker before its
pressure is ever read, silently disabling the mechanism for it — this is
the one mistake that fails quietly.

`PlantGrowthWorker` is the smallest complete example:

```python
# app/core/world/plant_growth_worker.py, abridged
class PlantGrowthWorker:
    """Promote one stage per due plant per sweep."""

    name = "plant_growth"

    def is_ready(self, *, now, last_run_at) -> bool:
        # No hard veto: this worker has no feature flag and no cold-start
        # guard. The interval that used to gate here is now the heartbeat
        # and the demand() probe decides the rest (P36).
        return True

    def demand(self, *, now, last_run_at) -> "WorkSignal | None":
        # ... the same walk run() does, minus the writes ...
        due = sum(1 for item in plants if stage_promotion_due(item, now=now))
        return WorkSignal(
            pressure=pressure_from_count(due, saturation=3),
            reason=f"{due} due",
        )
```

Note `stage_promotion_due`: extracting a read-only predicate from the
mutating `promote_stage` was necessary because `list_items` hands back
live mirror references, so the naive probe would have promoted the
plants it was only supposed to count. **A probe must not mutate.** If
the cheap read does not exist yet, factor it out of `run()` rather than
letting the probe call the mutating path.

Other things worth knowing:

- Set `needs_llm` from the same condition `run()` will use to decide
  whether it calls a model. Guessing pessimistically wastes LLM lane;
  guessing optimistically puts a generation in the compute lane.
- Return `None` from a probe that fails. Exceptions are caught and
  treated the same way, but returning is cheaper and clearer.
- Give `reason` something short and specific (`"12 stale"`, `"cooldown
  4m"`). It is displayed verbatim in `probe_idle_worker_demand` and is
  usually the fastest way to see why a worker is or is not running.
- Keep `interval_seconds` meaningful. It is no longer the cadence, but
  it is still the staleness denominator, the heartbeat backstop, and the
  basis of the anti-thrash floor.

**The rule for removing config keys:** a deleted config key must be
replaced by the worker *knowing* something, not by a hardcoded constant.
That is the bar for retiring the remaining `*_interval_seconds` and
`*_per_hour_cap` keys.

## Observability

Every tick that had due workers logs one line:

```
idle_workers tick: ran=2 due=7 admitted=3 skipped_budget=1 tick_ms=412
  depth=away(1204s) contention=queueing compute_ms=18000 llm_ms=18000
  names=plant_growth,concept_lifecycle
```

`due` counts workers that passed `is_ready()`; `admitted` counts those
the demand evaluation actually accepted. A large gap between them is the
mechanism working — workers were asked and said they had nothing to do.
`ran` below `admitted` means a lane filled up, `max_per_tick` was hit,
or the user came back mid-tick (which also appends `stopped_early=1`).

Four MCP debug tools, over the server at `http://localhost:6274/sse`:

- **`get_idle_workers_status`** — the full picture. Header gives
  `idle_seconds`, `idle_depth`, `depth_multiplier`, `contention`, and
  both effective lane budgets after scaling. Per worker: `demand_aware`,
  `last_pressure`, `last_urgency`, `last_admit_reason`, `last_lane`,
  `last_probe_reason`, `avg_probe_ms`, `min_interval_seconds`, plus the
  older run/error/duration fields.
- **`probe_idle_worker_demand`** — ask one worker (or all of them) for a
  signal right now, without running anything. The first thing to reach
  for when a worker is not firing.
- **`force_idle_worker`** — run any registered worker once, bypassing
  every gate. Ignores quiet, readiness, pressure, floors, and lanes.
- **`inspect_idle_workers`** — the older, terser run-state dump.

Two useful tricks when testing depth and contention by hand: idle depth
reads a `kv_meta` key (`idle.last_user_activity_at`), so backdating that
row simulates an absence without waiting for one; and the contention
grade is recomputed per tick from the route table, so pointing
`worker_default` at a different model on the same Ollama flips you into
`swapping` on the next tick.

## Configuration

All under `memory.` in `config/default.json`. Full descriptions in
[`configuration.md`](configuration.md).

| Key | Default | What it does |
| --- | --- | --- |
| `idle_worker_wake_seconds` | 30 | Tick period |
| `idle_worker_quiet_threshold_seconds` | 20 | Quiet gate |
| `idle_worker_tick_budget_ms` | 6000 | LLM lane base budget |
| `idle_worker_compute_budget_ms` | 6000 | Compute lane base budget |
| `idle_worker_max_per_tick` | 0 | Hard cap on runs per tick (0 = unlimited) |
| `idle_worker_pressure_enabled` | `true` | Master switch for demand-driven scheduling |
| `idle_worker_urgency_threshold` | 0.35 | Minimum urgency for pressure-based admission |
| `idle_worker_min_interval_ratio` | 0.1 | Anti-thrash floor as a fraction of heartbeat |
| `idle_worker_depth_max_multiplier` | 10.0 | Cap on depth scaling (1.0 disables) |
| `idle_worker_contention_override` | `auto` | Force a contention grade |

## Turning it off

`memory.idle_worker_pressure_enabled: false` restores the pre-P36 tick
exactly: one budget, oldest-first ranking, no probes, slot one exempt
from the fit check at every depth. The legacy path is kept verbatim
rather than emulated, so it is a genuine escape hatch if demand-driven
scheduling misbehaves.

Softer options: set `idle_worker_depth_max_multiplier` to 1.0 to keep
demand ranking but pin budgets at their base sizes, or set
`idle_worker_contention_override` to `swapping` to keep the LLM lane
conservative while letting compute scale.

## Known gaps

- **[P44](personality-backlog/perf.md#p44-migrate-the-remaining-idle-workers-to-demand)**
  — the other forty workers still ride the legacy path, which also means
  they are all charged to the LLM lane whether or not they touch a model.
- **[P45](personality-backlog/perf.md#p45-retire-the-per-hour--per-day-caps-in-favour-of-satisfaction)**
  — partly done. The seven cue workers on the
  [cue pool](cue-pool.md) report pressure from their unspent inventory,
  which retired five `*_daily_cap` keys. The three event-armed types that
  joined the pool later need no scheduling work at all — nothing produces
  them, so they carry `inventory_target=0` and never ask for a slot. The
  remaining `*_per_hour_cap`
  keys on other workers are still fixed numbers. The intended
  replacement there is the same idea: a worker that produced a cue
  nobody engaged with should report low pressure because it has said
  enough, not because a counter hit five.
- **[P46](personality-backlog/perf.md#p46-parallel-compute-lane-drain)**
  — the compute lane drains sequentially. True parallelism is blocked on
  shared mutable state, notably the `WorldStore` in-memory mirror and
  SQLite connection affinity; lane-major ordering was the cheap way to
  get most of the win.
