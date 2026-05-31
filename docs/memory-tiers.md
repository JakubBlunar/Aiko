# Memory tiers, revival drift, and the IdleWorker framework

*Schema v8 (May 2026). Ships E1 + E2 + G1 from the personality backlog
in a single migration.*

The long-term memory store was a flat pool: a half-baked observation
from yesterday sat next to a year-old relationship anchor, and
`decay()` / `prune()` treated them with the same coarse
`salience + use_count` heuristic. Schema v8 splits writes into three
tiers, makes decay proportional to elapsed wall-clock time, and
rewards memories Aiko actually cites with persistent salience drift.

This document covers the runtime model, the configuration knobs, and
the producer rules every new worker should follow.

## TL;DR

- Every memory row has a `tier` in
  `{scratchpad, long_term, archive}` and a `revival_score` in `[0, 1]`.
- New auto-extracted observations land in **scratchpad**. They decay
  fast and either get **promoted** to `long_term` (use_count + age,
  or revival_score) or **deleted** after `scratchpad_ttl_days`.
- Verified anchors (promises, catchphrases, shared moments, manual
  edits, `[[remember:...]]` tags, relationship pulses, milestones) go
  straight to **long_term**.
- Stale `long_term` rows demote to **archive** after
  `archive_demote_idle_days` of silence (and `revival_score < 0.05`).
  Archive decays at 0 — the past is allowed to be cold.
- Decay is **wall-clock-driven**: `MemoryStore.decay` reads the
  persisted anchor `memory.last_decay_run_at` from `kv_meta`,
  computes elapsed days (clamped by `decay_max_catchup_days`), and
  applies proportional decay + a revival rebate. Running once an
  hour applies 1/24 of a day's worth.
- Decay + promotion run through a shared **`IdleWorkerScheduler`**
  that fires during quiet windows (no Live mode, no recent user
  activity). Future workers (F1 fact-checker, G2 schedule learner,
  G3 curiosity) plug into the same scheduler.

## Tiers in detail

| Tier | Decay/day (default) | RAG offset | Cap (default) | Notes |
|------|---------------------|------------|---------------|-------|
| `scratchpad` | `0.05` | `-0.02` | `1000` | Probationary lane. Speculative LLM observations land here. |
| `long_term` | `0.02` | `0.0` | `5000` (`memory.max_memories`) | Verified anchors live here. Pinned rows are coerced here. |
| `archive` | `0.0` | `-0.03` | `10000` | Cold history. Surfaces only on strong cosine matches. |

The RAG offset is a small additive nudge applied in
[`rag_retriever.py`](../app/core/rag/rag_retriever.py) (`_MEMORY_TIER_OFFSET`)
so verified anchors win ties against speculative scratchpad siblings,
and archive rows need a stronger match to break out.

### Promotion / demotion rules

Run by `MemoryPromotionWorker`
(`app/core/memory/memory_promotion_worker.py`), default cadence is hourly
(configurable via `memory.promotion_worker_interval_seconds`).

- **Promote scratchpad → long_term** when either
  - `age_days ≥ memory.scratchpad_promote_min_age_days (default 7)`
    AND `use_count ≥ memory.scratchpad_promote_min_use_count (3)`, OR
  - `revival_score ≥ memory.scratchpad_promote_min_revival (0.3)`.
- **Delete scratchpad** when
  `age_days ≥ memory.scratchpad_ttl_days (14)` AND `use_count == 0` AND
  `revival_score == 0`.
- **Demote long_term → archive** when the row is **not pinned**,
  `revival_score < 0.05`, AND
  `idle_days ≥ memory.archive_demote_idle_days (180)`. Idle is measured
  from `last_used_at` (falling back to `created_at`).
- **Coerce pinned rows back to `long_term`.** Defense in depth — the
  store also does this on `set_pinned`, `add`, `update`. Pinning is the
  user's "always keep this" signal; pinned rows must never sit in
  scratchpad or archive.

After each sweep the worker calls `MemoryStore.prune()` so any tier
that grew past its cap during promotion is trimmed back.

## Revival drift (E2)

`revival_score` is a tiny per-row counter that tracks how often Aiko
actually cited the memory in her reply.

