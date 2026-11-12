# Live initiative: remaining lifecycle work

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## Implementation log

Phases 1-7 and C6 pilot write-ups moved to [shipped initiative](shipped/live-initiative.md). Every
L19-L31 item still has remaining scope; no whole-item closure is inferred from a phase.

## L19. Explain silence across the entire funnel

**Status: Partial.**

**Implemented / available:** Policy failures and bounded content-free decision/menu/execution traces
exist in MCP and rotating logs.

**Remaining:** Add persistent/reportable pre-policy omissions, complete build/route identity and
candidate-to-delivery joins with unknown states.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l19).

**Source check:** [diagnostics.py](../../app/core/live/diagnostics.py),
[controller.py](../../app/core/live/controller.py),
[live_mode_tools.py](../../app/mcp/server_tools/live_mode_tools.py),
[delivery.py](../../app/core/conversation/delivery.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Reproduce no wait, empty menu, disabled speech, invalid JSON,
queue failure, main-brain silence and unavailable audio as distinct results.
An `offer_now` blocked before execution must not disappear from statistics.
Missing backend/telemetry must read unknown, not healthy. Existing diagnostic
payload consumers keep working through additive fields.

</details>

## L20. Availability is not window focus

**Status: Partial.**

**Implemented / available:** Known zero-client enqueue/dispatch and idle-wake suppression exist;
blurred connected windows are not automatically absent.

**Remaining:** Add explicit away/DND, per-device capability/evidence/expiry and source-aware
interruptibility; connection is not physical presence.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l20).

**Source check:** [proactive_presence_mixin.py](../../app/core/session/proactive_presence_mixin.py),
[evidence.py](../../app/core/activity/evidence.py),
[assembler.py](../../app/core/live/assembler.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Cover alt-tab while working, long reading without input,
locked desktop, disconnected client, suspended mobile tab, two disagreeing
windows, explicit away, and stale C6. No fabricated activity when sensors are
off. Returning reevaluates current relevance and permits at most one bundled
opening, not one greeting per device. User-initiated turns remain immediate.

</details>

## L21. Durable intentions, short-lived action attempts

**Status: Partial.**

**Implemented / available:** Source-bounded attempt lifetimes, deferred retries and
completed-eligible evaluation accounting exist in process.

**Remaining:** Persist intention/attempt/outcome ownership across reset, paginate fairly beyond
bounded intake and model quiet/withdrawn/consumed states without renewing sources.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l21).

**Source check:** [cue_adapter.py](../../app/core/live/cue_adapter.py),
[urge_store.py](../../app/core/live/urge_store.py),
[inclination.py](../../app/core/live/inclination.py),
[cue_store.py](../../app/core/proactive/cue_store.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A cue created during a data-only heartbeat remains eligible
after 45 seconds if never evaluated. A cue held during typing can meet a later
opening once, without reviving the original action. Explicit dismissal,
answered/used cues, source deletion and expiry do not rearm. A ninth eligible
cue remains discoverable behind eight ineligible ones. A day-long synthetic
run and restart leave bounded storage and no cross-user/session leakage.

</details>

## L22. Event and deadline scheduling with a liveness guarantee

**Status: Partial.**

**Implemented / available:** One-shot cue-ready/idle reconsideration and bounded deferred deadlines exist.

**Remaining:** Provide restart-safe event/deadline scheduling with a demonstrated liveness/fairness
guarantee; no policy call on every heartbeat.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l22).

**Source check:** [live_mode_mixin.py](../../app/core/session/live_mode_mixin.py),
[wait.py](../../app/core/live/wait.py), [epochs.py](../../app/core/live/epochs.py),
[budget.py](../../app/core/live/budget.py), [controller.py](../../app/core/live/controller.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A valid candidate plus an available recipient reaches evaluation
within one configured opportunity deadline without user input. Empty-stock,
disabled, DND and away scenarios do not cause model polling storms. Exhausting
an expression bucket does not suppress an eligible main-wake. Event floods
coalesce, user input preempts, and self-generated events cannot oscillate.

</details>

## L23. A compact 4B deliberator with useful choices

**Status: Partial.**

**Implemented / available:** Prompt v2 has explicit quiet-opening/fresh-input timing, source
lifetime and evaluation counts.

**Remaining:** Run paired actual-route choices, compact input and useful alternatives; prompt
fixtures do not prove the model chooses appropriate speech.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l23).

