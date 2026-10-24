# Higher-order concepts and reasoning

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

Canonical contracts: [integration](../concept-integration.md), [lifecycle](../concept-lifecycle.md),
[hypotheses](../hypotheses.md), [selective-context proposals RS1-RS9](reasoning-surfacing.md). One
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

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l22).

**Source check:** [concept_quality.py](../../app/core/concepts/concept_quality.py),
[concept_snapshot.py](../../app/core/concepts/concept_snapshot.py),
[identity_user.py](../../app/core/concepts/proposers/identity_user.py),
[concept_lifecycle.py](../../app/core/concepts/concept_lifecycle.py),
[concept_sweep_unreinforced.py](../../scripts/concept_sweep_unreinforced.py),
[concept_intake_report.py](../../scripts/concept_intake_report.py).

## L29b. Meta-narrative -- an arc whose steps are concepts

**Status: Open implementation.**

**Implemented / available:** Memory/moment-backed narratives and generalization/depth machinery exist.

**Remaining:** Implement a source-linked temporal arc over concept changes, with alias/revision
handling, lifecycle evidence and bounded rendering. A generalization is not a meta-narrative.

**Source check:** [concept_view.py](../../app/core/concepts/concept_view.py).

<a id="l30a-hypothesis-surfacing-lane-the-read--render-side"></a>

## L30a. A grounded reason for hypothesis uncertainty

**Status: Partial.**

**Implemented / available:** T3 hypothesis surfacing, importance/unsettledness ranking, origin split
and question-budget coupling exist; L30b/c shipped.

**Remaining:** Expose a concise source-grounded reason for uncertainty. RS2 owns evidence-preserving
recall; H7/H44 own actual ask/answer quality.

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

**Status: Open implementation.**

**Implemented / available:** Self-correction styles, L50 applicability and L51 working-model state exist.

**Remaining:** Pilot source-linked strategy transfer with
situation/constraints/approach/result/exception evidence and held-out contrasts. Engagement is not
success; L52 must invalidate unsupported reuse. New kind/schema depends on need.

**Source check:** [concept_kinds.py](../../app/core/concepts/concept_kinds.py).

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

**Status: Open implementation.**

**Implemented / available:** Aiko/user concepts, relationship axes and correction/surfacing evidence exist.

**Remaining:** Design a revisable contextual model of received behavior, resolve overlap with axes
and guard negative spirals/reassurance fishing. Silence/latency alone cannot establish another
person's appraisal.

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

**Remaining:** Complete staged lifecycle/generative rollout: align dormant/contradicted revival with
kind gates, validate write-gate changes, then consider per-kind floors, coupled cosine bands and
relevance telemetry. Global-floor revival branches remain.

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

**Remaining:** Verify grounded depth-2 concepts actually reach T3 and inspect representative label
quality. Structural depth tests do not establish useful abstraction.

[Implemented slice](shipped/audit-2026-10-24.md#concepts-l46).

**Source check:**
[concept_synthesis_worker.py](../../app/core/concepts/concept_synthesis_worker.py),
[concept_meta_depth.py](../../app/core/concepts/concept_meta_depth.py),
[base.py](../../app/core/concepts/proposers/base.py),
[generalization_user.py](../../app/core/concepts/proposers/generalization_user.py),
[generalization_aiko.py](../../app/core/concepts/proposers/generalization_aiko.py),
[concept_dedupe.py](../../app/core/concepts/concept_dedupe.py).

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

## L50. Context-scoped claims and temporal validity

**Status: Partial.**

**Implemented / available:** Bounded communication-style applicability has source/context admission,
correction/consolidation/lifecycle guards and synthetic contrasts.

**Remaining:** Evaluate model behavior, arbitrary contexts, erased quotation framing, scoped
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

**Status: Open implementation.**

**Implemented / available:** L33 alternatives, L51 working state and L52 support viability exist.

**Remaining:** Trial competing mechanisms with assumptions, differing predictions and a permitted
disconfirming check; revise failed explanations and evaluate matched contrasts.

**Source check:** [introspection_worker.py](../../app/core/proactive/introspection_worker.py),
[conversation_situation.py](../../app/core/conversation/conversation_situation.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Matched scenarios with the same correlation but a different
mechanism lead to different predictions. A failed prediction revises the
explanation rather than producing a post-hoc excuse. No irreversible tool
action, personal experiment or intervention occurs without normal permissions.

</details>

## L54. Choose evidence by how much it can change a decision

**Status: Open implementation.**

**Implemented / available:** Uncertainty/importance ranking and bounded premise recall exist.

**Remaining:** Rank checks by discrimination and decision value, including retrieval/wait/silence;
compare equal latency/conversation budgets rather than generating more questions.

**Source check:** [hypothesis_lane.py](../../app/core/concepts/hypothesis_lane.py),
[conversation_situation.py](../../app/core/conversation/conversation_situation.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Ask a discriminating question over a redundant one; retrieve an
already-known answer rather than ask again; decline a low-value personal
question; choose silence when no answer would change the decision. Replay must
hold conversation and latency budgets constant when comparing policies.

</details>

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

L49-L55 above and RS1-RS9 own the remaining reasoning experiments.
