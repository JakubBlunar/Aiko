# Hermes ideas for Aiko's local task harness

Research date: 2026-11-08. Compared NousResearch's public Hermes Agent source and
documentation with Aiko at `ab1d2382`. This is a proposal, not a runtime change
or a benchmark of Hermes against Aiko. Public `main` and documentation can move.

The useful direction for the shared 9B model is fewer inference turns, reusable
prompt prefixes, recoverable evidence and narrowly selected speaking context.
More concurrent agents or more summarization calls would not automatically help.

## Recommended order

| Order | Change | Expected benefit | Aiko boundary |
| --- | --- | --- | --- |
| 1 | Worker-authored reports delivered directly | Avoid a cloud turn used only to restate task results | Shared speaking context and durable task delivery |
| 2 | Stable task prompt prefix | Make repeat task requests eligible for more prefix reuse | Task planner, review and discussion |
| 3 | Recoverable large results | Retrieve omitted evidence without repeating an operation | Generic result storage plus plugin projections |
| 4 | Bounded compound operations | Fewer planner round trips for predictable procedures | Plugin-owned procedures, common host admission |
| 5 | Better capability discovery and playbooks | Reduce schema/instruction load without hiding useful tools | Generic SDK metadata and task working set |
| 6 | Grounded procedural learning | Reuse successful approaches over subsequent jobs | Evidence-backed procedures, separate from companion facts |

These are expected mechanisms, not measured savings. Keep the current 64k
allocation and cognition priority while measuring each change independently.

## 1. Let the task worker speak as Aiko

This addition comes from the user's request; it is not a claim that Hermes
already implements Aiko's companion-style reporting design.

### Current extra work

- `app/core/tasks/report_decision.py` asks the maintenance model whether to
  surface a result and drafts an `angle`. Its contract explicitly says that
  this is a hint for the main model, not a finished line.
- `TaskOrchestrationMixin._dispatch_task_report` parks/arms a user-requested
  result immediately and can also start a shadow report-decision call.
- `ProactiveDirector.notify_task_escalation` invokes a forced proactive turn.
  That path deliberately skips prepared nudges and uses the configured main
  model to compose the actual report.
- Prepared local text can already be persisted and spoken without that model
  through `_speak_prepared` / `_speak_prepared_typed`. The current one-row-per-user,
  short-lived nudge store is unsuitable for a durable queue of task reports.

### Proposed speaking context

Build one bounded, read-only speaking context shared by task reporting and work
discussion. Reuse the effective user-editable persona and existing style helpers
instead of maintaining a second hardcoded Aiko personality. Include:

- A small identity/talking-style section, resolved user/assistant names and the
  existing learned-style interpretation rule.
- A registered `ConceptView.for_consumer` diet for relevant communication-style
  and boundary concepts, with subject identity, status and evidence provenance.
  Relevant task-domain concepts can guide interpretation; returned task evidence
  remains the authority for claims about what happened.
- A small recent conversational excerpt and last delivered task update so the
  worker can avoid repetition and fit the current exchange.
- Original request/constraints, current absolute time, result time, verified
  findings, uncertainty, evidence IDs and whether this is progress or completion.

Do not copy the full brain prompt into each task step. Resolve the speaking
context when drafting a report, and budget it alongside evidence and output.
Version persona/concept inputs so a delayed draft can be refreshed when needed.
Keep task facts, concept interpretations and speaking preferences distinguishable.

### Draft once, then deliver

The goal worker should produce an explicit user-facing `report` field, distinct
from internal planning notes. Where the same bounded call already has the facts
and speaking context, it can draft a report without another model round trip.
Completion review can return a final draft with its evidence verdict; delivery
still requires the completion contract to pass. Partial/failed work needs honest
wording rather than a false success report.

Only meaningful milestones earn progress speech: a finding, a blocker requiring
input, a changed plan worth explaining, or a finished deliverable. Starting a
tool, switching profiles and waiting for the model are drawer status updates.
Coalesce superseded progress and avoid manufacturing reports on a timer.

