# Idle life: concepts, conversation continuity and behavioral observability

Status: approved 15 November 2026; implementation in progress.

Phase 1 implements the 14-day store, scheduler/admission and action tracing,
read-only REST/MCP queries and a World timeline. Validation: 117 focused backend
tests, a representative integration set of 223 tests plus six subtests, 118
related frontend tests, and project lint. Sets overlap. Shared database/session
imports expanded the affected selector widely, so focused representative coverage
was used. Runtime restart and ordinary-use observation remain outstanding.

Phase 2 implements optional source-linked context for activity, hobby and daily
intention selection, a 50% default sampling rate, bounded fallback weights and
actual saved-work references. Validation: 240 targeted tests plus 101 subtests,
400 representative settings/concept/bootstrap/API tests plus 102 subtests, 15
final context regressions through the affected selector, related World tests and
project lint. Sets overlap. Context reads are bounded per decision rather than
cached, so source deletion/editing is visible on the next choice. No ordinary-use
model naturalness is claimed from fixture tests.

User clarification: context must remain optional inspiration. Preserve independent
choices and the ability to ignore recent dialogue; do not tie all activity to chat.

## Recommendation

First make the last few days of autonomous behavior inspectable. Then give the
existing activity and hobby planners a bounded, source-linked context bundle
from Aiko's concepts, recent memories and conversations. Reuse the current
scheduler, action catalogue and executors. Evaluate normal away periods before
adding further behavioral controls. Simulation follows as a separate tool.

The objective is continuity and believable choice, including rest and abandoned
ideas. Activity counts, novelty and productivity are diagnostic measures, not
objectives for Aiko to maximize.

## What exists and what is missing

Source inspection:

- `app/core/world/idle_activity_worker.py` already coordinates grounded activity,
  episode chains, open beats, revalidation and the completed-activity journal.
  Its journal defaults to eight entries. The planner receives a hobby label,
  intention, room state, recent activities, time of day, mood and day color;
  it has no direct concept or recent-conversation input.
- `app/core/world/idle_activity_composition_mixin.py` supplies the model with
  supported household actions and saved writing context. A successful choice
  still needs execution; prose alone cannot establish an experience.
- `app/core/world/activity_selection.py` already penalizes recent activity keys
  and applies gentle circadian, mood and intention weights. The model path uses
  a recent-activity instruction rather than those same weighted constraints.
- `app/core/proactive/hobby_worker.py` chooses projects using the previous project,
  recent project subjects and room contents. Its next-project prompt also lacks
  direct concepts and recent chat. Research-backed advances are separate from
  actual drawing or writing.
- `app/core/world/home_actions_mixin.py` saves household mutations and execution
  receipts transactionally. That receipt ring holds 64 entries; it is useful for
  retries, but is not a time-based behavioral history. Receipts contain changed
  item IDs, not a full resource-delta history.
- `app/core/proactive/idle_worker_scheduler.py` exposes current demand/admission
  reasons, counters and the latest result. Logs contain run results. Neither the
  status snapshot nor size-rotated logs guarantees several days of decision history.
- The concept facade and consumer diets already exist in
  `app/core/concepts/concept_view.py` and `concept_diets.py`. Use those seams
  rather than introducing an unrestricted concept query for this worker.

Read-only live snapshot on 15 November:

- The retained activity ring contains eight entries from 13-14 November: wandering,
  awake rest, garden activity, looking outside, tea, reading and stretching.
  The last entry is 14 November at 15:27 Bratislava time. This small sample does
  not demonstrate a repetitive loop or establish that the whole system is healthy.
- The scheduler is quiet and reports Aiko awake. The latest away-activity probe
  reason is `active_shared_conversation_situation`; its run counter is 38, with
  zero recorded errors. Runs and completed activities clearly need separate views.
  The snapshot does not establish whether the shared-situation hold is appropriate
  or stale. Record the owning situation, its age and changes before judging it.
- Her hobby is the skyline sketch, with seven research-backed findings. That
  counter is not seven saved sketch revisions. Recent dialogue discussed the
  overlapping rooftops and separating them with shadows. This is a useful source
  of an optional next practice step, provided an actual sketch can be saved.
- Recent dialogue also discussed the fit of a warm ceramic cup handle. That could
  inspire a design sketch or a short written idea. It does not establish that she
  owns a new mug, has pottery equipment or has made one.

