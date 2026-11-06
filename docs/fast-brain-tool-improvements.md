# Fast brain tool improvements

Implementation of the [6 November audit](fast-brain-tools-audit-2026-11-06.md).
Historical stores remain authoritative and are not bulk rewritten.

## Qualified recall and exact source inspection

Fast recall retains subjects, source mode/domain, recorded versus event dates,
provenance, identity/revision and source-state qualifications. Whole bounded text
is preserved; oversized text is omitted explicitly rather than sliced past an
exception. Relevance is labelled as ranking rather than confidence.

`inspect_memory` reads an exact owned memory with source context. Long memories
have revision-bound character pages; original messages are included whole when
small, otherwise linked directly to the existing archive reader. This does not
write, reconfirm, or turn interpretations into observations. Foreign owned rows
and original messages are not exposed.

Topic recall reports one-cluster coverage, totals, withheld records, alternative
clusters and revision-bound continuation. Withdrawn/changed/out-of-validity rows
are excluded by default; explicit historical inspection returns their status.
Embedding/search failures are distinguishable from a completed no-match, including
partial multi-source search. Concept lookup adds subject and exact-ID selection,
keeps bounded rationale/evidence whole or marks it omitted, and correctly describes
the wider evidence mode as capped rather than exhaustive.

Focused first-slice checks: 176 tests passed; `npm run lint` passed. Ordinary model
selection and wording still need post-restart use.

## Natural-request reachability

The gate recognises explicit grounding questions, relationship-change questions,
background-work status and file-action requests advertised by the tool schemas.
Tests check the selected schemas as well as whether the decision pass runs.
Inline creative writing and ordinary statements still skip the added work routes;
information-need recall retains its read-only subset with the router disabled.

Focused routing/runner checks: 120 tests and 6 subtests passed; lint passed.