- After every turn, `SessionController._mark_revived_memories` reads
  the surfaced-IDs snapshot off the `RagRetriever`, computes a
  content-word set for Aiko's reply (4-char minimum, stopwords
  dropped), and bumps `revival_score` by
  `memory.revival_per_hit (default 0.15)` on memories whose content
  shares at least `memory.revival_min_word_overlap (3)` words with the
  reply. Clamped to `[0, 1]`.
- On every decay tick (per-tier), `MemoryStore.decay` applies a
  rebate before the decay step:
  `salience += revival_coefficient * elapsed_days * revival_score`.
  Then `salience -= rate * elapsed_days`. The clamp is `[0, 1]`.
- `revival_score` itself decays at `revival_decay_per_day (0.02)` so
  a one-time spike fades without permanently locking in the rebate.

Net effect: memories that Aiko keeps citing drift toward
`salience = 1.0` and behave like soft pins; memories with zero revival
across a long window decay faster than the baseline. The promotion
worker also reads `revival_score ≥ 0.3` as an early-promotion signal,
so a freshly-cited scratchpad row escapes the probationary lane on the
next sweep.

## Wall-clock-driven decay

A periodic decay was correct only when the app ran continuously. A
desktop assistant that's offline for two days and then opens for
fifteen minutes shouldn't lose all its memory state. Schema v8 makes
decay **proportional to elapsed wall-clock time**.

- `MemoryStore` keeps an anchor in the new `kv_meta` table under the
  key `memory.last_decay_run_at`.
- On each `decay()` call (or the first call after boot), it reads
  the anchor, computes `elapsed_days = (now - anchor) / 86400`,
  clamps to `memory.decay_max_catchup_days (default 30)` so a 6-month
  absence doesn't zero everything, and applies decay scaled by
  `elapsed_days`.
- On first-ever run (anchor missing), `decay()` just writes the
  anchor and returns — no retroactive penalty.
- The `MemoryDecayWorker` (`app/core/memory/memory_decay_worker.py`)
  ticks hourly by default (`memory.decay_worker_interval_seconds`).
  Idempotent: calling it more often is safe, just wastes a little CPU.

## The IdleWorker framework (G1)

Both new workers run through a single
[`IdleWorkerScheduler`](../app/core/proactive/idle_worker_scheduler.py) instead of
each owning its own daemon thread. The scheduler:

1. Wakes every `memory.idle_worker_wake_seconds (60s)` (configurable
   down to 0.5s for testing).
2. Asks an `is_quiet_callback` whether it's safe to run. In production
   this is `SessionController._is_user_idle`:
   `not live_mode AND not turn_in_progress AND
   seconds_since_last_activity ≥ idle_worker_quiet_threshold_seconds`.
3. Asks each registered worker `is_ready(now, last_run_at)`.
4. Runs **one** ready worker per tick (cap so heavy workers don't
   stack). Ties broken by oldest `last_run_at` so no worker starves.
5. Persists `last_run_at` to `kv_meta` so a restart doesn't refire a
   worker that just completed.

Registering a new worker is two lines:

```python
sched.register(MyWorker(deps...))
```

The protocol lives in `app/core/proactive/idle_worker.py`. Workers expose `name`,
`interval_seconds`, `is_ready`, and `run() -> dict | None`. Errors
are caught, recorded on the per-worker `IdleWorkerRecord`, and surface
through the optional MCP debug tool (force_run / inspect endpoints —
see below).

### When future workers should land here