These observations came from the live MCP tools `get_away_activities_state`,
`get_idle_workers_status`, `get_hobby_state` and bounded read-only SQLite queries.
The working tree has household/sketch capabilities; the live snapshot does not
establish that every inspected source change is loaded in the running process.
Capture runtime version/build identity when tracing is implemented.

## 1. A durable history of the last 14 days

Add a small SQLite event store alongside the existing stores. Retain detailed
history for 14 calendar days by default; show the last 72 hours initially, with
24-hour, seven-day and custom date filters. Preserve all decisions and outcomes
inside that window rather than silently trimming them to a small entry count.
Any emergency storage limit must expose its dropped-event count and missing span.
Longer-lived daily summaries can be added later; they are not required for v1.

Keep the eight-entry conversational journal for its current surfacing purpose.
The diagnostic history must not make Aiko mention more activities or become an
additional autobiographical-memory feed.

### Events to capture

Use linked decision, episode, action and project IDs. Capture:

| Stage | Structured evidence |
| --- | --- |
| Admission | Due/not due, quiet/sleep state, cooldown, shared-situation or placement hold, owning source and age, scheduler contention and budget deferral |
| Context | Offered concept/memory/message IDs, bounded labels, source ages and versions, project/item references, omissions due to limits |
| Choice | Eligible action families and targets, the subset actually offered to the model, exclusion reason codes, deterministic weight components, chosen action, selection route and fallback/rejection reason |
| Execution | Started/succeeded/rejected/failed/partially completed, canonical receipt ID, item/resource deltas, created artifact/page/revision/hash, stale-state vetoes |
| Continuity | Activity opened/paused/closed, project resumed/put aside/completed, intention earned or still open, new research finding linked separately |
| Surfacing | Receipt/cue offered to a conversation turn, selected into the prompt, consumed according to the existing accounting; actual mention only where utterance evidence supports it |

Keep factual selector metadata. A model can optionally return one small reason
code and offered source IDs. Validate the IDs and label them as model attribution,
not proof of causal influence. Do not request or store chain-of-thought. A concept
being available to the planner is distinct from the model citing it.

Cover away activity, hobby/research, garden visits, passive growth, room evolution,
home maintenance and sleep transitions. Household actions share an executor with
conversation tools: record the actor/initiator so conversation-owned effects are
not counted as autonomous decisions. Passive replenishment/growth must appear in
resource accounting without being claimed as something Aiko personally did.

Do not insert a row for every unchanged 15-second scheduler probe. Store state
transitions and bounded heartbeat intervals with counts and first/last observed
timestamps. Persist a coarse scheduler heartbeat so offline time, a legitimate
hold, starvation and missing telemetry can be distinguished.

### Durability and honesty

- Record world effects with their existing transactional receipts. Extend that
  transaction with a durable event/outbox reference where possible, then populate
  the searchable trace idempotently. A crash after mutation must not erase the
  only record of the effect or trigger a duplicate action on retry.
- Record planning and failed attempts independently, because they may never
  produce an action receipt. An unfinished decision after a crash is explicitly
  incomplete, not retrospectively declared failed or successful.
- Multi-step episodes retain per-step outcomes. If step two fails after step one
  succeeds, the trace shows the partial outcome.
- Include schema/config/runtime version, initiator, UTC narrative time, real
  recording time, debug-clock offset/mode, and monotonic inference/execution
  duration. Display local time in the UI.
- Distinguish actual step execution from a modeled activity span and LLM compute
  time. A saved step followed by a 20-minute open beat is not proof of 20 minutes
  of continuous work. Instantaneous actions do not invent elapsed duration.
- Index by time, worker, project, action and target. Paginate queries. Bound event
  payloads and keep full artwork in its existing artifact store.
- Surface trace-write failures, coverage gaps and oldest available time. Ordinary
  log rotation or app restart must not silently remove the history.
- Do not reconstruct historical decisions from Aiko's narration. Older journals,
  receipts or logs may be displayed as partial legacy evidence, clearly labeled.
- Store minimal local excerpts only when needed; keep stable source references.
  Source deletion must redact copied private context too. A diagnostic trace is
  not a second permanent copy of private chat.

## 2. Let concepts and recent conversation inform choices

Add one shared context provider used by away-activity composition, daily intention
selection and hobby project selection/resumption. It returns typed evidence, not
an unbounded prose summary of everything Aiko remembers.

A starting bundle, tuned to the worker's available context budget:

- Two or three relevant active Aiko concepts: interests/tastes, aspirations,
  pursuits and values. Use `ConceptView.for_consumer` with declared, balanced
  diets. Relationship or user concepts enter only when relevant; a user preference
  is not automatically Aiko's preference.
