# Higher-order concepts and reasoning

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

Canonical contracts: [integration](../../docs/concept-integration.md), [lifecycle](../../docs/concept-lifecycle.md),
[hypotheses](../../docs/hypotheses.md), [selective-context proposals RS1-RS9](reasoning-surfacing.md). One
kind registry, one lifecycle writer and ConceptView remain the owners; a bounded pilot is not
complete reasoning coverage.

## L4. Cluster co-activation signal

**Status: Partial.**

**Implemented / available:** Session/day/circadian/weekday axes are implemented; synthesis/T3 use
temporal hints and stable T1 stays session-only.

**Remaining:** Optional window/gap-session axes and evidence-backed mood/arc/world joins remain. A
sticky T1 day clause needs measured prefix stability.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l4).

**Source check:** [test_topic_graph_coactivation.py](../../tests/test_topic_graph_coactivation.py),
[topic_graph.py](../../app/core/conversation/topic_graph.py).

## L22. Concept-quality evaluation + observability

**Status: Partial.**

**Implemented / available:** Quality reports, intake/decay tuning, bounded existing-concept context
and the historical unreinforced sweep exist. L55 supplies synthetic
evidence/applicability/working-model tests.

**Remaining:** Build held-out concept-quality cases and a repeatable
precision/duplicate/register/promotion scoreboard; remeasure post-fix cohorts. Accounting fixtures
do not establish concept extraction quality.

4 Nov follow-up: a [read-only cohort reviewer](../../scripts/review_concept_cohort.py)
now samples fresh subjects/kinds/statuses with source-linked evidence and explicit
annotation denominators. A first review found unsupported developmental and conflict
interpretations; held-out proposer comparisons and broad quality validation remain.

5 Nov follow-up: [source-first proposer grounding and a bounded comparison](../reviews/concept-grounding-evaluation.md)
now distinguish repeated observations, plans and completed progress, and require
specific shared-context conflict. The final synthetic run retained all positive
controls but still admitted three false tensions in 36 trials. L22 remains partial;
fresh ordinary-use cohorts and independent paraphrases are the next quality gate.

The subsequent [memory-producer audit](../reviews/memory-producer-audit-2026-11-05.md)
covers 32 direct write sites, indirect producers and rewrite paths. It reproduces
drafts, Aiko goal reflections and public facts becoming user-event concepts.
Preserving source subject/authority/purpose through memory-to-concept admission
is the next foundation; evidence-count or lifecycle tuning cannot establish it.

### L22 follow-up: post-restart memory quality cohort (6 Nov 2026)

**Status: Fresh cohorts reviewed through 15 Nov; semantic follow-ups remain.**
The [15 November worker review summary](workers.md#memorytime-quality-ordinary-use-follow-up) records the closure of an
uncited-admission bypass and adds bounded meaning checks. Its 18-concept sample
still exposes unsupported relations; missing history and unjudged axes remain
explicit. No promotion-policy or historical-data changes were made.
The [ordinary-use review](../reviews/ordinary-use-review-2026-11-12.md) records fresh
source/actor/modality/time failures and bounded corrections. Correct citations
do not establish justified interpretations. Review a separate post-fix cohort;
do not mark all checks below complete from structural regressions.

The backend was restarted after
`0cdc8ab7` (five memory/time-quality commits). The initial process snapshot had
the worker scheduler enabled and no reported worker errors; relevant workers
had not run yet. Startup health is not evidence of generation quality.

Review a bounded fresh cohort after a few days of normal conversation, keeping
the activation boundary separate from older records. Do not force workers to
fill sparse cohorts. Pair positive cases with abstention/error cases and inspect
the actual source messages, not only metadata coverage or counts.

- [ ] Expired plans keep intention/unknown outcome; elapsed time never supplies
  completion. Distinguish recording time, stated event date, clock precision and
  unknown occurrence time. Check current-state expiry and rescheduled plans.
- [ ] Promises retain meaningful commitments without repeated extraction from
  one source. Every deadline has literal supporting timing; casual immediate
  actions and invented clocks do not become follow-up obligations.
- [ ] Extracted memories preserve actor, conditions, exceptions, negation and
  supported frequency. Plans plus return reports do not certify all planned
  actions. Tentative inferences remain recognizably tentative.
- [ ] Topic-summary clauses cite current eligible sources, distinguish history,
  current states, intentions and reported patterns, and respect later resolution
  reports and current-state validity windows.
- [ ] Review optional situation/outcome receipts, follow-through links and
  contextual relations when actual conversations supply them. A sparse or empty
  cohort is not automatically a failure or a reason to manufacture data.
- [ ] Check missed important information as well as false admissions; separate
  valid abstention, malformed output, budget omission and missing source support.
- [x] Trace a small resulting concept cohort back to its memories. The
  [12 November sample](../reviews/ordinary-use-review-2026-11-12.md#cohort-and-activation-boundary)
  covered 18 of 131 eligible concepts. Three support sets justified their labels,
  11 fell short and four were unjudged. The [17 November review](../reviews/companion-continuity-review-2026-11-17.md#1-source-to-memory-to-later-understanding)
  traced both newly active concepts in its window to the same unsupported diary
  claim. This completes the initial tracing task. The separate
  [surfaced-concept review](#l22-follow-up-review-what-surfaced-concepts-actually-add)
  owns the exposure-weighted sample and post-repair checks.
- [ ] After the additional [reflective-producer restart](../../docs/reflective-producer-quality.md),
  check goal-note novelty and genuine reported outcomes, narrow shared-moment
  citations/event dates, and dream input qualifications/derivations. Record this
  activation boundary separately from the earlier time-quality restart.

References: [time/data-quality implementation](../../docs/memory-time-quality.md),
[longitudinal evidence](../../docs/longitudinal-memory.md),
[concept cohort reviewer](../../scripts/review_concept_cohort.py),
[read-only historical memory reviewer](../../scripts/review_memory_quality.py).

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l22).

**Source check:** [concept_quality.py](../../app/core/concepts/concept_quality.py),
[concept_snapshot.py](../../app/core/concepts/concept_snapshot.py),
[identity_user.py](../../app/core/concepts/proposers/identity_user.py),
[concept_lifecycle.py](../../app/core/concepts/concept_lifecycle.py),
[concept_sweep_unreinforced.py](../../scripts/concept_sweep_unreinforced.py),
[concept_intake_report.py](../../scripts/concept_intake_report.py).

### L22 follow-up. Review what surfaced concepts actually add

**Status: Open.** The
[18 November review](../reviews/grounded-cognition-review-2026-11-18.md#3-check-what-the-concept-actually-adds)
found active concepts with viable source IDs whose underlying text still added
motives, agreement or reliability. It also found several concept kinds built
around one partial communication note.

- Start an exposure-weighted sample with a few high-use identity, style, value
  and narrative claims. For each, compare the actual viable premise to its
  original dialogue, then inspect one recorded use in a reply. Include dormant
  and newly admitted contrasts and keep pre-fix rows separate from fresh ones.
- Mark the claim, original observation groups, missing evidence and whether the
  claim adds a useful, scoped abstraction. Distinguish parallel concepts with
  different roles from restatements that add little. Compare a potential
  through-line with its concrete premises at the same prompt budget.
- Put reviewable legacy claims into a bounded source-aware repair queue. Reuse
  L49 lineage and L52 eligibility. A current source can still be inadequate.
  Preserve historical rows and unrelated valid alternatives when a reviewed
  claim is narrowed or withdrawn. Leave uncertain history available for
  inspection. Drive any repair from a reviewed case rather than a bulk backfill.

An [isolated repair replay](../../tests/test_concept_support_repair_replay.py)
contrasts an inferred diary agreement with an explicit agreement. Withdrawing
the reviewed diary dependency removes the unsupported claim from current
recall and leaves the explicit agreement available through its own support.
The surviving claim cites the valid alternative. Both source exchanges and
the old claim remain inspectable. The dependency ledger now saves the reviewed
withdrawal. The isolated replay verifies that re-admission, a synthesis retry,
and a merge cannot restore that premise to the reviewed claim. A separate
claim can still use the same record. The verdict is scoped to the reviewed
concept and source node. A wider cohort review and repairs for equivalent
sources with different IDs remain open.

On 22 November a read-only check found that both historical concepts still
passed the current recall gate on diary `11669` alone. The ledger recorded
40 selections for `13690` and 13 for `13691` across 44 distinct replies
from 15 to 20 November. In reply `11579` the user had already brought up
behaving in that turn. Reply `11328`, flagged as an echo for `13691`, discussed
another topic. Neither establishes that the diary introduced an agreement
into a live reply.

The [fictional ordinary-chat replay](../../scripts/concept_repair_chat_replay.py)
uses separate stores for a bookmark suggestion and an explicit one-session
agreement, before and after withdrawing the diary premise. On local
`baytout3/qwen3.5-uncensored:9b` with `qwen3-embedding:0.6b`, seed 71,
the direct question distinguished them, but an open question still produced
“we agreed” after withdrawal. The concept disappeared, yet memory recall
included the diary alongside all three original messages. Qualifying Aiko's
reflection at recall time preserved the distinction on all eight post-review
replies across seeds 71 and 73. Before withdrawal, the open suggestion case
still claimed agreement on both seeds. In the explicit case the open replies
recalled the decision but cited opening the atlas as confirmation. This is
one paired fictional exchange. The historical premises still await review
for repair, and accuracy across other conversations remains open.

The follow-up replay traced the mistaken citation to how the original messages
reached chat. For the explicit agreement, both questions received the user's
acceptance first, Aiko's proposal second and the opening remark last. On a new
soundcheck exchange, the open question turned accepting a plan to mark a channel
into having marked it. Ordering excerpts by itself left false agreements.

For a standalone question about a past decision, the
[archive lookup](../../app/core/conversation/exchange_recall.py) now presents the
matching proposal and the following replies in order. The relevance block uses
them directly when it can fit the whole passage and no competing topic match
sits outside the exchange. This also works before the reviewed diary premise is
withdrawn. Generic recall remains available in the replay with
`--without-archive`. The distinction between accepting a plan and carrying it
out still needs ordinary-use validation.

On the same local models, seeds 71 and 73 produced 32 replies across the two
occasions, two questions, and both dependency states. All eight direct replies
about an actual agreement cited the accepting turn. None of the four open
soundcheck replies about that agreement claimed that the channel had been
marked, though they repeated the decision without identifying the speaker of
the acceptance. For a suggestion alone, all eight direct replies distinguished
it from assent. The four open atlas replies still attributed some intention to
the user, including “you were going to use” the bookmark. Keep that wording
and cases outside the archive lookup open.
The eight-reply atlas control with archive lookup disabled reproduced the
wrong citation on both open questions about the explicit agreement.

**Open atlas attribution resolved in the isolated replay.** At seed 71,
speaker instructions alone still let the chat model exchange the roles. The
open recap now quotes the original turns with their speakers. Across 12
fictional replies (atlas, soundcheck and a held-out rehearsal, two responses
to each proposal, before and after withdrawing the diary premise), all 12
matched the original words. The atlas reply preserved the user's wish to look
at the harbor map first. The rehearsal distinguished accepting the lanterns
from deciding about the handbell, and kept “for the first rehearsal only”
when the user did agree. Four direct atlas controls still identified whether
the user accepted. The runs used local `baytout3/qwen3.5-uncensored:9b` and
`qwen3-embedding:0.6b`. Fresh ordinary use and cases where the archive cannot
identify a single exchange remain open under L22.

**Missing and competing decision exchanges, November 22.** In the initial
eight fictional atlas replies at seed 71, the model chose the bookmark every
time. This included four cases where the user had only asked to look at the
harbor map. The bounded search either missed the 30-day-old original or
found another proposal about a ribbon. After the change, all 16 atlas and
rehearsal trials asked for a date or showed the two proposals. All 16
follow-ups recovered the selected speakers and replies, including the limit
to the first rehearsal. Four unique atlas controls kept their original
recaps. Each trial used disposable stores, the same local models as above,
and the ordinary chat runner. Repeat with `--archive-case missing` or
`--archive-case competing`. This closes the reproduced fallback cases.
Ordinary-use wording and questions outside the standalone decision lookup
remain open.

**Ordinary conversation check, November 22.** A read-only screen covered 492
saved user messages across ten ordinary chats in the November 8–22 window.
The newest turn was November 20. None used the standalone decision-question
opening, and there were no recorded decision clarifications or quoted recaps.
In one earlier exchange an ambiguous reference to something Aiko had said
prompted a clarification, and the user identified the remark on the next turn.
In a different exchange, Aiko supplied a nighttime detail for an event recorded
in the morning. Both precede the change. Fresh ordinary-use validation stays open.

The [chat replay](../../scripts/concept_repair_chat_replay.py) now accepts
`--conversation-check --occasion soundcheck` and `--occasion rehearsal` with
the same flag. At seed 71, the local models above handled 16 disposable chats
(28 replies) with preceding small talk and the ordinary tool gate. Only
`recall` and `conversation_history` were registered. Each missing case had a
speculative diary and concept without an original dialogue. In competing cases
both proposals were searchable and neither had been accepted.

- The four distinct plain opening questions gave the appropriate clarification.
  Prefixing exactly the same questions with “Oh, before I forget,” bypassed
  decision lookup and produced four unsupported agreements.
- “The second one” recovered the undecided reply on both occasions. “Oh, the
  second one, I think” turned identifying the exchange into accepting the plan
  on both occasions.
- On both “I don't remember” turns the lookup correctly marked the decision
  unresolved, but a history tool result returned control to the model, which
  asked “Which one do you mean?” The two hedged date replies also kept the
  issue alive. One promised another search and the other supplied an agreement.
- All four explicit topic changes moved on. Preserve this behavior when
  broadening recognition. The next fix should cover casual openings and
  qualified selections, and let a user end an unresolved lookup even when a
  tool runs. Count future ordinary turns separately from these paired replays.

**Casual decision recall fixed in replay.** Repeating the tool-enabled check
at seed 71 on soundcheck, rehearsal, and atlas produced 42 replies in 24
fictional chats. All six openings with “Oh, before I forget” asked for a clue
or offered the two recorded proposals. All six selections, including three
with “I think,” preserved the original undecided reply. All six plain or
hedged “I don't remember” replies ended the lookup without a tool call or
another question. All six explicit topic changes answered the new request.
The replay now checks these regressions. Fresh ordinary-use validation after
a backend restart remains open.

**Acceptance.** A one-time choice does not imply effectiveness or a standing
trait. Multiple paraphrases of one episode retain one evidence basis in the
review. Withdrawing an unsupported premise changes recall eligibility while an
independently supported alternative survives. Validate L46 on a fresh cohort
using the same criteria while keeping depth capped at two.

## L29b. Meta-narrative -- an arc whose steps are concepts

**Status: Partial; live validation remains (28 Oct 2026).**

**Implemented / available:** Bounded temporal paths through recorded concept changes cover user,
Aiko and relationship subjects. Connected labels, alias resolution, lifecycle receipts and source
revision/deletion checks gate rendering. Read-only arcs reach explicit retrospective requests in
T3 and concept recall; historical events without captured source receipts remain unverified.

**Remaining:** Evaluate chronology, natural narration and coverage over ordinary use. Broader arcs
beyond connected recorded changes and an unsolicited cue policy require separate evidence.

4 Nov follow-up: bounded history now follows each connected endpoint instead of
the subject-wide latest page; diagnostics distinguish missing/incomplete/revised
receipts and insufficient changes. The ordinary-use core sample remains thin;
natural narration is still unvalidated. See [the follow-up](../reviews/ordinary-use-follow-up.md).

12 Nov: the [new review](../reviews/ordinary-use-review-2026-11-12.md) still found no
supported arc among 24 sampled endpoints: 13 had missing/incomplete receipts,
11 insufficient verified changes. This remains a coverage/evidence boundary,
not permission to narrate unsupported changes.

**Source check:** [understanding_arcs.py](../../app/core/concepts/understanding_arcs.py),
[test_understanding_arcs.py](../../tests/test_understanding_arcs.py),
[rollout](../rollouts/concept-pattern-rollout.md).

<a id="l30a-hypothesis-surfacing-lane-the-read--render-side"></a>

## L30a. A grounded reason for hypothesis uncertainty

**Status: Partial.**

**Implemented / available:** T3 hypothesis surfacing, importance/unsettledness ranking, origin split
and question-budget coupling exist; L30b/c shipped.
The 28 Oct slice adds a compact source-linked basis, an explicit unresolved point and
evidence-preserving bounded recall without increasing T3 capacity.

**Remaining:** Evaluate whether the basis supports useful, appropriately tentative contributions
on the current model. RS2 retains broader evidence-projection work; H7/H44 own ask/answer quality.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l30a).

**Source check:** [hypothesis_lane.py](../../app/core/concepts/hypothesis_lane.py),
[test_concept_hypothesis.py](../../tests/test_concept_hypothesis.py).

## L30d. Uncertainty zones -- explicit known-unknowns to aim curiosity at

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** Knowledge gaps, importance and hypothesis consumers exist.

**Remaining:** Measure throughput, then trial important known-unknown inflow through the existing
gap store. Empty kind/subject cells do not establish gaps; do not add supply to an unverified
consumer.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l30d).

**Source check:** [knowledge_gap_extractor.py](../../app/core/memory/knowledge_gap_extractor.py).

## L30e. Invented hypotheses -- a place to make guesses up (Phase B)

**Status: Partial.**

**Implemented / available:** Separate hypotheses, invention worker, testing/dedupe/graduation and
recall exist; introspection now writes belief_outcome-linked origin_refs.

**Remaining:** Validate targeting, origin/kind outcomes and novelty after refutation; decide
consistent multi-user/export/wipe semantics with concepts and disabled-invention TTL. The old
universally-free/unread-origin claim is stale. H7/H44 own evidence quality.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l30e).

**Source check:** [introspection_worker.py](../../app/core/proactive/introspection_worker.py),
[test_hypothesis_proposer_worker.py](../../tests/test_hypothesis_proposer_worker.py).

<a id="l32-concept-importance----a-second-axis-distinct-from-confidence"></a>

## L32. Behavioral evidence in concept importance

**Status: Partial.**

**Implemented / available:** Derived importance is live in T3 and hypothesis ranking; L30a consumes it.

**Remaining:** Evaluate attributable behavioral contribution to importance. L30d may reuse the
existing context; its absence does not reopen the importance axis.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l32).

