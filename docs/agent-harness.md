# Aiko's checkpointed work harness

Implemented 2026-11-07 over the existing task system. Aiko still starts work
through `start_workflow` during the ordinary conversation, answers through the
existing task-input path, and reports through the existing task-result lane.
No separate agent service or vendor framework is required.

## What a job saves

The existing SQLite task owns the objective, completion criteria, constraints,
initiating user/session, bounded original conversation request, profile, short
plan, factual notes, result references, pending operation and usage counters.
Companion memories are not a scratchpad for work. Work messages live in the task
event log; private reasoning is not requested.

Profiles are selected by Aiko: general, reader, researcher, browser, writer and
reviewer. They change role instructions and available operations, using the
configured `workflow` model route and host worker gate. They are sequential work
roles, not concurrent autonomous agents. No user profile setup is necessary.

Each planning call sees at most 12 complete operation contracts and 18,000
catalogue characters. Aiko can discover another page or search the catalogue.
Full JSON schemas, including nested fields and enums, are validated before
dispatch; an undisclosed operation cannot be invoked. Legacy native operations
with an empty v1 schema remain explicitly open object contracts.

Child creation and the parent's step-to-child link commit together. A step
surviving restart can adopt its existing child. Checkpoint epochs fence old
planner generations; pause, resume, answer and cancellation share task admission
locking. Completed/cancelled/failed terminal states cannot overwrite each other.

## Waiting, recovery and allowance

Waiting for a child, approval, answer or scheduled continuation releases the
execution slice. A single runtime wake loop resumes ready jobs. Aiko can request
a bounded delay with `wait`; this continues the same job, rather than creating a
calendar entry or recurring automation. Jobs have a seven-day expiry, checked
when their next execution slice starts.

Defaults for new configurations are 32 planning decisions, 48 children, 1,800
seconds of active model-call time and 40,000 reported tokens. Existing explicit
nonzero limits remain effective; a legacy zero time budget uses the new bounded
default. Time spent waiting for a user or child is not
charged to active planning time. A model call interrupted by pause/restart is
conservatively charged its saved time reservation. Provider token usage can be
missing, and usage from a lost response cannot be reconstructed: this is not a
hard billing or money cap.

Allowance exhaustion pauses. Explicit continuation adds bounded credit without
resetting consumed usage, capped at 128 decisions, 128 operations, 7,200 active
seconds and 200,000 reported tokens. Manual pauses remain paused after restart.
Ordinary non-harness tasks retain their existing interrupt-on-restart policy.

Read operations interrupted by restart may retry twice. A write/unknown-effect
operation interrupted after dispatch, including a timeout with unknown remote
outcome, stops with an **uncertain** result. It is never blindly repeated.
Cancellation stops future steps; it cannot undo an external effect already sent.

## Evidence and completion

Results are retained on the child task. The planner receives a compact
observation and can inspect a 5,000-character page or search within an owned
result, with its task id, source timestamp and content revision. MCP text retains
up to 100,000 characters; omitted text and unsupported non-text blocks are marked
partial. This does not implement image or binary-artifact inspection.

Claimed success requires successful owned evidence ids and a separate model
review against the original criteria. The review receives evidence, not just the
planner's verdict, and distinguishes tool acknowledgement from read-back proof.
Unsupported claims get bounded repair opportunities, then become partial.
This is a model's evidence review, not a guarantee that arbitrary claims or
external effects have been independently proven correct.

Generic external MCP operations now pass through the host's existing approval
policy per concrete operation. Filesystem reads and server read-only annotations
inform effect classification; other operations default to unknown. The live
schema/effect contract is checked again after approval and cancellation is checked
before RPC dispatch. Installed plugins and their annotations remain trusted code.
Server resource restrictions still apply; this is not an OS security sandbox or
a complete job-specific resource-grant system.

## Work drawer

The Work button is available in desktop chat and the mobile top bar. Desktop
docks the drawer **after** the avatar rail only when there is sufficient room
for the existing avatar width and a usable chat column. Otherwise it overlays.
Mobile uses a full-screen dialog with focus handling, Escape and a close button.
Desktop has a keyboard-accessible shared resize separator when docked.

The drawer has work history, the current plan and state, Conversation / Steps /
Result views, pause/resume/cancel, lazy paged source results and approval answers.
History always includes active roots, even when newer completed jobs fill a page.
Messages can **ask about work** (a bounded, read-only discussion) or **guide the
next step**. Guidance arriving during planning discards the stale decision before
dispatch. Already-dispatched operations may finish. Pending questions are answered
through the actual input API, rather than being mistaken for discussion messages.
Drafts survive job switches and close/reopen; accepted messages have retry ids.

The work conversation is independent of the main chat. Its replies use saved
job state and evidence and have no tools. A follow-up that launches new work
still belongs in the ordinary conversation with Aiko.

## Remaining platform work and live checks

The broader [plugin platform roadmap](personality-backlog/plugin-system.md)
remains open: job-specific grants and resource locks, provider idempotency and
read-back reconciliation, durable completion-delivery outbox, cross-process
leases, artifact/media adapters, SDK version migrations and parallel children.
Saved results survive restart, but the existing conversational delivery queue and
parked result cues are in memory; this slice does not promise exactly-once spoken
delivery across a crash. Results remain inspectable in the drawer and task tools.

Verification uses disposable databases, scripted models/tools and an isolated
browser preview with live API requests blocked. Responsive checks cover wide
desktop docking, narrower desktop overlay and mobile; the avatar rail keeps its
width. No production MCP mutation or live-model reliability claim follows from
these tests. After restarting Aiko, exercise a normal conversation-started read
job, one approval wait and a restart between reads; inspect completion wording,
worker priority/voice latency and the usefulness of her chosen plan and profiles.
