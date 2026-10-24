# Grounded reasoning with selective context (RS-series)

**24 Oct 2026. Analysis and backlog proposals only. All RS items are open.**
This review inspected checkout `b8a651e`, including retrieval, concept rendering,
prompt assembly, cue admission, tool dispatch, the situation working set and the
post-reply second thought. It did not inspect personal conversation content, run
inference, restart the app or measure the currently loaded model. Historical
measurements linked below retain their original dates; they are not a new baseline.

The objective is for Aiko to give grounded replies and explain their basis when
useful, while preserving conversational speed and prompt-cache reuse. More
retrieved material, more private thinking and more explicit reasoning prose are
candidate interventions, not success measures.

## Recommendation

First measure the complete source-to-answer path and preserve usable evidence
references. Then select a smaller, sufficient working set and remove competing
optional steers. Trial an automatic reasoning tool only on turns with a specific
material uncertainty. Keep changing evidence in existing dynamic context/tool
results and preserve the stable prompt contract.

This is a set of bounded follow-ups to existing owners, rather than a replacement
memory architecture. L49-L52, K92/K97/K100/K101, P43/P50/P52 and L55/T5 already own
much of the necessary machinery. Their implemented pilots should remain intact;
the RS items specify remaining integration and evaluation work.

## What the system currently does

1. SQLite holds messages and durable memory metadata; LanceDB supplies retrieval.
   [RagRetriever](../../app/core/rag/rag_retriever.py) gathers memories, messages
   and documents, adds recency/provenance/other ranking signals, and can expand
   through topic clusters. Dates and inferred/uncertain memory markers already
   exist. The missing feature is not simply another relevance search.
2. [ConceptView](../../app/core/concepts/concept_view.py) is the common concept
   read facade. Active concepts contribute a pinned core lane, topical flex lane
   and activated neighbours; provisional hypotheses have a separate register.
   L49 exposes bounded source-independence reports, L50 scopes a communication-
   style pilot, and L52 validates recorded structural support sets. Historical
   lineage and semantic sufficiency remain incomplete, explicitly documented in
   [the integration contract](../concept-integration.md).
3. [build_relevant_context](../../app/core/session/inner_life_part1.py) collects
   those candidates and passes them to the pure
   [ContextBudgetSelector](../../app/core/session/context_budget_selector.py).
   Selection admits pinned concepts, source floors, then a weighted greedy fill
   of memories, clusters, concepts and hypotheses. These share the T3 reservation.
4. [PromptAssembler](../../app/core/session/prompt_assembler.py) registers **127
   block names in this checkout**, including 82 T6 detectors. These are registered
   slots, not 127 nonempty blocks per turn. Providers build most of the system
   base first; conditional handling and stance run before T3 is selected and
   inserted into its earlier reading position. T3 is reserved before history,
   but after the rest of the system base has consumed capacity.
5. [Cue policies](../../app/core/proactive/cue_accounting.py),
   [pool claims](../../app/core/session/cue_pool_mixin.py) and
   [stance](../../app/core/conversation/stance.py) supply topical/freshness/cadence
   gates, staged pooled consumption and bounded optional-offer competition.
   `POOLED_CHOICE_BLOCKS` currently contains six blocks. Most other optional
   offers are still separate providers; a stance choice is not general semantic
   comparison or removal of every competing instruction.
6. The [situation working set](../../app/core/conversation/conversation_situation.py)
   and its [provider](../../app/core/session/conversation_situation_mixin.py) can
   retain a bounded goal, user-cited facts/constraints, tentative alternatives,
   unresolved premise and proposed check/result. K101 can open a strict recall-
   only tool pass for one fresh need. That is a useful existing entry point;
   it is not a general same-turn evidence or reasoning planner.
7. [TurnRunner](../../app/core/session/turn_runner.py) may perform a tool decision
   before streaming the substantive reply. Enabled schemas are narrowed by
   [tool_pass_gate.py](../../app/core/session/tool_pass_gate.py); the no-tool
   escape and provider-specific round limits remain significant. Speech is
   released in chunks during streaming, so a later check cannot retract words
   already heard. K96's
   [second thought](../../app/core/proactive/second_thought_worker.py) instead
   reviews a completed reply and publishes a future cue. It cannot ground the
   opening sentence of that completed reply.
