# Aiko personality backlog

**Reviewed 24 Oct 2026, checkout 8c666cf.** This index lists only unfinished implementation,
explicit validation and deferred decisions. Completed write-ups live in [shipped](shipped.md); the
[migration audit](shipped/audit-2026-10-24.md) records completed slices and source checks. [Dated
review history](shipped/review-history.md) is reference, not a queue or current production
measurement.

**Status meanings:** open = proposed work absent; partial = named implemented slice plus a concrete
remainder; validation = implementation exists but a specific acceptance check remains; deferred = a
stated evidence/capability gate must be met. Opt-in or default-off implementation still counts as
implemented.

Implementation/test sources were inspected. No model calls, app startup, runtime settings changes or
test runs were made in this documentation-only audit. SQLite observations are read-only dated
aggregates, not a quality score. Earlier synthetic runs do not establish general naturalness or
device behavior.

**Queue:** 172 tracked items/follow-ups (70 open, 83 partial, 9 validation, 10 deferred). IDs are
namespaced: health H versus immersion H, concepts L versus Live L, and performance P versus plugin
P.

## Where to start

- Source-to-answer reliability: H7/H44, K100/K107, L49-L52 and RS1/RS2/RS5.
- One competing optional contribution: K92/K93, G6 and P43/P50; preserve required repairs and permissions.
- Live lifecycle: L19-L24 before broader autonomy; source freshness and queue success are not delivery.
- User-visible new experiences: IM1/IM2 first; existing reading/research notes do not close IM3/IM4.
- Evaluate with T5/L55 alongside implementation. K97-K99/K102/K103 are shipped implementations with
  named checks under T5.

## Open items at a glance

### Architecture and code quality

