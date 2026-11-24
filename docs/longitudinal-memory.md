# Longitudinal memory evidence

This implements the four follow-ups in [memory-topic-correctness.md](../backlog/reviews/memory-topic-correctness.md).
These records enrich the basis of an interpretation; they are not confidence votes.

For a fresh shared name or a reported strategy, the extractor can propose
message IDs with literal terms or spans. Ollama can constrain these fields in
the same call as ordinary memories. The worker expands them using the messages
in its packed transcript and checks adoption, speaker, goal, constraints and
reported outcome before writing. A naming agreement alone supplies no booking.

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

The dated synthesis packet gives each offered focus topic space for an example.
It favors current source-backed user reports, postpones repeated notes from the
same message, and includes valid linked intentions and outcomes together where
they fit. Topic recall and personal-topic expansion favor those reports among
nearby semantic matches. External research remains searchable on its own merits.
This evidence selection works within the existing topic assignments.

## Topic digest recall

Ordinary recall, cluster expansion and the recall tool use the stored clause
receipt from the canonical digest. Each displayed clause cites living memory
IDs with the revisions recorded at generation. Eligibility and quote receipts
are rechecked on those sources. An edit outside a clause's citations leaves its
support intact. A stale citation removes its clause, and an expired digest
validity window removes the whole digest from recall.

The recall context labels each complete clause by subject, scope, source domain
and cited memory IDs. These are derived summaries. Older digests without clause
and revision receipts remain in storage and are withheld from recall. Edited
digest wording also needs a matching receipt to be eligible. When a
cluster digest has no usable clause, expansion can spend its usual sibling
budget on source memories. Digest validation uses the memory mirror and existing
receipts and requires no new model call or migration.

## Source-backed situations and outcomes

The ordinary extractor can attach optional `observation` receipts for a context,
action, explicit reported outcome and exceptions. Every field cites a literal
span from the memory's actual owner. Actions and outcomes use newly offered
turns. The packed earlier context can supply a qualification or exception when
the fresh evidence quotes an action or outcome about the same subject. The
extractor checks the link using shared topic words and requires the saved note
to retain the condition. A bare assent or a topic match alone supplies no new
action or outcome. Earlier facets carry `context_only` and stay separate from
the new evidence IDs and any claim about when the reported outcome occurred.
Quotes retain their source timestamp and role. Unknown facets remain absent; an empty object
does not manufacture context. Outcome quotes preserve their full clause,
including qualifications and negation; predicted benefits do not count.

Receipts are checked before and after embedding during queued writes and when
new concept evidence or topic-summary inputs are admitted. Source edits/deletion
invalidate them. Direct and topic recall recheck notes that carry earlier
qualifiers and show their verbatim facets while the sources are current. Literal
fallbacks keep any observation receipt that passes the same admission checks.
If an optional observation fails validation, the writer drops its facets and
checks the literal note again before saving it.
The original qualifiers remain in content and are also rendered in memory source
annotations. Differently scoped facets cannot collapse through cosine dedupe or
consolidation. The semantic revision includes these fields only when present,
so ordinary legacy hashes do not all change on upgrade. This adds no confidence
formula, model call, or compulsory extra memory.

## Reported follow-through

The extractor can link a new report to one of eight explicitly offered intentions.
It ranks intentions by overlap with new messages and nearby context from the
same actor, with recency as a tie-breaker. An independently supported memory
survives a refused or stale optional link. Optional follow-through preserves
the earlier intention.
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

Current guidance loses support from resolved or elapsed intentions and from
expired, uncited legacy episodes. Historical narratives can still cite those
episodes. The live support check keeps independently supported alternatives
usable, and the existing lifecycle owns any status change. New linked reports
and corrections schedule a review of dependent concepts. Multiple valid
memory notes citing the same message count as one recorded support group in
the lifecycle assessment.

