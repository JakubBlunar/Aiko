# Shipped -- Tools & dev tooling (D / DT-series)

Part of the [shipped log index](../shipped.md). Capabilities Aiko can call and
the debug tooling we use to build her. The image-vision work (D2, both parts)
lives with the task machinery in
[`proactive-tasks.md`](proactive-tasks.md#d2-part-a-local-vision-describe_image-task--one-model-no-cloud).
Open items still live in [`tools.md`](../tools.md).

---

## DT1. Virtual clock / time-travel for time-gated features — SHIPPED

**Motivation.** A large fraction of Aiko's behaviour is **wall-clock
gated**, which makes it brutal to verify end-to-end in the live app:
memory decay + tier promotion (schema v8), anniversaries + milestones
(J8), the cooldowns on nearly every inner-life cue, reconnection /
gap-return (J5 / K28 / K36), day colour (K27), routine learning (K3),
vulnerability-budget regen (K15), the conflict-repair watch window (J6).
The only way to exercise these in a running instance used to be to **wait
real hours or days**.

**Status.** Shipped, gated behind `AIKO_DEBUG_CLOCK=1`.

The seam already existed and was built for this:
[`timephrase.now()`](../../../app/core/infra/timephrase.py) routes through a
swappable `_now_provider` whose docstring names DT1 as its intended
consumer — but nothing in production ever called `set_now_provider`, and
only five call sites used `timephrase.now()`. The work was **adopting**
the seam, not designing one.

[`DebugClock`](../../../app/core/infra/debug_clock.py) holds an in-memory
`timedelta` offset and installs itself as the provider. Offset-based
rather than absolute, so time keeps *flowing* while shifted and the
un-virtualised monotonic paths stay coherent. A new `timephrase.utcnow()`
gives the UTC-normalised twin the rest of the app needs (`now()` stays
local for its five existing callers), and ~60 per-module `_utcnow()` /
`_now_iso()` helpers now delegate to it. Two of those redirects carry most
of the leverage: `IdleWorkerScheduler._utcnow()` is the single `now`
handed to every worker's `is_ready()`, and the `clock=` constructor
defaults on ~20 workers.

**Two levers, because wall-clock is not the interesting one.** Concept
(L3) and memory decay run on
[`EngagementClock`](../../../app/core/infra/engagement_clock.py) —
accumulated *active-conversation* seconds — so `advance_clock(days=60)`
does nothing to them at all. `advance_engagement(days)` credits that
counter instead. It is the one persisted, destructive operation, so it
stashes an undo anchor (`engagement.debug_anchor`) that `reset_clock`
restores.

Five MCP tools in
[`debug_clock_tools.py`](../../../app/mcp/server_tools/debug_clock_tools.py):
`get_clock_status`, `advance_clock`, `set_clock`, `advance_engagement`,
`reset_clock`. A live offset is echoed into `get_status` and logged at
WARNING on every advance, so it can never be silently in effect.

**The dividing line:** `datetime`-based *narrative* time moves; every
`time.monotonic()` / `time.time()` reader stays real. That rule maps
almost exactly onto the must-not-virtualise list (LLM latency, TTS/STT
audio, brain loop, HTTP timeouts, tick budgets, worker perf metrics are
all monotonic), so the safety boundary came nearly for free. Also left on
real time on purpose: log + crash timestamps, `app/core/tasks/` stall
detection (a runtime timeout compared against real wall time in
`task_heartbeat.py` — virtualising the stamps but not the comparison
would manufacture phantom stalls), and outbound weather API dates.

**Acceptance.** Against a copy of a live `chat_sessions.db`, simulating 60
engaged days took ~90 seconds and moved mean active-concept confidence
0.817 → 0.483, with concepts crossing the dormant floor — a transition the
L22 scoreboard says needs ~54 hours of real conversation to observe.

**Known sharp edges.** Rows written while advanced keep their virtual
timestamps after a reset; nothing can rewrite them, so run against a
database copy. And L3 decay is catch-up-clamped per sweep
(`concept_decay_max_catchup_days`, default 3), so a long simulation means
interleaving `advance_engagement(3)` with `force_concept_lifecycle`
rather than one big jump.

**Workflow:** [`rules/debugging.md` §f](../../../rules/debugging.md).
**Tool reference:** [`rules/mcp-server.md`](../../../rules/mcp-server.md).
