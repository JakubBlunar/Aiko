# Strategy, scoped-pattern and lifecycle review — 5 November 2026

This was a read-only review of the running app and its SQLite database. No
worker was forced, clock advanced, threshold changed or personal record edited.
Private snapshots and source excerpts are retained under ignored
`tmp/strategy-lifecycle-review-2026-11-05/`.

## Strategy and pattern evidence

The owner-scoped live diagnostic reports zero stored strategies and zero shared
terms. Strategy schema was offered in nine extraction batches, with no recorded
proposal. Shared-term schema was offered in 49 batches; one proposal was rejected
before rejection reasons were recorded. Its reason remains unknown. These
counters do not establish nine genuine strategy-learning opportunities or a
broken admission validator.

The transcript inspection covered 209 user messages from 28 October onward.
The current schema-trigger vocabulary matched only one user message, where
"worked" was unrelated to a problem-solving outcome. A broader vocabulary search
returned 24 messages, reviewed with context where potentially relevant. Most
were ordinary conversation, intentions or ongoing problems, not reusable
approaches with explicit results and conditions.

There is one plausible outcome-language near-miss: a user described a hardware
problem, an adjustment and a concrete result using "now it's fine" / "not
complaining". Neither the extractor's schema trigger nor the strategy admission
success vocabulary recognizes those phrases. This identifies a useful contrast
case, not proof that a safe, reusable strategy should have been admitted. The
historical extractor's exact packed batch and output were not retained, so its
actual decision cannot be reconstructed. Assistant predictions, enthusiasm and
role-play must continue to fail the outcome requirement.

Four stored scoped communication patterns are active and pass the production
`scope_sources_valid` check. Contexts cover troubleshooting and three explicitly
named activities. All four qualifier sources are older Aiko `self` memories,
recorded in June/July, without direct source-message pointers. Thus the checks
establish intact memory receipts, not independently verified user testimony or
successful transfer during recent conversation. The sample is too small for a
quality rate. Source revision validity and semantic support remain separate.

Owners: [extractor](../app/core/memory/memory_extractor.py),
[strategy admission/transfer](../app/core/memory/learned_strategies.py),
[scope validation](../app/core/concepts/concept_applicability.py),
[aggregate diagnostic](../app/core/session/concept_patterns_mixin.py).

## Lifecycle is running, with two concrete policy gaps

The live worker snapshot showed seven lifecycle runs and no scheduler-reported
errors since process startup. All 12,578 stored concepts had evaluation stamps;
the oldest was approximately 40 minutes before the snapshot. No dependency-due
markers were pending. The debug clock was disabled with zero offset. These
measurements rule out a stopped or never-visiting worker in this snapshot; they
do not certify every transition or historical deployment.

Current stock: 1,895 active, 1,953 candidate, 1,403 dormant, 7,293 retired and
34 contradicted. The stock spans months of history. Confidence decay and
candidate age use engaged time, while the dormant quiet-period expiry uses
calendar time. These units must not be treated as interchangeable.

### Revival takes a weaker route back to active

The dormant and contradicted branches require fresh support and the global
0.60 confidence floor, but do not call the kind-specific promotion gate. New
values and generalizations require at least 0.72, plus their source/age gates.
This is the existing L45 rollout gap, now supported by live-history evidence.

The seven-day event audit (29 October 01:59 UTC through 5 November 01:59 UTC)
contains 552 revival events and 224 initial promotion events. Of 209 revival
events whose preceding recorded lifecycle transition was dormancy, 29 were below
their kind's minimum confidence: 24 values and five generalizations. None of the
224 initial promotions was below its kind's minimum confidence.

The audit imports each production kind gate and supplies deliberately ample
source count and age to isolate its necessary confidence condition. It is not
a replay of historical source validity, engagement anchors or runtime settings.
The event schema does not record explicit old/new status. Retired-to-dormant or
retired-to-candidate revival is intentionally possible, so the additional revival
events below a promotion floor are **not** all counted as faulty activation.

The next bounded change should require the same kind gate whenever a dormant or
contradicted concept returns to active, retaining fresh-support, disproof-review
and dependency checks. It should not relax the initial promotion gate.

### Expiry leaves a large weak candidate population

The configured candidate TTL is 21 engaged days, calibrated at 3,600 engagement
units per day. There are 1,834 candidates older than that threshold. All have at
least two stored distinct sources. Of those, 1,471 have confidence below 0.15;
1,439 also have no reinforcement timestamp since before 6 October.

The cause is explicit in `_is_stale_candidate`: retirement requires both age
past TTL and source count below the global floor of two. A candidate with two
old sources can remain a candidate indefinitely while confidence approaches
zero, even when its kind needs more support to promote. This is a policy gap,
not evidence that the worker failed to visit it. Age alone does not justify
retiring every one of these records; some older candidates have recent support.

The next slice should define bounded retirement for old, weak candidates without
fresh support, preserve recently reinforced and still-developing candidates,
and retain revival on genuinely new evidence. Retirement is not deletion. Do not
bulk-edit the historical population from this report.

Owners: [lifecycle transitions and expiry](../app/core/concepts/concept_lifecycle_worker.py),
[kind gates](../app/core/concepts/concept_lifecycle.py),
[revival support review](../app/core/concepts/concept_revival.py),
[intake cohort report](../scripts/concept_intake_report.py).

## Pickup order

1. Align dormant/contradicted activation with kind-specific gates; test fresh
   support, confidence/source/age failures, disproof review and successful revival.
2. Define and measure stale-candidate retirement with explicit protection for
   fresh evidence, using a dry-run cohort before applying a policy change.
3. Evaluate natural outcome wording with source-linked synthetic contrasts before
   expanding strategy intake. Existing useful-transfer and correction tests remain
   required; one reported result is not a guarantee that an approach works.
4. Review fresh scoped patterns and concepts after normal post-restart use.

No production code changed in this review, so no pytest or lint run was needed.
The findings identify implementable follow-ups rather than close L22/L36/L45.

## Subsequent implementation: revival

Dormant and contradicted activation now calls the same kind gate as initial
promotion. Fresh support delayed by a gate remains pending in the existing
support ledger across evaluations/restarts. A rejected or invalidated review of
the same support is not reopened. The existing bounded disproof review remains
required before activation; historical concepts and source memories are retained.
Validation: 165 affected lifecycle/dependency/evidence tests and `npm run lint`
passed. This addresses the gate discrepancy, not semantic support quality.