Use a durable task-report record keyed by task, execution generation, milestone
and result revision. At delivery, recheck owner/session, task state, newer
guidance, report freshness and whether a real conversation is in progress.
Commit the accepted assistant message and its report identity together, then
use normal WebSocket/text and TTS delivery. Strip hidden tags through the shared
visible-text seam; apply TTS preparation only to spoken text. Preserve typed
mode's TTS preference and voice ownership. Track text commitment and audio
delivery separately; neither proves that the user understood the report.

Reuse the earlier durable-report proposal in
[harness next steps](agent-harness-next-steps-2026-11-07.md). Prompt selection or
queue insertion must not mark a report delivered. Suppress a second cloud
rewrite after direct delivery, and make the committed message visible to the
next ordinary brain turn. Explicitly requested results remain reportable even
when the optional local draft fails; retain them for retry or a natural turn.

Work-only follow-up questions can use the same cheap speaking context and owned
evidence. Questions combining the task with broader conversation still belong
to the ordinary conversation route. This keeps the two response paths coherent.

Acceptance: complete a simple job with zero main-model reporting calls; verify
style/concept edits, unsupported claims, repeated milestones, new guidance,
pause/cancel, restart recovery, session switches, text-only mode and voice
interruptions. Compare cloud calls/tokens avoided against added local latency.

## 2. Preserve the task prompt prefix