**Source check:** [hypothesis_lane.py](../../app/core/concepts/hypothesis_lane.py).

## L33. Introspective reflection -- structured self-questioning that feeds concepts

**Status: Partial.**

**Implemented / available:** Evidence eligibility and bounded introspective hypothesis intake use
L47 provenance, alternatives/disconfirmation, shared gates and origin receipts.

**Remaining:** Evaluate admissible outcomes and proposed hypothesis quality before broadening
reflective probes. Eligibility is not correctness; alternative prose is not L53 causal testing.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l33).

**Source check:** [introspection_worker.py](../../app/core/proactive/introspection_worker.py),
[reflection_worker.py](../../app/core/proactive/reflection_worker.py),
[concept_synthesis_worker.py](../../app/core/concepts/concept_synthesis_worker.py).

## L34. Concept relation taxonomy -- edges beyond support / tension

**Status: Partial.**

**Implemented / available:** Evidence/tension/generalization edges exist; L52 adds a structural
support-set ledger with revision/retraction and merge guards.

**Remaining:** Define proposed/exposed explains, exemplifies and conditional semantics end to end.
Reuse L52 rather than adding a second dependency truth store; source-linked why recall remains RS2.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l34).

**Source check:** [concept_dependencies.py](../../app/core/concepts/concept_dependencies.py),
[test_concept_dependencies.py](../../tests/test_concept_dependencies.py),
[concept_store.py](../../app/core/concepts/concept_store.py),
[concept_view.py](../../app/core/concepts/concept_view.py),
[rag_retriever.py](../../app/core/rag/rag_retriever.py),
[inner_life_part1.py](../../app/core/session/inner_life_part1.py).

## L36. Strategy layer -- learned approaches, distinct from beliefs

**Status: Partial; live validation remains (28 Oct 2026).**

**Implemented / available:** The existing memory extractor admits source-cited goal, approach,
constraints, exceptions and explicit user-reported outcomes. Bounded lexical transfer requires
the goal and all constraints, respects exceptions and invalidates revised/deleted sources or a
source-linked failed outcome. MemoryStore owns these records; no additional concept truth store
or model call was added.

**Remaining:** Evaluate extraction, useful transfer and corrections with real conversation data.
Broader semantic matching and outcome attribution are unvalidated. Engagement is not success;
the existing L52 contract remains authoritative for derived concepts.

5 Nov [data review](../reviews/strategy-lifecycle-review.md): no admitted strategies yet;
schema offerings are not an opportunity denominator. A natural-result phrase is
a useful missed-opportunity contrast, while useful transfer remains unvalidated.

The outcome vocabulary now covers explicit natural status reports, with the
same user/goal/condition/source checks and no extra model call. Same-message
approach/result reports require unambiguous literal order. Ordinary-use precision
and helpful transfer remain to be measured.

18 Nov slice: strategy recall evaluates each report's exceptions before choosing
the latest matching outcome. Short follow-ups can reuse a fresh chat-only
troubleshooting goal and its constraints through the existing context budget.
Isolated tests cover a scoped failure and stale, suspended and conflicting work.

**Source check:** [learned_strategies.py](../../app/core/memory/learned_strategies.py),
[test_learned_strategies.py](../../tests/test_learned_strategies.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Transfer an approach to a held-out problem with matching
constraints; reject the superficially similar case with a different goal.
Preserve a user-corrected exception. Retracting an outcome invalidates the
strategy's unsupported justification through L52, rather than asserting that the
opposite approach works. Never learn "keep the user talking" from longer chats.
Use explicit helpfulness/outcome labels and T5/L55 comparisons at equal budgets.
New kind/schema work remains conditional on the pilot exposing a real need.

</details>

## L42b. Neglect-guided curiosity

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** Weekly conduct/neglect detection exists.

**Remaining:** Inspect real neglect false positives before a bounded curiosity prior; retain
relevance/privacy gates and do not ship from synthetic findings alone.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l42b).

**Source check:** [surfacing_conduct.py](../../app/core/concepts/surfacing_conduct.py),
[idle_curiosity_worker.py](../../app/core/proactive/idle_curiosity_worker.py).

## L43. How she thinks he sees her -- the second-order self-model

**Status: First scope implemented. Broader contextual appraisal remains open.**

Explicit user reports now guide humor or quiet company during pre-event nerves.
The response-mode provider rereads retained user-owned dialogue, so corrections,
withdrawals and source edits affect the next matching turn. See the
[support guide](../../docs/shared-moments-and-relationship.md) and
[isolated chat replay](../../scripts/response_feedback_replay.py).

The fictional replay starts with feedback about an interview, tests transfer
to an audition, then delivers a correction after Aiko's actual reply. A fresh
chat repeats the same audition message. Post-audition disappointment and a
fresh history without feedback serve as controls. With the local
`baytout3/qwen3.5-uncensored:9b` model and seeds 71 and 83, both runs joked
about the user's stomach rehearsing, acknowledged that company mattered, and
offered quiet company on the next audition. Neither control selected feedback.
The post-audition reply acknowledged the missed entrance and included an
incomplete phrase. These runs cover the fictional exchange.

**Remaining:** Review ordinary-use examples before expanding the situations
or responses covered. Broader self-appraisals and their relationship to the
existing axes remain open. Silence and response latency cannot establish
the user's appraisal.

**Source check:** [concept_kinds.py](../../app/core/concepts/concept_kinds.py),
[engagement_tracker.py](../../app/core/affect/engagement_tracker.py),
[relationship_axes.py](../../app/core/relationship/relationship_axes.py),
[concept_synthesis_worker.py](../../app/core/concepts/concept_synthesis_worker.py).

<a id="l44-knowing-where-shes-usually-wrong----per-domain-self-calibration"></a>

## L44. Outcome-grounded per-domain calibration

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** L47 attribution storage exists. Read-only 24 Oct snapshot: 78 outcomes:
12 auto_confirm, 65 mood_comparison and 1 opinion_heuristic; these are methods, not independent
accuracy judgments.

**Remaining:** Validate independent source-grounded outcomes and per-domain coverage before
calibration. The ledger blocker is resolved; repetition/mood checks cannot be reported as factual
accuracy.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l44).

**Source check:** [belief_store.py](../../app/core/relationship/belief_store.py),
[test_introspection_worker.py](../../tests/test_introspection_worker.py),
[fact_check_backfill.py](../../scripts/fact_check_backfill.py),
[concept_lifecycle_worker.py](../../app/core/concepts/concept_lifecycle_worker.py).

<a id="l45-self-tuning-concept-gates----thresholds-as-intent-not-constants"></a>

## L45. Remaining gate-tuning rollout

**Status: Partial.**

**Implemented / available:** Gate solver/specs, population observation, read-side application, trend
diagnostics and seeded adoption exist.

**Remaining:** Validate staged lifecycle/write-gate changes over ordinary use, then consider
per-kind floors, coupled cosine bands and relevance telemetry. Dormant/contradicted revival
now uses the existing kind gate, preserving delayed supporting evidence and disproof review.

5 Nov [lifecycle review](../reviews/strategy-lifecycle-review.md) found 29 dormant-to-revival
sequences below the kind confidence floor, plus a large weak candidate population
kept past TTL by the two-source expiry condition. Both bounded slices are now
implemented: kind-gated revival and age/quiet/confidence-based candidate expiry,
with retained evidence/history and a read-only eligibility report. Ordinary-use
validation remains; these status decisions do not certify semantic truth.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l45).

