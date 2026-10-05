# New tools / capabilities

Open work only. Shipped entries live in
[`shipped/tools.md`](shipped/tools.md) (D3 synchronous web search, DT1
virtual clock) and
[`shipped/proactive-tasks.md`](shipped/proactive-tasks.md) (D2 image vision,
both parts).

---

## D8. Conversation archive recall with daily overviews

**Status: open; backlog only (1 Oct 2026).**

**Motivation.** Aiko should be able to answer "what did we talk about last week?"
or find an exchange from a month ago, including material that never became a
long-term memory. A small relevance-ranked snippet set is not a complete account
of a date range. Keep this a read-only, on-demand capability for the brain, not
another always-on prompt block or an automatic invitation to reminisce.

**Existing foundation, not work to repeat.**
[K-time2](shipped/awareness.md#k-time2-date-anchored-retrieval-for-relative-time-queries--shipped)
already resolves relative dates and adds bounded historical message hits to RAG.
The existing `recall` tool searches memories, messages and documents, but returns
at most 12 snippets, each clipped to 280 characters. Its output is not a paged
transcript or an overview with explicit coverage. The rolling `SummaryWorker`
summary serves current-session compaction; it is not a per-day archive index.

**Recommended order.** Ship fast deterministic transcript access first. Add
daily overviews only as a derived navigation layer once message lookup is sound.
Summaries help choose which day/topic to open; original messages remain the
authority for exact wording, attribution and corrections.

**First slice: a bounded archive tool.** Extend `recall` or add a small
`conversation_history` tool with explicit range, optional topic/session filters,
and opaque pagination. Reuse `time_expr.parse_time_window` and the `timephrase`
clock; resolve local calendar boundaries server-side and report the actual range
and timezone. Scope every lookup to the current user, spanning that user's
sessions only. Reuse SQLite's message-range access rather than relying on the
semantic top-N, and include neighboring turns so speaker and pronouns resolve.
Return message/session IDs, timestamps, roles, bounded text, total matched count,
coverage and a continuation cursor in stable chronological order. Optional topic
search must label its results as selected matches, not a complete period recap.

The read path needs no extra LLM call, embedding call or background task. Enforce
row/token caps and bounded indexed scans; offer explicit next-page or narrower
range actions instead of flooding context. "No messages in this range", "only
part of the range inspected", "no topic match" and "store unavailable" must be
distinct outcomes. Register the tool's description, `recall` gate family and
skill-router exposure so normal brain tool selection can actually reach it.

**Second slice: source-linked daily overviews.** A demand-driven idle worker
builds one bounded overview per user/local day across sessions with retained
conversation. Store it in SQLite as a derived archive record, separate from
durable memories and rolling session summaries. Each topic/event bullet cites
its source message IDs or bounded ranges, with separate user/assistant attribution;
record coverage, source watermark/hash, timezone and generator version. Age-tag
worker input and use `today_anchor` plus `STORED_TEXT_TIME_RULE` for stored prose.
No bare relative dates, invented events or converting Aiko's suggestions into
facts about the user. No conversation means no fabricated "daily activity" entry.

Expose overview lookup through the same archive capability, followed by explicit
message drill-down. Build missing summaries lazily or in bounded idle batches,
with checkpoints, retries and an opt-in bounded historical backfill. Handle
late-arriving messages and edits as invalidation/revision, including incomplete
current-day coverage. Missing or stale overviews must not block raw transcript
access. Session/message deletion must invalidate affected derived content and
cursors; a summary must never preserve deleted private text. A month recap can
inspect a bounded page of daily entries before requesting deeper sources, not
quietly mistake the last few messages for the entire month.

**Acceptance checks.** Synthetic conversations spanning sessions, local midnight
and daylight-saving changes resolve "last week" and "last month" correctly;
pagination neither skips nor repeats messages; context survives page boundaries;
cross-user rows never leak; broad recaps expose partial coverage; exact quotes
come from source rows rather than summaries. A fact omitted by memory extraction
is still retrievable. Empty dates, unavailable stores, missing summaries, retries,
late messages, corrections and deletions have explicit tested outcomes. Include
tool-gate/skill-router reach checks and real-model scenarios that choose overview
then drill-down without guessing missing dialogue. Measure lookup latency and
token cost before choosing page sizes or an overview-generation cadence.

**Key files (existing).** [builtins.py](../../app/llm/tools/builtins.py),
[tool_pass_gate.py](../../app/core/session/tool_pass_gate.py),
[chat_database.py](../../app/core/infra/chat_database.py),
[rag_retriever.py](../../app/core/rag/rag_retriever.py),
[time_expr.py](../../app/core/infra/time_expr.py),
[timephrase.py](../../app/core/infra/timephrase.py),
[summary_worker.py](../../app/core/proactive/summary_worker.py).

**Open decisions.** Extend `recall` versus a dedicated archive tool; initial
row/token budgets; timezone policy for older messages after a timezone change;
idle versus on-demand overview generation and historical backfill budget.
**Effort.** Small-Medium for transcript access; Medium for durable daily overviews.

---

## D-approval. Spoken / Aiko-voiced task approvals

**Motivation.** The task-approval framework ([`docs/task-approvals.md`](../task-approvals.md))
ships UI-only: a destructive action (file overwrite today; shell exec /
http post later) parks an `awaiting_input` approval that shows up as a
clickable prompt in the TaskStrip, and Aiko stays silent. As of the
timed-escalation retirement, `_on_task_input_needed_event` is
unconditionally UI-only — it parks no chat cue and arms no escalation
for **any** task; the TaskStrip's `awaiting_input` chip (fed by the
orchestrator's input-needed listener) is the whole surface. That's the
simple, safe v1. The natural follow-up is to let Aiko *ask in her own
voice* — "I'd like to overwrite your todo list, that okay?" — so
approvals feel conversational instead of a popup, while the TaskStrip
buttons stay as the fast path.

**Key files (existing).**
- [`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py)
  `_on_task_input_needed_event` — currently logs `task_input_needed
  UI-only` and returns without parking a cue. This is the single point
  to extend for a spoken path (per-capability, or per a new
  `agent.spoken_approvals_enabled` flag): re-introduce a `notify_aiko`
  gate + chat-cue park here.
- [`approval.py`](../../app/core/tasks/approval.py) `build_request` — the
  prompt copy Aiko would voice.
- [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) /
  the T6 task-cue provider — where a spoken approval cue would render.

**Open questions.**
- Per-capability opt-in (voice `file_write` but not a future `payment`)
  vs. one global switch.
- How to keep the chat reply and the TaskStrip in sync when the user
  answers in prose ("yeah go for it") vs. clicks — `parse_decision`
  already handles both, but the answer needs to route back to
  `orchestrator.answer(task_id, ...)` from the chat path.
- Escalation: a spoken approval should probably reuse the existing
  input-needed escalation window so a silent user still gets nudged.

---

The one-shot D1 implementation is recorded in
[`shipped/tools.md`](shipped/tools.md#d1-local-one-shot-reminders--shipped).
Recurrence and OS notifications remain future work.

---

# Dev / debug tooling (DT-series)

Not capabilities Aiko uses — tooling *we* use to build, test, and debug
her. The codebase leans hard on the embedded MCP server for
introspection; these fill the gaps that make personality work slow to
verify. All DT items are debug-only and must never reach an end-user
build.

---

## DT2. Relationship state inspector — one-shot consolidated snapshot

**Motivation.** The relationship state is scattered across a dozen
`get_*_state` MCP tools (axes, emotion episodes, tease ledger,
vulnerability budget, beliefs, shared moments, anniversaries, day colour,
wants ledger, …). Debugging "why is Aiko reading cold / clingy / off right
now?" means calling many of them and assembling the picture by hand. Add
one `get_relationship_snapshot()` MCP tool (and a read-only Settings →
Diagnostics panel) that dumps, in one shot: the four axes + **derived
stage** (J4), active emotion episodes (K57), vulnerability budget +
capacity (K15), open tease debts (K59), top-N beliefs (K2), recent +
upcoming anniversaries / milestones (J8), today's day colour (K27), and
**which relationship-cue providers actually fired last turn**. One call,
the whole relationship at a glance. Key files: a new aggregator that reads
the existing stores; the per-feature `get_*_state` tools as the data
sources. **Effort.** Small–Medium.

---

## DT3. Feature-flag catalog + "minimal mode" preset

**Motivation.** There are dozens of `agent.*_enabled` toggles — one per K
/ F / H feature. There's no way to (a) see them all with their current
values + defaults + a one-line description in one place, or (b) quickly
turn **all** inner-life cues off for clean A/B testing of a single
feature, or for bisecting "which cue is producing this weird line." Add an
MCP `list_feature_flags()` (name, value, default, source module, one-line
purpose) and a `set_minimal_mode(on)` that flips every inner-life / cue
flag off and restores them. Pairs with DT2: turn everything off, enable
one thing, watch exactly what it does. Key files:
[`agent_settings.py`](../../app/core/infra/agent_settings.py) (the flag
surface), a small reflection helper, the MCP server tools. **Effort.**
Small.

---

## DT4. Scenario / conversation replay harness

**Motivation.** [`data/persona/golden_turns.jsonl`](../../data/persona/golden_turns.jsonl)
already anchors a golden-turn eval, but there's no harness to drive a
**scripted multi-turn conversation** against the live agent with a fixed
clock (DT1) and seeded relationship state, then assert **which inner-life
blocks fired** and snapshot the rendered system prompt per turn. Most
personality tests today are unit-level on the pure helpers + the provider
in isolation — they verify "the cue *would* render given this state," not
"the cue actually fired in a real turn." This harness closes that gap and
makes the K/F/J features regression-testable end-to-end. **DT1 has now
shipped, so the deterministic-clock half of this is unblocked** — a
scenario runner can arm `AIKO_DEBUG_CLOCK=1` and drive `advance_clock` /
`advance_engagement` between scripted turns. Build on DT1
(deterministic time) + DT2 (state assertions) + the existing
`send_message(skip_tts=true)` MCP path + `get_last_response_detail` (per-turn
prompt + `provider_ms`). Key files: a new `scripts/scenario_runner.py`,
the MCP message path, `get_last_response_detail`. **Effort.** Medium
(largely unlocked once DT1 + DT2 exist). This is the *live-app* face of
the offline [`testing.md`](testing.md) T2 chain-test harness — same idea,
one drives the running instance, the other runs in pytest with a fake
LLM; they should share the "which blocks fired" assertion vocabulary.

---

## DT5. `get_surfacing_outcomes` — did what she brought up land?

**Motivation.** `get_last_concept_trace` answers "why is this concept in the
prompt *right now*" and `get_prompt_block_costs` answers "what did this prompt
cost". Neither answers the question that matters over time: **which of the
things she surfaces actually go anywhere?** Once L37's `surfacing_outcomes`
ledger exists, that question is a query — but without a debug surface the data
is invisible and none of the tuning it enables (L38 standing weights, G5
cooldowns, P43 block value) can be sanity-checked against reality before being
made load-bearing. This is the read side of the whole loop, and it should ship
*with* L37 rather than after it, because the first thing anyone will want to
know is whether the ledger is recording something sensible.

**Partly shipped.** `get_surfacing_outcomes` landed with L37 (leaderboard +
per-lane rollup, then F12's echo-kind split and semantic-floor replay), and
G4 added the sibling `get_cue_outcomes` for the worker-cue side. Still open
from the sketch below: the per-turn trace and the coverage view, plus the
wider read surface over the rest of the inner-life state.

**Key files.** A new tool in
[`app/mcp/server_tools/`](../../app/mcp/server_tools/) alongside the existing
concept and prompt-cost tools (`proactive_task_tools.py` holds
`get_last_concept_trace`; `prompt_cost_tools.py` holds the P31a tool — either is
a reasonable model). Reads the L37 table; see
[`concepts.md`](concepts.md) and [`rules/mcp-server.md`](../../rules/mcp-server.md)
for the registration pattern and the docs table that needs a row.

**Sketched approach.** Three views off the same table, because they answer
different questions and a single dump answers none of them well: a **leaderboard**
(top and bottom items by engaged rate, with observation counts so the reader can
see which numbers are meaningless yet — a 2-of-3 rate must *look* untrustworthy),
a **per-turn trace** for the last N turns showing what was surfaced and how the
turn went, and a **coverage** view — how much of the concept and memory store has
ever surfaced at all, which is the fastest way to spot a whole population that
retrieval never reaches.

Report shrunk estimates next to raw counts, matching whatever L38's scorer
actually consumes, so the tool and the ranking never disagree. Include the
unsettled-row count as an explicit field: a large backlog of unsettled turns
means the off-by-one attribution is silently failing, and that is the failure
mode most likely to go unnoticed.

**Open questions.** (1) Does this want to be one tool with a `view` argument or
three tools? One tool with a mode keeps the registry small, which the MCP
surface is already straining. (2) Is a UI panel worth it eventually, or is MCP
plus the existing diagnostics drawer enough? MCP first — this is a tuning tool,
not something a user needs. (3) Whether it should also expose the *decline
reasons* G4 records, or those belong with the worker-status tool.

**Effort.** Small.

**Depends on.** L37 (the ledger). Pairs with G4.

---

## D7. Anticipatory routine assistance — act on what she's learned

**Motivation.** K3 already learns the user's recurring `(weekday, bucket)` slots
("gym Tuesdays", "work starts ~9am") and the brain-orchestration task framework
can run real background work, but the two never meet: Aiko *knows* your rhythm
and *can* do things, yet never **offers** anything anchored to it. The natural
next beat is gentle anticipation — "you usually start work around now; want me to
pull up where we left off / your todo for today?" — a learned-routine trigger
that, at a recurring moment, optionally pre-stages a useful task and offers it
(never auto-runs anything destructive; reuses the D-approval posture). This is
where the companion crosses from *reactive* to *quietly helpful*. The whole risk
is becoming a clingy reminder app, so it's hard-gated: tied to an actually-learned
routine (high K3 confidence), one offer per slot, easy to wave off, and silent if
ignored. Distinct from D1 (explicit reminders the user sets) — this is *Aiko*
noticing the pattern. Key files:
[`schedule_learner.py`](../../app/core/infra/schedule_learner.py) (the routine
source) + a routine-trigger worker, the
[`ProactiveDirector`](../../app/core/proactive/) surface for the offer, the task
orchestrator for any pre-staged work, `agent.routine_assist_enabled` + cooldowns.
**See also [C6](proactive.md#c6-companion-mode--the-desktop-as-a-sensory-channel)**
— OS activity is the same idea sourced from the desktop rather than from
K3 learned slots, and the two should share an intake rather than grow
parallel offer machinery. Duration-as-concern
[C9](proactive.md#c9-activity-duration-as-wellbeing-evidence-k72)
shipped into K72 in Pass 11 (not a task).
**Effort.** Medium.