- Two or three relevant recent memories, initially searching a 72-hour window
  with a bounded fallback to seven days when sparse.
- One or two short source-linked conversation passages relevant to an eligible
  activity or ongoing project. Read actual messages as well as extracted memory,
  so fresh topics can matter before extraction and imperfect memories can be
  checked against their source.
- Current project state, latest saved artifact/revision and relevant new finding.
  Continuing work may be older than the recent-chat window.

Start around 600-1,000 tokens for this added bundle, within the existing worker
prompt budget; trim by relevance and source diversity. Record actual selected
counts, estimated tokens and truncation. Refresh on new messages, memory/concept
revisions and relevant world changes; avoid a new LLM context-summarization call
on every scheduler tick.

Age-tag all transcript and memory inputs through `timephrase`, with the current
date anchor and stored-text time rule. Preserve source role and epistemic status:
user request, suggestion, shared fictional scene, assistant claim, recorded action
and inference are different inputs. A tentative suggestion is not a commitment,
and Aiko saying she drew something is not a saved drawing receipt. Unsupported
concepts must not certify an action merely because they have an active label.

Select context against current capabilities: drawing, writing, reading, garden
care, household actions, public-subject research, thinking or rest. Reject stale,
retracted or unsupported evidence using the existing source policies. Keep
uncertainty visible and allow an empty context bundle.

### How it affects her day

- Concepts gently shape subject, style and preference. They do not force her to
  enact a personality label or turn every recent topic into a task.
- Conversation can suggest an executable small step. The skyline discussion
  could suggest revising shadows on an existing saved page. If no page exists,
  she may start a new drawing; do not relabel it as resuming a previous revision.
- The cup-handle discussion could inspire a design sketch. A warm drink remains
  an independent choice using actual available vessels and recipes.
- A garden-care aspiration can favor checking an actually dry plant. It does not
  make watering eligible when the plant has no need.
- Keep optional continuations with a project/source reference and a concrete next
  supported step. Reuse hobby/project and artifact state; add a small bounded
  continuation record only where no suitable existing state exists. Do not create
  a second general task scheduler or turn every casual suggestion into a project.
- Prefer continuing a suitable work sometimes, while preserving unrelated activity,
  quiet rest and the ability to put a project aside. Completion requires a receipt,
  supplied book passage or evidence-bearing finding as appropriate.
- The deterministic fallback can favor an eligible continuation or mapped action
  family using bounded context weights. It must not fabricate drawings or prose
  when there is no generated artifact payload.

An activity still has to pass the existing catalogue, scene/availability checks,
mutation guard, sleep policy, quotas and execution validation. Context provides
inspiration, not new capabilities. Public research stays in the existing curiosity
path with its privacy policy and budget; private dialogue never becomes an
unreviewed web-search query.

Avoid a reinforcement loop: diagnostic events are not concept evidence. A
completed receipt-backed activity may use the existing pursuit-note path, but
repeating a concept-inspired behavior is not independent proof of that concept.

## 3. Inspect behavior without rewarding busyness

Provide read-only MCP queries and a small expandable Idle life view in the World
or debug area. Both read the same event-store query service.

The initial view should answer:

1. What did she actually do over the last 72 hours, and what did she save?
2. What is open or paused, and what did she return to?
3. Why was she inactive during a particular interval?
4. Which concepts or conversations were offered for this choice?
5. Which objects/resources changed, and were the actions voluntary or passive?
6. What evidence is missing, and was this process running the inspected version?

Show a timeline with linked artifact revisions and expandable decisions. Add a
compact daily report and filters for worker, actor, project, action family,
failures and suspicious patterns. Export bounded JSON for later review without
requiring a manual log scrape or copying the entire private transcript.

Suggested proposed tools: `get_idle_life_history`, `get_idle_life_decision` and
`get_idle_life_summary`, with time-range/pagination parameters. These do not exist
yet. Summaries should be computed from structured events; an optional LLM review
can interpret an explicitly requested export later, not run as another always-on
reasoning worker.

### Measures and review flags