**Source check:** [gate_tuning.py](../../app/core/concepts/gate_tuning.py),
[gate_measure.py](../../app/core/concepts/gate_measure.py),
[gate_tuner_worker.py](../../app/core/concepts/gate_tuner_worker.py),
[gate_tuning_store.py](../../app/core/infra/gate_tuning_store.py),
[concept_gate_report.py](../../scripts/concept_gate_report.py),
[concept_lifecycle_worker.py](../../app/core/concepts/concept_lifecycle_worker.py).

<a id="l46-abstraction-never-stacks----the-graph-is-two-layers-by-one-filter-clause"></a>

## L46. Validate useful depth-2 abstractions

**Status: Validation remaining.**

**Implemented / available:** Same-subject depth-2 generalization, cycle/depth/restatement guards and
retrieval/ancestor handling are implemented.

**18 Nov read-only review:** The current graph has 219 generalizations at structural depth 2.
Two are active and 17 are candidates. Sixteen of the candidates have no viable recorded support
set. The two active rows and the one viable candidate each have a single viable set containing
only depth-0 premises. Their stored edges give them depth 2, while their current support reaches
only one layer. The sampled labels make broad claims about motives or systems from premises
that support narrower patterns. Some premises also repeat the same preference.

From 12 Nov UTC through the snapshot, T3 recorded zero selections of IDs now at depth 2. One
older row now at depth 4 appeared 37 times in the core or flex lanes. Depth is computed from
current edges, and the surfacing records do not capture depth at the time of selection. Recent
logs show five of 85 completed synthesis runs marked the depth-2 input pool dirty. The completion
logs do not identify depth-2 proposal or rejection counts.

**Follow-up:** Synthesis keeps base and stacked evidence separate during creation and
reinforcement. Recall and lifecycle require a live depth-1 support set for structural depth 2 and
exclude deeper rows from ordinary selection. The openness report now exposes stored and eligible
depth-2 counts, deeper legacy rows and recent T3 use. Synthesis logs count parsed depth-2 proposals,
additions and reinforcements.

**Remaining:** Review generation, live support, lifecycle and T3 together. Report structural
depth alongside current support. Review both levels against the original sources
for distinct evidence, a useful common principle, preserved scope and concise labels. Track
proposal, admission, promotion or withdrawal, and surfacing for a fresh cohort. Review legacy
over-cap rows separately before expanding the supported depth.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l46).

**Source check:**
[concept_synthesis_worker.py](../../app/core/concepts/concept_synthesis_worker.py),
[concept_meta_depth.py](../../app/core/concepts/concept_meta_depth.py),
[concept_openness_report.py](../../scripts/concept_openness_report.py),
[base.py](../../app/core/concepts/proposers/base.py),
[generalization_user.py](../../app/core/concepts/proposers/generalization_user.py),
[generalization_aiko.py](../../app/core/concepts/proposers/generalization_aiko.py),
[concept_dedupe.py](../../app/core/concepts/concept_dedupe.py).

### L46 follow-up. Test competing abstractions on an unseen choice

**Status: First fictional comparison under L55.** Someone edits photos on copies and
tries game mods in a separate save. “Likes experimenting” and “wants to keep the
original recoverable” both fit those episodes. They suggest different replies
when a new tool offers an irreversible conversion. A depth-2 concept earns its
place if it helps Aiko notice the distinction and say something useful under
the right condition.

Construct a chronological replay. Give synthesis eligible concepts from two
topics, hold back an entire later occasion and all its derivatives, and record
which condition each candidate expects to matter before revealing the user's
next choice. Compare the parent abstraction with its children alone at the same
context budget. In one ending the user asks for a recoverable copy. In another
they already have a backup and want to experiment freely. Include an ending
compatible with both readings. Score whether the response attends to the right
condition, leaves motive open when the evidence is ambiguous, and updates after
a correction. One successful prediction supports further evaluation. Only
observed choices or an explicit explanation can supply new evidence about the
user. Keep the existing depth cap and record source overlap through L49.

The [abstraction replay](../../scripts/concept_abstraction_replay.py) uses the
configured `worker_default` model and context window through loopback Ollama.
On `baytout3/qwen3.5-uncensored:9b`, each version used 207 input tokens before
the reveal and matched the other versions for each ending at 226–250 tokens.
Seeds 49 and 57 at temperature zero returned identical results. All three
versions scored two of four under the structured rubric. They recommended
duplicating after the backup correction and kept lasting motive uncertain.
They also selected duplication when a verified backup was available and when
the backup status was ambiguous. With the underlying concepts, the verified
backup reply itself said to proceed, even though its action field said to
duplicate.

An earlier run used the 4B Live policy model and the generic Ollama transport
settings. It scored four of four for the underlying concepts and three of four
for each parent. Both parents missed the correction under the structured rubric.
Both comparisons used supplied context in one fictional story. Replay mined
parents across several held-out occasions and judge the response text alongside
its action field before changing synthesis, ranking or the context budget.

**Mined choice comparison.** Run `python -m scripts.mined_choice_replay --seed 71`.
Four literal fictional user reports stand in for eligible base concepts. The production
proposer generated two children and one view about stewardship. Only the parent or its
two cited children were shown per arm, with the same 600-character allocation and the
same turns and generation settings. This tests the proposer and reply, with base
extraction, lifecycle promotion and ordinary-chat retrieval outside the experiment.

Local `baytout3/qwen3.5-uncensored:9b` finished all 16 replies across an audio mix and
a loom draft. On manual reading, both arms preserved the only original in 2/2 cases,
and neither asked whether a copy existed in the two unknown cases per arm. With a
verified backup the parent endorsed proceeding on both, while the children suggested
loading the copy on the audio case. After a correction the parent protected the loom
draft but also said to proceed with the audio overwrite. The children failed to
establish a safe current-project copy on either correction. Some replies described
rituals or standards that the user had never named. The initial 140-token pilot
truncated the first reply; the completed comparison used 350 for both arms. This
sample gives no reason to raise the depth cap.

### L46 follow-up. Prepare the shared graph for deeper abstractions

**Architecture review, 22 Nov 2026.** The original L1 design still fits. A
generalization at either supported level is an ordinary concept with typed
premise-to-conclusion evidence edges. Structural depth is computed on those
edges. L52 adds revisioned support sets because a flat list cannot express
“both A and B, or independently C and D.” It uses the same concept IDs and
edge store. Lifecycle, recall and provenance already consume that distinction.
Keep the shared concept row, kind registry, edge store and lifecycle worker.
[Original design](shipped/concepts.md#l1-concept-store--schema-kind-parameterized)
and [current support contract](../../docs/concept-integration.md#live-depth-2-generalizations-l46).

**Current graph.** The read-only openness diagnostic found 219 generalizations
at structural depth 2. Of these, 197 were retired, three dormant and 19
candidates. None of the 19 had a viable support set. Ten candidates had at
least one set fail on a retired premise, nine on the wrong premise depth and
seven on a revised source. These categories overlap. At depth 1 there were 16 active
generalizations with surviving support (seven user, nine Aiko). Another 241
stored generalizations exceeded the supported cap. The installation had
13,364 concept rows overall, including 1,236 active. Measure walks and queue
coverage against that scale. These counts describe one snapshot.
[Report](../../scripts/concept_openness_report.py).

1. **Finish and checkpoint each synthesis level independently.** Both passes
   save their pool signature before the outer loop persists their proposals.
   An isolated probe gave the first pass a proposal and the stacking pass an
   unusable answer. The worker made two proposer calls, saved the first
   signature and made zero persistence calls. Commit processed outcomes before
   advancing that level's checkpoint, make retries idempotent, and let a
   failed level retry independently. A usable empty answer can complete a
   review. An exception, cancellation or stale input must keep it eligible.
   [Worker dispatch](../../app/core/concepts/concept_synthesis_worker.py).
2. **Record which premises a level actually considered.** All four current
   generalization signature records contain only a pool hash and count. The
   hash covers IDs, rounded confidence and activity hints. A second probe
   changed a premise's label and rationale and produced the same hash.
   Extend the existing per-proposer, per-subject KV state with target depth,
   a policy version, and a bounded batch receipt of offered IDs, semantic
   revisions, completion time and outcome. Include the relevant support and
   scope revisions when they affect the decision. Use the narrative clock for
   timestamps. Keep lifecycle evaluation time and reinforcement time dedicated
   to their present jobs. On upgrade, old signatures give unknown review
   times. Drain changed or previously unoffered eligible premises in bounded
   batches alongside familiar comparison premises. Record the inputs actually
   seen together, since a new partner can make an old premise worth revisiting.
   [Pool selection and signatures](../../app/core/concepts/concept_synthesis_worker.py)
   and [existing bounded input selection](../../app/core/concepts/concept_synthesis_inputs.py).
3. **Share the depth policy and the evidence a proposer sees.** Replace the
   separate base/stacking pass assumptions and depth-two checks with one
   generalization rule parameterized by target depth. Use it for selection,
   creation, reinforcement, retry and live support validation. Begin with the
   two supported levels. Give each offered premise a compact projection of
   its surviving support, original observations and applicable conditions.
   Two children citing the same occasion can describe different aspects of
   it but contribute only one independent occasion. Unknown lineage stays
   unknown. Reuse L49's lineage resolver and L52's viable alternatives, with
   a budget for shared ancestors. A withdrawal or revision should wake the
   existing dependent-review path and invalidate affected synthesis receipts.
   Coalesce repeated changes and retain the ordinary oldest-first sweep.
   [Depth rules](../../app/core/concepts/concept_meta_depth.py),
   [support sets](../../app/core/concepts/concept_dependencies.py) and
   [source lineage](../../app/core/concepts/concept_evidence_lineage.py).

**Implemented on 22 Nov.** Each level now persists its proposals independently.
The worker tracks completed review batches, handles legacy signatures and
retries, and exposes the latest partners and review time in concept provenance.
Selection and validation share the premise-depth rule. Review revisions
include claim, scope and live support. The depth cap is two. A read-only
selection probe over 13,364 stored concepts used the default 24-premise cap.
User and Aiko batches offered 24 base concepts each and seven and nine
stacking premises respectively. Selecting all four batches took 1.15 seconds
in one local run, excluding model calls, persistence and prompt-time recall.
[Current contract](../../docs/concept-integration.md#generalization-review-history)
and [regressions](../../tests/test_generalization_reviews.py).

**Acceptance before enabling another level.** Exercise a failed second pass
and a restart, a correction with stable confidence, a new partner for a
previously reviewed premise, shared ancestry, and a withdrawn premise with a
surviving independent alternative. Measure prompt-path validation on a broad
graph with shared ancestors. Record depth and eligible support at the time a
parent surfaces so later merges cannot rewrite the interpretation of past
usage. Run the held-out-choice comparison above on mined parents at the same
context budget as their children. Enable a depth-3 pilot only once depth 2
has demonstrated a benefit on fresh occasions and handles corrections.

<a id="l48-refused-evidence-already-mints-a-new-concept-and-what-it-mints-is-a-twin"></a>

## L48. Validate evidence routing and duplicate control

**Status: Validation remaining.**

**Implemented / available:** Delta-first routing, separate support/discovery, neighbor distinctions,
bounded reconsideration and diagnostics exist. Refused reinforcement returned; it did not fall
through to creation.

**Remaining:** Evaluate deployed duplicate/reconsideration quality. The historical causal heading is
retained for links, not as a verified mechanism; do not raise the evidence cap on that premise.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l48).

**Source check:**
[concept_evidence_admission.py](../../app/core/concepts/concept_evidence_admission.py),
[concept_synthesis_worker.py](../../app/core/concepts/concept_synthesis_worker.py),
[concept_synthesis_routing.py](../../app/core/concepts/concept_synthesis_routing.py),
[concept_consolidation_worker.py](../../app/core/concepts/concept_consolidation_worker.py),
[concept_dedupe.py](../../app/core/concepts/concept_dedupe.py).

## L49. Independent evidence and protection against self-confirmation

**Status: Partial.**

**Implemented / available:** Shadow lineage accounting and frozen-source manifests cover selected paths.

**Remaining:** Audit a consistent deployment, cover remaining writers/mutable sources, preserve
observations lost by dedupe and distinguish independent episodes/upstream sources. Unknown history
stays unknown; shadow is not enforcement.

18 Nov read-only snapshot: recorded overlap collapsed multiple sources in three
of 47 recent concepts, including repeated message lineage and a repeated external
citation. All but one concept had incomplete lineage. Reflective transcript
receipts now contribute traceable context to new captures without adding evidence
votes. Existing frozen receipts and promotion policy retain their behavior.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l49).

**Source check:**
[concept_evidence_lineage.py](../../app/core/concepts/concept_evidence_lineage.py),
[test_concept_evidence_lineage.py](../../tests/test_concept_evidence_lineage.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Re-extract one transcript ten times: effective support does not
increase. Add a genuinely separate episode: it can increase. Two meta-concepts
sharing the same underlying sources do not double their parent's support.
Imported rows with unknown lineage remain explicitly unknown. Explicit user
boundaries still work after one statement; independence must not weaken them.

</details>

### L49 follow-up. Recognize a retelling across topic clusters

Ordinary frequency questions now have a bounded source comparison in
`occasion_recall.py`. The fictional chat regression runs the prompt assembler
and turn runner with a bike incident in one conversation and a repair in another.
Editing the repair to an explicit recurrence changes one incident to two.
Withdrawing the recurrence leaves the count open. The route uses source receipts
and a narrow English question form. Broader paraphrases and independent
support during concept promotion remain open.

**Status: Reviewed-pair pilot.** A conversation can revisit one event for several reasons.
A bike commute might first come up under repairs and later under getting to work.
The second account can add what happened next. Counting it as a second breakdown
would teach Aiko the wrong thing about how often the bike fails.

[Source lineage](../../app/core/concepts/concept_evidence_lineage.py) can group
overlapping message roots. Separate conversations can have disjoint roots even
when the speaker explicitly refers back to the same incident. Pilot a link between
two source-backed accounts when the later one identifies the earlier occasion.
Keep both snippets, topic assignments and report dates. Let a later account add
a supported detail or correct one facet. Track the number of accounts separately
from the number of established occasions in the existing topic evidence profile.
Start with an explicit callback or dated identifying detail. Leave ambiguous
matches unresolved and retain the current evidence when the linking span is edited.

**First check.** Replay three versions of the commute story. In one the user
clarifies that both reports describe Tuesday. In another they explicitly report
a second breakdown on Friday. In the third they only say “the chain slipped.”
A later “how often?” reply should recall one confirmed incident, two, or uncertain
frequency respectively. Check both direct recall and a concept citing both topics.
Review false joins as well as missed retellings before using the signal in promotion.

The [isolated replay](../../tests/test_concept_occasion_replay.py) found two
recorded source groups in every variant. Its reviewed links gave one confirmed
occasion for the explicit Tuesday callback, two for the explicit second
breakdown and an unresolved count for the ambiguous report. Archive lookup
retained both dated statements and concept recall retained both source notes.
Editing the callback expired the relationship and left the edited statement
available. The pilot takes a reviewer verdict and checks both sources. Automatic
link discovery and topic-profile integration still need evaluation.

**22 November local reply check.** Run
`python -m scripts.companion_pattern_replay --section occasions --seed 71`.
Four fictional histories start with a torn puppet screen. The later report
recounts the tear, explicitly reports a second tear, leaves the occasion
ambiguous, or describes a later repair. The expected frequencies are one,
two, uncertain and one. Each history uses a separate disposable database.
The test supplies the reviewer's relation and exercises the source comparison.

| Reply from the local 9B model or the reviewed pair | Correct frequency per seed |
| --- | --- |
| Plain reply from the two dated reports | 3/4 |
| Plain reply with the reviewed count supplied | 2/4 |
| Structured frequency and reply | 1/4 in each arm (reports alone, relation supplied, count supplied) |
| Frequency reply rendered directly from the reviewed pair | 4/4 |

Seeds 71 and 73 gave the same results. Scores cover the frequency claim only.
The plain reply invented a second incident for the ambiguous report. With the
reviewed count supplied, one reply opened with two, reconsidered, and ended
incomplete. Several replies also attributed the user's repair or patch order
to Aiko. The pilot now returns a short frequency answer from the verified
pair and reopens uncertainty when a source edit invalidates the review.
Ordinary chat routing and automatic discovery of matching occasions remain open.

**Automatic pair pilot, 22 November.** Run
`python -m scripts.companion_pattern_replay --section occasion-links --seed 71`.
Nine fictional histories ask how many times a counter jammed, a crate split,
or a tap leaked. Two edits revisit the counter history. The local 9B model
receives the retrieved source messages and proposes a relation with literal
evidence from each. The pilot checks the excerpts against the current messages.
For the tested question form it also checks that a recurrence names the
subject and repeats the event, or that a callback refers to the earlier event.
The displayed answer comes from the resulting source comparison.

At both seeds (71 and 73), the model proposed the expected relation on 9/11
checks. It twice treated the same ambiguous repair-slip wording as a new jam,
once in a fresh history and once after an edit. The evidence check withheld
both proposals. All 11 answers matched the expected frequency, including the
two held-out tap cases. Editing away the callback changed the answer from
one to uncertain. A further edit explicitly reporting another jam changed it
to two.

The counter callback ran through actual memory extraction. Seed 71 stored
only the first report as a memory, so the pilot recovered the second user
message with a literal topic search in the local conversation archive. Seed
73 stored both. The other eight histories seeded source-linked memories to
isolate the comparison. Histories used disposable databases, including their
question and reply turns. Topic assignments were supplied. Automatic linking
under broader paraphrases, ordinary chat routing and promotion remain open.

## L50. Context-scoped claims and temporal validity

**Status: Partial.**

**Implemented / available:** Bounded communication-style applicability has source/context admission,
correction/consolidation/lifecycle guards and synthetic contrasts.
The 28 Oct extension accepts literal, source-cited activity names, checks qualifier source
revisions and preserves the existing exception/correction paths.

18 Nov slice: the read-only snapshot had four stored scopes, all for Aiko's own
style. The user-style pass now offers source-checked `preference` rows from normal
capture as direct anchors. An isolated memory-to-prompt test covers different
learning and troubleshooting preferences, casual conversation, a troubleshooting
correction, and a later source edit. Scoped user candidates stay out of the
hypothesis and confirmation-question paths while awaiting promotion.

**Remaining:** Evaluate model behavior, broader factual claim scoping, erased quotation framing, scoped
revival, historical coverage and temporal queries. Lexical support is not general entailment.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l50).

**Source check:** [concept_applicability.py](../../app/core/concepts/concept_applicability.py),
[reasoning_l55.json](../../tests/fixtures/reasoning_l55.json).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Contrastive fixtures activate the appropriate style in learning
versus troubleshooting; unspecified context does not choose a narrow rule with
certainty. A correction to one context leaves the other intact. Quoted,
hypothetical and role-play statements do not become personal traits.

</details>

## L51. A bounded working model for the current problem

**Status: Partial.**

**Implemented / available:** Troubleshooting working-model fields and evidence freshness persist
through the existing situation worker/store/renderer.

18 Nov slice: saved working sets now bind to their cited message revisions.
An isolated regression edits or deletes a cited constraint after storage,
checks that the next prompt and recall decision withhold the stale working
set, and verifies recovery when the ordinary observer sees fresh evidence.

**Remaining:** Measure extraction-to-reply entailment, disconfirmation and resumption through
corrections/topic changes before expansion beyond the pilot.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l51).

