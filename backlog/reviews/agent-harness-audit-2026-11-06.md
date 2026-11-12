# Aiko's agent harness: source audit and proposed next steps

Historical assessment from 2026-11-06. The subsequent
[checkpointed harness implementation](../../docs/agent-harness.md) addresses the first
contract, evidence and continuation slices and documents what remains. The accompanying
[offline probe](../../scripts/probe_agent_harness.py) exercises existing contracts
with simulated planner decisions, tasks and tools; it makes no model calls or
external changes.

## Recommendation

Evolve the existing task system into Aiko's host-managed execution harness.
Keep the conversation brain, TaskOrchestrator, task identities, event history,
plugin SDK and MCP integration. The missing layer is a reliable contract around
planning, evidence, dispatch, continuation and completion.

This follows the existing [plugin platform and durable agent-job roadmap](
personality-backlog/plugin-system.md), especially P3–P6. Those milestones remain
design work. This audit identifies concrete current gaps and a smaller first
implementation slice; it does not introduce a competing framework.

The largest gain is making Aiko's existing tools more usable and her work more
inspectable, before increasing the number of tools or autonomous agents.

## What exists and should remain

- A serialized brain event loop and a separate background execution lane.
- Persistent task rows, parent/child relationships, phases, heartbeat checks,
  task inputs, event logs, cancellation and conversation-aware result delivery.
- A workflow planner with failure, exact-repeat and no-progress guards.
- Plugin discovery/activation gating, SDK registrations, per-group operational
  guidance and generic MCP task execution.
- Approval helpers and session/per-capability policy settings. They are not a
  universal execution gate for all current plugin operations.
- Qualified task-result inspection through `check_my_work`.

These are useful foundations. Persistent task rows and event logs do not yet
make the planner's execution restart-safe.

## Evidence boundary

The live debug endpoint was unreachable during this audit. A read-only SQLite
snapshot retained twelve tasks: three workflows and nine MCP children, all marked
done. The workflows were dated 2026-11-01 and recorded `success`. That small,
retained sample cannot establish reliability or a failure rate. Historical
filesystem writes were not replayed or assessed for user authorization.

The findings below come from current source and deterministic probes. Scripted
bad decisions demonstrate what the harness permits; they do not show that the
live model actually made those decisions.

## Concrete gaps

| Boundary | Current behavior and evidence | Consequence |
| --- | --- | --- |
| Tool understanding | MCP schema projection retains only type/description/required; enum, nested constraints and other schema details are dropped. The planner renderer then omits even the argument types and descriptions. | A smaller model has to guess how to call tools whose complete contracts the host originally received. |
| Argument admission | `WorkflowSkillRegistry.spawn_child` resolves a name and forwards a dictionary without schema validation. The offline probe's invalid enum/type/extra argument reaches the spawn callback. | Individual tools must catch malformed calls; there is no shared repairable validation result. |
| Selected operations | The planner receives a potentially narrowed menu, but response validation receives every registered skill name. | A registered operation absent from the selected menu can still be accepted. Disclosure and authorization are also separate concerns. |
| Cancellation | The loop checks cancellation before planning and after acting, but not at the final dispatch boundary. A simulated cancellation during planning still dispatches one child. | Cascade cancellation cannot cancel a child that did not exist when the parent was cancelled. New work needs admission against the current cancellation generation. |
| Result understanding | MCP text is capped at 6,000 characters, then `_summarize_child` caps the planner's observation at **500**. Planner history additionally caps observations at 600 and ages older steps out of a 4,000-character default history. | Reading more data does not mean the planner can use it. Important content beyond the prefix is invisible; increasing a loop limit does not fix this. |
| Evidence and media | Planner steps store skill/args/status/short observation, without child-result references. MCP text is flattened; non-text blocks are counted rather than retained as usable artifacts. | A planner cannot reliably revisit omitted source data or inspect a returned image/artifact through a shared result interface. |
| Completion | `finish` is accepted as a planner verdict. Invalid/missing outcome values default to success. A scripted failed child followed by `finish: success` produces a successful workflow result. | Tool termination, goal completion and evidence-supported success are conflated. Prompt instructions alone enforce honesty. |
| Continuation | Steps, repeated-call state, counters and timing are local variables in a daemon thread. `resume` explicitly fails. Boot recovery marks running tasks interrupted. | Long investigations lose their working state across restart; there is no safe side-effect replay contract. |
| Waiting and budgets | A child awaiting input releases the shared worker pool but its parent retains a waiting daemon thread and running task status. The parent's elapsed wall budget includes user waiting; no cumulative token/cost reservations survive restart. | Waiting, active computation, expiry and resource admission need distinct accounting before longer jobs or parallel delegation. |
| Policy and effects | The generic MCP handler calls the manager directly. The host's approval helpers are not called on that path; `TaskCapability` currently describes only id/label/destructive. Native file-write handlers described in older docs have been replaced by the filesystem plugin. | Future browser, mail, filesystem and device operations need one scope-aware dispatch contract. Existing approval settings must not be advertised as universal plugin enforcement. |

