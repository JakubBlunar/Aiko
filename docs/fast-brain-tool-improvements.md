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

## Work status and persisted results

`check_my_work` now supports an exact owned task ID and a bounded recent-history
list, including completed and failed tasks. It returns saved status, dates,
result/error and pending input; long JSON details use revision-bound pages.
Inspection never starts/restarts work. A failed active-task read raises an
unavailable result, disabled tasks are explicitly unavailable, and failed
capability-gap reads make the response partial. An empty active list cannot be
used as evidence that an earlier task succeeded.

Focused task/tool checks: 47 tests passed; lint passed.

## Narrative time and current capability guidance

Explicit-zone time reads convert the same `timephrase` instant as default reads,
including a shifted debug clock. The world snapshot schema directs file work to
the available background workflow rather than the retired `list_file_roots` tool.
Focused time/world checks: 79 tests and 9 subtests passed; lint passed.

## Current interpretations and relationship history

`recall_beliefs` reads current, context-relevant structured interpretations from
BeliefStore. It keeps the situation, uncertainty, revision conditions, expiry and
cited user words, and rejects missing/changed quoted sources. It does not invent,
confirm or write a belief. Existing receipts lack full-message revision hashes;
the output explicitly marks that limitation and includes bounded original context
or source IDs for archive inspection. A still-present quote is not proof the
surrounding conversation was unchanged.

`recall_self_history` now accepts relationship/world subjects and literal query or
concept-ID focus. Its new modes browse the separate evolution diary, open an exact
historical paragraph/period, or inspect a linked learning event. They preserve
dates, event/concept IDs and thin-record semantics; an understanding change is not
proof that the user or relationship changed. Unknown ownership is withheld when
the historical concept cannot be resolved, and known foreign ownership is blocked.
Read failures remain unavailable rather than fabricated empty history. Long work
and history records share revision-bound JSON paging.

Focused understanding/history/belief/work checks: 243 tests and 3 subtests passed;
lint passed. The updated offline audit probes pass all 18 targeted scenarios,
which establishes these local contracts rather than a model accuracy rate.
