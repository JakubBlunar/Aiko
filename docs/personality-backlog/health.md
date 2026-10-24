# Health checks of shipped behavior

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

<a id="status-index"></a>

Closed historical H entries: [health archive](shipped/health.md). H2/H8 were retired in this review.
[Recurring failure patterns](shipped/health-patterns.md#recurring-shapes) are reference material.
This H-series is distinct from immersion H numbers.

## H4. The cue shelf produces well and spends badly

**Status: Partial.**

**Implemented / available:** Cue reach/usage diagnostics and unconditional lexical-plus-cosine
measurements exist; sampled misses were not a broken matcher.

**Remaining:** Remeasure producer/renderer usefulness and echo-floor calibration with correct
eligible/shown/used denominators. Do not lower gates merely to turn genuine misses into hits.

[Implemented slice](shipped/audit-2026-10-24.md#health-h4).

**Source check:** [cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[echo_detector.py](../../app/core/memory/echo_detector.py),
[cue_reach_report.py](../../scripts/cue_reach_report.py).

<a id="h7-the-hypothesis-loop-invents-and-never-adjudicates"></a>

## H7. Validate the grounded hypothesis answer loop

**Status: Partial.**

**Implemented / available:** Awaiting-answer handling, passive listen and 23 Oct grounded
target-linked asks/exact reply evidence are implemented, with bounded persisted funnel diagnostics.

**Remaining:** Sample verified ask -> actual answer -> resolver reason -> applied update on a
consistent deployment, including fictional/quoted/unclear negatives. An unscored ask does not prove
an answer was missed.

[Implemented slice](shipped/audit-2026-10-24.md#health-h7).

**Source check:** [hypothesis_expression.py](../../app/core/concepts/hypothesis_expression.py),
[answer_adjudicator.py](../../app/core/concepts/answer_adjudicator.py),
[hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py),
[cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[hypothesis_proposer_worker.py](../../app/core/proactive/hypothesis_proposer_worker.py),
[retire_machine_hypotheses.py](../../scripts/retire_machine_hypotheses.py).

## H11. The prompt tells her what she won't do 4.5× more than how she feels

**Status: Partial.**

**Implemented / available:** Kind-first balancing and generative supply fixes are implemented.

**Remaining:** Compare instruction register/ratio and total volume on matching contexts; retain real
boundaries. Do not pin unrelated affect merely to change the mix.

[Implemented slice](shipped/audit-2026-10-24.md#health-h11).

**Source check:** [concept_view.py](../../app/core/concepts/concept_view.py),
[boundary_user.py](../../app/core/concepts/proposers/boundary_user.py).

<a id="h14-almost-nothing-feels-bad"></a>

## H14. Verify negative-affect concepts reach matching context

**Status: Validation remaining.**

**Implemented / available:** Negative-affect singleton, balanced focus, succession and
topic-evidence boost repairs are implemented.

**Remaining:** Verify a genuinely negative user affective is created and reaches T3 on a
matching-topic turn. Do not mint tender/tired context as dislike or lower global floors to
manufacture a success.

[Implemented slice](shipped/audit-2026-10-24.md#health-h14).

**Source check:** [cluster_affect.py](../../app/core/concepts/cluster_affect.py),
[affective_user.py](../../app/core/concepts/proposers/affective_user.py),
[test_l13_affective_concepts.py](../../tests/test_l13_affective_concepts.py).

<a id="h17-four-prompt-blocks-disagree-about-what-a-lull-is-and-three-lose"></a>

## H17. Calibrate the remaining short-reply gate

**Status: Partial.**

**Implemented / available:** Lull predicate sign/threshold mismatches were repaired.

**Remaining:** Decide and measure absolute-versus-relative short-reply calibration; no demonstrated
shared-predicate defect remains beyond that follow-up.

[Implemented slice](shipped/audit-2026-10-24.md#health-h17).

**Source check:** [topic_appetite.py](../../app/core/conversation/topic_appetite.py),
[test_topic_appetite.py](../../tests/test_topic_appetite.py).

## H34. Echo is a reliable measure, and it is not a measure of him

**Status: Open implementation.**

**Implemented / available:** Echo is attributable reuse; shared next-turn engagement is not per-item
user benefit.

**Remaining:** Establish an item-level user outcome/reaction signal with message-length/confound
controls; coordinate F12/RS9. Reweighting two unsuitable signals does not solve attribution.

**Source check:** [surfacing_outcome_store.py](../../app/core/memory/surfacing_outcome_store.py),
[echo_detector.py](../../app/core/memory/echo_detector.py).

## H35. A third of the surfacing ledger cannot be scored by either signal

**Status: Partial.**

**Implemented / available:** Memory/concept echo and cue usage settlement exist; bounded traces
improve joins.

**Remaining:** Join cue settlement with ledger views and define cluster echo or explicitly label
affinity as weaker evidence. Correct judged/surfaced/settled denominators must survive any change.

[Implemented slice](shipped/audit-2026-10-24.md#health-h35).

**Source check:** [post_turn_helpers_mixin.py](../../app/core/session/post_turn_helpers_mixin.py).

<a id="h44-nothing-has-ever-graduated-and-at-this-calibration-nothing-can"></a>

## H44. Validate graduation evidence after the loop repairs

**Status: Validation remaining.**

**Implemented / available:** Passive second-confirmation listen and graduation exits exist.
Read-only 24 Oct snapshot: 107 hypotheses, 14 ever asked, 3 with a test stamp and 1 stored graduated
row; the old zero-exit headline is stale.

**Remaining:** Verify source-linked independent confirmations and a valid graduated/merged result. A
recorded exit alone cannot close this: an earlier audit found unrelated confirmations. Keep H7
evidence review and post-guard model revalidation; do not lower support bars.

[Implemented slice](shipped/audit-2026-10-24.md#health-h44).

**Source check:** [hypothesis_graduation.py](../../app/core/concepts/hypothesis_graduation.py),
[hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py),
[cue_reach_report.py](../../scripts/cue_reach_report.py).

## H54. `topic_miss` at 95% of eligible declines is not, by itself, a starvation

**Status: Validation remaining.**

**Implemented / available:** Topic-gate/offline reports and newer persisted final-claim/ask/answer
traces separate parts of the funnel.

**Remaining:** Hand-check genuine same-subject near-misses with per-turn shelf/eligibility context.
Separate never-shown, shown/ignored and true semantic misses; a dominant topic_miss percentage is
not starvation.

[Implemented slice](shipped/audit-2026-10-24.md#health-h54).

**Source check:** [hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py),
[topic_gate_report.py](../../scripts/topic_gate_report.py).

## H22-followup. Distinguish a shared ritual from a timing habit

**Status: deferred.**

**Implemented / available:** K73's persistent offered/named ledger prevents repeated announcement.

**Remaining:** Decide and evaluate evidence that makes a repeated calendar slot a shared ritual; do
not lower thresholds to manufacture names. Historical counts are not current.

**Source check:** [shared_ritual_worker.py](../../app/core/proactive/shared_ritual_worker.py),
[shared_ritual.py](../../app/core/relationship/shared_ritual.py).

## H21-followup. Decide the question-style axis

**Status: deferred.**

**Implemented / available:** K13's baseline-relative style signal retains five axes and register
calibration.

**Remaining:** Measure whether the question axis carries useful variation before retaining, retiring
or recalibrating it; do not label a current stream dead from the old snapshot.

**Source check:** [style_signal.py](../../app/core/persona/style_signal.py).

## H20-followup. Fact-check claim selection coverage

**Status: deferred.**

**Implemented / available:** Claim extraction and privacy-scrubbed fact-checking are implemented.

**Remaining:** Measure source-grounded sentence coverage and false-positive search cost, then decide
whether to widen named-entity/measurement/date heuristics.

**Source check:** [claim_extractor.py](../../app/core/memory/claim_extractor.py),
[idle_fact_checker.py](../../app/core/memory/idle_fact_checker.py).

## Moved out of the active queue

- **H2** — retired: [record](shipped/audit-2026-10-24.md#health-h2).
- **H8** — retired: [record](shipped/audit-2026-10-24.md#health-h8).