Hermes separates relatively stable prompt content from per-call additions and
orders system layers by volatility. Its provider-specific cache markers are not
portable to Ollama, but stable prompt bytes are a useful design principle.
[Prompt assembly](https://hermes-agent.nousresearch.com/docs/developer-guide/prompt-assembly),
[cache implementation](https://github.com/NousResearch/hermes-agent/blob/main/agent/prompt_caching.py).

Aiko's main conversation assembler already has a T0-T6 stability ladder. The
task planner separately prepends `timephrase.today_anchor()` to its system
instructions. That anchor includes the live ISO timestamp. A render-only probe
with fixed inputs at 08:00:01 and 08:00:02 produced a 3,442-character system
message with only 71 common leading characters; the local estimator counted
about 1,149 system tokens. This demonstrates unstable input, not an observed
cache hit rate or latency gain.

Keep task rules and a versioned selected capability set first. Place the current
time anchor in a later dynamic message, preserving the mandatory time anchor
and stored-text rule. Put the full job contract before changing observations
and counters; avoid restating it in several sections. Stable schema ordering
helps, but a genuine capability/configuration edit must still refresh admission.
Apply the same reasoning to review/discussion and shared speaking context.

Measure provider-reported evaluated/cached prompt tokens when available, plus
prefill time. Other workers sharing the same local model can reduce cache reuse;
prompt ordering alone cannot guarantee that a job retains its KV state.

## 3. Keep large outputs recoverable

Hermes's code execution tool spills oversized stdout to a retrievable file and
returns a marked head/tail excerpt. Its deterministic pruning can replace exact
duplicate results with references before paying for LLM compaction.
[Execution source](https://github.com/NousResearch/hermes-agent/blob/main/tools/code_execution_tool.py),
[pruning source](https://github.com/NousResearch/hermes-agent/blob/main/agent/context_compressor_prune.py).

Aiko already has durable child results and `inspect_result` paging/search.
However, `McpToolHandler` caps persisted content at 100,000 characters, so text
past that boundary cannot be recovered by inspecting the stored row. A browser
middleware projection is also a derived view, not necessarily the original data.

Add a bounded, owned result-artifact store with revision, length, partiality,
retention and pagination metadata. Save the recoverable source separately from
its model-facing view. Preserve plugin scope and source identity. A result over
the artifact limit must explicitly remain partial. Restore omitted details by
reading the artifact rather than rerunning an effectful operation.

Deduplicate prompt payloads, not tool execution: the same request can return
different data later. An unchanged-data reference needs both revision identity
and a way to retrieve its content after the original excerpt leaves the prompt.

## 4. Fewer decisions through bounded procedures

Hermes's `execute_code` allows tool pipelines and data filtering in one model
turn. Its current browser wrapper also instructs the model to combine short
navigation/extraction procedures and save larger collections in a workspace.
[Code execution](https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution),
[browser implementation](https://github.com/NousResearch/hermes-agent/blob/main/tools/browser_use_cli.py).

Aiko's harness asks for one action per planning decision. An adaptation for a
9B model should start with small typed procedures, rather than requiring it to
write arbitrary Python: navigate/wait/observe, act/wait/observe, or read a few
independent sources and select matching fields. These are proposed Aiko APIs,
not existing Hermes tools or existing Aiko capabilities.

Plugins own domain behavior and schemas; the SDK/host provides common execution
and checkpointing. Every underlying call keeps normal scope/schema/effect
validation, configuration generation, cancellation and approval. Recheck new
guidance between steps. Serialize shared-tab operations, stop on ambiguous
targets, and do not retry a whole procedure after an uncertain write. Parallel
read I/O is distinct from parallel LLM inference; retain TASK admission priority.

Compare end-to-end calls and success for the same goal. Do not claim a fixed
speedup just because three deterministic operations fit into one decision.

## 5. Discover tools without loading every playbook

Hermes supports searchable deferred tool descriptions and full schemas loaded
on demand. It retains short capability listings so omitted tools do not look
nonexistent. Its skills similarly separate an index, a main document and linked
references; unchanged repeat views can return a digest-based stub.
[Tool search](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-search),
[skills source](https://github.com/NousResearch/hermes-agent/blob/main/tools/skills_tool.py),
[repeat-view source](https://github.com/NousResearch/hermes-agent/blob/main/tools/skills_tool_dedup.py).

Aiko already selects six complete schemas and offers discovery/profile changes.
The next improvement is a versioned working set with deterministic retrieval
over tool names, descriptions and parameter names, plus per-plugin playbook
sections. Keep basic browser tools directly available for simple tasks; an
extra discovery call can erase savings for a small model. Mandatory operation
rules stay available whenever their tools can run. Optional troubleshooting or
long examples can load only when needed. Disabling a plugin revokes its tools,
guidance and procedures through the same existing lifecycle boundary.

## 6. Reuse grounded procedures over time

Hermes lets the agent save procedural skills after solving a repeatable task or
learning from corrections. Its skills are loaded selectively rather than all
being permanent prompt text.
[Skills system](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills).

For Aiko, a future procedure should record prerequisites, verified steps,
expected checks, source jobs and invalidation conditions. Start as a candidate
and retain negative outcomes/corrections. Do not memorize browser refs, account
secrets or exact page state as a general procedure. Repeated use must still
verify the current environment. Keep procedural learning distinct from
companion memories and grounded user/self concepts.

## What to postpone

Hermes documents per-turn micro-compaction as opt-in: it adds an auxiliary call
and rewrites prompt history, with a cache-versus-context tradeoff.
[Micro-compaction](https://hermes-agent.nousresearch.com/docs/developer-guide/micro-compaction).
For the shared local worker, first remove redundant narration/inference and
deterministically reduce tool payloads. Summarization may later help unusually
long jobs, but it should earn its cost through measured reclaim and preserve
the original instructions/evidence access.

Hermes also anchors context counts to provider usage and estimates only new
messages while a fingerprint proves the old prefix unchanged.
[Usage-anchor source](https://github.com/NousResearch/hermes-agent/blob/main/agent/usage_anchor.py).
Aiko's current task pack is rebuilt and can change its menu/history each call;
an append-only delta estimate is unsafe without first establishing the same
prefix identity. Retain whole-request accounting and actual-usage calibration
until that invariant exists.

## Verification boundary

Read public primary sources and local code; ran only a pure prompt-rendering
probe. No Hermes installation, model calls, live browser operation, companion
memory edits or runtime setting changes. No measured claim that these changes
make the 9B model match a stronger model. This document changes no behavior.
