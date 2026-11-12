# Longitudinal memory evidence

This implements the four follow-ups in [memory-topic-correctness.md](../backlog/reviews/memory-topic-correctness.md).
These records enrich the basis of an interpretation; they are not confidence votes.

## Topic evidence profiles

`topic_evidence_profile` is a bounded read model over current eligible topic members.
It separates note count, recorded observation groups, actor/source-role mix, unknown
lineage, questions, recording dates, observed event dates and planned dates. A group
is recorded source separation, not proof of independence, truth or recurrence.
Plans and Aiko's reflections do not supply observed event recurrence. Missing and
truncated inputs remain visible. Question resolution is explicitly not assessed
until a real resolution receipt exists.

Profiles are exposed in topic-graph snapshots and appended to the actual focus
packets in concept synthesis, including reconsideration. They are context, not
additional citeable evidence, and do not change promotion gates or confidence.
No model call or live-data rewrite is needed to compute them.

## Source-backed situations and outcomes

The ordinary extractor can attach optional `observation` receipts for a context,
action, explicit reported outcome and exceptions. Every field cites a literal
span in a newly offered message from the memory's actual owner. Quotes retain
their source timestamp and role. Unknown facets remain absent; an empty object
does not manufacture context. Outcome quotes preserve their full clause,
including qualifications and negation; predicted benefits do not count.

Receipts are checked again before queued writes and when new concept evidence
or topic-summary inputs are admitted. Source edits/deletion invalidate them.
The original qualifiers remain in content and are also rendered in memory source
annotations. Differently scoped facets cannot collapse through cosine dedupe or
consolidation. The semantic revision includes these fields only when present,
so ordinary legacy hashes do not all change on upgrade. This adds no confidence
formula, model call, or compulsory extra memory.

## Reported follow-through

The extractor can link a new report to one of eight explicitly offered intentions.
The target is snapshotted before generation, removed from admission if its context
did not fit the prompt, and rechecked before persistence. The report must come
from the same actor, name the specific action and preserve its literal clause.
Known original message IDs must precede the report. Unknown original provenance
is marked as unknown rather than supplying an invented chronology.

States distinguish planned, reported action, reported completion, cancellation,
changed plan and explicitly unknown. Started/tried cannot become completion;
predictions, negated completion and an ambiguous "did it" cannot close a plan.
Original intentions are retained unchanged. The latest valid source-backed report
is a read model over indexed links; an edited report becomes unknown. These are
reports, not independent verification of execution or causal progress.

Topic profiles and source annotations expose those distinctions. Follow-up and
forward-curiosity production skip explicitly resolved old plans, and queued
forward questions cannot bypass that check even through a forced pool claim.
There is no extra model call, new proactive question or automatic promise
fulfilment based on elapsed time. Indexed reference lookup avoids rescanning the
whole memory table for every intention.

## Context changes and scoped concepts

Optional source-cited links distinguish corrections, explicitly changed
preferences, different contexts and counterexamples. Only a newly offered
message from the same actor can qualify a specific, snapshotted existing claim.
Both records are retained. A different situation or single contrasting episode
cannot supersede the original or turn into a universal opposite trait.

An explicit correction or reported preference change asks for review of the
linked basis. Existing dependent concepts stop being assertable on that basis;
the lifecycle worker still owns concept status. This does not mark the old
memory false, invalidate neighboring claims or automatically create a replacement.
Edited/deleted receipts and changed linked sources are treated conservatively
as unavailable support. Bounded, indexed lookups avoid full-table relation scans.

All personal concept proposers receive a bounded packet of dated sources,
including explicit contrasts and original claims. Source revisions are checked
again after generation. A conditional interpretation can retain literal,
source-cited contexts and exceptions through the existing applicability ledger,
beyond communication styles. Unsupported scope is rejected. Distinct scopes
cannot merge or reinforce through cosine similarity; surfacing checks current
scope and source validity, and frames an unknown context as a possible reading.

Topic dirtiness includes semantic source fingerprints, so new details can prompt
reconsideration even when topic labels and sizes stay the same. Ordinary legacy
support hashes remain stable; a new packet is not a new observation or confidence
vote. These changes add no model call and do not rewrite historical live data.

## Validation

Paired checks cover missing/unknown provenance, shared observation roots,
source edits and deletion, queued writes, hypothetical and negated outcomes,
ambiguous completion, same-actor links, different contexts, explicit corrections,
scope-preserving dedupe and rendering, and source changes during generation.
SQLite round trips verify extractor admission, dependency review, queued cue
suppression and indexed link lookup. Representative affected checks cover the
memory-to-concept path; lint covers Python and frontend types. All test state is
isolated. These checks verify behavior, not the semantic quality of future model
output; review a fresh post-restart cohort before changing promotion thresholds.