Current plugin Python executes in process and remains trusted installed code.
A host dispatch policy is an application contract, not an OS sandbox for hostile
plugins; preserve that distinction from the existing roadmap.

## Source owners

- Planner inputs, rendering and response parsing:
  [workflow_planner.py](../../app/core/tasks/workflow/workflow_planner.py).
- Ephemeral loop, child observations, dispatch and final aggregation:
  [goal_workflow_handler.py](../../app/core/tasks/workflow/goal_workflow_handler.py).
- Skill contracts and spawn:
  [skill_registry.py](../../app/core/tasks/workflow/skill_registry.py),
  [MCP bridge](../../app/core/tasks/workflow/mcp_skills.py).
- External result flattening and cancellation limitations:
  [MCP handler](../../app/core/tasks/handlers/mcp_tool.py),
  [MCP manager](../../app/mcp/client/manager.py).
- State, delivery and recovery:
  [task_orchestrator.py](../../app/core/tasks/task_orchestrator.py),
  [task_store.py](../../app/core/tasks/task_store.py),
  [recovery.py](../../app/core/tasks/recovery.py).
- Delegation's current goal-only input and late work inspection:
  [workflow_tools.py](../../app/llm/tools/workflow_tools.py).
- Current approval wiring and plugin trust:
  [task_orchestration_mixin.py](../../app/core/session/task_orchestration_mixin.py),
  [capabilities.py](../../app/core/tasks/capabilities.py), [plugins.md](../../docs/plugins.md).

## What the harness should provide

### 1. A clear job contract

Capture the objective, requested deliverable, completion criteria, constraints,
initiating user/session, source of authorization, permitted resources and budget.
Include bounded references to relevant conversation, attachments and knowledge
rather than expecting a rewritten goal to reproduce every detail or copying all
memories into every worker prompt. Evidence carries its source time and revision.

Distinguish user-requested jobs from Aiko's self-directed projects. Both need
scope and evidence; a self-directed interest does not grant new external access.

### 2. A durable workspace for facts and outputs

Keep the task id as the run identity. Persist a compact plan, current step,
accepted facts with evidence references, open questions, constraints, child ids
and produced artifacts. Store observable decisions and short reasons, not hidden
chain-of-thought.

Expose bounded, versioned result inspection to the planner: fetch a child result,
search within it, request the next page, or inspect a media/artifact reference.
Preserve full available content outside the prompt and state explicitly when a
source is partial. Compact context should retain references to earlier evidence.

Tool output is data, not permission or instructions. Generated work stays a task
artifact until an explicit, source-aware memory path qualifies it for memory or
concept learning.

### 3. One operation contract and execution gate

Preserve full input schemas. Give each operation a stable owner/version, output
contract, effects classification, resource scope, timeout, concurrency key and
retry/reconciliation policy. Adapt existing v1 plugins rather than breaking them.

Discover capability metadata, then expose a bounded selection of complete schemas
and relevant skill guidance. Validate the exact selected set and arguments.
Recheck live grants, resource scope, cancellation and budget immediately before
dispatch, including after model or approval waits. Existing authorization should
be reusable within its scope; this should not become repeated generic approval.

