# Context and scheduling for background work

Task planning, evidence review and work discussion have their own context and
generation policy. These policies do not reduce cognition workers' reasoning
allowance or change the shared model's allocated context window.

## Context selection

The local workflow transport supplies its effective configured context window;
remote clients use the workflow route's setting. Divergent local workflow routes
have their own transport settings even when they share the worker's model/gate.
An unknown window
uses a conservative 4,096-token fallback without probing or loading a model.
`context_budget.py` reserves answer tokens, task-specific thinking tokens and a
safety margin before selecting input. The initial soft input target scales with
the window up to 12,288 tokens. It can grow toward the hard headroom boundary to
retain a long request, current evidence or a complete operation schema.

Selection preserves the original objective, completion criteria, constraints,
original conversation request and explicit user guidance. These are never
replaced by model-generated notes or shortened to make history fit. New jobs
retain the full original request, and explicit guidance no longer loses older
messages after ten updates. If the fixed request cannot fit safely, the task
asks for a narrower step or larger context. It does not infer after silently
discarding a constraint.

Current observation, whole tool schemas and capability guidance come before
optional excerpts, plans, notes and older history. Menus contain up to six tools
with stable six-item discovery pages and goal-based ordering. Schemas and their
descriptions remain whole; the compact renderer avoids repeating argument lists
already present in the schema. Omitted tools cannot be selected for dispatch.

Large observations use marked, goal-relevant whole-line excerpts. Full results
remain in the existing task store. `inspect_result` can read/search them, and
`inspect_result {list:true,offset:0}` discovers owned result IDs when old history
has been omitted. An excerpt never proves that an omitted item is absent.

Token counts are estimates, using the existing calibrated estimator plus a
conservative character/Unicode floor. Provider-reported input usage adjusts a
per-job estimate scale for the current model/context/client revision. It tightens
immediately when needed and relaxes slowly when the prompt mix changes. Route
changes reset this calibration. This is not an exact model tokenizer.

Completion review has its own bounded pack with the full contract and findings.
Inspected excerpts receive priority over giant file prefixes. Truncation and
omitted records remain explicit. Cited IDs must belong to successful child
results, and a review missing a cited record does not run or approve completion.
Acknowledgement is still not read-back proof. Work discussion uses the same
accounting and preserves the latest user question; it cannot dispatch tools.

## Plugin-owned current observations

The SDK's optional `MiddlewareResult.planner_context` carries a plugin-formatted
projection, for example:

```python
MiddlewareResult(
    content=rendered_page,
    summary="Current page observed",
    planner_context={
        "kind": "current_page",
        "resource": page_identity,
        "content": rendered_page,
        "summary": "Current page observed",
    },
)
```

The browser bundle supplies this projection. Core attaches the source task,
recorded time, server ownership and configuration generation. The latest
projection is separate from the 600-character historical observations, allowing
later controls/refs to survive in the next decision's prompt.

Another operation on that owner conservatively invalidates the projection, and
a changed/unavailable capability generation invalidates it before planning.
Resuming a saved execution slice also invalidates cached targets; a newly
completed observation can replace them. This avoids reusing refs after a pause.
The planner must obtain a new observation before reusing targets. Reading an
old result does not refresh its browser refs. Browser semantics and formatting
remain in the plugin; the host hook is generic.

## Scheduling and budgets

The existing priority order remains conversation workers, maintenance workers,
then task inference. Gate admission is FIFO within each tier. Running inference
is non-preemptive; shorter task calls create more frequent scheduling points.
Routine browser decisions disable thinking and reserve up to 384 answer tokens.
Planning/recovery reserve up to 768 answer tokens and 512 thinking tokens, scaled
down for small windows. Per-call Ollama headroom is capped through a context-local
scope, leaving the provider's normal cognition settings unchanged.

Inference calls have a 30-second request allowance bounded further by remaining
work time. The provider's automatic no-thinking fallback shares the task call's
remaining deadline and skips retries after cancellation. These are cooperative
client limits, not a guarantee of immediate GPU preemption or cancellation of an
already-dispatched external operation.

Queued work checks cancellation/execution generation before inference. A
ten-second admission timeout pauses checkpointed work as `waiting_model`, then
automatically retries after a short wait. Queue time consumes neither active
inference allowance nor planning iterations. A pending completion review is
saved and resumed directly rather than repeating the planner. A manual pause
still owns the wait. Model/client/context edits while queued force a fresh pack.

Time reservations are zero while queued. Once admitted, a bounded reservation
protects crash/restart accounting; returned calls replace it with measured active
time. Completed inference-only repair attempts retain their reported token cost
even if a later attempt defers. Invalid planner JSON gets at most one bounded
correction; repeated invalid operation arguments stop with saved progress. No
repair automatically repeats a dispatched browser/file operation.

`GatedChatClient` captures an immutable client/gate target for each call, so a
runtime retarget cannot release another gate or change the admitted provider.
Reconfiguration on the same worker resource retains the gate and its existing
holders/waiters, including when its concurrency setting changes.

New user guidance during planning or review discards the old decision before
dispatch or completion. The next planning step includes the saved guidance.

## Diagnostics and verification

The existing work drawer shows shared-model waiting separately from active
calls. Its Usage and allowance panel distinguishes estimated versus reported
input tokens, generation reserves, safety headroom and the job's separate total
allowance. `work_context_budget` events expose selection/omission and calibration
details for debugging without logging prompt bodies.

Focused checks cover 4k–64k packing, Unicode, immutable constraints, complete
schemas, late browser refs, stale observations, inference-only repair, queue
cancellation, retargeting, deferred review and cognitive headroom parity. These
tests use synthetic responses/owned stores. Real 9B browsing success, token
throughput and cognition latency require a live-model evaluation after restart.