**Source check:** [prompt.py](../../app/core/live/prompt.py),
[proposal.py](../../app/core/live/proposal.py), [urge_menu.py](../../app/core/live/urge_menu.py),
[policy_context.py](../../app/core/live/policy_context.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** On a fixed synthetic corpus the configured 4B differentiates
share, continuation, question, preparation and silence; it does not choose
questions merely because a source is called curiosity. Include low-vitality,
focus, direct-answer-owed, thin evidence, multilingual names and injection-like
source text. Compare selection quality, validity, latency and eligible speech
rate; do not claim that a 4B is sufficient without this evaluation.

</details>

## L24. Transactional brain handoff and honest completion

**Status: Partial.**

**Implemented / available:** Failed-enqueue token refunds and known-client checks exist; K99 tracks
recent delivery provenance.

**Remaining:** Settle reservation/claim/dispatch/presentation/playback transactionally, preserve
outcomes across restart/cancellation and handle consumed-but-undelivered cues. Queue success is not
delivery.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l24).

**Source check:** [main_wake.py](../../app/core/live/main_wake.py),
[live_mode_mixin.py](../../app/core/session/live_mode_mixin.py),
[task_orchestration_mixin.py](../../app/core/session/task_orchestration_mixin.py),
[delivery.py](../../app/core/conversation/delivery.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Cover enqueue failure, cue deletion, user interruption before
and during generation, main silence, partial audio, disconnect, late duplicate
ack and session switch. One logical candidate cannot produce two simultaneous
utterances. No generated-but-undelivered thought is learned as disliked or
marked answered. Existing typed/voice user turns and task report contracts
remain intact.

</details>

## L25. Grounded impulses and a connected curiosity ledger

**Status: Partial.**

**Implemented / available:** Existing curiosity/tasks/concepts and C6 observational-share projection
supply grounded candidates.

**Remaining:** Connect fresh result/source renewal and answer/use outcomes with explicit purpose;
reuse K97-K99/H7 rather than creating a second curiosity ledger.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l25).

