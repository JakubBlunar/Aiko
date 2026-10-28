# Concept and pattern rollout, 28 October 2026

Implementation is split into five independently committed phases. Live model
quality will be assessed over subsequent ordinary use; deterministic tests
establish the data contracts, not naturalness.

1. Grounded hunches (L30a / bounded RS2): source-linked recall and a compact
   hypothesis basis with an explicit unresolved point. Existing T3 budget,
   candidate isolation and invented-hypothesis wording remain authoritative.
2. Context-specific patterns (L50): extend source-cited applicability and
   preserve exceptions and temporal validity.
3. Shared shorthand (K106): explicitly adopted, source-linked meanings with
   context, correction and deletion behavior.
4. Learned approaches (L36): outcome-backed strategy transfer with constraints,
   exceptions and source invalidation.
5. Changes in understanding (L29b): temporal concept-change arcs for the user,
   Aiko and the relationship, with source and revision validation.

## Phase 1

Implemented a bounded read-only evidence projection. Recall retains memory and
concept IDs/revisions, source authority, source timestamps, support coverage,
applicability and tracked support validity. Full evidence is capped at 16
excerpts. A grounded hypothesis receives one short basis and an unresolved
point; the selector budgets that extra text without increasing T3 capacity.
Existing hypothesis trace rows record basis IDs and the uncertainty reason.
Stored rationale remains an interpretation, separate from supporting excerpts.

## Phase 2

Scoped communication patterns now accept literal, source-cited activity names
alongside learning, troubleshooting and casual conversation. The existing
proposer, correction, contradiction and T3 paths share these names. A fresh,
compatible situation can resolve a short follow-up; stale situations cannot.
Changed/deleted qualifying sources suppress the rule. Exceptions and validity
windows remain enforced. This extends communication-style learning for both
subjects; arbitrary factual concept scoping remains future work.

Validation: phase 1 focused affected selection passed 197 tests; phase 2 passed
211 tests plus 72 subtests. The import selector reaches shared session hubs, so
these runs explicitly select recall, context-budget, hypothesis, applicability,
contradiction and dependency behavior rather than unrelated UI/voice tests.
`npm run lint` passed for both phases.

## Phase 3

The existing memory extractor can emit explicitly adopted shared terms. Exact
definition/adoption spans and message revisions are validated before writing
through MemoryStore; queued writes recheck sources. Names default to session
scope. Explicit relationship-wide or named-activity scope and ISO expiry are
supported. New adopted meanings supersede earlier meanings in the same scope;
retirement, edited/deleted sources, other users/sessions and ambiguous equal
scopes do not yield a meaning. Definitions are capped at 256 recent records.

Validated definitions compete inside the existing T3 memory budget only when
the user uses the term. They provide referent context and offer no stance or
unsolicited topic. Generic vector retrieval cannot bypass their validation.
The existing concept trace records structured-pattern IDs and actual text
inclusion; the memory outcome ledger records subsequent use. Inclusion and
echo remain different from helpfulness. No additional model call is added.

Validation: 197 affected tests passed, followed by a 144-test focused run
including an actual extractor-to-SQLite round trip. Lint passed.