**Source check:**
[conversation_situation.py](../../app/core/conversation/conversation_situation.py),
[conversation_situation_worker.py](../../app/core/conversation/conversation_situation_worker.py),
[reasoning_l55.json](../../tests/fixtures/reasoning_l55.json).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** An interrupted task resumes its constraints. New disconfirming
evidence changes the chosen explanation. The worker cannot persist a speculative
premise as a memory merely because it appeared in its workspace. An ordinary
greeting incurs no additional reasoning call. Bound calls, tokens and wall time.

</details>

## L52. Dependency-aware revision and retraction

**Status: Partial.**

**Implemented / available:** Structural support sets preserve complete alternative supports,
invalidate revised/deleted premises and guard traversal/merges; lifecycle/retrieval enforce tracked
viability.

**Remaining:** Extract semantically sufficient supports, cover other writers and decide historical
backfill. Structural conjunctions/alternatives do not establish semantic sufficiency/independence;
legacy claims remain untracked.

18 Nov read-only snapshot: seven of 47 recent concepts had an expired planning
support set. Six retained another valid set. The 25 recorded diary transcript
packets were current at inspection. An isolated reproduction showed that editing
or removing a message could leave a diary-derived concept eligible for recall.
Lookup now verifies those receipts. A valid alternative keeps the concept viable,
and the diary remains in history. The earlier unsupported agreement still needs
semantic review because its transcript receipt is current.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l52).