**Source check:** [curiosity_seed_worker.py](../../app/core/proactive/curiosity_seed_worker.py),
[idle_curiosity_worker.py](../../app/core/proactive/idle_curiosity_worker.py),
[forward_curiosity_worker.py](../../app/core/proactive/forward_curiosity_worker.py),
[cue_producer.py](../../app/core/proactive/cue_producer.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Test a natural follow-on after a lull; a researched interest
shared without a question; an answered or declined question never reasked;
an overdue but irrelevant plan kept quiet; a corrected premise invalidated;
an empty-history install that does not invent shared memories. Every claimed
activity/result has evidence from the owner that completed it.

</details>

## L26. Background work during real co-presence

**Status: Open implementation.**

**Implemented / available:** Demand probes, compute/LLM lanes and resource contention gates exist.

**Remaining:** Use actual capture/inference load and declared worker cost/interruptibility to
schedule bounded preparation during co-presence; an open mic is not GPU ownership.

**Source check:** [lifecycle_mixin.py](../../app/core/session/lifecycle_mixin.py),
[idle_worker_scheduler.py](../../app/core/proactive/idle_worker_scheduler.py),
[worker_matrix.py](../../app/core/live/worker_matrix.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A long mic-on quiet session can replenish one depleted thought
source. Speech and typed input interrupt preparation promptly; no voice dropout
or main-turn latency regression beyond a measured budget. Shared scenes are
not relocated, sleep does not trigger unsolicited speech, and queueing/model
swapping scenarios cannot deadlock. Compare mic-on and mic-off inventory age.

</details>

## L27. An extension contract for producers and behaviors

**Status: Open implementation.**

**Implemented / available:** CuePolicy/CueProducer, the impulse bus and ToolPlugin SDK exist.

**Remaining:** Define a small producer/behavior contract with ownership/freshness/purpose/cost and
two real adapters; plugin worker/context lifecycle remains platform work.

**Source check:** [bus.py](../../app/core/live/bus.py),
[cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[cue_producer.py](../../app/core/proactive/cue_producer.py), [sdk.py](../../app/plugins/sdk.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A synthetic worker and a synthetic plugin each produce one
candidate without editing controller/arbiter/prompt branches. Duplicate,
late, malformed, high-volume and malicious-text events remain bounded. A
producer cannot mint user intent, bypass DND, supply a rig instruction, or
execute an unregistered tool. Test crash between commit and wake publication.

</details>

## L28. Learn timing and interests without rewarding pestering

**Status: Open implementation.**

**Implemented / available:** Outcome and cue accounting exist.

**Remaining:** Learn only from attributable more/less/not-now/dismissal and source-linked use;
separate unavailable/undelivered/unknown from disinterest and never optimize chat duration.

**Source check:** [cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[concept_view.py](../../app/core/concepts/concept_view.py),
[delivery.py](../../app/core/conversation/delivery.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Explicit dismissal suppresses the topic; not-now postpones
without permanently deleting interest; absent/no-delivery outcomes leave
preference estimates neutral. One enthusiastic reply cannot dominate future
selection. Small samples remain neutral and no adaptive value can loosen DND,
permissions, quiet hours, daily limits or privacy. Compare with a fixed-policy
baseline before enabling learning.

</details>

## L29. Desktop and mobile delivery are different capabilities

**Status: Partial.**

**Implemented / available:** Mic/audio ownership and zero-client suppression exist.

**Remaining:** Add per-device delivery capabilities/leases and cross-device attempt/presentation
dedupe; explicit deployment posture is required and reachability is not audibility.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l29).

**Source check:** [LiveModeToggle.tsx](../../web/src/features/chat/LiveModeToggle.tsx),
[ws_live_commands.py](../../app/web/ws_live_commands.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Desktop plus mobile cannot both speak the same attempt. Cover
tab suspension, lock/unlock, network loss, stale leases, audio denial, backend
restart and phone reconnect. Test browser-only operation without C6. No hidden
microphone permission request or promise of background operation on an
unsupported deployment. Record platform limitations in the UI/docs at rollout.

</details>

## L30. Replay and rollout are part of the architecture

**Status: Partial.**

**Implemented / available:** Bounded traces include loaded decision-code identity and deterministic
regression coverage.

**Remaining:** Build shared DT4/T5 replay, actual-route comparisons and verified whole-build/route
rollout identity; rotating logs alone are not a reproducible evaluation corpus.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l30).

**Source check:** [test_live_mode_pass13.py](../../tests/test_live_mode_pass13.py).

## L31. Later pilot: permissioned email assistance

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** Plugin/task/approval foundations exist.

**Remaining:** After platform/durable-job work, pilot permissioned read-only email with explicit
account/data/operation scope; provider integration and sends are not implemented.

[Implemented slice](shipped/audit-2026-10-24.md#live-initiative-l31).

**Source check:** [sdk.py](../../app/plugins/sdk.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Begin with fake inbox fixtures: duplicate mail, expired auth,
offline host, ambiguous importance, quiet hours, sensitive previews and prompt
injection. An incoming message may become a deferred digest or be ignored;
it must not automatically interrupt or execute a request contained in the mail.
Revoke access and verify polling, queued access and sensitive cached payloads
follow the selected deletion/retention policy. The pilot should require plugin
registration and adapters, not a Gmail branch in the Live controller.

</details>
