# Brain orchestration: BrainEventQueue, BrainLoop, and the Task system

*Schema v16. Phase 1 of the brain-orchestration refactor. Lays the
foundation for user-initiated long-running tasks while migrating every
existing conversational producer through a single-consumer event queue.*

Today, every input that wants the brain's attention lives on its own
thread with cooperative boolean gates. Typed messages enter on
`web-chat-turn`, voice on the `live-session` thread, MCP `send_message`
on a uvicorn worker, idle workers on the `idle-worker-scheduler` daemon,
proactive nudges on a freshly-spawned thread per fire, speaking-window
jobs on a drain thread, and the summary worker on its own loop. They
coordinate by reading `_turn_in_progress` and a handful of similar
flags. There is **no global turn mutex**: two concurrent typed messages
can race past every check and both call `TurnRunner.run()` at the same
time, and MCP `send_message` bypasses the per-WebSocket `active_turn`
guard entirely.

This is fine for synchronous tools and silent idle housekeeping, but it
falls over the moment Aiko needs to do *long, user-initiated work that
can pause for input and report back* — the canonical example being
"open YouTube and play some lofi". The brain has to spawn the work,
keep talking, and weave the eventual result into the conversation
without ever cutting Aiko off mid-sentence.

This document covers the runtime model, the contract every producer
must follow, the persistent Task abstraction (schema v16), and the
phase 1 reference handler that exercises every state-machine path.

## TL;DR

- A new **`BrainEventQueue`** is a priority heap with one consumer
  (`BrainLoop`). All conversational inputs flow through it.
- Eight event kinds, eight priorities, deterministic ordering. User
  input always wins; task completions defer behind active TTS.
- The `BrainLoop` thread runs `TurnRunner.run()` directly on its own
  thread, not a per-request daemon. Two concurrent typed messages now
  serialize for free; MCP and WS share the same lock.
- A new persistent **`tasks`** table (schema v16) holds long-running
  work. Each row carries a JSON state blob the handler resumes from
  on boot.
- Task completions never interrupt. They **park as one-shot cues** on
  `SessionController._pending_task_cues` (same shape as the K32
  `_pending_user_reactions` list). `PromptAssembler` renders them as a
  T6 system block on Aiko's next turn. If silence stretches past
  `task_completion_proactive_after_seconds`, the loop escalates to a
  `ProactiveEvent` so Aiko speaks unprompted.
- Phase 1 ships one real handler — **`file_search` + `file_read`** —
  sandboxed to a single configurable root. Between them they exercise
  every terminal state (`done`, `failed`, `awaiting_input → done`,
  `awaiting_input → cancelled`).
- Existing `IdleWorkerScheduler` is **not** absorbed. It cooperates by
  raising a `MaintenanceDueEvent` on its wake tick; the BrainLoop
  runs `IdleWorkerScheduler._tick()` on its own thread (gated on the
  free-to-speak predicate, sequential with turns by construction).

## Architecture

```
                ┌──────────────────────────────────────────┐
                │       BrainEventQueue (priority heap)    │
                │   P0  UserMessage     (typed/voice/MCP)  │
                │   P1  TaskInputNeeded                    │
                │   P2  TaskResult                         │
                │   P3  Proactive                          │
                │   P4  SpeakingWindowJob                  │
                │   P5  TaskProgress           (UI only)   │
                │   P6  MaintenanceDue                     │
                │   P7  StateSync             (presence,   │
                │                              reactions)  │
                └────────────────┬─────────────────────────┘
                                 │  single consumer
                                 ▼
                ┌──────────────────────────────────────────┐
                │   BrainLoop (the brain-loop daemon)      │
                │   free-to-speak = NOT turn_in_progress   │
                │                   AND NOT tts_active     │
                │   pop event → gate? → route → handler    │
                └──┬───────────────────────────────┬───────┘
                   │ tool call: start_file_search  │ events back
                   ▼                               │
                ┌──────────────────────────────────┴───────┐
                │           TaskOrchestrator               │
                │   - handler registry                     │
                │   - SQLite-backed state machine          │
                │     running / awaiting_input / paused /  │
                │     done / failed / cancelled /          │
                │     interrupted                          │
                │   - per-user soft cap                    │
                └──┬───────────────────────┬───────────────┘
                   ▼                       ▼
            FileSearchHandler       FileReadHandler
                                                (more in phase 2)
```

**The single invariant of phase 1:** at any moment, exactly one of the
following is happening — a `TurnRunner.run()`, a `ProactiveDirector`
run, an `IdleWorkerScheduler._tick()`, a `SpeakingWindowScheduler` job,
or nothing. The queue's single consumer enforces this by construction.
The pre-refactor cooperative-boolean choreography becomes a property of
the architecture instead of a checklist contributors have to remember.