An explicit source review can withdraw a premise from one concept through
`DependencyLedger.withdraw_premise`. The original evidence and source history
remain inspectable. Any support set that needs the withdrawn premise becomes
ineligible. Synthesis cannot reinforce the same concept with that premise, and
duplicate consolidation carries the withdrawal into the survivor. An unrelated
concept can still use the source. No automatic semantic review is performed.

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

## Learned approaches in troubleshooting

An outcome-backed strategy is eligible when its goal and all constraints match
and its exceptions are absent. The newest matching report controls reuse of
the approach. A failure or a changed source withholds the approach within that
report's scope. A short follow-up can use the goal and constraints from a fresh,
active troubleshooting working set, including a chat-only task with no world
anchor. A suspended or conflicting working set supplies no inherited context.

## Recalling a particular episode

For supported retrospective questions, ordinary chat checks the retrieved
memories for a current, user-reported episode that matches the named subject.
A short follow-up can borrow the subject from recent conversation. The selected
evidence includes the action and latest reported result, including any remaining
problem. A verified correction replaces the earlier result. When several
episodes fit equally well, Aiko is prompted to ask one short question. She can
use a named subject to retrieve the episode on the next turn. Once the match is
verified, the reply model receives the identification and original question as
one request. A second clarification can carry the request forward while the
original question is in recent history. The transcript retains the original
messages.

Episode recall reuses the existing retrieval candidates and context budget.
Source checks exclude virtual, transient and other users' episodes. Restricted
wording, missing retrieval candidates and unavailable sources can limit recall.

## Requested look back

A question about what mattered or stood out across a week or month can draw on
dated user messages across that owner's remembered conversations. The lookup
counts separate chat dates mentioning a word and includes excerpts about shared
activities and personal significance. These counts describe conversations.
Only the speaker can establish how much an experience meant to them. The scan
uses up to 1,500 recent messages in the requested window, or the last 30 days
for an unspecified recent period. The answer is assembled on request.

## Tracing a shared idea

Questions about how a shared activity started, who suggested an idea or who
tried it can retrieve the original, speaker-labelled exchange and later
reports of uptake. The lookup searches up to 1,500 recent messages over
180 days for the current owner. A short follow-up can use the activity named
in the preceding user turn. The answer can credit each contribution separately
and leave uptake open where the conversation only records a proposal.

## Applying a past taste to a fresh choice

When asked which alternative she prefers, Aiko can retrieve her own relevant
remarks from remembered dialogue over the preceding 180 days. The current
reply receives the earlier reason and recent qualifications, including an
exception. The fresh choice is made in chat. An explicit question about how
her taste changed can use the same evidence.

## A shared routine after a change

A direct question about losing or keeping a named routine can retrieve the
earlier exchange and the speakers' later accounts. A remark describing a
change can lead to the new activity by name. The reply can distinguish a
shared purpose from a similar schedule, and the two speakers can miss
different things.

## Choosing something to carry forward

An explicit invitation to pick a shared activity for the future can use
dated descriptions of time together and Aiko's own reasons for enjoying
it. Calendar windows constrain the search. A season name uses the speakers'
own references to that season and related activity. If only the user gave a
reason, Aiko can make a present-day choice. Both people can prefer different
things, and a pick by itself leaves plans open. When there is no recorded
reason from Aiko, the reply can explore one documented activity as a present
possibility. It favors an activity named in the question, then a recent one.

## Changing parts in a shared activity

For puzzles and creative writing, Aiko can act on the part requested in the
current exchange or clarify a bare introduction. On an explicit question
about how her part changed, she can compare original user invitations from
remembered conversations over the past 180 days.

## Judging a past choice

When asked to judge an earlier decision, Aiko can look for original remarks
about what was expected, the choice itself and the reported result. A later
retelling of an expectation keeps its timing qualified. The answer can use
the information available at the time or reconsider the choice with the
outcome known, depending on which question the user asked.

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