8. The T0-T6 ladder, stable ordering/banding, explicit breakpoint construction,
   [prefix diagnostics](../../app/core/session/prompt_prefix_telemetry.py) and
   provider usage already address caching. K96 deliberately reuses the system
   bytes, breakpoint offsets and session affinity. Actual reuse still needs
   provider usage evidence; an identical prefix alone is not a measured cache hit.

See [prompt-surfacing.md](../prompt-surfacing.md) for the broader ownership map and
[context-budget.md](../context-budget.md) for T3 sizing. Source takes precedence
over older inventories or implementation-status paragraphs in those documents.

## The unresolved design problem

### Four different meanings of "why"

- **Why selected:** a concept won through similarity, confidence, habituation,
  importance or standing. L35/L41 expose/frame this. Selection is not evidence.
- **Why believed:** which observations support the claim, whether they are
  independent, and what revisions or counterevidence exist. L49/L52 address
  parts of this, but those records are not generally carried into the reply.
- **Why applicable now:** whose claim it is, its context, time and exceptions.
  L50 covers one pilot; an old preference or inferred situation is not current
  authority merely because it is semantically close.
- **Why the conclusion follows:** which mechanism or inference connects those
  premises, what alternative survives, and what observation would discriminate.
  L51 supplies working state; L53/L54 remain the main open experimental owners.

A more convincing lead-in cannot substitute for the other three kinds of why.
The desired output is a concise grounded conclusion or an honest uncertainty,
not a transcript of private chain-of-thought.

### Relevance is not the marginal value of another item

The selector orders individual candidates by `relevance * source_weight` after
pins and floors. It has no joint representation of which evidence is already
sufficient, which candidates repeat one root observation, which missing premise
could change the answer, or which two claims apply in different contexts.
Moreover, its inputs are not calibrated probabilities: memory scores include
priors/bonuses/penalties, concept scores blend other signals, and cluster scores
are centroid similarities. Comparable numeric ranges do not prove comparable
usefulness. Greedy selection can leave space unused; that is currently a
consequence of gates/caps/cost, not an explicit sufficiency decision.

This is a plausible source of unnecessary inference work, **not a measured
explanation of confusing replies**. A controlled context ablation must separate
missing evidence, distracting evidence and a model failing to use correct evidence.

### The graph contains more provenance than the hot prompt exposes

Concept rendering uses hedged labels and up to two shortened evidence labels;
the optional rationale clause applies to pinned concepts. That makes sense for
size, but offers limited support for a precise explanation. `recall_concept`
already expands support and related concepts, yet its returned concept object
omits the concept ID and its evidence snippets contain only truncated text.
They do not carry source IDs, dates, provenance or applicability. Related links
are not a substitute for evidence references on each supporting observation.

The first opportunity is a better read projection over existing stores, with
selective expansion. Dumping the whole graph into T3 would amplify both cost and
the interpretation burden.

### Capacity, attention, latency and cache cost are separate budgets

T3 can fit inside the window while still being distracting. A large stable
persona can be cheap to reuse while still occupying model attention. A smaller
dynamic packet can cost less overall despite a lower cache-hit percentage.
Extra reasoning consumes inference/output budget and wall time even when its
input is cached. Provider schemas, routing, eviction and request shape can also
affect reuse; local block hashes only describe part of that system.

P52 already fixed one source of excessive concept turnover. Its remaining size
and topic-aware continuity work should be evaluated, not reopened as if no
stabilization exists. P51 measured global summary/RAG deduplication and closed
that proposal; repeat it only with new evidence. Narrow same-source redundancy
within a selected evidence packet is a different question.

### Selection receipts can overstate what was actually available

The T3 builder estimates candidate costs, renders sections, then clips the
combined string. It subsequently marks the selected memory hits surfaced,
updates selected concept habituation and snapshots the original selection.
Those operations do not intersect selection with item boundaries in the clipped
text. Estimates also omit some headers/grounding; expanded digests can be longer
than the memory candidate estimate. This is a source-level accounting risk,
not a measured clipping rate. Pool staging protects its covered claims, but
does not make T3, previews and every provider side effect transactional.