| Measure | What makes it useful |
| --- | --- |
| Coverage/inactivity | Awake eligible time, sleep, shared-situation holds, budget deferrals, offline/unknown spans; show counts and denominators |
| Activity mix | Families and targets over eligible opportunities; low variety may be reasonable in a sparse room |
| Repetition | Same action and same object instance, short action cycles, repeated maintenance without relevant progress |
| Project continuity | Return to saved work, actual revisions/findings, unresolved next steps, explicit pauses/retirement |
| Resources | Consumption, passive regeneration, replenishment, waste and inventory net change |
| Attention | Share of decisions by household upkeep/creative work/rest; modeled spans shown separately where available |
| Context use | Context offered versus selected source IDs, stale/unsupported rejection and topic over-concentration |
| Reliability/cost | Planning fallback/rejection rate, partial effects, trace gaps, inference latency and token use |

For tea, distinguish a vessel from a particular drink in it: a refill starts a new
drink instance. Correlate prepare, reheat, sip and empty events for that instance.
Repeated reheating with unchanged volume and no sipping can be flagged; one warm
cup held for a while is not automatically a problem. Likewise, repeated sketch
revisions can be productive, while create/discard/recreate loops merit review.

Resource exhaustion is judged against observed regeneration/replenishment and
real eligibility, not a universal fixed daily allowance. A held shared situation
needs its owning source and lifetime, not an automatic timeout that interrupts a
valid shared scene.

Start flags as explainable review candidates with example event IDs, confidence
and sample size. Calibrate thresholds using actual traces. No mandatory daily
productivity score, diversity target or automatic punishment for eccentricity.

## 4. Simulation and replay after production tracing

Run simulations in a separate process using disposable snapshots of world,
relevant context, scheduler watermarks and configuration. Never advance the live
process-wide debug clock or point a simulated worker at production stores.
Redirect memory, cues, diaries, tasks, artifacts and notifications as well as
world state. External side effects are disabled by default.

Use the existing `timephrase` seam for narrative time, a seeded RNG, and normal
selection/execution logic. Advance to meaningful deadlines such as cooldowns,
bath stages, growth or sleep transitions rather than skipping those events.

Two explicitly different modes:

- Replay captured decisions/model responses against compatible snapshots. Save
  RNG state/seed, config and input fingerprints; stop or mark divergence when the
  world changes. This tests execution and orchestration for those decisions, not
  the quality of new model choices under a changed prompt.
- Generate new model choices over virtual days. This evaluates the new context
  integration, but inference still takes real time and consumes its usual budget.
  Keep a strict call/token limit and label fixture-driven runs separately.

Useful scenarios include the cooling-tea cycle, sparse supplies, many belongings,
interrupted sketch work, unavailable research, sleeping/returning user, persistent
shared-situation hold, stale model plans and restart between mutation and journaling.

## Delivery sequence and acceptance

1. **History first:** event store, execution links, scheduler admission intervals,
   read-only MCP history/summary and a minimal timeline. Observe several ordinary
   away periods and establish a baseline before changing choice behavior.
2. **Context and continuity:** shared bounded provider, consumer diets, grounded
   next steps and planner/fallback integration. Extend the timeline with source
   links; compare several days before/after without interpreting more output as
   automatically better behavior.
3. **Richer review and simulation:** explainable cycle/resource flags, replay and
   virtual-day model scenarios, guided by real production observations.

Acceptance should combine focused regressions, fault/restart tests and at least
72 hours of ordinary observation after deployment:

- History survives restart and exposes the full configured retention window once
  that much history has been collected. Coverage starts at installation; missing
  earlier days are not reconstructed.
- At any inspected interval we can distinguish a recorded activity, an explicit
  hold/deferral and missing/offline telemetry. Repeated unchanged holds remain
  compact but keep their duration and source identity.
- Successful effects link to canonical receipts; retry/crash scenarios cannot
  double-count or reapply them. Partial sequences and rejected choices remain visible.
- A context-based action links to offered source evidence and any saved artifact.
  A suggested/claimed action without an artifact never becomes a completed creation.
- Tea-instance loops, project progress and household resource changes can be
  reconstructed from evidence. Quiet rest and a reasonably unfinished project do
  not fail the evaluation merely for being unproductive.
- A returning user and sleep continue to veto incompatible activity; a stale plan
  never bypasses the existing mutation guard.
- Simulations leave production databases, memory, cues, belongings and clocks
  unchanged. Replay results are not presented as proof of fresh model behavior.
- Trace capture adds no LLM calls. Measure its database latency/storage growth and
  the context bundle's token/latency cost before broadening either.

Open choices for review are the proposed 14-day retention, whether the first UI
lives in World or diagnostics, and how much artifact-continuation behavior to ship
with the context phase. None blocks agreeing that durable observability comes first.