### 4. Verified completion and safe continuation

The planner proposes completion; the host checks the criteria and the receipts.
A failed exploratory step does not prevent success if another path genuinely
satisfies the goal. Conversely, a successful tool call does not establish that
the goal was achieved. Represent verified, partial, unsupported and uncertain
results explicitly rather than defaulting malformed verdicts to success.

Persist step intent, child association, evidence and cumulative budget. Waiting
releases execution resources and has a durable wake condition. Resume already
completed reads from their saved results. Commit completion and its delivery
outbox together so restart does not lose or repeat the report.

For external writes, record effect intent and receipt separately from narration.
A timeout after dispatch can mean the effect happened. Reconcile using provider
idempotency/read-back when supported; never blindly replay an uncertain write.
Cancellation suppresses future dispatch and reporting but cannot undo a remote
effect that already happened.

### 5. Capability profiles and measured delegation

Researcher, reader, browser operator, writer and verifier can be bounded work
profiles: role instructions, selected operations, model route, context policy,
output requirements and budget. They need not be separate permanently running
agents or new models. Route model work through the existing host priority lanes.

Add parallel children after durable ownership and shared budget reservations
exist. Start with independent reads; serialize mutations to shared resources such
as one browser session, file or account. Conversation and voice latency remain
measurable constraints, including while jobs wait or retry.

## Proposed implementation sequence

1. **Operation contracts and admission.** Preserve/render schemas, validate
   arguments and the selected operation set, recheck cancellation before child
   creation, and remove malformed-outcome success fallback. Introduce the shared
   dispatch seam and explicit v1 compatibility behavior. This is the first slice.
2. **Evidence workspace and completion.** Retain child ids/full result references,
   add bounded result inspection, capture a job contract and completion checks.
   Prove that an answer depending on data after character 500 can be grounded.
3. **Durable read-only continuation.** Persist a two-read job, stop/restart between
   reads, reattach its child, preserve budget and deliver one checked result.
   Include a crash between child creation and checkpoint publication.
4. **Scoped effects and recovery.** Finish operation grants, resource locks,
   effect reconciliation and completion outbox. Test a fake write whose response
   is lost before adding wider real write capabilities.
5. **Profiles and new capabilities.** Extend the plugin SDK and add a read-only
   source-code investigation profile first. Follow with other capabilities and
   bounded parallel reads after their contract suites pass.

Extract these concerns into focused modules behind existing interfaces. Avoid
expanding the already large session mixin and task orchestrator with every new
responsibility. The full P3–P8 roadmap remains the design reference; more tools,
larger iteration caps or more delegated agents alone do not implement it.

## Acceptance scenarios

- Cancellation during a planner call dispatches zero new children.
- Invalid/undisclosed arguments produce a structured validation observation and
  no side effect; one bounded correction is possible.
- A result whose key fact is beyond the visible prefix remains inspectable and
  can support a cited answer without flooding the prompt.
- Invalid finish metadata and unsupported success claims cannot become verified
  success; recovered exploration can still succeed when criteria are met.
- Restart between reads does not reread already completed work unnecessarily;
  duplicate wake/child events do not duplicate execution or reporting.
- Waiting for the user consumes no model slot or active-compute budget; cumulative
  consumption survives restart and cannot reset through child spawning.
- A timed-out fake mutation is reconciled, not repeated; revoked scope prevents
  new dispatch after a wait. Existing authorized scope avoids redundant prompts.
- Many installed capabilities leave unrelated prompts bounded; mixed/ambiguous
  goals can discover more capabilities without a full-catalogue fallback.

## Verification

The offline probe reproduced the cancellation/finish/schema/selection boundaries
and the 500-character child observation limit. The existing related regression
baseline passed 168 tests. No app behavior was modified and no live model behavior
was evaluated. New implementation should turn these observations into regression
tests for the desired contracts, rather than pinning the current gaps as expected
behavior.

The standalone probe has no importing tests in the affected-test selector; it was
executed directly, and the 168-test baseline was chosen explicitly from workflow,
planner, skill registry/router, failure guards, orchestrator, approval and MCP
bridge/handler tests. `npm run lint` passed.