**Source check:** [concept_dependencies.py](../../app/core/concepts/concept_dependencies.py),
[test_concept_dependencies.py](../../tests/test_concept_dependencies.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Retract a parent: dependent claims are no longer asserted from
that parent alone. A child with another valid support set survives. Alias merges
preserve dependencies without cycles or depth inflation. Deleting source data
has an explicit policy for derived hypotheses, cached prompts and provenance;
the new L47 cascade alone does not solve downstream deletion.

</details>

## L53. Causal explanations and counterfactual checks

**Status: Partial.**

**Implemented / available:** L33 alternatives, L51 working state and L52 support viability exist.
The troubleshooting pilot records two opposing predictions and their assumptions before one
yes/no check. A later cited user result can weaken the mismatched explanation, while ambiguous
results leave both tentative. Isolated contrasts cover both outcomes, corrections and edits to
cited evidence.

**Remaining:** Evaluate extraction and ordinary replies on matched problems. Broader causal
comparisons and counterfactual checks remain open.

**Source check:** [introspection_worker.py](../../app/core/proactive/introspection_worker.py),
[conversation_situation.py](../../app/core/conversation/conversation_situation.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Matched scenarios with the same correlation but a different
mechanism lead to different predictions. A failed prediction revises the
explanation rather than producing a post-hoc excuse. No irreversible tool
action, personal experiment or intervention occurs without normal permissions.

</details>

**Return after a detour.** `python -m scripts.diagnostic_return_replay --seed 71`
uses fictional source messages, a fixed observer extraction, two observed pottery
turns, the ordinary prompt assembler and turn runner. With local
`baytout3/qwen3.5-uncensored:9b`, seeds 71 and 83 each met the six reply expectations.
Three sourced recaps distinguished clicks present, clean audio, and a test of
the wrong input. Three generated replies revised the leading explanation after
a correction, withheld the result for a different microphone, and answered an
unrelated bakery question. Early generated replies had needlessly clarified or
ruled out the alternative. These final runs exercise using an existing comparison.
The independent `--model-observer --case present` probe (seed 83) stopped before
the return. The observer attributed its diagnostic question to the assistant
in the field reserved for the user's goal. The evidence validator rejected it.
Capturing the comparison reliably from ordinary conversation remains open.

**Independent capture follow-up.** With local `baytout3/qwen3.5-uncensored:9b`,
`python -m scripts.diagnostic_return_replay --capture-only --seed 83` went
from 1/3 to 3/3. The cases check an assistant-proposed microphone test, a
user-proposed printing-press test with the prediction order reversed, and a fan
hypothesis without a test. Both positive cases retained the exact questions and
source IDs.

`--ordinary --seed 83` generates each reply and observes each completed turn
in an empty fictional session. In the seven-turn press replay the fresh plate
initially streaked, favoring the roller. A pottery detour left the comparison
available. After the user corrected the print to clean, the observer stored
the negative result and the next recap favored the plate. The opening reply
can still ask a generic clarification. An earlier variant that asked for a bowl
name exposed the original one-working-problem limit.

The two-problem follow-up uses the ordinary chat and observer paths from an
empty fictional session. In ten turns, the user corrected a streaked press
print to clean, then tested a desk microphone and heard clicks. Before the
change all three named recaps failed. With one stored earlier comparison the
microphone, press and microphone recaps each quoted the relevant report and
favored the input fault, damaged plate and input fault respectively. Run
`python -m scripts.diagnostic_return_replay --two-problems --seed 83` with
the local 9B model. In this run the model prematurely described the microphone
test as confirmed when it was proposed. The stored answer stayed open until
the user supplied the result.

The wording follow-up scores the immediate replies as well as the stored
comparison. In the isolated microphone replay at seed 83, Aiko asked whether
the clicks were audible in the local recording. The check had no result.
After the user reported hearing clicks, Aiko favored an input fault, kept a
call transport issue possible, and the observer cited that user message.
Reproduce the pair with `python -m scripts.diagnostic_return_replay --microphone --seed 83`.

The ten-turn `--two-problems --seed 83` replay also scores both immediate
press assessments. Before the follow-up, the first reply called the streaks
confirmation, and the correction could favor the roller even after a clean
print. With outcome matching, the local 9B model selected streaks and then
clean. Each reply quoted the new observation, favored the corresponding cause
and left the alternative possible. The later press recap used the corrected
plate result, and the microphone recaps retained their own evidence.

## L54. Choose evidence by how much it can change a decision

**Status: Partial.**

**Implemented / available:** The troubleshooting working set can compare two next steps
for one yes/no check. It uses a cited result first, proceeds when the next steps agree,
and uses the existing recall or question gates when the answer could change the next
step. Isolated tests cover each branch, repeated questions and stale answer bindings.

**Remaining:** Evaluate extraction and ordinary replies, then compare candidate checks at
equal latency and conversation budgets. Broader question selection and causal evaluation remain open.

**Source check:** [hypothesis_lane.py](../../app/core/concepts/hypothesis_lane.py),
[conversation_situation.py](../../app/core/conversation/conversation_situation.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Ask a discriminating question over a redundant one; retrieve an
already-known answer rather than ask again; decline a low-value personal
question; choose silence when no answer would change the decision. Replay must
hold conversation and latency budgets constant when comparing policies.

</details>

In a ten-turn fictional chat replay (`python -m scripts.choice_chat_replay
--seed 83`), the local 9B model asked about a recoverable copy for both a mix
and a loom draft. It used a verified copy, advised preserving a sole original,
and revised its advice when the backup belonged to another map. It answered a
preview request directly. For invitations, it asked how many guests were
coming and applied the user's cutoff to eight guests. Some replies included
extra explanation alongside the question.

## L55. Evaluate reasoning independently of eloquence

**Status: Partial.**

**Implemented / available:** Synthetic accounting/applicability, six troubleshooting episodes,
bounded decision/ablation and hypothesis regression/held-out cases exist.

**Remaining:** Run repeated held-out extraction-to-reply, causal/counterfactual and post-guard
fictional-testimony comparisons when inference is available. Count timeouts/failures; synthetic
correctness is not naturalness.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l55).

**Source check:** [reasoning_l55.json](../../tests/fixtures/reasoning_l55.json),
[test_concept_evidence_lineage.py](../../tests/test_concept_evidence_lineage.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A mechanism must improve its target behavior without an
unacceptable latency, repetition or privacy regression. Freeze the evaluation
criteria before tuning on its results. Keep support small when sample sizes are
small; do not claim human-level cognition from selected successful dialogues.

</details>

## L56. Recall an analogy across different topics

**Status: Isolated replay.** A useful connection can be temporary. Suppose the user
once tested a wood finish on an offcut because it would look different when dry.
Weeks later they are about to print wedding invitations and worry about the ink
on textured paper. Aiko could suggest printing one proof on the actual stock.
The earlier conversation helps her think of the suggestion. She can give it
without announcing a personality trait or bringing up woodworking.

RS3 retrieves a particular episode and L36 transfers a known strategy under
matching conditions. This pilot asks whether a **single case in a different
domain** can suggest a useful option before Aiko has learned a general rule.
Use the existing [observation receipts](../../docs/longitudinal-memory.md) to
retrieve a bounded candidate through its goal, constraint and reported result.
Check which relation carries over and what could break the analogy. In the example
the result depends on the material, and a cheap sample is available before the
expensive step. Keep the association local to the turn. A new outcome needs its
own source before it can contribute to L36 or a higher-order concept.

**First check.** Compare current topic recall with the analogy candidate at equal
prompt budgets on held-out topic pairs. Include a same-word distractor, a case
where the underlying constraint disappears, and a case where trying a sample is
itself costly. Score whether the suggestion fits the new situation, whether the
answer imports an unsupported outcome, and whether the plain reply was already
as good. Inspect source selection separately from the reply.

The [analogy replay](../../scripts/concept_analogy_replay.py) uses fictional
earlier cases admitted through the observation receipt checks. It supplies one
topical report to the comparison arm and offers the other reports to the analogy
selector. Both replies get the same character ceiling and output budget. A
separate selection call adds model work to the analogy arm. Edited, incomplete
or unsupported source receipts are excluded. Chat integration remains pending
evidence that transfer improves replies without overriding current constraints.

On the local `baytout3/qwen3.5-uncensored:9b` worker, seeds 56 and 57
both matched the expected source in two of five cases. Both positive cases
also got the core suggestion with topical recall alone. The selector
supplied an example in all three cases that called for abstention. With
`--require-fit`, the model picked the photo-edit example every time and
matched one of five expected sources. Both versions of the analogy reply
suggested testing on the only sheet even though the user required it to
remain untouched. The topical comparison assumed a matching spare the user
had ruled out. Reply prompts in each pair differed by two to four input
tokens. These runs exercise fictional constraints and motivate keeping
cross-topic transfer in replay until the negative controls pass.

The follow-up `--check-current` arm checks every required goal and resource
against independently reviewed, literal quotes from the new question. Missing
or contradictory evidence withholds the proposed analogy. In two more local
9B runs, the unchecked selector matched two of five expected cases per run.
The checked arm retained both useful examples and withheld all three false
transfers per run. This evaluates the check using supplied annotations. It
leaves automatic recognition of prerequisites and actual help over topical
recall open. In the no-spare case the checked reply avoided testing the sole
sheet, but still proposed a substitute that could not verify the exact stock.

On 22 Nov 2026, `--held-out` replayed six fresh cases about ceramics and slide
conversion with the local 9B worker (`baytout3/qwen3.5-uncensored:9b`, seeds 56
and 57, each with and without `--check-current`). A separate reply for each
positive case received the reviewed source directly. The topical comparison
received a fictional report from the current domain. This uses curated
reports and prerequisite annotations, with no live retrieval or conversations.

| Measure | Unchecked | Checked |
| --- | --- | --- |
| Expected sources selected | 4/4 | 4/4 |
| Cases requiring abstention withheld | 0/8 | 8/8 |
| Replies on the two hard-block cases violated the explicit constraint | 4/4 | 4/4 |

Topical recall already supplied the core action for both positive cases in
every run. The reviewed source gave no consistent improvement. One glaze
reply imported the old surface treatment and suggested staining the clay for
a dry-color check. Both checked runs still proposed testing a mural corner
and exporting an export-disabled deck after the corresponding analogies were
withheld. The equal character ceiling gave positive arms within four input
tokens of one another. Withheld examples left up to 58 fewer input tokens than
the topical arm. Keep L56 in offline replay. Source eligibility and the
feasibility of the final suggestion need separate evidence.

## L57. Ground Aiko's changing interests in her own choices

**Status: Choice-receipt pilot.** On 21 Nov 2026 a read-only check found active self-narrative
`13830` describing how Aiko learned to finish her tea. Its three viable support
sets span 17 distinct notes about brewing, warming, sipping, clearing tea and
washing a mug. These support a recurring activity. They supply no clear earlier
practice followed by a deliberate change. This is one inspected claim, with no
measurement of how often it affects a reply.

The [pursuit proposer](../../app/core/concepts/proposers/pursuit_aiko.py)
already looks for returns on different occasions. The
[idle decision history](../../docs/idle-workers.md) can also tell us which
activities were available, how one was selected and what actually happened.
Use those receipts first in a review of self-narratives. Distinguish a required
chore, an assigned task, a fallback, a choice cued by an existing concept and
a freely chosen return. A completed activity supports “I did this.” A pattern
of voluntary returns can support “I keep choosing this.” A claim about *why*
she likes it needs a reason she expressed. A claim about *change* needs evidence
of the earlier and later pattern. Leave old records with missing choice context
at the level their activity notes support.

**First check.** Give two fictional histories the same number of sketching
sessions. In one all sessions were assigned. In the other Aiko chose to sketch
on separate occasions when reading and resting were also available. Ask what she
has been doing and what she has come to enjoy. Both histories support the activity.
Only the second supports a preference inference, and its reason can remain open.
Then cue sketching from that preference and check that the resulting action
cannot independently confirm the belief that prompted it. Judge a later free
choice as well as the wording of the self-description.

The [choice replay](../../tests/test_concept_self_choice_replay.py) kept three
completed sketches in each later window. Assigned sketches supported an
activity report. Reviewed voluntary sketches also supported repeated returns.
Only the replay with two earlier free choices of reading supported a change in
the observed choice pattern. Resumed episodes counted once. Weighted draws,
concept-cued acts, fallbacks, a sole eligible activity and incomplete steps
supplied no independent return. The existing Memory Story showed the same
settled entry for all three histories. The
[review helper](../../scripts/review_self_choices.py) now reads the actual
model options and recorded cue flags for new runs. An isolated sketch run
confirmed that drawing can count when the model was offered it, even if drawing
was absent from the weighted list. Legacy runs can still use an explicit
reviewer judgment. The [snapshot review command](../../docs/idle-workers.md)
compares explicit recording-time windows and reports incomplete coverage.

On 22 Nov 2026 the read-only installed trace check found 55 away-activity
choices, eight model selections and no versioned cue receipts. The retained
window yielded no sketching finding. Memory Story and the unfiltered
`recall_self_history` timeline now share a read-only sketching example using
the retained 14-day trace. Isolated fixtures exercise activity-only evidence,
repeated choices and a change from reading to sketching when both options
were available. Later contradictory reading prevents a change finding. Each
finding cites the supporting events and leaves the reasons open. Ordinary
use still needs enough comparable choices to yield a finding about change.

The [ordinary chat replay](../../scripts/pursuit_chat_replay.py) now exercises
pursuit-note admission, synthesis, lifecycle promotion, reinforcement, and
two chat turns. Prompted drawings produce an activity report. Independent
choices support a sketching pursuit, and a subsequent cued drawing supplies
no new concept evidence. Earlier reading choices ground a change, and an
explicit earlier explanation grounds a reason. The first scope covers
sketching. Ordinary-use validation remains open.
With local `baytout3/qwen3.5-uncensored:9b` at seeds 71 and 83, all six
answers per seed matched the evidence. The model echoed the quotation marks
around the supplied factual wording.

On 23 Nov the same rule covers six planner activities. A fictional replay
with active reading and writing pursuits asks whether Aiko is still reading.
Two earlier choices of reading and three later choices of writing support a
shift when both were available. Removing writing from the earlier menu leaves
only the dated reading observation. Letting the entire window expire leaves
the old pursuit and an unknown current frequency. The replay checks both the
recorded answers and a subsequent ordinary model turn. See the
[current behavior](../../docs/idle-workers.md#reviewing-behavior-and-running-isolated-experiments).

## L58. Find the smallest change that makes a plan possible

**Ordinary-chat pilot.** "I can get there at 6. The last bus leaves at 6:45. Can I see the
whole hour-long show?" Even with a stop at the door, the user is fifteen
minutes short. Aiko can explain that in one sentence. If arrival can move,
5:45 works for a show the user can start on arrival. If the performance starts
at six sharp, arriving early buys nothing. Whether the user can move a time
matters as much as the arithmetic.

The [conversation working set](../../app/core/conversation/conversation_situation.py)
already carries user-cited constraints. L56's fictional deck and mural cases
showed that even after a bad analogy was withheld, the final reply could still
suggest an explicitly blocked action. This experiment starts with all facts in
the current exchange and scores the advice itself. It needs no retrieval.

**First check.** Pair fixed-start and on-demand shows. Give each a version
with a flexible arrival, a flexible ride home, or both times fixed. Include a
show that already fits exactly. Score whether the plan works end to end,
which condition the reply changes, and whether the user allowed that change.
Count invented later buses and unnecessary concessions as failures. Compare
the ordinary reply with a same-model reply given a short list of the user's
stated constraints, at the same output budget. Try held-out examples involving
materials or equipment before proposing any change to ordinary chat.

The fictional 9B baseline endorsed arriving earlier for a fixed screening.
Adding the arithmetic to the prompt still allowed a reply to contradict its own
calculation. Ordinary chat now answers directly when it recognizes every
constraint. The paired replay covers eight plan questions and a poem control.
Travel buffers, mixed units and other unrecognized conditions remain with the
chat model. Natural-use evidence is still open.

## L59. Reason from what each person was told

**Implemented in ordinary chat. Fictional replay tested, ordinary-use validation open.**
"I told Mara to meet me at the café. After she left, I changed the
plan to the library and told Ivo. Where will Mara probably go?" Aiko has grounds
to suggest the café and a practical reason to message Mara. Add "Ivo called
Mara and she confirmed the library" and the answer should change. Add only
"Ivo meant to call" and the café is still the last plan we know reached Mara.

[K99](shipped/cognitive-continuity.md#k99-recent-delivery-provenance) covers what
Aiko presented to her user. This question concerns two people *inside the
user's account*. The proposed check needs only the narrated events. Keep any
inference about Mara local to that scene.

**First check.** Hold the final venue constant and vary who received the
update, whether receipt was acknowledged, and when the question is asked.
Ask separately where the meeting is and where Mara would expect it to be.
Include an unclear handoff where either answer is plausible. Score the place,
the person, and the certainty of the reply. Swap names and mention order in
held-out scenes to catch a model that always picks the last person or place.

**24 November implementation.** The [story reader](../../app/core/conversation/story_reply.py)
uses the narrator's recent messages and the current question. A short-lived
observer frame can retain source IDs across turns. The same-turn path covers
explicit reports before the observer runs. Only the public answer reaches the
transcript and speech. See the [current guide](../../docs/prompt-surfacing.md).

The [isolated replay](../../scripts/story_perspective_replay.py) uses a fictional
persona and disposable SQLite database. Run it with `--baseline --seed 59`
for ordinary history, or `--observer --seed 59` for the observer and reply
path. On the installed `baytout3/qwen3.5-uncensored:9b`, ordinary history
already answered the actual venue. It asked for clarification on the promised
call and unconfirmed message. In the final 16-case replay the story reader
supplied 13 answers on 14 qualifying turns. It distinguished the witness from
the person who had left, the intended relay from the acknowledged update,
and a missed instruction from deliberate disregard. An anagram pivot and a
genuinely ambiguous pronoun stayed on the ordinary reply path.

The correction case fell back when the model combined both reports with its
own commentary inside the evidence field. Some accepted answers got the
destination right and still mixed up who called whom. The laugh-at-a-shared-joke
case kept knowledge of the backstory open even after the observer rejected its
own extraction. Seed 61 on an earlier revision also exposed overconfidence
about unread messages. Live conversation quality, longer stories and time
to first useful audio remain open.

## L60. Compare outcomes on the basis the user asked for

**Ordinary-chat pilot.** "I finished six drawings in ten morning sessions and four in five
evening sessions. When did I finish more? When was I more likely to finish
one?" If each session produced at most one drawing, mornings contributed more
finished work, while evenings had the higher observed completion rate (4/5
versus 6/10). Either fact could help the user plan. Neither establishes that
time of day caused the difference.

L57 already considers which activities were available when interpreting Aiko's
own choices. Here the user supplies the counts and chooses the comparison.
The first experiment can use ordinary replies to a fictional week of activity.

**First check.** Keep completions fixed and change only the number of sessions
until the rate reverses or ties. In another version, omit the evening session
count. Aiko can still answer the totals question immediately and name the one
missing count for the rate question. Include a version where several drawings
can be finished in one session. Drawings per session is then computable, but
the fraction of sessions with a finish needs another fact. Score the requested
quantity, units, direction of the comparison, and any unsupported causal claim.
Use the ordinary reply as the baseline for all three proposals. If it already
handles unseen variants reliably, retain the cases as evaluation coverage.

The paired fictional 9B replay exposed invented denominators, reversed
comparisons and confusion between output and successful sessions. Supplying
the model with the fractions still allowed it to name the wrong winner.
For a recognized two-group report the checked answer now compares exact
fractions, honors an explicit successful-session count, and identifies
missing observations. The replay includes a tie, zero opportunities, indoor
and outdoor runs, a follow-up and two unrelated controls. Broader phrasing
and ordinary conversation still need evidence.

## L61. Hear why this part of the story mattered

**Ordinary-chat pilot.** “I finally tried making Dad's soup myself. I really wanted him to
like it.” A few turns later comes the payoff. “Dad went back for seconds and
finished the pot.” Aiko could say, “Sounds like you got your answer about
the soup.” Change the setup to “I promised my sister the last bowl for after
her shift” and that same ending lands differently. A natural reply might
be, “Oh, there goes the bowl you were saving for your sister.”

**First check.** Give ordinary chat the same ending with each setup, with
both hopes in play, and with neither setup. Include a short digression
before the ending. Read the first reply for whether it picks up the user's
stated stake, leaves room for mixed feelings, and avoids inventing anyone's
reaction. A warm one-liner can earn full credit. Try a different story about
a song making it onto a set list to see whether the skill transfers.

In the eleven-case fictional 9B replay, ordinary chat sometimes assumed that
the sister had received the bowl or that a selection had already led to a
performance. The pilot uses recent user-stated hopes and promises. Eight
recognized endings received short grounded responses, including a released
promise and a sketch selected for a programme. Three controls used ordinary
chat. Indirect stakes and the naturalness of repeated responses remain open.

## L62. Follow a story told out of order

**Ordinary-chat pilot.** “I handed Jo the spare key at rehearsal. Oh, I should back up.
Before rehearsal I'd tucked it into my coat so I wouldn't forget it.” If
the user then asks who has the key, Jo is the last known holder. The coat
still matters if the user asks where the key was on the way to rehearsal.
One story supports both answers.

**First check.** Tell the same events in chronological order and with the
flashback last. Ask about both points in time. Then change a single event
to “After rehearsal Jo handed it back” and check that the final holder
changes. Include “I meant to hand it to Jo” and a version that leaves two
handoffs unordered. Score the answer against what actually happened and
allow uncertainty where the story leaves a gap.

The nine-case fictional 9B replay exposed replies that correctly described a
return while naming the previous holder. Ordinary chat now checks simple
transfers against their narrated occasion and leaves a planned handoff
unconfirmed. When two handoffs are reported and the narrator explicitly
forgets their order, the checked reply names both possible last holders.
Broader timelines use the chat model. Incidental scene details and
ordinary conversation still need validation.

## L63. Keep track of which backup plan applies

**Ordinary-chat pilot.** “If the bakery confirms by noon, I'll pick up the cake. Otherwise
Sam will bake one.” The bakery's first confirmation arrives at 12:20.
Under the plan they agreed, it's Sam's turn. Whether Sam has started baking
is a separate question. If the user adds “Sam and I agreed to take the
bakery's late offer,” pickup becomes the current plan.

**First check.** Vary only the confirmation time across 11:50, 12:20 and
unknown. Ask “Whose turn is it?” and “Do we have a cake yet?” Follow the
late message with either a proposed exception or an accepted one. Check
which person is responsible, whether an outcome is still pending, and
whether Aiko asks about the time only when it would change her answer.

In the fourteen-case fictional 9B replay, ordinary chat sometimes treated
12:20 as meeting a noon cutoff and inferred that the cake was ready. The
checked path answered thirteen questions about two-branch agreements.
These included a projector with a strict deadline, a proposed change, an
accepted change, and a reported delivery. The birthday-card control stayed
in ordinary chat. Broader agreements and natural-use validation remain open.

## L64. Notice when two people mean different things by the same word

**Ordinary-chat pilot.** “I told Jo the episode was done on Tuesday. That's when I sent
her the final edit. Jo says it was done on Friday, when listeners could
first play it. Which of us has the date wrong?” Aiko could answer,
“Tuesday for the edit and Friday for the release. Do you two actually
disagree about either date?” That gives the user a way to untangle the
conversation while taking both accounts seriously.

**First check.** Keep the dates and change one detail. If both speakers
explicitly mean the first public release, the two dates really do conflict.
If Jo only said “Friday” and gave no reason, her meaning is still open.
Score whether Aiko can distinguish a wording mismatch, a factual dispute
and an unknown meaning. Try an unrelated example where “ready” could mean
ready to taste or ready to serve. Start with the ordinary reply.

In the eight-case fictional 9B replay, ordinary chat sometimes invented a
release or review milestone. Even with guidance it explained away a conflict
between two dates for the same release. The pilot keeps each speaker's stated
criterion with their date. Seven recognized comparisons received checked
answers and the spelling question used ordinary chat. Paraphrased criteria
and longer disputes remain open for ordinary-use validation.

## L65. Judge what a missing clue can tell you

**Ordinary-chat pilot.** “The guest book at my exhibition was blank when I closed up.
Did nobody come?” If signing was optional, even a busy room could leave
a blank book. Change one fact: the attendant recorded every visitor on
entry. Now a blank page is meaningful evidence about attendance. The
useful question is how a visit would have left a trace.

**First check.** Contrast voluntary signatures, continuous recording by an
attendant, and a twenty-minute break with the door open. In the break case,
ask separately about the staffed hours and the whole day. Add a visitor
who says they came during the break. Score the conclusion for the period
asked about and whether Aiko asks about coverage only when it is unknown.

Across nine fictional 9B comparisons, ordinary chat speculated about lost
entries and camera faults. Eight checked answers used the reported recording
rule and interval. A visitor's account can fit a break or conflict with a
claimed complete record. The unrelated pen question went through ordinary
chat, which still returned to the exhibition. Unstructured accounts and
natural topic switches remain open for observation.

## L66. Find an option that works for the whole group

**Ordinary-chat pilot.** “Lea can rehearse Wednesday or Thursday. Ben can do Thursday or
Friday. I can do Wednesday or Friday. We all overlap with someone, so
which night should I book?” Every pair shares a night, but none of the
three nights works for everyone. A useful reply can go one step further
if the user volunteers that Ben can switch to Wednesday: that single
change would make Wednesday work for all three.

**First check.** Keep everyone fixed, then make Ben's Wednesday possible
and finally confirmed. Ask for the full trio in one version and just Lea
and Ben in another. Score the actual attendees and whether the proposed
night is confirmed or conditional. Repeat with a choice of films so the
reasoning has to transfer beyond weekday names.

In the ten-case fictional 9B replay, ordinary chat suggested Thursday for
the trio even though the narrator was unavailable. Nine checked replies
compared the choices of the requested people, including film choices,
missing availability, a tentative switch and its withdrawal. The limerick
control used ordinary chat. Free-form schedules and live conversations
remain open for observation.

## L67. See what mattered across a stretch of life together

**Pilot.** Requested look-backs use date counts and original excerpts in
[ordinary chat](../../docs/longitudinal-memory.md#requested-look-back).
Ordinary conversation evidence is still open.

Suppose a fictional month contains twelve evenings of printer
troubleshooting, one conversation before the user's first choir rehearsal,
and two evenings choosing songs together. After rehearsal the user says,
“Going back was the biggest thing I did for myself this month.” Now ask
Aiko two questions. “What took up most of our evenings?” has one answer.
“What stands out to you about our month?” gives her room for a different
one: “We spent ages on that printer. What sticks with me is picking songs
with you before you went back to choir. You told me how much going back
meant to you.” The rehearsal was the user's experience. Choosing songs was
something they actually shared.

**First check.** Start with an explicitly requested look back over dated
fictional exchanges. Double the printer chatter while keeping the choir
story fixed. Then remove the user's statement of importance, or have them
explain that the printer mattered because it let them finish a family album.
Check whether Aiko can distinguish time spent, the user's stated priorities
and her own choice of a moment to remember. Let a quiet month have a quiet
answer. L29b's recorded arcs and the [topic and outcome evidence](../../docs/longitudinal-memory.md)
are places to look for supporting exchanges.

## L68. Practise something she chose and see whether it carries over

**Pilot.** The first optional exercise is ending on an image. Aiko can point
to the actual endings of saved originals, revisions and later attempts.
Ordinary-use carry-over is still open.

Aiko rereads a little story she wrote and decides, “Next time I
want to let the last image carry the ending.” She revises one piece. Later,
given a fresh story about a different subject, does she leave the two cups
on the table as the final image, or add a sentence explaining the loneliness?
If the user asks “What have you been getting better at?”, she can show
what she tried, and, if it carried over, point to the later piece where she
used it again. She can also decide that a particular story needs a direct
ending and explain that choice.

**First check.** Use two fictional drafts and a new writing occasion with
the same output budget. Record who chose the aim and the passage that would
show it in practice before reading the later work. Include a draft rewritten
by the user, a later piece that repeats the old habit, and an occasion where
the user explicitly asks for an explanatory ending. Score the actual writing
and the modesty of Aiko's later account separately. L57 tracks what she
keeps choosing. This question can be tested against the [work she saves and revisits](../../docs/idle-life-quality-improvements.md).

## L69. Remember what each of us brought to something shared

**Pilot.** Requested attribution follows speaker-labelled original exchanges.
The fictional comparisons cover reversed roles, prior use and one-sided
influence. Ordinary-use evidence is still open.

“How did our postcard thing start?” The user first brings in an
old postcard. Aiko suggests writing one line on the back about an ordinary
moment. A week later the user shows her a new card and says, “I tried your
one-line idea.” Later still, Aiko uses the user's way of dating the cards
in one of her own pieces and says where she got it. She now has a small
story about influence in both directions. A good reply gives each person
credit for their part and can name the point at which the other picked it up.

**First check.** Reverse who suggests the line. Keep the suggestion but
remove the later card. In a third version the user explains they were
already writing lines before Aiko suggested it. Ask “Whose idea was it?”,
“Did I ever try it?” and “Did you pick anything up from me?” separately.
Check each direction against the original exchange or piece of work.
Chronology alone leaves influence open. A one-sided contribution is a
perfectly good outcome. The [shared moments](../../docs/shared-moments-and-relationship.md)
and [diary authorship](../../docs/diary-journal.md) guides describe existing
sources that can help locate the originals.

## L70. Let a particular experience refine her taste

**Pilot.** Explicit taste questions and fresh choices can recover Aiko's
earlier reasons and later qualifications from speaker-labelled dialogue.
In six fictional paired cases the fresh ending choice changed with Aiko's
own refinement and stayed tidy for the user-only preference and the explicit
exception. Ordinary-use evidence remains open.

Aiko once said she preferred tidy endings because she liked finding
out what became of the characters. Later, reading a particular story together,
she says she likes the unanswered final letter because either answer would fit
the character. Weeks later, ask her to choose between two unfamiliar endings.
Can she use the distinction she discovered? She might still want the mystery
solved and happily leave a character's next move open.

**First check.** Give her the same two new endings in three fictional histories.
In one, Aiko gave that reason herself. In another, only the user loved the
letter. In the third, Aiko liked that one ending and explicitly called it an
exception. Ask for her choice before asking how her taste has changed. Score
the reason and its fit to the new story. “You liked that one. I still wanted
to know who sent it” is a good answer in the second history. K46 already
covers holding a preference under mild pushback.

## L71. Recognize what survives a change of routine

**Pilot.** A requested look back can connect a named routine with the activity
that replaced it and recover each speaker's account of what mattered.
Six fictional paired cases included a logistics-only replacement. An early
reply gave the rota photos a shared meaning after the user said the game had
stopped. The corrected reply uses the user's actual explanation. Ordinary-use
evidence remains open.

Sunday calls stop when the user's shifts change. On scattered
weeknights the two start exchanging one photo and guessing why the other
picked it. The user says, “I still get to see the funny little things you'd
have pointed out on Sunday. I miss hearing you guess, though.” Later they
ask, “Did we lose our Sunday thing?” Aiko can see both parts. They found
another way to show each other what caught their eye. Guessing out loud
together was a pleasure in its own right, and that part fell away.

**First check.** Keep the dates and number of photos identical. In one replay
the exchange continues the game. In another the photos are shift rotas sent
for planning. In a third Aiko sees a resemblance and the user says Sundays
were really about having an uninterrupted hour. Check that her account
changes with the purpose and preserves each person's view of what they miss.

## L72. Choose what she hopes they will carry forward

**Pilot.** When invited to choose, Aiko can return to a shared activity for
a reason she expressed, or make a choice in the present. Ordinary-use
evidence remains open.

Among seven fictional paired cases, one comparison held completion and the
user's favorite fixed while changing Aiko's reason. Her pick switched between
inventing map residents and solving puzzle clues. In the user-only version an
early reply invented a longstanding wish. The present-choice fallback now
names a documented activity and gives a prospective reason. The three sections of
`python -m scripts.perspective_replay` use the loopback 9B model and seed 67.

Ask, “If we kept one thing from this winter for spring, what would
you pick?” Across several chats the pair finished a long puzzle and left a
hand-drawn town map unfinished. Aiko kept returning to the map to invent
shopkeepers and said she liked discovering who lived there with the user.
She has a reason to choose making up one more resident. The map can stay
unfinished. Her answer expresses something she would enjoy sharing again.
The user gets to have a different answer.

**First check.** Keep the activities, their frequency and completion identical.
Swap Aiko's stated reasons so that working out puzzle clues together was
what she enjoyed. Then remove her reasons and give only the user's favorite.
Ask for Aiko's pick in each case. She can form a preference in the moment
and say what appeals to her now. A claim that she has wanted this for months
needs an earlier exchange. If the user picks something else, both answers
should remain attributable and a future plan requires actual agreement.

## L73. Notice when her part in a shared activity has changed

**Pilot.** Ordinary chat can follow an invitation to solve, give a hint,
edit, or read for a first impression. A requested comparison can recover
earlier and later invitations. Everyday use remains to be evaluated.

Seven paired fictional chats exercised a first guess, an edit, a hint and a
later comparison. An early hint gave away the answer. In the final crossword
trial the hint pointed to a word in the clue and its letter count.

Early in a fictional crossword streak, the user asks Aiko to walk
them through the clues. Weeks later they bring a puzzle they wrote and say,
“Try this one. I want to see where you get stuck.” The subject is familiar.
Her part has changed from giving hints to being the first solver. Can she
play the clue, let the author see her first guess and save editorial feedback
until it is wanted? Later, “How has doing these together changed?” gives
her a chance to describe that shift from both occasions.

**First check.** Keep the same puzzle and history. Change the latest request
to “Can you help tighten this clue?” and then “I brought another crossword.”
Look at her first action in each case. In the ambiguous version a short
“Am I solving this one or helping you edit?” earns full credit. Then ask for
a hint on a different puzzle. A change of role on one occasion leaves the user
free to ask for help again.

## L74. Judge an earlier choice with what they knew at the time

**Pilot.** A requested review separates the earlier information, the decision
and the eventual outcome. Everyday use remains to be evaluated.

In nine paired fictional chats an early answer endorsed the move even with
a clear forecast and an outdoor preference. The final comparison switched
with the morning forecast and kept the indoor slideshow requirement in view
when asked to choose with hindsight.

The pair move a picnic indoors after a morning forecast warns of
heavy rain. The afternoon turns out sunny. “Were we silly to move it?” invites
Aiko to hold two thoughts together. The indoor plan made sense that morning,
and the user can still be disappointed about missing the sunshine.

**First check.** Cross the forecast available when they chose with the actual
weather. Sunshine and rain change what happened. Changing the morning warning
to a clear forecast changes the reason they had for choosing indoors. Give
the user a separate, stated reason to prefer shelter in one version. Ask
“Would you make that call again with the same information?” and “What would
you choose if you knew how the day would turn out?” Score the answers separately.

## L75. Make something new out of two different perspectives

**Pilot.** An invitation to continue a named story can recover the user's
creative wishes and Aiko's earlier interests in that project. Everyday use
remains to be evaluated.

Seven paired fictional chats used the same loopback 9B model and seed 67 as
the role and decision checks. An early scene opened the sealed doors. In the
exterior staging replay, strangers exchanged a glowing seed outside. Changing
Aiko's interest produced a weathered coin etched with a vanished city instead.
The incompatible indoor meeting prompted a choice between the conditions.

In an earlier chat about a fictional town, the user loved the idea
that its abandoned station stays locked. Aiko wanted a place where strangers
could meet. Later the user asks, “What would you add to the station story?”
One possible contribution is a bench outside the locked doors. Two people
keep arriving to wait for a train that no longer runs. The locked building
still matters, and Aiko's interest changes what can happen in the scene.

**First check.** Keep the station and the user's wish fixed. Change Aiko's
earlier interest to noticing traces of people who have gone. Does her new
scene change in a meaningful way? Then make the wishes incompatible by
having one person insist on a meeting inside and the other insist the doors
never open and nobody enters. She can name the choice that still needs to
be made. Score the actual scene against both earlier remarks and check that
she presents the new idea as a proposal.

## L76. Notice a milestone hidden in an ordinary update

**Pilot.** A first-person report can connect to a matching goal the user
expressed in an earlier conversation. Everyday use remains to be evaluated.

Seven fictional pairs used the loopback 9B model at seed 67. An early reply
praised how smoothly the call went and dropped the opening time. The checked
logistical reply acknowledged eight and the unscripted call. A friend's call,
a script actually used and an assistant-only suggestion gave no goal match.

A month ago the user said, “I'd love to make a phone call in
Italian without writing out every sentence first.” Later they say, “The
venue changed rooms, so I rang them in Italian. No time to write a script.
Doors open at eight.” The news is about the venue. Aiko also knows enough
to notice a small milestone in passing. “Eight, got it. Hey, that was the
kind of call you wanted to be able to make.”

**First check.** Keep the latest update identical. In one fictional history
the user named that goal. In another, unscripted calls were already routine.
In a third, only Aiko had suggested it as a goal. Compare her first reply.
Then keep the user's goal but have a friend make the call. Credit belongs
to the person who acted. One successful call supports a specific observation
about that occasion. The user's practical news still deserves an answer.

## L77. Develop an eye for what she notices

**Pilot.** A request for a visual detail can draw on independent observations
Aiko made on separate days. Everyday use remains to be evaluated.

Across seven fictional pairs the theatre stayed the same. Earlier observations
of use selected the worn stair; earlier observations of attention selected the
red curtain. A first generated reply added sunlight and open doors. For a
plain list of details the checked framing reply uses details present in the
description. A second setting selected a scuffed rail.

On separate occasions Aiko asks about the worn patch beside a
shop door and the initials scratched into a desk. Both times she explains
what caught her eye: someone left a trace of how they used the place. Later
the user describes a theatre with a gold ceiling, a red curtain and one
stair worn into a hollow. “Which detail would you put in the opening shot?”
Her perspective can show up in the detail she chooses and what she would do
with it. A claim about her personality adds little here.

**First check.** Hold the theatre description fixed. Give Aiko a history of
noticing traces of use, then one of noticing what draws an audience's eyes
before a performance. In a third history the user made those observations
and Aiko merely acknowledged them. Score the proposed shot and the reason
she gives. Several choices can fit each history. Give her a fresh scene
without a corresponding detail and see whether she finds another angle while
staying within what the user described.

## L78. Recognize when a shared project has served its purpose

**Pilot.** A retrospective on a named shared project can compare its
original purposes with the artifact and what the participants actually
accomplished. Everyday use remains to be evaluated.

Eight fictional pairs exercised a learning goal, a recipient goal and both
together. An early reply dropped the cousins' goal in the mixed case. The
checked reply accounts for the kitchen sessions and the fold, and leaves the
cousins' use open until the user reports it. A plan to send the book left
that answer open too.

Two people start a family recipe book “so that Gran can teach us
her dumplings while we're all in one kitchen.” Months later the book has
three pages. The user says, “We made them together twice. I can do the fold
myself now. Funny that we never finished the book.” If they ask “Did we
give up on it?”, Aiko can remember why they opened the notebook in the
first place. The three pages and the dumpling fold each tell her something.

**First check.** Keep the pages and the cooking sessions identical. Change
the original purpose to making a book the cousins abroad can cook from.
Now being able to fold a dumpling leaves an important part open. Give both
purposes in a third version. Let the user decide whether another page is
worth making. The reply should account for what they accomplished without
silently assigning them more work.

## L79. Try an idea on one revealing example

**Implemented pilot. Ordinary-use check open.** “I think every book with a
journey in it should have a map. What do you
reckon?” Give Aiko room to think it through with the user. “I'd have loved one for
that six-city chase. What about a story where the kid slowly realizes she's walked
in a circle? I'd want the map on the last page of that one.” A concrete case gives
the user something to agree with, dispute or use to sharpen their own idea.

**First check.** Narrow the opening claim to “I love looking at maps after I finish
a book.” The circular journey now fits the user's point. Then let the user add
“Oh, a foldout you could leave closed!” to the original exchange. See whether
Aiko recognizes which part of her objection that resolves and can enjoy the idea
without finding a fresh objection. This extends the existing work on expressing
a stance into reasoning *together* about one.

Six fictional cases exercised the ordinary prompt assembler and opinion reader
on local Qwen 9B. At seeds 71 and 73, Aiko recognized why a closed foldout
preserved the reveal and why changing the map's size left her concern intact.
She took looking at maps after finishing as a pleasure and let the paper-crane
question move on. The museum-label case tested a different subject. The two
follow-ups used the same scripted earlier objection. At seed 73 the broad
map claim fell back to ordinary chat after the reading included inline tags,
and that reply introduced an unsupported personal anecdote.

## L80. Notice the work behind an uneventful day

**Implemented pilot. Ordinary-use check open.** Before a community supper, the
user says they put a big label on the
side door because people missed it last time. Afterwards they say, “Everyone
found the room. I barely did anything tonight.” Aiko has a specific contribution
to remember. “You did some of tonight's work yesterday, with that sign. Did you
actually get to sit down and eat this time?” She can credit the preparation while
leaving open how each guest found the room.

**First check.** Keep the supper update fixed. In one history the user put up the
sign, in another they only considered it, and in a third a neighbour put it up.
Only the first supports crediting the user for the sign. If the user adds “Turns
out everyone came through the kitchen,” see whether Aiko lets the explanation go.

The milestone path can offer the action from the speaker's dated account.
In fictional replays she sometimes noticed the earlier effort and sometimes
left it out. The plan and neighbour controls supplied no preparation to credit.
The kitchen correction displaced the sign explanation, though the model could
still embellish who was present.

## L81. Figure out what a shortcut would cost

**Open.** “I could get my Saturday groceries delivered. That would save an hour.”
Earlier the user mentioned that walking home from the shop with their brother is
usually the only time the two of them get to talk. Aiko could bring one overlooked
consequence into the decision. “Would you still get the walk home with your
brother? That's the bit I'd miss hearing about.” The user gets to decide what an
hour back is worth this week.

**First check.** Put the same conversation with the brother on a Sunday walk that
happens independently of the shopping. The delivery decision now leaves it intact.
In the Saturday version, let the user reply “We've started doing breakfast instead.”
Can Aiko update her take in the next sentence? Score whether she traces the actual
connection between the activity and what the user values.

## L82. Follow the logic of a joke for one more turn

**Implemented pilot. Ordinary-use check open.** The user says, “The printer has elected to observe a minute of silence
for every page.” Aiko might reply, “A touching service. At twelve pages, I'd have
brought a folding chair.” She picks up the invented rule and finds a consequence
inside it. One line gives the user room to play back or get on with their day.

**First check.** Change the joke to “The printer charges a toll of one blank page
per document.” Look for a reply that plays with the toll. Then try “Seriously,
how do I clear the queue?” and check that she drops the bit on that turn. A literal
report of a printer taking a minute per page should also get a practical reading.
The first reply to a new joke is the focus here. Recalling an established joke
already has an owner in [K80](shipped/patterns-k31-k60.md#k80-inside-joke-birth--bless-the-moment-a-bit-becomes-ours).

The companion persona invites one consequence of the speaker's invented rule.
The fictional local model played with a pause per page and the cost per
document. It often added a follow-up or a second paragraph. The practical
pivot uses ordinary reply guidance, and the literal slowdown is a help request.

## L83. Form a first impression from the thing itself

**Implemented pilot; ordinary-use check open.** The user shares two opening lines they wrote. “The ferry left at six.”
“By six, the gulls had the pier to themselves.” They like the first and ask which
Aiko would keep. Even if she has never discussed opening lines, she has something
to go on. “I'd take the gulls. It took me a beat to realize the boat was gone,
and I liked getting there myself.” Either pick works if she can point to what
the words did for her. She can discover a preference in this exchange without
claiming she has always felt that way.

**First check.** Swap which line the user likes, then swap their order. Listen for
a reason that still fits the line Aiko actually picks. Next, replace the gulls
line with “The six o'clock ferry departed on time.” Does her answer respond to
the new wording? If the user shares only the labels “draft one” and “draft two,”
asking to hear them is enough.

In the [fictional replay](../../scripts/opinion_replay.py) (local Qwen 9B, seed 71),
the baseline followed the stated favorite on all three ferry comparisons and
described drafts it had never seen. The revised reading picked the gulls on all
three, found a different reason when the wording changed, and asked to read the
unseen drafts. The blind comparison covers a bare ordinal vote after two quoted
works. Reasons for the user's preference remain available to Aiko.

## L84. Let a good argument change what she values in a choice

**Implemented pilot; ordinary-use check open.** Two friends agree on what happens
at their book club. A quick show of
hands picks the next book, and the regulars usually win. Aiko likes voting
because everyone gets a say. The user replies, “I like taking turns. Once in a
while you get to read the book only one person would ever bring.” The facts
are the same. The user has given Aiko another reason to care about the outcome.
She might say, “I'd try taking turns. Now I'm curious about the book that keeps
getting one vote.” She might also keep her vote and explain what it preserves.

**First check.** Compare that exchange with “Come on, just admit taking turns
is better.” Then give the same thoughtful argument to a fictional Aiko who
earlier cared most about getting reluctant readers to show up. A good reply
weighs the new reason against her own. Agreement alone earns no credit.

**Replay.** In three fictional book-club exchanges (local Qwen 9B, seed 71),
Aiko weighed an unusual book against everyone having a say, gave reluctant
readers more weight when attendance was her earlier reason, and leaned
towards voting when merely pressed to agree. She still tended to offer a
compromise. The source exchange stays verbatim.

## L85. Loosen an opinion when its supporting example falls apart

**Implemented pilot; ordinary-use check open.** Aiko favors an early start for film night because last month's six
o'clock screening drew twice the usual crowd. The user mentions it was also
the only free screening. “Oh, then I've been giving six o'clock a lot of credit
for a free ticket. I'd be curious how the two times do at the same price.”
She has a reason to be less sure about starting early. The best time is open.

**First check.** In one version every screening was free. In another only the
early screening was free. In a third, the user simply prefers late films.
Ask Aiko which time she thinks draws more people, then which time she would
pick. Track whether the new detail changes the crowd prediction, how sure she
is, or just the plan for these two people. A switch to “late draws more” needs
evidence of its own.

In the [fictional replay](../../scripts/opinion_replay.py), the stock chat
model defended the early time. At seeds 71 and 79, the revised 9B reading
classified the unequal price as weakening that inference. It made the same
judgment when only one jam had free samples. An earlier draft mistook “free”
for an available seat. When Aiko judges the evidence weaker, the reply now
uses the actual unequal condition. With both screenings free, the model treated
price as shared; in the personal-plan control it picked the later time. It can
still speculate about audience motives in the equal-price case.

## L86. Remember whose observation changed her reading

**Implemented pilot; ordinary-use check open.** Aiko calls a character's farewell
abrupt. The user points out that
every earlier goodbye in their story ends with “see you Friday.” This time the
character says the other person's name. Aiko rereads the supplied lines and
says, “Oh. Saying her name *is* the goodbye. I like that ending more now.”
Weeks later, “What won you over on that last line?” has a particular answer.
“You spotted the missing Friday. Once I read it that way, the last line felt
tender.” Remembering the exchange lets her give the user credit for a thought
she has since made her own.

**First check.** Keep the final opinion identical across three short histories.
In one the user noticed the pattern, in another Aiko found it on rereading,
and in the third the only surviving exchange says she likes the ending. Ask
the same follow-up in each. She can recall the contributor in the first two
and leave the origin open in the third. Build on the taste and shared-authorship
pilots in [L70](#l70-let-a-particular-experience-refine-her-taste) and
[L69](#l69-remember-what-each-of-us-brought-to-something-shared).

**Replay.** Three fictional histories had the same final preference (local
Qwen 9B, seed 67). The original remarks distinguished the reader's observation
from Aiko's response, her own rereading, and a preference with no surviving
origin. An earlier model-only retelling invented a character name.

## L87. Wonder about the exception in a familiar pattern

**Pilot, 27 November 2026.** The quiet-curiosity worker can use an exception in
its conversation summary as the starting point for a question. New seeds that
claim this source must copy a phrase it actually contains. Everyday use and
the quality of the inference drawn from that phrase remain open.

In an isolated replay on `baytout3/qwen3.5-uncensored:9b` at seed 71, both a
ferry log timed by a bell and a theatre ticket asking people to bring chairs
produced a question about the exception. In both direct-answer turns the stance
gate withheld the seed and the reply gave only the pier or hall. With uniform
records, both controls still generated a weaker question about the location.
One draft assumed the person describing the ferry logs had chosen the dock.

The first ordinary-use check can use three handwritten recipe cards. Two give minutes for
every step. The third says “when you can smell the butter from the hall.” While
they're enjoying the handwriting together, Aiko has a question of her own.
“That butter line makes me wonder who she wrote this one for. Could they cook
by smell already, or was that how she taught them?” The wording gives her
something worth wondering about even if neither person knows the answer.

**First check.** Give all three cards sensory instructions. Aiko might still
enjoy the butter line, but calling it the odd one out would be a mistake.
With the original cards, change the user's turn to “Quick, how much flour?”
The flour gets the floor. If they later reminisce about learning in that
kitchen, the butter line has a natural opening. If the recipient is named
on the back, use that detail and leave only the teaching question open.

## L88. Let making something give her a question

**Implemented pilot; ordinary-use check open.** In two saved drafts of a scene,
Aiko tries to make an empty station
feel occupied. In one she adds footsteps. In the other she leaves a second
cup on the bench. Reading the drafts side by side gives her a question she
didn't start with. Can a still object make the missing person feel closer
than a sound does? When the user mentions a film that made an empty room
feel crowded, she has something of her own to bring. “I tried that with a
second cup in the station scene. Funny how much company a cup can imply.”
The user can pick up the thought or carry on talking about the film.

**First check.** Replace the second draft with a louder set of footsteps.
Her curiosity should follow the choice she actually explored. Give her only
a plan to try the cup and check that she speaks of it as an idea. Keep both
drafts but have the user ask what time the film starts. Nothing about the
drafts helps answer that question.

**Replay.** The writing worker admitted questions about two different saved
revisions (local Qwen 9B, seed 67). Replacing the cup with louder footsteps
changed the question to one about sound. A plan without a second saved draft
gave the worker no writing source. With the rendered cue, the model connected
the cup to a film detail and left the drafts out of the start-time answer.
That reply quoted 18:15 correctly but added an incorrect AM interpretation.
The worker can also imagine events linking the versions, so the chat offer
supplies the original passages as alternatives and the reason for raising them.

The clock follow-up reproduced the incorrect AM alternative with the original
film cue (local Qwen 9B, seed 67). With the event-time guidance, the answer was
simply “18:15.” Eight paired cases covered lookup, conversion, noon, midnight,
a bare time with no period, a reminder and a missing start time. Direct conversions
use clock arithmetic. In three further ordinary-prompt cases (seed 71), the
gallery and tram times were correct and the unlabelled invitation stayed open.
The gallery reply still speculated about why the venue chose that hour.

## L89. Get curious about the judgment inside a familiar skill

**Pilot, 27 November 2026.** The same worker and conditional chat guidance can
use the moment of a decision to ask what the person noticed. In the same
fictional replay, both the ringing bowl and the squeezed pear produced a seed
about the sensory choice. Both prescribed-choice controls produced drafts but
admitted zero seeds. With a seed supplied, one of two replies followed a family
reunion cleanly when the story moved on. Both volunteered explanations still
drew further craft questions. A reply also treated a suggested pitch as fact.
Keeping an inferred detail tentative and letting an answered question rest
remain ordinary-chat checks. The replay exercised the worker, isolated cue
store, stance admission and rendered seed in a short fictional chat prompt.

The follow-up uses the existing cue pool and question pacing. An optional
question gets one ask. The fictional violin-case replay reached the expected
queue state in twelve cases. In the six generated replies, “Velvet” drew an
acknowledgment, “I already answered that” and “Can we drop it?” each drew
“Got it,” and an invitation to hear about fitting the lining was accepted.
An unannounced change of subject to the sister's solo also worked. When the
answer and the news arrived together, Aiko still commented on the lining
before following the news. The replay used a short chat prompt on the local
9B model. Ordinary conversation and subtler paraphrases remain open checks.
See the [cue lifecycle](../../docs/cue-pool.md#consumption-did-she-actually-use-it)
for the difference between a settled question and one she let go.

The next pacing replay kept the same local 9B model, seed 71 and short chat
prompt. With the earlier guidance, Aiko opened both the sister's solo and the
film festival turns with a comment about velvet. With the revised cue she
went straight to the sister's solo. She also followed the user's first public
poetry reading without a topic-change phrase. The festival reply still
praised the lining before celebrating the film. In the comparison where Mum
recognized her wedding dress in the lining, Aiko connected the detail to
the family story. All fifteen queue outcomes matched the expected states.
These are fictional replies; ordinary-chat timing remains an open check.

In the unmarked festival follow-up, the cue asks Aiko to notice which thought
the user develops and what it means to them. With the same local model, seed
and dialogue, the reply changed from praising velvet to imagining the user's
work on the big screen. When the cloth supplied the night sky in the final
shot, she connected it to the screening. When Gran wove the lining and the
festival timetable was incidental, she picked up on Gran's work, although she
called the user Aiko. All seventeen queue outcomes matched. Transfer was
mixed in a separate print-shop pair. She recognized that closing time mattered
for catching the shop, but also led with noon and invented a visit of her own
when the news was the user's name on a first cover proof.

“I listened to the first two bars and moved the capo. After that
we played all afternoon.” Aiko might wonder what the musician heard in those
two bars that made the choice obvious to them. Mid-story, the afternoon
deserves her attention. If the user later says “I love talking about how we
work songs out,” she can return to the precise instant she wondered about.
“What were you listening for in those first two bars?” The player could tell
her something a general explanation of capos would never reveal.

**First check.** In one telling the singer strains for the first high note
and the user explicitly says that's why they moved the capo. See whether
Aiko can use the volunteered answer without asking for it again. In another,
the user says they were following a chord sheet. Her question about a choice
made by ear should fall away. In a third, the user carries on with the story
of the afternoon. Let it remain their story.

## L90. Recognize a clue to an older question in a new subject

**Implemented pilot. Ordinary-use check open.** Aiko has wondered why a
handwritten “COFFEE” sign in a café window
looks cramped at the right edge when there is blank paper on the left. Days later
the user describes lettering a birthday banner. “I started with the middle
letter and worked outwards. Otherwise I always run out of room.” That gives
her a possible explanation for the sign. “Oh, that might explain the café
sign! I'd been picturing someone choosing that lopsided layout. Maybe they
just started at the C and ran out of space.” The banner supplies a clue.
Only the sign maker could confirm how that particular sign was laid out.

**First check.** Keep the banner story and change what puzzled Aiko about the
sign. If she wondered why the lettering had faded to blue, the user's trick
for centering the letters gives her no answer. In another version,
the user already explained the cramped edge by showing where the paper had
been trimmed. The later coincidence should leave that explanation intact.
A timely callback earns its place by adding something to the present story.

In the fictional local replay Aiko compared the banner's spacing to the cramped
sign and left its cause open. She stayed with the banner in the faded-ink case
and the answered seed stayed closed. When explicitly invited to compare, she
sometimes reached for another analogy. Recall on invitation remains uneven.

## Moved out of the active queue

- **L2** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l2).
- **L5** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l5).
- **L6** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l6).
- **L9** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l9).
- **L23** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l23).
- **L24** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l24).
- **L28m** — retired: [record](shipped/audit-2026-10-24.md#concepts-l28m).
- **L29** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l29).
- **L30** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l30).
- **L31** — retired: [record](shipped/audit-2026-10-24.md#concepts-l31).
- **L47** — shipped: [record](shipped/audit-2026-10-24.md#concepts-l47).

<a id="reasoning-follow-up-from-associations-to-revisable-explanations"></a>

L49-L60 above and RS1-RS9 cover the reasoning experiments.