F1 (fact-checker), F2 (knowledge-gap journal), G2 (schedule learner),
G3 (curiosity worker), G4 (anything else that wants "run me when nobody
is talking") — all should plug into this scheduler. The protocol is
intentionally tiny so any cron-ish background job can fit.

## Configuration knobs

All under `memory.*` in `config/default.json` (or `config/user.json`
overrides).

```jsonc
"memory": {
  "max_memories": 5000,                // long_term cap
  "tiers_enabled": true,               // master kill switch
  "decay_rate_scratchpad": 0.05,       // per day, scaled by elapsed
  "decay_rate_long_term": 0.02,
  "decay_rate_archive": 0.0,
  "revival_coefficient": 0.05,         // rebate per day, per revival_score
  "revival_per_hit": 0.15,             // bump per overlap detection
  "revival_decay_per_day": 0.02,
  "revival_min_word_overlap": 3,
  "scratchpad_ttl_days": 14,           // delete-if-untouched threshold
  "scratchpad_promote_min_age_days": 7,
  "scratchpad_promote_min_use_count": 3,
  "scratchpad_promote_min_revival": 0.3,
  "archive_demote_idle_days": 180,
  "scratchpad_cap": 1000,              // per-tier cap (long_term reuses max_memories)
  "archive_cap": 10000,
  "decay_max_catchup_days": 30.0,      // wall-clock catch-up clamp
  "promotion_worker_interval_seconds": 3600,   // hourly by default
  "decay_worker_interval_seconds": 3600,
  "idle_worker_wake_seconds": 60.0,
  "idle_worker_quiet_threshold_seconds": 30
}
```

Drop the worker intervals down to ~60 for active testing — both
workers are idempotent, so running them more often is harmless.

## Producer rules

When adding a new producer that calls `MemoryStore.add(...)`, pick a
tier explicitly based on the trust level of the signal:

- **`scratchpad`** — speculative LLM journal output. Today:
  `MemoryExtractor` (LLM-distilled facts), `ReflectionWorker`
  (observations / open questions / callbacks),
  `DreamWorker`.
- **`long_term`** — user-confirmed or self-confirmed anchors. Today:
  `PromiseExtractor`, `CatchphraseMiner`, `RelationshipPulse` (Aiko's
  own stance self-tags), `SharedMoments`, the manual REST/UI add
  path, `[[remember:...]]` and `[[remember:self:...]]` tags via
  `TurnRunner`, milestone memories via
  `SessionController._record_milestone_memory`.
- **`archive`** — leave to the `MemoryPromotionWorker` and pinning
  rules. No producer writes directly to archive in v8.

Default is `long_term` if you forget — safer than leaving an
unverified observation eligible for promotion via the wrong lane.

## Storage shape

Schema v8 adds three things to the on-disk database:

```sql
ALTER TABLE memories ADD COLUMN tier TEXT NOT NULL DEFAULT 'long_term';
ALTER TABLE memories ADD COLUMN revival_score REAL NOT NULL DEFAULT 0.0;
CREATE INDEX idx_memories_tier ON memories(tier);

CREATE TABLE kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Reserved `kv_meta` key namespaces:

- `memory.last_decay_run_at` — wall-clock anchor for decay.
- `idle_worker.<name>.last_run_at` — per-worker last-run timestamp.

Existing rows default to `tier='long_term'` and `revival_score=0.0`,
which preserves pre-v8 behavior (uniform decay rate on a single pool).

## REST surface

```
GET /api/memories?tier=scratchpad&order=top&limit=50
GET /api/memories/counts
   -> {"scratchpad": 4, "long_term": 12, "archive": 3, "total": 19}
PATCH /api/memories/{id}  body: {"tier": "scratchpad"}
POST  /api/memories       body: {"content": "...", "tier": "long_term"}
```

Each memory row in the JSON now includes `tier` and `revival_score`.

## Frontend

The Memory tab grew:

- A **tier pill** on each row (color-coded: amber for scratchpad,
  emerald for long_term, slate for archive). Hover gives a tooltip
  explaining the tier's behavior.
- A **tier filter** dropdown next to the existing kind filter.
- A **counts header** showing per-tier totals from
  `/api/memories/counts`. Filtering by tier doesn't change the
  counts.
- A **revival % readout** that appears when `revival_score > 0.05`.

State lives in `useAssistantStore.memoryView`
(`web/src/store.ts`); the new fields are `tierFilter` and `counts`,
the new setters are `setMemoryTierFilter` and `setMemoryCounts`.

## Debugging from MCP

If the embedded MCP server is running, you can poke the new machinery
from Cursor without restarting the app:

- `force_promotion_sweep` — runs the promotion worker once and
  returns the result dict (`promoted`, `deleted_scratchpad`,
  `demoted_archive`, `coerced_pinned`, `pruned`).
- `force_decay_sweep` — runs the decay worker once.
- `inspect_idle_workers` — returns the per-worker
  `IdleWorkerRecord.to_dict()` snapshots (`last_run_at`,
  `last_error`, `run_count`, `last_result`).

Add new MCP debug tools in `app/mcp/server.py` whenever the runtime
state isn't directly exposed; the server has full access to
`SessionController` internals (`session._idle_scheduler`,
`session._memory_store`, etc.).