## Event taxonomy

Every brain input is a `BrainEvent` — a frozen dataclass with a `kind`
discriminator and a per-kind payload. Priorities are an `IntEnum`
where lower wins. Tie-breaker: monotonic enqueue sequence.

| Priority | Kind | Producer | Routes to | Bypasses free-to-speak? |
|---|---|---|---|---|
| P0 | `user_message` | WS typed chat, `LiveSession`, MCP `send_message` | `TurnRunner.run()` | Yes — user input is barge-in |
| P1 | `task_input_needed` | `TaskOrchestrator` | park cue + escalate after silence | No |
| P2 | `task_result` | `TaskOrchestrator` (done/failed/cancelled) | park cue + escalate after silence | No |
| P3 | `proactive` | Voice silence timer, typed silence timer, escalated task cue | `ProactiveDirector` | No |
| P4 | `speaking_window_job` | Post-turn submits, TTS drain | existing job callable | No — these never speak |
| P5 | `task_progress` | Handler emit | WS broadcast only, no LLM | No — UI-only by hard rule |
| P6 | `maintenance_due` | `IdleWorkerScheduler` wake | `IdleWorkerScheduler._tick()` | No — defers behind speech |
| P7 | `state_sync` | WS presence, REST reaction, world gift | state mutation, no LLM | No — these never speak |

User input **always** wins. A `task_result` parked behind active TTS
yields the moment Aiko's audio drains; a `user_message` arriving in the
same window pre-empts it. Barge-in is real intent.

## BrainLoop consumer semantics

Single daemon thread `brain-loop`. Loop body:

1. Pop highest-priority event.
2. **Free-to-speak gate** — AND of two flags:
   - `_turn_in_progress` (text streaming, today)
   - `_tts_active` (new flag toggled by
     `SpeakingWindowScheduler.on_tts_state("start"/"end")`, which we
     already track for the speaking-window drain)
3. If the event would speak AND the gate is held → **re-park** it (see
   *Task completion delivery* below) and sleep on a condition variable
   until both flags clear. Re-park is idempotent.
