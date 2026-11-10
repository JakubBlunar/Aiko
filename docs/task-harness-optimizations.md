# Local task execution, speech and procedural reuse

Implemented 2026-11-08 following the
[Hermes comparison](hermes-harness-review-2026-11-08.md). Uses the existing task
runtime, SQLite, provider routes and plugin lifecycle. No separate agent
framework, arbitrary-code executor or new companion-memory writer is introduced.

## What changed

| Area | Implemented behavior |
| --- | --- |
| Local reporting | Meaningful progress, questions and final reports use the workflow route and go directly to chat/TTS. No main-model reporting rewrite or automatic cloud fallback. |
| Prompt reuse | Stable task instructions precede changing time anchors; compact planning places the original contract first without duplicating it in WORK. |
| Recoverable results | Original MCP text survives separately from its compact middleware view, with owned pagination, revisions and quotas. |
| Compound operations | Plugins register short typed procedures; each underlying call uses the existing checkpoint/admission/approval path. |
| Discovery | Lexical ranking includes tool names, parameters and descriptions. Tools can be pinned; optional plugin playbooks load on demand. |
| Procedural learning | A compute-only idle worker learns successful operation order after independent verified requests, with source and correction checks. |

## Aiko's speaking guide

`SpeechGuide` samples up to 128 completed ordinary conversation replies from the
last 30 days. A `turn_outcomes` record with a real user message and known provider
is required. Prepared nudges, task reports, unknown legacy assistant rows and
failed/truncated outcomes do not become teacher examples. Multiple sessions are
balanced, long/code-heavy replies are skipped, and the inference input is
bounded independently of the sample count. The existing style-rut tracker gives
negative guidance about repetitive openers or questions.

Bootstrap needs 24 eligible replies. Refresh normally needs 32 new replies and
at least one day; persona/source invalidation can make a replacement due sooner.
An invalid refresh keeps the previous valid guide, with an hour retry backoff.
No new eligible data means no model call. Guide output is bounded to 600 estimated
tokens and validates cited source IDs and verbatim examples. Guidance describes
delivery, not facts. Deleted/edited sources or a changed persona invalidate its
use; the effective editable persona remains available as fallback.

Report composition combines this guide, selected persona sections and a small
registered `work_speech` ConceptView diet. Concepts guide interpretation and
delivery; tool evidence is the authority for task-result claims. Work discussion
uses the same speaking context. The sample's quoted content is never treated
as an instruction or as a claim about the current task.

## Direct reporting and delivery

New harness jobs carry `local_reporting`; resumed unfinished jobs adopt it.
Historical completed jobs are not reannounced on upgrade. The local reporter
looks for explicit `report` milestones supported by owned completed child IDs,
intentional work replies, questions and terminal results. Tool starts, profile
changes and model waits remain drawer status. Identical progress is deduplicated,
newer pending progress supersedes older updates, and completion supersedes
pending progress.

Reporting hardening (2026-11-09): the narrator receives saved task facts and
successful owned sources, not recent conversation as evidence. Unverified runs
without successful source results use a deterministic failure report, with no
narration call. Generated drafts receive a separate local grounding review;
rejected drafts fall back to the saved verified finding or an honest incomplete
result. The review's tokens/time count in the same attempt record. Old pending
drafts require a fresh source basis; already committed reports are not replayed.

The original session/user owns each report. The durable outbox stores its
milestone identity, source basis, draft and committed assistant-message ID.
Guidance, task state, persona/concept changes and session ownership are checked
again before delivery. Text commitment and the report transition share one SQLite
transaction. A returning old draft cannot finish a newer basis. Deleting a
committed report message does not authorize replaying it.

Delivery uses the brain event queue's free-to-speak gate, the existing delivery
ledger, notification path, supported reactions and TTS queue. Typed-mode TTS stays
opt-in. Hidden tags are removed before persistence, while TTS preparation applies
only to speech. A committed chat message is not proof of playback or human
perception; notification/audio failure after commitment does not trigger a blind
second spoken report.

Drafts use bounded local calls and at most three dispatched attempts per source
basis. Queue deferral/cancellation does not use an attempt. Attempt records keep
reported tokens and active/queue time separately; a lost in-flight attempt keeps
a conservative time reservation. Fresh guidance or speaking-context changes can
rearm drafting. A failed draft remains available through the saved work and
local work discussion. It does not trigger a paid cloud rewrite.