If an evaluator treats selected, included, used and heard as synonyms, it can
misdiagnose the model or train a selector against the wrong outcome.

## Backlog proposals

### RS1. Establish an end-to-end reasoning, attention and cost baseline

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: first. Effort: medium. Owners: L55/T5, P43/G6 and P24.**

Extend the existing [reasoning evaluator](../../scripts/evaluate_concept_reasoning.py)
and [T5 track](testing.md#t5-llm-behavioural-eval-suite-separate-track-not-a-ci-gate)
with isolated source-to-reply episodes. The current bounded supplied-note and
accounting evaluations do not establish retrieval/extraction/reply quality.

Use synthetic contrasts: useful missing evidence; irrelevant true memories;
duplicate derivations of one episode; stale versus current claims; a scoped
preference; a retracted premise; a short follow-up with an ambiguous referent;
ordinary banter; a correction after a proposed explanation. Compare current
assembly, smaller selected context, provenance-preserving context and selective
reasoning, with ablations and held-out paraphrases. Hold routes, versions,
history and output budgets fixed; repeat trials and report warm/cold conditions.

Measure unsupported assertions, correct source attribution, correction retention,
appropriate abstention, decision-changing evidence use and unnecessary questions.
Also measure all calls, input/cached/uncached/output/reasoning tokens when known,
worker queue wait, end-of-speech-to-useful-audio p50/p95, interruptions and failed
or late computations. Unknown provider metrics stay unknown. Review naturalness
and spoken qualifications separately; an LLM judge is not sole ground truth.

**Acceptance:** reproduce a fixed baseline and distinguish retriever omission,
assembly omission and failure to use included evidence. Agree quality, cost and
voice/typed latency limits before tuning. Add no routine live model calls and
use isolated stores through the managed test runner for deterministic checks.
No personal transcripts or live relationship mutations belong in the fixture set.

### RS2. Preserve evidence identity through concept recall and compact context

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: first. Effort: medium. Owners: L24/L49/L50/L52 and existing recall.**

Introduce a bounded, read-only evidence projection, starting with
`RagRetriever.recall_concept`. Return stable claim/source IDs and revisions,
subject, source authority (testimony, observation or inference), observed/event
time versus recorded time, and a short support excerpt. Include applicability,
known supersession, support validity and independence/coverage summaries when
available; preserve `unknown` for legacy or traversal-limited lineage. Read
concepts through `ConceptView`; do not create another truth store.

Use two depths: a compact claim with one usable basis for ordinary context, and
on-demand expansion of one selected claim. Keep selection reason separate from
support and mechanism. The stored rationale is an interpretation, not an extra
observation. A source reference makes inspection possible; it does not prove
the excerpt entails the claim. Bound nodes, bytes and tokens, including the
existing `all_evidence` escape from the snippet-count cap. Follow narrative-time
formatting, without baking shifting relative ages into durable evidence records.

**Acceptance:** two paraphrases of one source retain one support group; a
corrected/deleted source cannot certify the old conclusion; equal wording about
different subjects stays distinguishable. Recall can explain the recorded basis
using actual retrievable IDs, and returns explicit insufficient/unknown support
without inventing it. Preserve existing tool parameters or version compatibly.
The default T3 token budget must not grow to accommodate the new representation.

### RS3. Select a sufficient working set for the current question

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: second. Effort: medium-large. Owners: L23/L51, K97/K101, P43/P52.**

Begin as an offline shadow selector over existing candidate pools. Use the latest
user request and a fresh compatible working-set goal to resolve short follow-ups.
Separate query projections for known subject/goal and a missing premise; never
use a tentative hypothesis as a confirmed retrieval fact. The latest correction
and authoritative state override stale observer notes. Reuse the existing turn
embedding/prefetch where valid; a second query must have its own explicit budget.

Select for answer needs: current observations/constraints, a directly relevant
claim, its necessary support and material counterevidence. Penalize redundant
roots or content already sufficiently present in the current working set, while
retaining genuinely independent observations. Preserve explicit boundaries and
critical constraints. Keep ordinary companion turns valid without a task frame.
Compare against the cheaper alternative of reducing existing caps.

Use ordinal marginal-utility rules initially, not a purported calibrated score.
Stop when further candidates add no needed basis; do not fill every available
slot for its own sake. Reserve candidate diversity only when it helps the live
question, and diagnose source-score calibration before retuning global weights.

**Acceptance:** irrelevant but similar memories stay out; a user correction and
a decisive opposing observation survive. A short "why that one?" can recover
its referent without reopening an old task. An unanswerable query stays uncertain.
Improve RS1 grounding at equal or lower total context, with no additional LLM
selector call. Existing P52 continuity remains the comparison baseline.

### RS4. Admit optional cues jointly with their handling

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: second. Effort: medium per family. Owners: K92/K93 and P43/P50.**

Extend the existing six-block optional-choice pilot one family at a time.
Collect non-mutating candidates carrying purpose, grounding, expiry, floor-taking
cost and essential handling before rendering/claiming them. Distinguish evidence
the reply needs from discretionary material Aiko might bring up. A cue being
relevant or high-substance does not make its suggested action mandatory.

Keep the user's question, owed repair, authoritative state, delivery constraints
and permission rules protected. Choose at most one discretionary conversational
move in the pilot; omit competing optional instructions from final assembly.
Select each retained cue and its essential handling as a bundle. Compute the
decision from a provisional evidence selection, then finalize within budget;
avoid a circular sequence where stance requires T3 but T3 depends on an already
committed stance. Keep resulting render order within declared lifetime tiers.

**Acceptance:** a correction plus a curiosity plus a callback produces the
needed repair without two competing topic changes. An overflow retry or preview
spends no omitted cue. Reducing optional tail content demonstrably releases
capacity for necessary evidence/history. A quiet turn can select no optional
move. Compare expression/naturalness, not a target increase in cue consumption.

### RS5. Commit T3 accounting from complete final evidence bundles

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: first foundation, before learned selection. Effort: medium. Owners:
P43/G6, L37 and K99.**

Render candidate bundles before final packing, with their real header,
qualification and essential-support cost. Fit complete bundles or use a defined
short form; avoid cutting a disclaimer or an evidence clause in the middle.
Stage T3 memory-use and concept-habituation effects until final assembly inclusion,
extending the covered pooled-claim pattern rather than introducing a second
transaction framework. Track attempted selection versus final inclusion explicitly.

Attach content-free item receipts to the existing turn/attempt lineage. Preserve
separate statuses for included, semantically used, generated, presented, audio
played and delivery unknown. Aborted/failed provider turns must not become
successful expression or new corroboration. Preview/retry state must not leak
into the next real turn. This extends current accounting; it does not require
rewriting historical ledgers or pretending old unknowns can be recovered.

**Acceptance:** deliberately under-estimate a long digest and force clipping:
every recorded included item is complete in the final prompt, omitted items do
not accrue habituation/use, and required uncertainty survives. Repeat with
aggressive reassembly, preview, provider failure and an interrupted utterance.
Existing cue and delivery receipts retain their distinct meanings.

### RS6. Trial a bounded automatic reasoning tool before speech

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: experiment after RS1/RS2. Effort: medium-large. Owners: K96/K101,
L51/L53 and the existing tool pass.**

Add one proposed read-only `reason_about` tool, with fixed schema text describing
what it does, when it helps and its synchronous budget. Register it per turn and
declare a separate known family in `_TOOL_FAMILY`. Do not widen K101's strict
premise-recall allowlist. Cheap gates may expose it for an explicit reasoning
request or a fresh, evidence-linked material conflict/unresolved inference.
Length, a question mark, a fired cue or the word "why" alone is insufficient.
No new model call is needed just to decide whether to expose the tool.

The model chooses it through the existing tool decision, retaining
`respond_directly`. The handler receives the current question/constraints and
the RS2/RS3 evidence packet, not the entire inner-life prompt. Its result is a
small typed decision aid: conclusion, cited basis, key uncertainty, at most two
alternatives and one discriminating next check. It may say insufficient evidence.
It returns conclusions and support, not private chain-of-thought, durable memory
or a prepared spoken message. The final chat pass owns Aiko's voice.

**Initial offline envelope, to tune with RS1:** one handler inference per turn,
at most 2,000 input tokens and 512 total generated tokens under the provider's
actual token semantics, with a two-second end-to-end deadline including queue
wait. These are experimental ceilings, not claimed sufficient production defaults.
Count the preceding tool-selection call and final reply as well. Return to the
substantive stream after this result, without another selection round just to
inspect it. Enforce the one-call limit across duplicate calls in a tool batch.
No recursion, nested tools, iterative self-critique or silent extra tool rounds.
Discard results after interruption, session/topic change or source revision. Late results cannot
edit already spoken text. Deadline failure returns an explicit no-result status
so the reply can acknowledge uncertainty or defer through existing tasks.

Compare three separately budgeted arms: (a) selected context alone, (b) selective
per-call reasoning effort on the existing reply, (c) the tool plus normal reply.
A small worker packet and a main-model call reusing the exact prefix are
different cost/quality hypotheses; cache reuse does not make extra generation
or queue contention free. Use existing provider clients/gates and routes.
Do not enable all mechanisms together or change K96's post-reply role.

**Acceptance:** triggered hard cases improve grounded decisions beyond the
cheaper arms; ordinary chat adds zero reasoning calls. Enforce total token and
wall budgets, cancellation, stale-source guards and no hidden-note speech leaks.
Demonstrate acceptable useful-audio p95 in voice and typed mode before active
automatic use. Start disabled/shadow; heavier investigations use the existing
task lane and approval rules rather than blocking speech indefinitely.

### RS7. Reuse stable evidence without freezing stale conclusions

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: measure alongside RS1; rollout after RS3. Effort: medium. Owners:
P44/P52 and prompt-cache infrastructure.**

Give unchanged evidence deterministic serialization: stable IDs/order, bounded
qualifications, semantic revisions and existing banded strengths. Reuse a small
topic working set while its question and source revisions remain compatible;
rotate on a meaningful subject change rather than incidental score jitter.
Do not rewrite its text every turn to restate ages, retrieval scores or selection
explanations. Preserve narrative-time helpers and refresh time-sensitive claims
when their validity changes. Reuse must not suppress new decisive evidence.

Invalidate on correction/deletion/supersession, dependency failure, applicability
or world change, model/embedding identity where relevant, user/session switch
and changed retrieval intent. Include tool/document revisions and bounded TTLs
where no event hook exists. The model's own confidence/utility must not certify
that a cached packet remains true.

Keep stable tool descriptions and epistemic rules in their existing contracts;
variable evidence/reasoning results use T3, the existing T6 situation block or
tool results according to lifetime. Do not move all retrieval into T0 or append
a second copy of it. A new block still needs tier registration and, if it steers,
an explicit stance decision. Stable late text cannot recover a prefix already
broken earlier; test complete requests and provider breakpoints, not just blocks.

**Acceptance:** same-topic fixtures reuse unchanged bytes; an immediately
corrected premise is absent on the next assembly, including warm caches. Report
absolute uncached tokens, total spend/inference and useful-audio latency alongside
cache percentage. Include cold start, changed schema subsets and provider cache
misses. Do not claim savings from old P44/P52 cohorts or local hashes alone.

### RS8. Compare explanations through discriminating evidence

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: later vertical slice. Effort: medium-large. Owners: L53/L54, L51/L52
and K101.**

Pilot one low-risk troubleshooting workflow using the existing working model.
For a selected explanation retain its necessary premises, a plausible alternative,
different predicted observations and the smallest safe check that separates them.
Use RS2 support sets as provenance; they do not themselves prove the proposed
causal mechanism. A derived concept, its cluster digest and its originating
memory are not three independent confirmations.

Choose among answering from existing evidence, bounded recall, an authorized
deterministic tool, one necessary clarification or leaving uncertainty unresolved.
Prefer a check that changes a decision over another similar memory. Keep
predictions made before a result distinct from explanations generated afterwards.
Hypotheticals and reasoning-tool results stay local until ordinary, independent
memory admission; tools cannot authorize their own suggested external actions.

**Acceptance:** synthetic slow-render scenarios with the same old "network is
slow" memory but different offline/online observations yield different next
checks. Evidence that falsifies the network explanation changes the conclusion;
it does not trigger a longer defense of the same claim. When no available check
would change the answer, Aiko can state the limit without manufacturing a task
or a new cue. Evaluate via RS1 before expanding domains.

### RS9. Learn usefulness without rewarding repetition or confidence theatre

**24 Oct source review: open proposal.** Existing implementations are foundations, not closure of this experiment. The source-linked first slice and acceptance checks below remain the missing work; no new implementation or model measurement was found for this proposal in the reviewed checkout.

**Priority: after trustworthy inclusion/answer receipts. Effort: medium. Owners:
L37/L38/L44, F12/F17 and G5.**

Use the existing outcome spine and consumers, with source coverage explicit.
Separate whether evidence helped answer the current question from whether an
optional cue was welcomed, and both from whether a factual claim is supported.
An echoed sentence, a long next user message or fluent explanation is not an
independent truth label. Unspoken/delivery-unknown items cannot receive the same
negative credit as clearly presented but unhelpful material.

Start offline with item ablations, explicit correction retention and source-use
checks. Compare shared-root bundles as groups; a copied next-turn engagement
label cannot causally credit every surfaced item. Keep sparse and conflicting
feedback uncertain, preserve rare critical constraints, and bound any learned
ranking adjustment. Do not update durable concept confidence outside the L3
owner or convert successful reasoning-tool output into self-corroborating memory.

**Acceptance:** verbosity and repeated wording earn no extra usefulness; useful
unquoted evidence can receive credit; a social goodbye is not a failed recall.
A correction overrides a historically popular but unsupported explanation.
Report held-out quality and uncertainty before activating learned weights.

## Pickup order and decision gates

1. **RS1 + RS5**, with a narrow **RS2 recall** improvement: establish trustworthy
   denominators and preserve evidence before optimizing the answer policy.
2. **RS3 and RS4**, separately: compare fewer relevant premises and fewer optional
   instructions at existing inference budgets. Measure cache effects with RS7.
3. **RS6**, only if remaining failures require inference rather than missing or
   poorly presented evidence. A negative result can justify using selected context
   or adaptive effort alone. RS8 starts as one L53/L54 experiment, not a dependency
   requiring completion of all reasoning research before any useful trial.
4. **RS9**, after reliable receipts and enough adjudicated examples. More surface
   frequency, hidden tokens or engagement cannot substitute for grounded replies.

For implementation, run focused regressions and the affected-test selector under
the repository's managed runner, plus required lint for code changes. Reuse
existing context-budget, recall, prompt stability, tool-pass, situation, provenance
and delivery tests; add behavioral model evaluations separately. Do not run the
whole suite for this documentation pass. Each slice needs an independent disable
path, measured comparison and an explicit decision to expand, revise or stop.

## Existing proposal owners

- [Concept reasoning roadmap L49-L55](concepts.md#reasoning-follow-up-from-associations-to-revisable-explanations)
  and [integration contracts](../concept-integration.md).
- [K92 stance](patterns.md#k92-conversational-stance--one-decision-per-turn-not-ten-permission-slips),
  [K96 second thought](patterns.md#k96-one-shot-per-thought--in-turn-deliberation),
  [K97 working set](shipped/audit-2026-10-24.md#patterns-k97),
  [K100 evidence admission](patterns.md#k100-evidence-bound-memory-admission) and
  [K101 premise routing](patterns.md#k101-route-an-unresolved-premise-to-a-bounded-next-step).
- [P43 arbitration](perf.md#p43-105-blocks-no-arbitration----replace-the-aggressive-denylist),
  [P50 handling](perf.md#p50-the-persona-hoist-has-no-cap-and-it-is-the-second-largest-block),
  [P52 concept volume/continuity](perf.md#p52-t3-spends-85k-charsturn-on-30-concept-lines-it-never-repeats)
  and [G6 attribution](workers.md#g6-per-provider-decline-attribution).
- [T5 evaluation](testing.md#t5-llm-behavioural-eval-suite-separate-track-not-a-ci-gate)
  and [P24 useful-audio latency](perf.md#p24b-measure-end-of-user-speech-to-useful-audio-not-just-ttft).
