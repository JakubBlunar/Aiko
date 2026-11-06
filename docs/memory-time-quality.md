# Memory data quality and time

## Planning windows

Elapsed plans keep `future_plan`: this denotes the source's intention, not a
prediction that the planned date is still ahead. Maintenance records a bounded
`plan_retirement` receipt with outcome unknown, preserves the original intended
date and grants the existing seven-day retrospective relevance window. Repeated
sweeps do not renew it. Historical plans remain available in archive but cannot
stock new forward questions or pass a queued forward-cue claim. Retrieval says
outcome unknown. Actual action/completion reports remain separate source-cited
records; elapsed time never supplies one. Promise status retains its own lifecycle.

Focused and representative affected checks cover expiry, clockless grace,
idempotence, rescheduling, retrieval, queued cues and concept synthesis. No live
memory was changed during implementation. Historical rows already converted to
past events need source review; their original modality cannot safely be guessed
from their current date/type alone.

## Promise extraction

An exact same-speaker commitment quote still establishes source identity, not
an invented deadline. A separate literal `deadline_quote` is resolved by the
server against that message's timestamp. Unstated, vague and unparseable times
stay unknown; date-only wording cannot acquire an exact clock. The quoted
timing and its precision remain in metadata. Immediate enactment and casual
reassurance are excluded by the extraction contract.

A persisted source receipt and action comparison prevent an unchanged message
from becoming another commitment through paraphrasing, deadline changes or
terminal-status changes. A successful unchanged transcript window is skipped
before model generation. Failed writes/calls retain retry opportunity. New
messages and edited source windows are reconsidered, and explicit different
commitments or genuinely stated new deadlines remain possible. This changes
future writes, without deleting historical duplicates.

## Claim wording and event-time precision

The ordinary extractor now distinguishes a reported paraphrase from a tentative
inference. Lexical backstops reject invented completion, unsupported frequency
and dropped situational conditions. A plan plus a return report does not certify
every planned action. These conservative checks are not a semantic verifier;
source-faithful wording and abstention are part of the model contract.

`event_time_evidence` names a newly offered owner's message and its literal time
phrase. The server resolves that phrase against the original message timestamp.
The model's proposed clock never supplies missing precision. Day-only timing
retains day precision in storage and rendering; unknown occurrence time stays
unknown. Recording age is labelled as recording age rather than event age.
Quoted evidence and timing anchors are rechecked before queued writes and
downstream use. New semantic receipts participate in revisions without changing
ordinary historical hashes. The extractor's base prompt is compacted so the
new checks fit small contexts without displacing every new turn.

## Summary clauses and state freshness

Topic summaries consist of up to four short, source-cited clauses, each scoped
as history, current state, intention, reported pattern, interpretation or world
knowledge and naming its subject. Unchecked aggregate prose is not stored.
Structural admission checks cite only offered sources, preserve source roles,
refuse intentions as history, require explicit recurrence for reported patterns
and decline unsupported current-state wording. Current states require a fresh
ongoing source and cannot ignore a later overlapping resolution report. Their
summary receipt expires within 24 hours without new evidence. This is an upper
freshness bound, not proof a state remained true for that interval.

Whole bounded source records replace clipped prefixes that could lose trailing
exceptions. The source packet and semantic fingerprint are checked after the
model call as before. A format version invalidates old derived-summary caches
once; it does not add an observation or confidence vote. Structured output uses
at least 512 output tokens so citations do not starve the prose. No extra model
call is added. Expired ongoing memories stop presenting themselves as current
in routine retrieval; historical worker views mark current applicability unknown.

## Historical review

`scripts/review_memory_quality.py --output <private-report.json>` opens SQLite
read-only and prepares bounded source-linked review cohorts: repeated commitment
sources, deadlines missing timing receipts, plan wording classified as events,
claim overreach and active personal concepts with incompatible direct sources.
Optional `--concept-ids` selects particular concepts. Flags are nominations for
review, not errors, prevalence estimates or proof that all alternative support
failed. Cluster references inspect the representative, not historical membership.

The local review artifact is ignored under `tmp/`; personal source text is not
copied into tracked documentation. Source-less legacy records stay unknown.
Confirmed source meaning, user corrections and the owning lifecycle APIs should
guide any later repair, preserving originals and avoiding a global confidence or
status rewrite. The review tool never updates live memories or concepts.

## Validation

The representative timing selection passed 459 tests and 81 subtests; promise
consumers passed 78 tests and 25 subtests. The memory/summary/concept selection
passed 733 tests and 94 subtests. Final source-edit, timing, summary and review
checks passed 68 tests, and the review CLI's affected tests passed. Full lint
passed. Subprocess coverage explicitly checks the read-only CLI beyond static
import selection. Tests use isolated stores and controlled model responses;
these results do not certify the semantic quality of future model output.

Restart the backend, then review a fresh source-linked cohort. In particular,
check genuine commitment retention, ambiguous time abstention, missing important
memories and correctly preserved contexts alongside false admissions. Historical
review flags must not be treated as fresh post-restart generation failures.