4. Route to handler:
   - `user_message` → run `TurnRunner.run()` body directly on the
     brain-loop thread.
   - `task_input_needed` / `task_result` → park as one-shot cue; the
     next `user_message` consumes it via prompt assembly. May escalate
     to a `proactive` event after silence.
   - `proactive` → existing `ProactiveDirector._run` /
     `_run_typed` path.
   - `maintenance_due` → `IdleWorkerScheduler._tick()` (also gated on
     free-to-speak — maintenance never runs over Aiko's voice either).
   - `speaking_window_job` → existing job callable.
   - `task_progress` → broadcast WS, no LLM contact.
   - `state_sync` → mutate state, optionally arm a follow-up event.
5. On exception: log + record on the event, **never crash the loop**.

`MCP send_message` becomes synchronous on the caller thread by
enqueuing a `user_message` with a `Future` attached; the loop fills
the future with the assistant reply. This preserves the existing
return-the-reply contract while routing through the queue, and fixes
the no-global-mutex bug for free: two concurrent `send_message` calls
now serialize cleanly.

## Task completion delivery (the no-interrupt invariant)

A task completing while Aiko is mid-sentence must never cut her off,
but the completion still needs to land as a natural acknowledgment.
Two delivery modes, picked by the BrainLoop.

### Fold mode (default)

When a `task_result` or `task_input_needed` event is popped:

1. Park the event onto `SessionController._pending_task_cues` (same
   one-shot shape as the existing K32 `_pending_user_reactions` list).
2. Broadcast the WS event immediately for UI (the task strip chip
   updates; the chat transcript is unchanged).
3. Do **not** speak.

On the **next** `user_message` event for this user,
`PromptAssembler.assemble_with_budget` reads `_pending_task_cues`,
renders a T6 system block, and clears the list after assembly. Aiko's
reply naturally weaves the acknowledgment in ("oh, and the music's
playing — anything you want me to find next?"). Multiple completions
aggregate into one paragraph.

### Proactive escalation

When a cue is parked, BrainLoop arms a timer for
`agent.task_completion_proactive_after_seconds` (default `45`). If
`_pending_task_cues` is still non-empty at fire time AND the
free-to-speak gate is clear AND no `user_message` has arrived, the
loop enqueues a `proactive` event carrying the parked cues. The
existing `ProactiveDirector` path handles the actual speech (text-only
for typed sessions, with-TTS for voice). After the proactive turn
assembles, cues clear the same way.

`task_input_needed` uses a shorter window
(`agent.task_input_needed_proactive_after_seconds`, default `20`) —
a blocked task is more pressing than a finished one.

### Two-axis visibility

Tasks have two independent visibility flags on the row:

| Flag | Controls | Default | Internal-brain task |
|---|---|---|---|
| `notify_aiko` | Does completion park a cue for Aiko's prompt? | `true` | `false` |
| `visible_to_user` | Does the task show up in the TaskStrip / WS / `GET /api/tasks`? | `true` | `false` |

Common combinations:

- **User-initiated** (Aiko called `start_task` because the user asked):
  both `true`. Strip shows the chip, Aiko narrates completion.
- **Internal Aiko-brain task** (e.g. future "pre-fetch context for the
  next turn" or "fact-check this claim asynchronously"): both `false`.
  Pure background work, invisible to UI and to Aiko's prompt.
- **Dev / debug**: `notify_aiko=false`, `visible_to_user=true` —
  developers watch the chip in the strip, but Aiko stays silent. The
  inverse is also valid.

Handlers can flip either flag at completion time — a search that
returns zero results may choose to silently complete instead of saying
"I found nothing".

The existing `IdleWorkerScheduler` (memory decay, reflection, dream,
etc.) does **not** route through the task system in phase 1; those
workers keep their dedicated scheduler and remain invisible by
construction. The `visible_to_user` flag is forward protection for
when internal Aiko-brain work routes through tasks later.

### Aggregation and the failure sub-header

Successes and failures coexist in the same prompt block under
separate sub-headers:

```
Tasks that finished since your last message:
- file_search "Q4 report" — found 3 documents (notes/q4-draft.md, …)
- file_read "today.md" — 4.2 KB read

Tasks that ran into trouble since your last message:
- file_read "huge_log.txt" — file too large (took 280 MB, max is 256 KB)
```

The persona block teaches a slightly apologetic + curious tone for
failures vs the breezy success tone. Hard cap:
`agent.task_cue_max_aggregated=5` per turn (combined). Excess cues stay
in DB / WS so the strip is complete, but get dropped from the prompt
block to keep T6 cheap.

### Stale-cue sweep

Each cue is parked with an enqueue timestamp. On every BrainLoop
dequeue, cues older than `agent.task_cue_max_age_seconds` (default
`1800` = 30 min) drop silently. If the user vanished and never came
back, surfacing "by the way, that YouTube tab I opened 3 hours ago is
still going" reads as awkward.

### Running-tasks inner-life provider

When the user asks "are you still working on X?" mid-task, Aiko needs
live status. A new `InnerLifeProvidersMixin._render_running_tasks_block`
reads `TaskOrchestrator.list_running(user_id)` and renders a T6 block:

```
Tasks running for {user_name} right now:
- file_search "meetings" (started 2 min ago, 60% done)
- file_read "/docs/notes/today.md" (awaiting your input — 3 matches to disambiguate)
```

Capped at top 5 by recency. Only renders when at least one task is
`running` OR `awaiting_input`. When a task is `awaiting_input`, the
pending question is included so Aiko can ask it naturally even if the
user pivoted topics.

### Progress events are UI-only

`task_progress` events update the TaskStrip via WS but **never** park
a cue and **never** escalate. The only way running-task state reaches
the LLM is the running-tasks inner-life provider above. This keeps
progress events cheap (no prompt-cache pressure, no LLM cost per
percent change) while still giving Aiko awareness when the user asks.
Enforced by `tests/test_brain_loop_progress_silent.py`.

## Task state machine

```
                ┌─────────┐
                │ running │◄─────────────────┐
                └────┬────┘                  │
                     │                       │ resume (on_input)
       ┌─────────────┼─────────────┐         │
       │             │             │         │
       ▼             ▼             ▼         │
  ┌────────┐   ┌──────────┐   ┌────────┐    │
  │  done  │   │ failed   │   │awaiting│    │
  └────────┘   └──────────┘   │ _input ├────┘
                              └────┬───┘
                                   │ cancel
                                   ▼
                              ┌──────────┐
                              │cancelled │
                              └──────────┘

  boot recovery: status='running' → status='interrupted'
                 (Aiko sees cue: "the X task stopped, want me to retry?")
```

Tasks check-point state at named transitions only — **never** mid-LLM
or mid-IO. On boot, non-terminal rows are scanned by
`TaskStore.recover_interrupted_on_boot`:

- `running` → demoted to `interrupted`; a cue is parked for Aiko's
  next turn ("the file search I started earlier stopped when we last
  talked — want me to retry?").
- `awaiting_input` → kept as-is; the pending question is still valid.
- `paused` → kept as-is; can be resumed by user action.

The handler protocol does **not** auto-resume — explicit user intent
is required for retry. Auto-resume is a sharper footgun than asking
once.

## Schema v16: the `tasks` table

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    handler_name TEXT NOT NULL,
    args TEXT NOT NULL,                         -- JSON of start() args
    state TEXT NOT NULL,                        -- JSON of TaskState (handler-owned)
    status TEXT NOT NULL DEFAULT 'running',
    title TEXT NOT NULL,                        -- human label for UI
    progress REAL,                              -- nullable 0.0-1.0
    last_message TEXT,                          -- nullable status string
    input_request TEXT,                         -- JSON {prompt, kind?, options?} when awaiting_input
    result TEXT,                                -- JSON when status=done
    error TEXT,                                 -- string when status=failed
    notify_aiko INTEGER NOT NULL DEFAULT 1,     -- 0/1 — park a cue on completion?
    visible_to_user INTEGER NOT NULL DEFAULT 1, -- 0/1 — surface in UI/WS/REST?
    initiated_by TEXT NOT NULL DEFAULT 'aiko',  -- 'aiko' | 'background' | 'system'
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    metadata TEXT                                -- nullable JSON for handler extensibility
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
```

Valid statuses (Python-side `frozenset`):
`running | awaiting_input | paused | done | failed | cancelled | interrupted`.

Conventions copied from `agenda` + `beliefs`:
- TEXT ISO-8601 UTC for all timestamps (`_now_iso()` from
  `chat_database.py`).
- `status TEXT` enum validated in Python, not via SQLite CHECK.
- Nullable JSON columns are TEXT, NULL when empty, decode on read with
  try/except → `{}`/`[]`.
- `metadata TEXT` for handler-specific blobs (no schema migration
  needed when a handler adds a new field).

## TaskHandler protocol

```python
class TaskHandler(Protocol):
    name: str  # registry key, e.g. "file_search"

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState: ...
    def resume(self, state: TaskState, emit: TaskEmitFn) -> TaskState: ...
    def on_input(self, state: TaskState, answer: str, emit: TaskEmitFn) -> TaskState: ...
    def cancel(self, state: TaskState) -> None: ...
```

The `emit` callback drives state transitions. Events:

```python
@dataclass(frozen=True)
class TaskProgress:
    progress: float | None = None  # 0.0-1.0
    message: str | None = None     # status string

@dataclass(frozen=True)
class TaskInputNeeded:
    prompt: str
    options: list[str] | None = None  # for click-to-answer UI

@dataclass(frozen=True)
class TaskCompleted:
    result: dict[str, Any]
    notify_aiko: bool | None = None  # override row default

@dataclass(frozen=True)
class TaskFailed:
    error: str
    notify_aiko: bool | None = None
```

`TaskOrchestrator` catches each `emit`, persists the new row state to
SQLite at every named transition, and enqueues the matching
`BrainEvent`. **Handlers never persist directly** — they own `state`,
the orchestrator owns the row.

## Reference handler: filesystem (`file_search` + `file_read`)

Phase 1 ships exactly two handlers. Read-only, sandboxed to a single
configurable root (`agent.task_file_sandbox_root`, default
`data/user_documents/`). Path safety lives in
`app/core/tasks/sandbox.py` — normalize + reject `..` / symlink
escapes / absolute paths outside root.

Between them they exercise every terminal state:

| Flow | Path through the state machine |
|---|---|
| Tiny matching search | `running → done` |
| Search hits the result cap | `running → awaiting_input → done` |
| Direct file read | `running → done` |
| Ambiguous file read (multiple matches) | `running → awaiting_input → done` |
| File too large | `running → failed` |
| File not found | `running → failed` |
| User cancels mid-task | `running → cancelled` OR `awaiting_input → cancelled` |
| App restart mid-task | `running → interrupted` (on next boot) |

Tools registered in `ToolRegistry`:

| Tool | LLM-visible description |
|---|---|
| `start_file_search` | Search files in the user's sandboxed documents directory. Returns matching files asynchronously — you'll be told about results in a later turn. |
| `start_file_read` | Read the contents of one file from the user's sandboxed documents directory. Returns the contents asynchronously — you'll be told about the result in a later turn. |
| `cancel_task` | Cancel a running task by id. Use when the user clearly indicates they no longer want a task to finish. |

The `start_*` tools return immediately with `{"task_id": …}` so Aiko
can reference it in her streaming reply ("I'm searching for that
now"). The actual result lands as a cue on a later turn.

## Settings

| Field | Default | Purpose |
|---|---|---|
| `tasks_enabled` | `true` | Master switch for the whole subsystem |
| `tasks_per_user_cap` | `8` | Max concurrent `running`+`awaiting_input` rows per user |
| `tasks_resume_on_boot` | `true` | Whether to surface interrupted-task cues on next turn |
| `tasks_running_block_enabled` | `true` | Whether to render the running-tasks inner-life block |
| `brain_loop_deferred_grace_ms` | `100` | Time loop waits on turn-end before re-enqueueing a deferred event |
| `task_completion_proactive_after_seconds` | `45` | Silence before parked `task_result` escalates to proactive |
| `task_input_needed_proactive_after_seconds` | `20` | Shorter escalation window for blocked tasks |
| `task_cue_max_age_seconds` | `1800` | Cues older than this drop silently on next dequeue |
| `task_cue_max_aggregated` | `5` | Hard cap on cues rendered per turn (excess stays in DB/WS) |
| `task_file_sandbox_root` | `data/user_documents` | Single root the file handlers may access |
| `task_file_max_read_bytes` | `262144` | 256 KB cap on `file_read` (larger → `failed`) |
| `task_file_search_max_results` | `25` | More matches → emit `awaiting_input` |

## REST surface

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/tasks?limit=&offset=&status=` | Paginated like `/api/memories`. Filters `visible_to_user=true` only. |
| `GET` | `/api/tasks/{id}` | Single snapshot. 404 if `visible_to_user=false`. |
| `POST` | `/api/tasks/{id}/cancel` | Idempotent. |
| `POST` | `/api/tasks/{id}/answer` | Body `{"input": str}`. Only valid when `awaiting_input`. |

No `POST /api/tasks` to spawn — tasks are created exclusively from
inside a turn via the `start_*` tools, so Aiko owns the decision.

## WebSocket events

| Event | Payload | When |
|---|---|---|
| `task_started` | `{ task: TaskSnapshot }` | `TaskOrchestrator.start()` succeeds (skipped if `visible_to_user=false`) |
| `task_progress` | `{ task_id, patch: { progress?, last_message?, status? } }` | Handler emits `TaskProgress` |
| `task_input_needed` | `{ task: TaskSnapshot }` | Handler emits `TaskInputNeeded` |
| `task_completed` | `{ task: TaskSnapshot }` | Status is `done`, `failed`, or `cancelled` |

Broadcast via `_Hub.broadcast` (same pattern as `memory_*`,
`world_updated`, `relationship_axes_updated`). Listener bridges
registered in `app/web/server.py` next to the existing ones.

## Logging contract

This subsystem follows the level-disciplined logging stream documented
in [`AGENTS.md`](../AGENTS.md) (single stderr + `data/app.log` rotating
+ in-process ring buffer, accessed via `tail_logs` / `read_log_file`
MCP tools). Every line carries the standard prefix:

```
[YYYY-MM-DD HH:MM:SS,mmm] LEVEL [logger.name turn=abc12345 task=def67890] message text key1=val1 key2=val2
```

`turn=…` is the existing 8-char hex correlation id from
[`app/core/infra/log_context.py`](../app/core/infra/log_context.py).
**`task=…`** is a new 8-char hex correlation id allocated by
`TaskOrchestrator` when a handler starts running, stored in a sibling
`task_id` `ContextVar` so every per-handler `emit` and downstream log
call carries it automatically. A line can carry both ids (a turn's
`start_file_search` tool call spawns a task whose first log lines see
both `turn=` and `task=`); after the turn returns and the task keeps
running, only `task=` remains.

### Logger names

| Logger | Module |
|---|---|
| `app.brain_queue` | `app/core/brain/queue.py` |
| `app.brain_loop` | `app/core/brain/loop.py` |
| `app.task_orchestrator` | `app/core/tasks/task_orchestrator.py` |
| `app.task_store` | `app/core/tasks/task_store.py` |
| `app.task.<handler_name>` | per-handler — e.g. `app.task.file_search`, `app.task.file_read` |

The per-handler namespace (`app.task.<name>`) lets a developer scope a
firehose to one handler without flooding from the rest:
`set_log_level("app.task.file_search", "DEBUG")`.

### Level discipline

| Level | What lands here |
|---|---|
| **ERROR** | `BrainLoop` dispatch crashed (caught + logged, loop continues); `TaskStore` SQL failed; resume failed on boot. |
| **WARNING** | Per-user task cap hit and a new task was rejected; handler raised an unhandled exception; sandbox violation rejected; cue cleared because assembly raised. |
| **INFO** (default) | One structured line per lifecycle moment — see the next table. A healthy session emits ~5–10 INFO lines per task lifecycle plus one per BrainLoop dispatch decision worth knowing about. |
| **DEBUG** | Every queue enqueue / dequeue; every gate state change; every handler emit; every cue park / clear; per-tick maintenance summary. The full firehose. |

### INFO-level lifecycle lines

Each line is one structured event, `key=value` after the message:

| Module | Message | Fields | When |
|---|---|---|---|
| `app.brain_loop` | `brain-loop init:` | `priorities=` `consumer=brain-loop` | Boot |
| `app.brain_loop` | `brain-loop dispatched:` | `kind=` `route=` `elapsed_ms=` `gate_waited_ms=` | After each handler returns |
| `app.brain_loop` | `brain-loop deferred:` | `kind=` `reason=turn_in_progress\|tts_active\|both` `deferred_count=` | Event re-parked behind gate |
| `app.brain_loop` | `brain-loop escalated:` | `task=` `silence_s=` `cue_kind=task_result\|task_input_needed` | Parked cue escalated to proactive |
| `app.task_orchestrator` | `task spawned:` | `task=` `handler=` `initiated_by=` `notify_aiko=` `visible_to_user=` `running_count=` | After `start()` returns first state |
| `app.task_orchestrator` | `task transition:` | `task=` `from=` `to=` `progress=` `elapsed_ms=` | Every state change |
| `app.task_orchestrator` | `task completed:` | `task=` `status=done\|failed\|cancelled` `elapsed_ms=` `notify_aiko=` `result_size=` | Terminal state reached |
| `app.task_orchestrator` | `task cue parked:` | `task=` `kind=` `aggregated=` | Cue added to `_pending_task_cues` |
| `app.task_orchestrator` | `task cue surfaced:` | `count=` `turn=` `aggregated=` | Cues rendered into a turn's prompt |
| `app.task_orchestrator` | `task cue stale-dropped:` | `task=` `age_s=` | Cue exceeded `task_cue_max_age_seconds` |
| `app.task_orchestrator` | `task recovered on boot:` | `task=` `was_status=` `now_status=interrupted` | Boot-time scan |
| `app.task.<name>` | `<handler> emit:` | `task=` `kind=progress\|input_needed\|completed\|failed` plus handler-specific fields | Per-handler emit fan-out |

### Canonical structured fields (memorise these for grep)

| Field | Source | Example |
|---|---|---|
| `task=abc12345` | `task_id` contextvar | `task=def67890` |
| `kind=` | `BrainEvent.kind` | `kind=task_result` |
| `priority=` | `BrainEvent.priority` | `priority=2` |
| `route=` | BrainLoop's dispatched handler | `route=turn_runner` |
| `gate_waited_ms=` | Time the event sat behind the free-to-speak gate | `gate_waited_ms=1840` |
| `reason=` | Why an event was deferred | `reason=tts_active` |
| `from=` / `to=` | Task status transition | `from=running to=awaiting_input` |
| `initiated_by=` | `tasks.initiated_by` | `initiated_by=aiko` |
| `notify_aiko=` / `visible_to_user=` | row visibility flags | `notify_aiko=1 visible_to_user=1` |
| `aggregated=` | How many cues were folded together | `aggregated=3` |
| `silence_s=` | Time since last user input before escalation | `silence_s=46` |
| `running_count=` | Active tasks for this user after the transition | `running_count=2` |
| Handler-specific | per `app.task.<name>` | `query=` `matched=` `truncated=` for `file_search`; `path=` `bytes=` for `file_read` |

### Symptom → grep target (brain-orchestration extras)

These extend the table in [`AGENTS.md`](../AGENTS.md). For each symptom,
start with `tail_logs(module_contains="…")` and widen to
`read_log_file(grep="task=<id>")` for cross-turn forensic work.

| Symptom | First check |
|---|---|
| Task never starts | `tail_logs(module_contains="task_orchestrator")` for `task spawned:` — if missing, look one level up for the `start_*` tool call in `app.turn_runner` and any WARNING with `reason=per_user_cap`. |
| Task never completes | `read_log_file(grep="task=<id>")` — the last `task transition:` line is the current state. Cross-reference with `list_tasks` MCP. |
| Aiko interrupts herself mid-TTS (critical) | `tail_logs(module_contains="brain_loop")` for `brain-loop deferred: reason=tts_active` — if a `task_result` event landed mid-TTS but no `deferred:` line was emitted, the free-to-speak gate is broken. This is the no-interrupt invariant; the test `tests/test_brain_loop_no_interrupt.py` exists precisely to catch this. |
| Cue parked but never surfaces | Grep `task=<id>` over `data/app.log`. The chain should be `task cue parked:` → eventually `task cue surfaced:`. If you see `task cue stale-dropped:` in between, the user vanished and the cue expired (`task_cue_max_age_seconds`). If you see `brain-loop escalated:` it became proactive instead. |
| Proactive escalation never fires | `brain-loop escalated:` is the only line that proves it ran. If absent: the gate may still be held (look for `brain-loop deferred:`), or silence window hasn't elapsed, or `_pending_task_cues` is empty because the cue was already surfaced. |
| Background work blocks an active turn | `brain-loop deferred:` should fire for every `maintenance_due` while a turn is in flight. If a maintenance line appears with high `elapsed_ms=` *during* a turn, the gate broke. |
| Handler emits never reach orchestrator | `set_log_level("app.task.<name>", "DEBUG")` and grep `<name> emit:`. Each `emit` should produce one line; the next-up `task transition:` should fire within the same millisecond. Gap between them = orchestrator dispatch failed. |
| Per-user cap hit silently | WARNING line `task spawn rejected: reason=per_user_cap user_id=… running_count=…` is the canary. If the LLM emitted `start_file_search` and the user got no chip in the strip, this line is the answer. |
| Boot recovery surfaced nothing | INFO line `task recovered on boot: task=<id> was_status=running now_status=interrupted` for each surviving non-terminal row. If the DB has stranded `running` rows but boot was silent, the recovery hook isn't wired. |

### Practical level presets

| Goal | How |
|---|---|
| Default | `app.brain_loop`, `app.task_orchestrator` at INFO. Turn-by-turn trace plus task lifecycle. |
| Investigate one handler | Keep global INFO; `set_log_level("app.task.file_search", "DEBUG")` for the per-handler firehose. |
| Investigate cue folding | Bump `app.task_orchestrator` AND `app.brain_loop` to DEBUG; every park / dispatch / gate decision lands in the log. |
| Production / quiet | Both at WARNING — a healthy session emits zero brain-loop lines. |

### Notes for contributors

- Add new lifecycle lines at INFO whenever the design adds a new state
  transition or routing decision. The rule of thumb: if a future
  contributor would reach for `print()` to debug it, log it instead.
- Every new structured field added to a log line MUST be added to the
  canonical-fields table above. A grep-target that isn't documented
  may as well not exist.
- The "Symptom → grep target" table in
  [`AGENTS.md`](../AGENTS.md) gains the relevant rows from the
  brain-orchestration table when phase 1 lands, so the workspace-rule
  context-bundle covers them too.

## Debugging from MCP

The full MCP debug surface for the brain loop and task system. Same
posture as the existing `inspect_idle_workers` / `get_touch_state`:
every debuggable invariant has a force-it tool and a query-it tool.

| Tool | Purpose |
|---|---|
| `get_brain_queue_state` | Current depth, head event preview, `_turn_in_progress`, `_tts_active`, deferral count, last 10 dequeued events with timings |
| `force_event(kind, payload)` | Hand-inject any event for end-to-end repro |
| `list_tasks(status?, include_invisible=False)` | All rows with handler / progress / age; `include_invisible=True` surfaces `visible_to_user=false` rows for dev/debug |
| `force_task_input_needed(task_id, prompt)` | Arm an `awaiting_input` request without a real handler |
| `cancel_task(task_id)` | MCP-level cancel |
| `force_file_search(query)` | Spawn a `file_search` task bypassing the LLM tool path |
| `force_file_read(path)` | Same for `file_read` |
| `simulate_task_failure(task_id, error)` | Force a running task into `failed` for cue tone testing |

### End-to-end repro: "Aiko opens a file for the user"

1. `force_file_search("Q4 report")` — spawn the task. `get_brain_queue_state`
   should show a `task_started` event and a row at `status='running'`.
2. Wait for the handler to complete (or `simulate_task_failure` to
   force a failure path).
3. `list_tasks(status="done")` — confirm the row landed.
4. `send_message("anything else I should know?", skip_tts=true)` — the
   next assistant reply should include the parked cue (weaved into the
   reply, not raw JSON).
5. `get_last_response_detail` — inspect `system_prompt` to verify the
   T6 block rendered and the `_pending_task_cues` list cleared.

### End-to-end repro: "task completes during active TTS, no interruption"

1. `send_message("hello", skip_tts=false)` — start a voice turn so
   `_tts_active=True`.
2. While TTS is still draining, `force_file_search("test")` immediately
   followed by `simulate_task_failure(<task_id>, "test error")`.
3. `get_brain_queue_state` should show the `task_result` event parked
   (not consumed), and the loop blocked on the free-to-speak condition
   variable.
4. Once TTS drains, watch the next dequeue land cleanly without
   cutting Aiko off. `tail_logs(module_contains="brain_loop")` shows
   `brain-loop: dequeued task_result deferred_ms=NN`.
5. The next `send_message` includes the failure cue in the prompt.

### End-to-end repro: "awaiting_input flow"

1. Populate `data/user_documents/` with 30+ files containing the word
   "meeting" so `file_search` exceeds `task_file_search_max_results=25`.
2. `send_message("find me anything about meetings", skip_tts=true)`.
3. Aiko's reply includes "I'm searching for that now" and a task chip
   appears in the strip.
4. The handler emits `TaskInputNeeded` because the match count
   exceeds the cap. `list_tasks(status="awaiting_input")` confirms.
5. Next turn (or after the escalation window) Aiko asks "I found a
   lot — want me to narrow it down? maybe the most recent ones?".
6. `send_message("yeah, just recent")` — the task resumes via
   `TaskOrchestrator.answer(task_id, "recent")`, the handler filters,
   and the result cue surfaces on the following turn.

## Risks and the invariants the tests pin

1. **Prompt-cache thrash from task cues.** Task cues land in **T6** of
   the `_PROMPT_BLOCK_TIERS` ladder, never higher. A test in
   `tests/test_prompt_assembler.py` will fail loudly if a contributor
   moves the block up the tier ladder.
2. **No-interrupt-during-TTS** is the single highest-impact correctness
   bug in this refactor (Aiko talking over herself feels broken in a
   way no other failure mode does). Pinned by
   `tests/test_brain_loop_no_interrupt.py` with a `# DO NOT WEAKEN
   WITHOUT DESIGN-DOC UPDATE` banner.
3. **Cue starvation.** A task completes, TTS finishes, user starts
   typing simultaneously. `_pending_task_cues` is cleared only after
   `PromptAssembler.assemble_with_budget` returns successfully — if
   assembly raises, the cues stay parked for the next turn.
4. **MCP path compatibility.** `send_message` becomes
   synchronous-via-future. Two concurrent calls now serialize instead
   of racing. The reply is still returned via the same return value;
   no MCP-side breaking change.
5. **`IdleWorkerScheduler` stays separate.** Phase 1 does not merge
   the maintenance scheduler into the task system. The anti-starvation
   + EMA-budget drain semantics are preserved; the BrainLoop simply
   drives the tick on its thread.

## Where to look next

- [`app/core/brain/queue.py`](../app/core/brain/queue.py) —
  `BrainEventQueue` implementation.
- [`app/core/brain/loop.py`](../app/core/brain/loop.py) — `BrainLoop`
  consumer, free-to-speak gate, escalation timer.
- [`app/core/brain/events.py`](../app/core/brain/events.py) — frozen
  dataclasses for every event kind + priority enum.
- [`app/core/tasks/task_store.py`](../app/core/tasks/task_store.py) —
  schema v16 SQL facade.
- [`app/core/tasks/task_handler.py`](../app/core/tasks/task_handler.py)
  — Protocol + `TaskState` + `TaskEmitFn` types.
- [`app/core/tasks/task_orchestrator.py`](../app/core/tasks/task_orchestrator.py)
  — registry + lifecycle + WS broadcast.
- [`app/core/tasks/recovery.py`](../app/core/tasks/recovery.py) —
  boot-time scan of non-terminal rows.
- [`app/core/tasks/sandbox.py`](../app/core/tasks/sandbox.py) —
  path-safety helper for the file handlers.
- [`app/core/tasks/handlers/file_search.py`](../app/core/tasks/handlers/file_search.py)
- [`app/core/tasks/handlers/file_read.py`](../app/core/tasks/handlers/file_read.py)
- [`app/core/session/prompt_assembler.py`](../app/core/session/prompt_assembler.py)
  — running-tasks provider, task-cue block at T6, clear-after-assembly
  semantics.
- [`tests/test_brain_event_queue.py`](../tests/test_brain_event_queue.py)
- [`tests/test_brain_loop_no_interrupt.py`](../tests/test_brain_loop_no_interrupt.py)
- [`tests/test_brain_loop_task_completion.py`](../tests/test_brain_loop_task_completion.py)
- [`tests/test_brain_loop_progress_silent.py`](../tests/test_brain_loop_progress_silent.py)
- [`tests/test_brain_loop_voice_completion.py`](../tests/test_brain_loop_voice_completion.py)
- [`tests/test_task_store.py`](../tests/test_task_store.py)
- [`tests/test_task_orchestrator.py`](../tests/test_task_orchestrator.py)
- [`tests/test_file_search_handler.py`](../tests/test_file_search_handler.py)
- [`tests/test_file_read_handler.py`](../tests/test_file_read_handler.py)
- [`tests/test_chat_database_v16_migration.py`](../tests/test_chat_database_v16_migration.py)

## See also

- [`docs/memory-tiers.md`](memory-tiers.md) — the `IdleWorkerScheduler`
  framework that this design cooperates with.
- [`docs/prompt-caching.md`](prompt-caching.md) — the tier ladder
  contract that task cues must respect (T6).
- [`docs/voice-mode.md`](voice-mode.md) — the TTS lifecycle hooks
  (`on_tts_state`) that the free-to-speak gate reads.
- [`AGENTS.md`](../AGENTS.md) — the LLM-provider routing layer that
  all task-spawned LLM calls will eventually route through.
