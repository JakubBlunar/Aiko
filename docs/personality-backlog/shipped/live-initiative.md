# Shipped Live initiative slices

Historical phase notes; their remaining scope is superseded by the [current L19-L31 queue](../live-initiative.md). Recorded old test totals describe those implementation runs, not this documentation audit.

## Implementation log

### Phase 7: quiet-opening context and reachable idle wakes (30 Sep 2026)

**L23 plus a bounded L20/L29 follow-up.** Prompt version 2 explicitly supplies
the current trigger, floor/turn/typing/speech state, elapsed user-intent and
Aiko-speech times, stale-source flags and situation age. Recent impulses carry
their age, or `unknown` if unavailable. Candidate menus show remaining attempt
and source lifetime plus completed evaluation count, explicitly not a claim
that the underlying content is fresh. Instructions distinguish a fresh user
message from an idle opening and prohibit re-acknowledging an old message as
if it just arrived; wait/noop and all existing speech gates remain available.

Known zero connected clients now suppress heartbeat-promoted idle
reconsiderations, including expired waits, without spending the candidate's
one-shot wake. Reconnection permits the next otherwise-eligible wake; unknown
client counts keep their previous behavior. This is not physical-presence
inference and does not change activity-driven decisions or enqueue/dispatch
reachability checks. No cadence or disclosure permissions were loosened.

**Validation:** 51 focused prompt/heartbeat tests, 400 Live tests plus nine
subtests, and full parallel suite 12,081 passed, eight skipped, with only the
existing MCP private-reach budget failure (471 > 466). `npm run lint` passed.
Tests distinguish synthetic fresh-message/quiet-opening inputs, preserve the
prompt budget, and cover disconnected candidate/wait suppression and a single
reconnect wake. **No real routed-model bake-off was run:** these are input and
runtime-contract checks, not evidence of an improved speech-choice rate.

**Next:** synthetic paired replay on the actual route, full build/route and
pre-policy diagnostics, persistent candidate outcomes, fresh C6/result supply,
and L24 delivery settlement before expanding unsolicited speech. Phases 5-7
require a backend restart to load; this work did not restart or reconfigure
the running app or write test conversations into its history.

### Phase 6: source-bounded candidate reconsideration (30 Sep 2026)

**L21/L22 partial implementation.** A pooled row with a parseable expiry now
gives its Live projection a separate source deadline. Expiry of the 45-second
action attempt defers it rather than blocking it forever; a current pending
row must be returned again before a real opening or a 60-second deadline can
reactivate it. The original source deadline never renews on merge. Source
expiry and consumption remain terminal. Rows without source-expiry evidence
retain the previous conservative 15-minute/one-opening fallback.

The controller records `eligible_urge_ids` at submission and
`evaluated_urge_ids` only after a valid, accepted, current-generation decision.
Only those menu members spend an evaluation; busy/user turns, invalid JSON,
missing clients and cancellations do not. An unresolved eligible choice
immediately defers with `policy_deferred`; a second completed evaluation ends
the local attempt with `evaluation_limit`. This is an explicit bounded stop,
not a claim the cue was spoken or resolved in the pool. Deferred deadlines
are visible in the frame. Existing eight-item menu limits remain; intake
examines at most 128 pending rows and applies cooldown/duplicate filtering
before filling that menu, rather than taking the first eight blindly.

**Still open:** persisted evaluation/retry ownership across process or session
reset, fair pagination beyond the bounded scan, richer source-purpose mapping,
quiet/withdrawal lifecycle and transactional delivery outcomes. No cue-pool
state is mutated by peeking or by this evaluation accounting. This slice is
not a guarantee that every unresolved source deserves repeated speech.

**Validation:** 397 Live tests plus nine subtests; full parallel suite 12,078
passed, eight skipped, with only the existing MCP private-reach budget failure
(471 > 466). `npm run lint` passed. Regressions cover long busy intervals,
removed/consumed/expired sources, the retry deadline and evaluation limit,
cooldown-before-pagination, and completed-eligible versus invalid/busy/user
or omitted-menu cases.

### Phase 5: joinable decision traces (30 Sep 2026)

**L19/L30 partial implementation.** The existing Live MCP snapshot now adds
`decision_trace`: a 64-record content-free tail with process/controller IDs,
decision IDs, loaded decision-code digest, prompt version, generation,
trigger, floor/typing/turn state, speech budget, actual standard-builder menu
IDs and durable cue IDs. Submission, completion, parsing/cancellation/failure,
chosen intent, execution and main-wake rejection are separate fields. Each
completed attempt also emits one structured `live decision` record into the
existing rotating app log, so retained logs can join attempts across restarts.
Custom prompt builders report an unknown menu rather than a fabricated one.

The additive `live_policy_warm_requested` names what the old loaded flag
actually measures; it does not assert model health. The digest fingerprints
the loaded decision and prompt functions plus instructions, **not the whole
checkout or a verified git revision**. No private subjects, dialogue, model
reasoning, generated reason tags or invalid-output previews are written into
the new trace; existing policy log lines also stop printing the latter two.
Other legacy diagnostic fields are not a content-free export.