`start_workflow` accepts `reporting: meaningful | quiet | frequent`. Quiet still
reports final results and questions. `control_work` now also accepts `guide` plus
a message, preserving the same job and deduplicating repeated tool invocations.
The worker's `set_reporting` action applies changed update preferences. Frequent
means useful intermediate findings, not a periodic heartbeat. Ordinary user
conversation still uses its configured main route.

## Evidence storage

`ResultArtifacts` stores text in SQLite, limited to 2,000,000 UTF-8 bytes per
artifact and 8,000,000 bytes per job. Quota admission is transactional. Original
size, retained size, source partiality, owner and digest are distinct from the
100,000-character inline tool view. Unsupported media and quota truncation stay
explicitly partial. Artifact rows follow task retention through cascading keys.

`inspect_result` defaults to recoverable source text when available. Use
`view: rendered` for the compact result or `view: projection` for an oversized
plugin projection. Search and paging operate on the stored source; they do not
rerun the tool and never refresh old browser targets. Inspection revisions are
checked by completion review. REST result paging supports the same views through
`GET /api/tasks/{id}/result?view=source`, retaining parent/user visibility checks.

## Plugin procedures and playbooks

Plugins expose the common SDK APIs:

```python
api.register_playbook("menus", "Recover a dynamic menu", "Detailed optional guidance...")
api.register_procedure(
    "navigate_and_observe",
    "Navigate and inspect the resulting page",
    input_schema={
        "type": "object", "properties": {"url": {"type": "string"}},
        "required": ["url"], "additionalProperties": False,
    },
    steps=[
        {"tool": "browser_navigate", "arguments": {"url": {"$input": "url"}}},
        {"tool": "browser_snapshot", "arguments": {}},
    ],
)
```

Definitions are plugin-owned, copied at activation and qualified as
`plugin_id:name`. A procedure contains one to four local MCP calls. Bindings
read declared input values; there is no eval, Python execution or access to
another plugin's namespace. The browser bundle supplies navigation/observation
and optional dynamic-control guidance. Its procedure is offered only when the
underlying tools are discovered. Unfamiliar servers can retain ordinary calls.

The planner invokes `run_procedure {name, inputs}` only for a procedure actually
retained in that prompt's budget. All input and child schemas are validated.
Every child retains normal configuration generation, effects, scope and approvals.
Each call and index is checkpointed; restart can adopt an existing child instead
of creating another. New guidance or reconfiguration stops unstarted calls.
An uncertain external effect is not repeated. `requires_observation` can require
a current owned observation before starting, including after a manual resume.

`discover {query,page}` ranks bounded complete schemas. `discover {names:[...]}`
pins up to six exact schemas across steps; a new search clears old pins. The
group manifest keeps omitted capabilities discoverable. `read_playbook {name}`
loads optional instructions by current plugin generation; no-op repeat loads
do not duplicate them. Mandatory operation/safety guidance remains in SKILL.md.
Disable/reconfiguration removes the plugin's procedures and playbooks through
the same lifecycle as its tools.

## Learning methods from completed work

`ProceduralLearning` is a compute-only idle worker. Its first implementation
learns schema-compatible operation order, not arbitrary scripts or autonomous
new capabilities. It stores operation names/contract digests and source-job
references, never arguments, credentials, URLs or saved browser refs.

A candidate needs two distinct real user-request message IDs and independently
supported successful jobs before it reaches a later task. Multiple tasks from
one request do not count twice. Suggestions are scoped to the same user,
compatible role/tools and lexical task relevance. Source deletion, changed
results or changed schemas can make a method ineligible. Runtime activation
generations still guard actual dispatch; a compatible method can survive an app
restart because it contains no old targets or arguments.

The planner explicitly records `recipe_id` when adopting an offered suggestion.
A failed matching adopted path withholds it until two newer independent successes
support it again. A method is guidance, not proof of the next result: new arguments,
current observations and completion review are still required. No method becomes
a companion fact or concept, and no LLM maintenance call is needed for this pass.

## Validation boundary

Focused owned-database/fake-model tests cover prompt stability, source recovery,
ownership, Unicode quotas, local-only reporting, restart/delivery identity,
teacher filtering, route changes, procedure interruption, plugin revocation,
independent learning evidence and correction/deletion behavior. Representative
affected tests cover existing migrations, worker scheduling, plugin lifecycle
and task/report integration. Lint remains mandatory.

No live 9B inference, real browser success or cost/latency speedup is established
by these tests. After restart, evaluate a short read-only browser job, one useful
progress update and a final report in both typed and voice mode. Confirm local
`work_report`/`work_speech_guide` surfaces and no main-model task-report rewrite.