- [A1](architecture.md#a1-typed-public-access-for-remaining-mcp-consumers) — **partial**: Migrate
  remaining MCP subsystem reads to typed public handles and ratchet the actual guard budget; the
  historical reach counts are stale.
- [A2](architecture.md#a2-public-database-connection-and-transaction-ownership) — **open**: Define a
  public connection/transaction contract and migrate private _get_conn consumers.
- [A3](architecture.md#a3-the-files-over-the-declared-budget--and-the-fact-that-they-grow) —
  **partial**: Continue splitting oversized owners when touched, especially prompt assembly,
  inner-life providers, concept synthesis and settings. Recount current files rather than reusing
  the historical inventory.
- [A4](architecture.md#a4-the-two-layering-inversions) — **open**: Resolve upward infra
  text/scheduling imports and decide the intended core-to-MCP protocol boundary before refactoring.
- [A5](architecture.md#a5-what-should-actually-gate-a-commit) — **partial**: Decide and implement
  hooks/CI and narrow React hook linting. Further Ruff rules remain optional. AGENTS.md supersedes
  the old full-suite-after-every-change proposal.
- [A6](architecture.md#a6-shared-provider-gate-and-error-frames) — **partial**: Standardize
  remaining provider gate/error frames while preserving distinct decline reasons, staging, protected
  blocks and prompt tiers.
- [A7](architecture.md#a7-general-settings-use-regression-guard) — **partial**: Add a general
  settings-use guard with explicit reserved/compatibility exemptions. Do not reopen the six resolved
  wiring bugs.

### Avatar and expressiveness

- [B3](avatar.md#b3-blink-rate-modulation-by-arousal-deferred-follow-up-to-b1) — **deferred**: Own
  or extend a safe blink interval setter, then modulate arousal without fighting wink gestures; no
  implemented arousal-controlled driver was found.

### Proactive and presence

- [C3](proactive.md#c3-persisting-last-fired-typed-cooldown-to-disk) — **open**: Persist a
  wall-clock last-fired stamp and safely restore remaining cooldown after restart; keep runtime
  interval math monotonic.
- [C6](proactive.md#c6-companion-mode--the-desktop-as-a-sensory-channel) — **partial**: Retain
  memories only after corroborated repeating patterns with a retention/redaction contract. UIA
  remains deferred pending process/performance/privacy design. Later grounded impulses and
  route-aware disclosure belong with Live L25/L27/L29.

### Awareness and grounding

- [F4](awareness.md#f4-source-cited-memories) — **partial**: Carry usable citation provenance
  through fact-check rewrites and remaining tool/document writers; add the general Memory-tab source
  badge/link. Metadata existence is not end-to-end citation coverage.
- [F11](awareness.md#f11-relevance-driven-memory-resurrection-latent-recall-from-the-archive) —
  **open**: Add topic-return-triggered archive nomination, relevance/staleness checks and a hedged
  callback sharing F15 cadence. Existing age callbacks are not this archive resurrection path.
- [F12](awareness.md#f12-attributable-user-side-memory-retention-credit) — **partial**: Design
  attributable user-side retention credit and decide tiers-disabled recording. Shared following-turn
  engagement is not item-level usefulness; coordinate H34/RS9. F17 owns semantic-credit calibration.
- [F17](awareness.md#f17-should-a-semantic-echo-earn-full-retention-credit) — **validation**:
  Separate real paraphrase use from topical leakage using source/item-level outcomes before changing
  credit. Similar shared-turn engagement does not establish usefulness. No credit policy was changed
  in this audit.
- [F15](awareness.md#f15-memory-repair-requests----admit-the-hole-instead-of-getting-vaguer) —
  **open**: Nominate low-stakes degraded-but-important memories, ask once, capture a source-linked
  answer and refresh the right row. Share F11 cadence and exclude sensitive losses. The old claim
  that L32 is unbuilt is stale.

### Shared moments and boundaries

- [J1](moments.md#j1-multi-user-moments--participant-attribution) — **open**: Add participant
  attribution, scoped reads/UI and migration for implicitly attributed historical moments.
- [J2](moments.md#j2-exportable-timeline) — **open**: Implement a source-faithful Markdown/PDF
  export endpoint and UI action, preserving participant scope and deletion behavior.
- [J3](moments.md#j3-axes-aware-proactive-nudges) — **open**: Wire axes into proactive topic
  selection when another reason already permits a nudge; axis movement must not independently
  trigger speech.
- [J7](moments.md#j7-moment-signal-expansion-and-detector-statistics) — **partial**: Evaluate
  first-time and resolved-reaction signals and expose consolidated detector statistics. Current
  scheduling still derives reaction signals from raw tags; broaden without fabricating milestones.
- [J12](moments.md#j12-explicit-relationship-preferences-and-boundary-propagation) — **partial**:
  Implement the 23 Oct extension: independent friend/flirty/romantic and affection-kind preferences,
  same-turn de-escalation, queued gesture/cue invalidation across clients, restart and paired
  checks. B7 removed touch dispatch gates, so the dial does not prove a universal hard touch cap.
- [J13](moments.md#j13-learned-nickname-adoption-and-reciprocity) — **partial**: Persist explicitly
  adopted names for Aiko, handle changes/reciprocity and let unused names fade within J12
  boundaries. Phase-based permission is not learned nickname adoption.

### Integration and wiring

- [I9](integration.md#i9-mobile-and-pwa-acceptance-checks) — **validation**: Verify installed
  mobile/HTTPS update, offline fallback, keyboard/safe-area and voice behavior on target devices.
  Source/config and layout-state tests are not device E2E proof. Do not rebuild the mobile/PWA
  foundation.
- [I11](integration.md#i11-graduate-a-corroborated-belief-into-a-concept) — **open**: Define
  evidence-spanning graduation, post-promotion ownership and contradiction/retraction routing, then
  implement belief-to-concept promotion. Mood/repetition outcomes are not factual accuracy.

### Background workers

- [G5](workers.md#g5-self-tuning-cue-cooldowns) — **open**: Validate an attributable usefulness
  signal and armed denominators before bounded opt-in cooldown adaptation. Do not tune away topic
  mismatch or use copied turn engagement as item reward.
- [G6](workers.md#g6-per-provider-decline-attribution) — **partial**: Instrument remaining generic
  provider bailouts by failing family; preserve supply-versus-gate and error-versus-silence
  distinctions. Accurate final inclusion precedes learned selection.
- [G7](workers.md#g7-worker-prompt-budget-adoption-beyond-the-first-three-writers) — **partial**:
  Roll out to other worker builders with their actual route/output limits and worker-owned reduction
  rules. The helper and summary cursor repair are already implemented.
-
  [G-CLEANUP](workers.md#g-cleanup-consolidator_statelast_cluster_index-is-not-dead-weight--do-not-drop-it)
  — **open**: Move pulse state to a dedicated key or migrate both users to a correctly named field;
  correct the misleading consolidator comment. Do not delete a column with a real reader.

### Higher-order concepts and reasoning

- [L4](concepts.md#l4-cluster-co-activation-signal) — **partial**: Optional window/gap-session axes
  and evidence-backed mood/arc/world joins remain. A sticky T1 day clause needs measured prefix
  stability.
- [L22](concepts.md#l22-concept-quality-evaluation--observability) — **partial**: Build held-out
  concept-quality cases and a repeatable precision/duplicate/register/promotion scoreboard;
  remeasure post-fix cohorts. Accounting fixtures do not establish concept extraction quality.
- [L29b](concepts.md#l29b-meta-narrative----an-arc-whose-steps-are-concepts) — **open**: Implement a
  source-linked temporal arc over concept changes, with alias/revision handling, lifecycle evidence
  and bounded rendering. A generalization is not a meta-narrative.
- [L30a](concepts.md#l30a-a-grounded-reason-for-hypothesis-uncertainty) — **partial**: Expose a
  concise source-grounded reason for uncertainty. RS2 owns evidence-preserving recall; H7/H44 own
  actual ask/answer quality.
- [L30d](concepts.md#l30d-uncertainty-zones----explicit-known-unknowns-to-aim-curiosity-at) —
  **deferred**: Measure throughput, then trial important known-unknown inflow through the existing
  gap store. Empty kind/subject cells do not establish gaps; do not add supply to an unverified
  consumer.
- [L30e](concepts.md#l30e-invented-hypotheses----a-place-to-make-guesses-up-phase-b) — **partial**:
  Validate targeting, origin/kind outcomes and novelty after refutation; decide consistent
  multi-user/export/wipe semantics with concepts and disabled-invention TTL. The old
  universally-free/unread-origin claim is stale. H7/H44 own evidence quality.
- [L32](concepts.md#l32-behavioral-evidence-in-concept-importance) — **partial**: Evaluate
  attributable behavioral contribution to importance. L30d may reuse the existing context; its
  absence does not reopen the importance axis.
- [L33](concepts.md#l33-introspective-reflection----structured-self-questioning-that-feeds-concepts)
  — **partial**: Evaluate admissible outcomes and proposed hypothesis quality before broadening
  reflective probes. Eligibility is not correctness; alternative prose is not L53 causal testing.
- [L34](concepts.md#l34-concept-relation-taxonomy----edges-beyond-support--tension) — **partial**:
  Define proposed/exposed explains, exemplifies and conditional semantics end to end. Reuse L52
  rather than adding a second dependency truth store; source-linked why recall remains RS2.
- [L36](concepts.md#l36-strategy-layer----learned-approaches-distinct-from-beliefs) — **open**:
  Pilot source-linked strategy transfer with situation/constraints/approach/result/exception
  evidence and held-out contrasts. Engagement is not success; L52 must invalidate unsupported reuse.
  New kind/schema depends on need.
- [L42b](concepts.md#l42b-neglect-guided-curiosity) — **deferred**: Inspect real neglect false
  positives before a bounded curiosity prior; retain relevance/privacy gates and do not ship from
  synthetic findings alone.
- [L43](concepts.md#l43-how-she-thinks-he-sees-her----the-second-order-self-model) — **open**:
  Design a revisable contextual model of received behavior, resolve overlap with axes and guard
  negative spirals/reassurance fishing. Silence/latency alone cannot establish another person's
  appraisal.
- [L44](concepts.md#l44-outcome-grounded-per-domain-calibration) — **deferred**: Validate
  independent source-grounded outcomes and per-domain coverage before calibration. The ledger
  blocker is resolved; repetition/mood checks cannot be reported as factual accuracy.
- [L45](concepts.md#l45-remaining-gate-tuning-rollout) — **partial**: Complete staged
  lifecycle/generative rollout: align dormant/contradicted revival with kind gates, validate
  write-gate changes, then consider per-kind floors, coupled cosine bands and relevance telemetry.
  Global-floor revival branches remain.
- [L46](concepts.md#l46-validate-useful-depth-2-abstractions) — **validation**: Verify grounded
  depth-2 concepts actually reach T3 and inspect representative label quality. Structural depth
  tests do not establish useful abstraction.
- [L48](concepts.md#l48-validate-evidence-routing-and-duplicate-control) — **validation**: Evaluate
  deployed duplicate/reconsideration quality. The historical causal heading is retained for links,
  not as a verified mechanism; do not raise the evidence cap on that premise.
- [L49](concepts.md#l49-independent-evidence-and-protection-against-self-confirmation) —
  **partial**: Audit a consistent deployment, cover remaining writers/mutable sources, preserve
  observations lost by dedupe and distinguish independent episodes/upstream sources. Unknown history
  stays unknown; shadow is not enforcement.
- [L50](concepts.md#l50-context-scoped-claims-and-temporal-validity) — **partial**: Evaluate model
  behavior, arbitrary contexts, erased quotation framing, scoped revival, historical coverage and
  temporal queries. Lexical support is not general entailment.
- [L51](concepts.md#l51-a-bounded-working-model-for-the-current-problem) — **partial**: Measure
  extraction-to-reply entailment, disconfirmation and resumption through corrections/topic changes
  before expansion beyond the pilot.
- [L52](concepts.md#l52-dependency-aware-revision-and-retraction) — **partial**: Extract
  semantically sufficient supports, cover other writers and decide historical backfill. Structural
  conjunctions/alternatives do not establish semantic sufficiency/independence; legacy claims remain
  untracked.
- [L53](concepts.md#l53-causal-explanations-and-counterfactual-checks) — **open**: Trial competing
  mechanisms with assumptions, differing predictions and a permitted disconfirming check; revise
  failed explanations and evaluate matched contrasts.
- [L54](concepts.md#l54-choose-evidence-by-how-much-it-can-change-a-decision) — **open**: Rank
  checks by discrimination and decision value, including retrieval/wait/silence; compare equal
  latency/conversation budgets rather than generating more questions.
- [L55](concepts.md#l55-evaluate-reasoning-independently-of-eloquence) — **partial**: Run repeated
  held-out extraction-to-reply, causal/counterfactual and post-guard fictional-testimony comparisons
  when inference is available. Count timeouts/failures; synthetic correctness is not naturalness.

### Companion patterns

- [K12](patterns.md#k12-calendar-linked-anticipation) — **partial**: UI, recurrence/all-day events,
  cancelled-event restoration, sync/alarms, automatic extraction and real-model anticipation checks
  remain.
- [K19](patterns.md#k19-cold-start-companion-onboarding) — **open**: Implement a short
  conversational onboarding arc with explicit preferences/boundaries and evaluated cold-start
  exploration; shared-turn engagement is not cue-level approval.
- [K33](patterns.md#k33-cozy-mode--persistent-register-softening) — **open**: Add explicit
  persistent cozy-register state, controls and bounded auto-entry/exit across prompt/cadence/UI.
- [K40](patterns.md#k40-comfortable-silence--dont-always-fill-space) — **partial**: Evaluate
  inferred comfortable silence separately; brevity, elapsed silence or low engagement do not
  establish completion. Preserve owed repairs/results.
- [K41](patterns.md#k41-same-reply-mid-stream-self-correction-embedding-variant) — **open**: Add a
  validated stream-time contradiction check and bounded abort/continuation splice across
  transcript/TTS; measure cost/detection first.
- [K42](patterns.md#k42-multi-bubble-reply-bursts--texting-rhythm) — **open**: Implement opt-in
  bounded bursts with shared turn identity, delayed-bubble cancellation and delivery/memory
  attribution.
- [K49](patterns.md#k49-rare-earned-self-edit-cues) — **partial**: Evaluate a rare earned self-edit
  cue. Do not manufacture mistakes or claim markup is missing; over-polish detection was
  deliberately deferred after disfluency overuse.
- [K50](patterns.md#k50-typed-mode-delivery-pacing--the-missing-read--pause--type-beat) — **open**:
  Implement opt-in context-sensitive read/pause/type presentation and typed delivery metadata; test
  responsiveness without forced delay on urgent practical replies.
- [K62](patterns.md#k62-co-experience-companion--follow-a-showalbumbook-with-the-user) —
  **partial**: Canonical prefix-limited retrieval, albums, concurrent titles, implicit progress, UI
  and real-model spoiler tests remain; IM3 owns the shared reader.
- [K77](patterns.md#k77-candor-gate--can-i-be-real-with-you) — **partial**: Evaluate generated
  consent and offer-versus-delivery behavior. Broader concern/relationship candor needs
  evidence/calibration, which L44 does not yet supply.
- [K79](patterns.md#k79-hesitation-tell--typing-latency-as-a-signal) — **open**: Define
  privacy-preserving compose timing/baselines and evaluate a rare leave-space cue. Delay plus
  brevity cannot establish concealed feelings; timing need not transmit draft text.
- [K83](patterns.md#k83-the-right-to-decline--she-always-answers-and-thats-the-tell) — **partial**:
  Evaluate generated adherence, alternatives and asking again. Broader emotional requests, affect
  triggers and repair arc remain absent; distinguish chosen boundary from provider refusal.
- [K84](patterns.md#k84-calibrated-jealousy--the-risky-one-written-down-honestly) — **partial**:
  Evaluate no-guilt/no-carryover and boundary behavior. Unsolicited/absence-based jealousy is
  deliberately excluded, not planned missing work.
- [K92](patterns.md#k92-conversational-stance--one-decision-per-turn-not-ten-permission-slips) —
  **partial**: Convert eager legacy providers to non-mutating candidates; arbitrate essential
  handling with content and evaluate final inclusion/real-route quality. Judgment shadow is not
  activated; global arbitration is unfinished.
- [K93](patterns.md#k93-expand-substance-ranking-coverage-and-validation) — **partial**: Extend
  provider coverage and compare useful content at equal budgets. Coarse priors are not semantic
  quality or causal engagement credit.
- [K96](patterns.md#k96-one-shot-per-thought--in-turn-deliberation) — **partial**: Diagnose deployed
  NONE/valid-thought behavior, then measure admission/expression/answer and next-turn interference.
  In-turn effort selection and other experiments remain unimplemented; adapter tests do not prove
  the live failure fixed.
- [K100](patterns.md#k100-evidence-bound-memory-admission) — **partial**: Cover other writers;
  evaluate paraphrase entailment, useful retention and false durable writes. Two guarded writers are
  not graph-wide evidence admission.
- [K101](patterns.md#k101-route-an-unresolved-premise-to-a-bounded-next-step) — **partial**:
  Evaluate decisions/latency and consider bounded ask/tool/wait choices beyond recall;
  empty/disabled subsets stay closed.
- [K104](patterns.md#k104-cause-specific-feelings-and-mixed-emotions) — **partial**: Semantic cause
  matching, generalized correction/resolution and paired conversational evaluation remain; unrelated
  positive turns are not resolution evidence.
- [K105](patterns.md#k105-expression-follows-communicative-intent) — **partial**: Run
  semantic-selection/listening and interruption/timing comparisons including neutral controls;
  general intent-to-expression selection and new laughter synthesis are not implemented.
- [K106](patterns.md#k106-shared-shorthand-with-negotiated-meaning) — **open**: Implement explicitly
  negotiated term/referent meaning with correction/scope/expiry/deletion checks; a proposed name is
  not user adoption.
- [K107](patterns.md#k107-provider-outcomes-are-not-character-evidence) — **partial**: Establish
  origin for ordinary text-only refusal, audit downstream promotion and evaluate route swaps/partial
  audio/cancellation/tool effects; streamed audio cannot be retracted and tool replay is not
  authorized.

### Performance and observability

- [P7](perf.md#p7-typed-mode-prefetch-parity-with-voice) — **open**: Add bounded cancellable
  typed-draft prewarm with explicit privacy/opt-out behavior. Composing status alone is not draft
  prefetch.
- [P11](perf.md#p11-measure-worker-reasoning-token-overhead-on-the-actual-route) — **partial**:
  Measure reasoning-token leakage on the actual worker model/provider and compare native controls
  before prompt suffixes or model changes; the historic qwen route is not assumed current.
- [P16](perf.md#p16-post-turn-inner-life-blocks-the-brain-loop) — **partial**: Collect current
  cost/ordering evidence, then split only safe eventual work with stale-turn guards; next-turn
  repair/answer state must remain timely.
- [P24](perf.md#p24-remaining-speech-latency-contracts-and-benchmarks) — **partial**: Complete the
  benchmark and targeted contracts below: useful-audio onset, safe endpointing,
  generation/audio-coverage identity, bounded cancellable finalization, first-chunk release,
  incremental synthesis, buffering and interruption recovery. No live mic timing was measured here.
- [P26](perf.md#p26-lip-sync-rides-the-server-clock-not-the-playback-clock) — **partial**: Extend
  the same path to desktop/Tauri audio owners, keep WS metering for non-owners and retune smoothing
  with measured lag.
- [P30](perf.md#p30-memory-mirror-reload-and-growth-costs) — **partial**: Remove decay's full mirror
  reload if measured worthwhile; stream reload instead of fetchall and evaluate mirror memory/LRU
  architecture. SQLite query speed was not the original bottleneck.
- [P31](perf.md#p31-audit--trim-the-baseline-system-prompt-25-30k-resting-floor) — **partial**:
  Remeasure the actual route/cache, then trim duplicate grammar, gate empty blocks and audit tier
  hygiene where evidence warrants. P43/P52 own volatile-tail/context work.
- [P32](perf.md#p32-concept-maintenance-and-history-growth) — **partial**: Measure actual scheduler
  contention and graph growth; define snapshot/event thinning and any remaining dirty-work
  refinements without discarding durable history. P34/P49 own retention decisions.
- [P33](perf.md#p33-inner-life-providers-walk-the-whole-memory-mirror-every-turn) — **partial**:
  Index genuinely unbounded iter_by_kind walks and replace whole-knowledge concatenation with
  bounded retrieval; keep add/update/delete/reload coherence.
- [P34](perf.md#p34-unbounded-tables-messages-lance-mirror-concept_events) — **open**: Decide
  retention separately for authoritative messages, the Lance mirror and concept-event noise. Add
  reconciled index filtering/thinning only with explicit preservation/deletion semantics.
- [P39](perf.md#p39-concept-snapshot--quality-report-n1-the-evidence-edges) — **open**: Add batched
  evidence lookup and measure the quality report's pairwise embedding cost before optimizing it.
- [P43](perf.md#p43-105-blocks-no-arbitration----replace-the-aggressive-denylist) — **partial**:
  Cover eager providers, protect owed/authoritative blocks, allocate complete evidence/handling
  bundles and record actual final inclusion/omissions before learning weights. RS1/RS4/RS5 and G6
  refine the remaining scope.
- [P46](perf.md#p46-parallel-compute-lane-drain) — **deferred**: Make shared
  WorldStore/ConceptStore/cache/SQLite state safe before parallel compute drain; do not add
  concurrency solely to reduce queue counters.
- [P49](perf.md#p49-the-telemetry-ledgers-are-the-fastest-growing-tables) — **open**: Schedule
  appropriate ledger retention and fix remaining non-selective aggregates; decide
  transcript/concept-history preservation separately. Bound disk without confusing diagnostics with
  authoritative history.
- [P50](perf.md#p50-finish-joint-handling-and-content-admission) — **partial**: Complete
  semantics-aware content/handling admission for other providers; decide frequent pooled-note
  placement and avoid admitting unexplained cues. The cap itself is already implemented (RS4).
- [P52](perf.md#p52-concept-volume-and-topic-aware-continuity) — **partial**: Evaluate smaller
  concept caps and topic-aware carry-over with quality/token/cache controls. Historical
  29.6-line/8.5k-char figures are snapshots, not current measurements; RS3/RS7 own follow-up
  experiments.
- [P24a](perf.md#p24a-repair-the-stt-partial-read-and-recorder-lifetime-contracts) — **partial**:
  Add phrase/generation/revision/audio-coverage identity, bound/cancel finalization and exercise a
  real recorder on isolated synthetic audio plus microphone checks.
- [P24b](perf.md#p24b-measure-end-of-user-speech-to-useful-audio-not-just-ttft) — **open**: Join
  input end, endpoint, final transcript, brain/tool/prompt, first speakable chunk, synthesis/PCM and
  scheduled onset by IDs. Respect clock mapping, count failures/underruns and compare p50/p95
  useful-answer onset rather than filler.
- [P24c](perf.md#p24c-make-endpointing-responsive-without-cutting-off-thinking-pauses) —
  **partial**: Separate voiced evidence from elapsed capture time, derive thresholds from frame
  duration and replay short answers/negations/hesitations/noise against premature cuts, not just
  faster endpoint time.
- [P24d](perf.md#p24d-finalize-the-already-fed-audio-once-keep-speculation-reversible) —
  **partial**: Validate real-recorder two-phrase/PTT/concurrent-interim cancellation and cache
  freshness. In-memory finalization and speculative drafting remain experiments; provisional audio
  must not execute tools, consume cues, write memory or speak.
- [P24e](perf.md#p24e-release-a-useful-first-speech-chunk-before-the-whole-answer) — **partial**:
  The normal streaming branch still requires a parsed mood; add safe provisional readiness and a
  bounded complete-first-chunk policy without leaking tags or truncating numbers/filenames/negation.
  Preserve shared offsets and cancellation.
- [P24f](perf.md#p24f-stream-synthesis-not-merely-a-completed-waveform) — **open**: Implement
  incremental synthesis with first-PCM/error/cancel semantics while preserving cache/gate,
  gain/brightness/tempo and pacing parity; emitted chunks of a finished waveform are not incremental
  generation.
- [P24g](perf.md#p24g-tune-client-buffering-only-with-underrun-evidence) — **validation**: Measure
  onset/underruns/device jitter before reducing or adapting buffering; test subsequent chained clips
  separately from the idle first clip.
- [P24h](perf.md#p24h-reduce-recovery-time-after-interruption-not-just-reply-onset) — **partial**:
  Measure cancellation through mic/STT/model/TTS/sidecar/client and new-turn recovery; exercise
  false interrupts and overlap. Full duplex/AEC remains immersion H7c.
- [P24i](perf.md#p24i-benchmark-modelprompt-cost-after-exposing-the-actual-waits) — **validation**:
  Benchmark warm/cold, tool/no-tool, short/long reply and cache conditions after stage tracing. Keep
  answer quality and interruption outcomes alongside latency; no automatic faster-model claim.

### Health checks of shipped behavior

- [H4](health.md#h4-the-cue-shelf-produces-well-and-spends-badly) — **partial**: Remeasure
  producer/renderer usefulness and echo-floor calibration with correct eligible/shown/used
  denominators. Do not lower gates merely to turn genuine misses into hits.
- [H7](health.md#h7-validate-the-grounded-hypothesis-answer-loop) — **partial**: Sample verified ask
  -> actual answer -> resolver reason -> applied update on a consistent deployment, including
  fictional/quoted/unclear negatives. An unscored ask does not prove an answer was missed.
- [H11](health.md#h11-the-prompt-tells-her-what-she-wont-do-45-more-than-how-she-feels) —
  **partial**: Compare instruction register/ratio and total volume on matching contexts; retain real
  boundaries. Do not pin unrelated affect merely to change the mix.
- [H14](health.md#h14-verify-negative-affect-concepts-reach-matching-context) — **validation**:
  Verify a genuinely negative user affective is created and reaches T3 on a matching-topic turn. Do
  not mint tender/tired context as dislike or lower global floors to manufacture a success.
- [H17](health.md#h17-calibrate-the-remaining-short-reply-gate) — **partial**: Decide and measure
  absolute-versus-relative short-reply calibration; no demonstrated shared-predicate defect remains
  beyond that follow-up.
- [H34](health.md#h34-echo-is-a-reliable-measure-and-it-is-not-a-measure-of-him) — **open**:
  Establish an item-level user outcome/reaction signal with message-length/confound controls;
  coordinate F12/RS9. Reweighting two unsuitable signals does not solve attribution.
- [H35](health.md#h35-a-third-of-the-surfacing-ledger-cannot-be-scored-by-either-signal) —
  **partial**: Join cue settlement with ledger views and define cluster echo or explicitly label
  affinity as weaker evidence. Correct judged/surfaced/settled denominators must survive any change.
- [H44](health.md#h44-validate-graduation-evidence-after-the-loop-repairs) — **validation**: Verify
  source-linked independent confirmations and a valid graduated/merged result. A recorded exit alone
  cannot close this: an earlier audit found unrelated confirmations. Keep H7 evidence review and
  post-guard model revalidation; do not lower support bars.
- [H54](health.md#h54-topic_miss-at-95-of-eligible-declines-is-not-by-itself-a-starvation) —
  **validation**: Hand-check genuine same-subject near-misses with per-turn shelf/eligibility
  context. Separate never-shown, shown/ignored and true semantic misses; a dominant topic_miss
  percentage is not starvation.
- [H22-followup](health.md#h22-followup-distinguish-a-shared-ritual-from-a-timing-habit) —
  **deferred**: Decide and evaluate evidence that makes a repeated calendar slot a shared ritual; do
  not lower thresholds to manufacture names. Historical counts are not current.
- [H21-followup](health.md#h21-followup-decide-the-question-style-axis) — **deferred**: Measure
  whether the question axis carries useful variation before retaining, retiring or recalibrating it;
  do not label a current stream dead from the old snapshot.
- [H20-followup](health.md#h20-followup-fact-check-claim-selection-coverage) — **deferred**: Measure
  source-grounded sentence coverage and false-positive search cost, then decide whether to widen
  named-entity/measurement/date heuristics.

### Tools and developer diagnostics

- [D8](tools.md#d8-conversation-archive-recall-with-daily-overviews) — **partial**: Generated
  topic/event summaries with citation/coverage controls, real-model overview-to-source scenarios and
  lookup/token benchmarks remain. Reuse the implemented dedicated tool; the
  extend-recall-versus-new-tool decision is resolved.
- [D-approval](tools.md#d-approval-spoken--aiko-voiced-task-approvals) — **open**: Add opt-in
  conversational approval wording and unambiguous routing back to the pending task, preserving one
  decision across voice/UI. Do not resurrect timed escalation without a separate explicit policy.
- [DT2](tools.md#dt2-relationship-state-inspector--one-shot-consolidated-snapshot) — **open**:
  Implement one bounded consolidated snapshot and decide a read-only UI surface; do not duplicate
  store ownership.
- [DT3](tools.md#dt3-feature-flag-catalog--minimal-mode-preset) — **open**: Add a catalog with
  current/default/purpose and a reversible minimal-mode preset with explicit restore semantics.
- [DT4](tools.md#dt4-scenario--conversation-replay-harness) — **open**: Implement shared live-app
  replay with isolated state, seeded scenarios and block assertions; exact/model replay is not
  supplied by the short Live action tail.
- [DT5](tools.md#dt5-get_surfacing_outcomes--did-what-she-brought-up-land) — **partial**: Expose a
  consolidated per-turn trace, explicit whole-store coverage and wider inner-life read surface with
  denominators/shrunk estimates. Existing bounded receipts are a partial trace, not full coverage.
- [D7](tools.md#d7-anticipatory-routine-assistance--act-on-what-shes-learned) — **open**: Join
  high-confidence learned slots to one bounded routine-specific offer/preparation, with
  ignore/dismiss behavior and shared C6 intake; destructive work still needs normal approval.
- [D1-R](tools.md#d1-r-reminder-recurrence-and-os-notification-delivery) — **open**: Add explicit
  recurring rules and OS delivery/cancellation/dedupe. Keep recurrence/timezone policy and
  notification permissions separate from the one-shot tool.

### Testing and evaluation

- [T1](testing.md#t1-shared-fakechatclient--behaviorharness) — **open**: Build a reusable
  ChatClient-protocol fake and minimal real-collaborator BehaviorHarness. Do not assume no shared
  fixtures or no integration tests exist.
- [T2](testing.md#t2-end-to-end-behavioural-chain-tests) — **partial**: Add reusable worker -> cue
  -> final prompt -> parsed reply -> post-turn state chains through T1; existing local tests do not
  cover every behavioral chain.
- [T3](testing.md#t3-worker-registry-conformance--smoke--fault-injection) — **partial**: Add
  registry-wide uniqueness/readiness purity, seeded/empty-store repeat runs and LLM fault injection
  without booting live install state.
- [T4](testing.md#t4-prompt-build--tag-parse-contract-golden-tests) — **partial**: Build a
  deliberate compact cross-feature golden contract layer for both assembled prompt and parsed
  output; avoid duplicating implementation-only unit tests.
- [T5](testing.md#t5-llm-behavioural-eval-suite-separate-track-not-a-ci-gate) — **partial**:
  Complete repeated held-out/blinded multi-turn and listening comparisons listed below, with equal
  budgets, source correctness, silence/decline/failure controls and no live relationship mutation.
- [T6](testing.md#t6-determinism-seams-for-flake-free-chain-tests) — **partial**: Standardize seeded
  RNG injection across remaining workers and advance-clock/scheduler chain tests; keep narrative
  clock separate from real runtime timing.

### Immersion and shared activities

- [H2](immersion.md#h2-calendar--time-context-block) — **partial**: Add holiday proximity and
  explicit user-birthday anticipation through existing owners; do not build a second calendar or
  circadian clock.
- [H7](immersion.md#h7-listen-while-speaking--soften-the-half-duplex-turn-lock) — **partial**: H7c
  full duplex/software AEC and actual microphone/playback validation remain. Coordinate P24; the
  overlap ring is not full duplex.
- [H12](immersion.md#h12-aiko-initiated-intentional-gifts--she-leaves-you-something) — **open**:
  Implement intentional Aiko-to-user gifts with a real saved output/source, permission and rare
  grounded delivery; a narrated gift is not an artifact.
- [H23](immersion.md#h23-avatar-shared-moment-snapshot--she-sends-you-a-selfie) — **open**:
  Implement deliberate source-linked avatar snapshots, file lifecycle and user-visible sending; do
  not claim unseen or generated real-world photos.
- [H24](immersion.md#h24-occasion---season-aware-outfits) — **open**: Add explicit occasion/season
  mapping with user override, ownership and no repeated changes; do not confuse existing
  sleep/weather changes with occasion-aware selection.
- [H27](immersion.md#h27-co-presence-mode--in-the-room-not-in-conversation) — **partial**: Evaluate
  coherent quiet co-presence and add explicit joined-activity state via IM1; optional ambience is
  IM8. Live availability/delivery gaps stay L20/L24, not a second controller.
- [IM1](immersion.md#im1-shared-quiet-sessions) — **open**: Implement explicit
  start/pause/resume/leave/checkpoint state with zero inference during silence, restart-as-paused
  and no invented work or attendance reward.
- [IM2](immersion.md#im2-point-at-the-same-thing) — **open**: Attach one explicit text selection
  with source/revision/range, removable preview, bounded context and server-side
  scope/deletion/revision validation.
- [IM3](immersion.md#im3-a-shared-reading-place) — **open**: Build a shared-reader UI and
  permitted-prefix retrieval with IM2 selection, rewind invalidation and actual-route spoiler
  contrasts. Grounded idle reading alone is not shared reading.
- [IM4](immersion.md#im4-aiko-can-show-what-she-made) — **partial**: Add a stable versioned creation
  with draft/ready/failed state, exact referenced-revision opening, cancellation preservation and
  cross-session comparison. Research notes alone are not the full creation contract.
- [IM5](immersion.md#im5-make-something-together) — **open**: Implement a jointly edited bounded
  document with proposed versus accepted revisions, authorship, undo and stale-write protection;
  discussion does not authorize edits.
- [IM6](immersion.md#im6-keepsakes-with-a-real-origin) — **open**: Add explicit pin/open/delete with
  exact origins, scoped excerpts and source-deletion invalidation. Optional revisit needs
  consent/K12; export stays J2.
- [IM7](immersion.md#im7-a-small-game-at-the-same-table) — **open**: Implement one deterministic
  legal-move game with pause/resign/reconnect/reset and no-model play; commentary is optional and
  cannot invent engine reasoning.
- [IM8](immersion.md#im8-a-quiet-acoustic-setting) — **open**: Add an explicitly selected
  local/licensed loop with independent gain/mute/fades/ducking, single-owner playback and STT echo
  checks. No inference is needed.

### Live mode: remaining runtime work

- [L4](live-mode.md#l4-authored-ambient-motion-presets) — **deferred**: Wait for a real authored
  idle-life motion group before a preset menu. This is a rig capability limit, not policy prompt
  work.
- [L5](live-mode.md#l5-bounded-micro-and-earcon-delivery) — **open**: Add validated
  delivery_style/backchannel_delivery enums mapped through shared TTS/earcon controls, rejecting
  invalid text before synthesis.
- [L9](live-mode.md#l9-floor-manners-while-the-user-owns-the-floor) — **open**: Add listen_posture
  as a bounded mapping to existing intents; typing cannot speak and sleep/DND/floor ownership still
  win.
- [L11](live-mode.md#l11-circadian-quieting-from-a-period-token) — **open**: Add deterministic
  period-token policy quieting with timephrase; night cannot increase intensity or speech authority.
- [L13](live-mode.md#l13-glance-targets-from-an-allowlisted-present-set) — **open**:
  Construct/validate an allowlisted menu from present frame entities; absent/unknown targets drop
  and private titles never enter policy context.
- [L14](live-mode.md#l14-return-and-reconnect-companion-beat) — **open**: Connect one bounded return
  beat with source/generation/dedupe checks and existing speech acts; no automatic greeting or
  second speech loop.
- [L15](live-mode.md#l15-compact-policy-prompt-v2-keep-the-4b-reliable) — **partial**: Measure a
  compact actual-route prompt at equal decision quality, preserving schema/rails/menu. Temporal
  context is implemented; smaller sufficient input is not validated.

### Live initiative: remaining lifecycle work

- [L19](live-initiative.md#l19-explain-silence-across-the-entire-funnel) — **partial**: Add
  persistent/reportable pre-policy omissions, complete build/route identity and
  candidate-to-delivery joins with unknown states.
- [L20](live-initiative.md#l20-availability-is-not-window-focus) — **partial**: Add explicit
  away/DND, per-device capability/evidence/expiry and source-aware interruptibility; connection is
  not physical presence.
- [L21](live-initiative.md#l21-durable-intentions-short-lived-action-attempts) — **partial**:
  Persist intention/attempt/outcome ownership across reset, paginate fairly beyond bounded intake
  and model quiet/withdrawn/consumed states without renewing sources.
- [L22](live-initiative.md#l22-event-and-deadline-scheduling-with-a-liveness-guarantee) —
  **partial**: Provide restart-safe event/deadline scheduling with a demonstrated liveness/fairness
  guarantee; no policy call on every heartbeat.
- [L23](live-initiative.md#l23-a-compact-4b-deliberator-with-useful-choices) — **partial**: Run
  paired actual-route choices, compact input and useful alternatives; prompt fixtures do not prove
  the model chooses appropriate speech.
- [L24](live-initiative.md#l24-transactional-brain-handoff-and-honest-completion) — **partial**:
  Settle reservation/claim/dispatch/presentation/playback transactionally, preserve outcomes across
  restart/cancellation and handle consumed-but-undelivered cues. Queue success is not delivery.
- [L25](live-initiative.md#l25-grounded-impulses-and-a-connected-curiosity-ledger) — **partial**:
  Connect fresh result/source renewal and answer/use outcomes with explicit purpose; reuse
  K97-K99/H7 rather than creating a second curiosity ledger.
- [L26](live-initiative.md#l26-background-work-during-real-co-presence) — **open**: Use actual
  capture/inference load and declared worker cost/interruptibility to schedule bounded preparation
  during co-presence; an open mic is not GPU ownership.
- [L27](live-initiative.md#l27-an-extension-contract-for-producers-and-behaviors) — **open**: Define
  a small producer/behavior contract with ownership/freshness/purpose/cost and two real adapters;
  plugin worker/context lifecycle remains platform work.
- [L28](live-initiative.md#l28-learn-timing-and-interests-without-rewarding-pestering) — **open**:
  Learn only from attributable more/less/not-now/dismissal and source-linked use; separate
  unavailable/undelivered/unknown from disinterest and never optimize chat duration.
- [L29](live-initiative.md#l29-desktop-and-mobile-delivery-are-different-capabilities) —
  **partial**: Add per-device delivery capabilities/leases and cross-device attempt/presentation
  dedupe; explicit deployment posture is required and reachability is not audibility.
- [L30](live-initiative.md#l30-replay-and-rollout-are-part-of-the-architecture) — **partial**: Build
  shared DT4/T5 replay, actual-route comparisons and verified whole-build/route rollout identity;
  rotating logs alone are not a reproducible evaluation corpus.
- [L31](live-initiative.md#l31-later-pilot-permissioned-email-assistance) — **deferred**: After
  platform/durable-job work, pilot permissioned read-only email with explicit account/data/operation
  scope; provider integration and sends are not implemented.

### Reasoning Surfacing

- [RS1](reasoning-surfacing.md#rs1-establish-an-end-to-end-reasoning-attention-and-cost-baseline) —
  **open**: Establish an end-to-end reasoning, attention and cost baseline
-
  [RS2](reasoning-surfacing.md#rs2-preserve-evidence-identity-through-concept-recall-and-compact-context)
  — **open**: Preserve evidence identity through concept recall and compact context
- [RS3](reasoning-surfacing.md#rs3-select-a-sufficient-working-set-for-the-current-question) —
  **open**: Select a sufficient working set for the current question
- [RS4](reasoning-surfacing.md#rs4-admit-optional-cues-jointly-with-their-handling) — **open**:
  Admit optional cues jointly with their handling
- [RS5](reasoning-surfacing.md#rs5-commit-t3-accounting-from-complete-final-evidence-bundles) —
  **open**: Commit T3 accounting from complete final evidence bundles
- [RS6](reasoning-surfacing.md#rs6-trial-a-bounded-automatic-reasoning-tool-before-speech) —
  **open**: Trial a bounded automatic reasoning tool before speech
- [RS7](reasoning-surfacing.md#rs7-reuse-stable-evidence-without-freezing-stale-conclusions) —
  **open**: Reuse stable evidence without freezing stale conclusions
- [RS8](reasoning-surfacing.md#rs8-compare-explanations-through-discriminating-evidence) — **open**:
  Compare explanations through discriminating evidence
-
  [RS9](reasoning-surfacing.md#rs9-learn-usefulness-without-rewarding-repetition-or-confidence-theatre)
  — **open**: Learn usefulness without rewarding repetition or confidence theatre

### Plugin System

- [P3](plugin-system.md#14-implementation-roadmap) — **open**: Versioned descriptors,
  disposal/ownership and scoped grants
- [P4](plugin-system.md#14-implementation-roadmap) — **open**: Bounded discovery, exact selected
  schemas and no full-menu fallback
- [P5](plugin-system.md#14-implementation-roadmap) — **open**: Durable steps/leases/waits/budgets,
  effect journal and result outbox
- [P6](plugin-system.md#14-implementation-roadmap) — **open**: Hosted worker/model/storage lifecycle
- [P7](plugin-system.md#14-implementation-roadmap) — **open**: Read-only source and mail capability pilots
- [P8](plugin-system.md#14-implementation-roadmap) — **open**: Supervised processes, packaging and
  approved external writes

## Keeping it current

Move completed narratives to shipped and leave only a short pointer in the domain file. A partial
item must name its implemented slice and exact remainder; put shared validation in T5/L55 with the
affected IDs. Retired proposals are not shipped features. Re-read dependencies at closure, preserve
stable links and run [check_backlog_links.py](../../scripts/check_backlog_links.py) over docs and
rules. Use focused/affected tests for code changes and run npm run lint; a full suite is not the
default completion gate.