**Still open:** a bounded SQLite/report surface beyond rotating-log retention,
pre-policy gate/selection omissions, full build/route identity, candidate
outcome ownership and delivery settlement. A completed inference is not an
eligible opportunity or delivery. No prompt behavior, cadence or permissions
changed in this slice.

**Validation:** 392 Live tests; full parallel suite 12,073 passed, eight skipped,
with only the existing MCP private-reach budget failure (471 > 466).
`npm run lint` passed. Tests cover menu privacy, source joins, bounded history,
invalid JSON, missing client, model failure and completed-but-cancelled calls.

### C6 pilot: companion-activity share (28 Sep 2026)

The shipped C6 `companion_activity` pool cue now projects into Live as
`share_observation`, not an `ask_about_result`. A worker-published cue
can trigger one policy reconsideration; tests cover question-disabled
admission and a bounded cue-ID handoff without the reading or title in
the policy call or queue payload. The main turn still takes the cue
under the existing L18 contract. This does not finish L19-L25 or add
route-aware disclosure, reliable delivery, repeated-pattern memory or
UIA. See [C6's Live pilot](../proactive.md#live-pilot-and-later-impulses)
for later grounded impulse ideas and the local/cloud routing decision.

### Phase 4: known-client reachability gate (28 Sep 2026)

**Limited L20/L24 slice; both remain open.** A reported zero WebSocket client
count blocks a new Live main-wake enqueue and drops an already queued wake
before starting the main turn. A pre-enqueue rejection uses Phase 3's token
refund. A connected but blurred window is not treated as absent.

**Shortcut and follow-up:** connection means only that an output channel is
reachable, not that the user is present, listening, or interruptible. Hosts
without a client count retain their existing behavior. A cue already consumed
when its queued wake is later dropped is not automatically restored; durable
L21/L22 candidate ownership and L24 dispatch settlement are still required.
L20 still needs explicit away/DND state, per-device evidence and expiry.

### Phase 3: failed-enqueue reservation release (28 Sep 2026)

**Limited L24 slice; L20/L24 remain open.** A Live main-wake now reserves its
speech token before `BrainLoop.enqueue`, but a false return or exception refunds
that token, keeps the urge active, and records `enqueue_failed` instead of an
admission. Successful enqueues still spend the token and consume the urge.

**Shortcut and follow-up:** a failed enqueue does not automatically reschedule
the same cue; durable L21/L22 scheduling is still needed. A successful queue
write is not generated speech or confirmed playback. Recipient availability,
source revision, pre-dispatch cancellation, delivery acknowledgment and
restart-safe caps remain L20/L24 work. This change does not enable a new speech
channel or relax any existing speech gate.

### Phase 2: policy-worker failure visibility (28 Sep 2026)

**Limited L19 slice; L19 remains open.** Live situation diagnostics now report
a redacted `policy_failure_count` and `last_policy_failure` with a reason,
session generation and trigger. Worker exceptions are `model_failure`; a
missing policy client is `no_client`. Neither is counted as successful quiet.
The existing Live situation MCP snapshot includes these additive fields.

**Shortcut and follow-up:** this is in-process, last-failure visibility, not a
joinable or persistent funnel. It does not yet report no candidate, in-flight
coalescing, declined cues, unavailable recipients, or brain delivery outcomes.
The exception message is deliberately absent because it can contain prompt
content. L19's bounded `why_silent` view and transition history remain open.

### Phase 1: one-shot cue-backed policy wake (28 Sep 2026)

**Shipped slice of L21/L22; L19-L31 remain open.** A pooled cue projected during
a `data_only` heartbeat no longer receives `opportunity_seen` before policy
evaluation. If a subsequent heartbeat finds a fresh cue with a legal
`request_main_speech` admission and there is no scheduled wait, it promotes
one `idle.reconsider` decision. The cue ID is marked woken in process memory to
avoid a policy call each second. User intent, wait expiry, sleep, quiet mode,
speech permission, floor state, question allowance and main-wake budget keep
their existing deterministic gates. Generation change clears the wake set.

**Shortcut and follow-up:** this is a one-shot opportunity for each active urge,
not a durable candidate scheduler. An available source that appears after the
first eight pending rows, or a cue expiring before a suitable opening, still
needs L21. A model choosing a nonverbal action may leave an unresolved cue
without another scheduled decision; L22 needs a bounded defer/deadline contract.
No delivery result or explicit recipient availability was added; L19/L20/L24
must precede a wider rollout. A read-only MCP snapshot from before this change
must not be presented as proof that the new path speaks proactively.

**Checks:** 11 targeted heartbeat tests and 51 neighboring Live tests passed.
This phase touched only Live behavior, its existing test file, and backlog docs.

This continues [Live mode](../live-mode.md), including shipped L17 deferral and
L18 exact cue handoff. It does not propose another companion model, another
conversation loop, or a second memory system. It replaces several local
heuristics with an explicit lifecycle for grounded initiative.
