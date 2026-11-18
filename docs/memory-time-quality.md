# Memory data quality and time

## Brain-chosen notes and important conversation details

The brain can write an invisible `[[remember:...]]` note during an ordinary reply,
without a tool pass or an extra inference call. Configuration-stable
`memory_awareness_block` instructions at the end of T0 explain what is worth
keeping: explicit requests, corrections, personal facts/projects, communication
needs, boundaries, agreed signals and Aiko's chosen recurring approaches. This
capability grammar supplies no conversational topic or stance offer and remains
available with a custom persona whenever the store and embedder are present.

The preferred source-backed form is:

```text
[[remember:{"content":"The user finds numbered steps helpful when setting things up.","quote":"Numbered steps help me when we set things up."}]]
[[remember:self:{"content":"I intend to use numbered setup steps.","quote":"I will use numbered setup steps.","temporal_type":"future_plan"}]]
```

User notes quote the user; self notes quote Aiko. The source window is the four
messages before the completed reply plus that reply itself, in the same session.
The host selects the source ID from a literal quotation rather than trusting a
model-supplied ID. The self example therefore requires those words in Aiko's saved
reply or an offered earlier reply. Unsupported/malformed structured notes are
refused; their JSON is never saved as a plain memory. Optional `temporal_type`
uses the existing catalogue; default is `durable`. Relative stored wording and
nested control tags are rejected in structured content. Source quotes retain the
original wording. Two distinct admitted notes may be attempted per reply.

Plain legacy user notes remain provisional; plain self tags still allow a new
personal declaration. A sourced paraphrase retains the conservative inferred,
scratchpad user-memory contract. A quotation proves its source identity, not
semantic entailment or a successful outcome. New receipts retain owner, message
revision and original time, and are rechecked after embedding. Existing memory
and concept consumers own recall, source validation and promotion; capture does
not force a concept or certify that a new practice has been carried out.

Memory tags, including JSON with brackets or newlines, share one grammar across
capture, transcript/stream cleanup and vulnerability accounting. Self-disclosure
accounting sees the note's content rather than its supporting quote or JSON keys.
Incomplete, cancelled or blocked replies remain ineligible for memory learning.
Content-free `app.inline_memory` logs distinguish saved notes and refusal causes.

The ordinary extractor prioritizes useful interaction agreements alongside other
durable information. A user's communication need and Aiko's chosen response are
separate memories with separate owner citations. A preference need not qualify as
a formally adopted shared term or an outcome-backed learned strategy. Suggestions
and trials retain their qualifications. Rejected proposals do not consume the
per-run write allowance and hide later admitted memories.

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
First-person wording alone is insufficient: a hope about present behavior or a
wish to pay attention does not establish an owned follow-up commitment. Timing
must be in the commitment's sentence or an adjacent stand-alone timing fragment;
a countdown for another event elsewhere in the message cannot date the promise.

A persisted source receipt and action comparison prevent an unchanged message
from becoming another commitment through paraphrasing, deadline changes or
terminal-status changes. A successful unchanged transcript window is skipped
before model generation. Failed writes/calls retain retry opportunity. New
messages and edited source windows are reconsidered, and explicit different
commitments or genuinely stated new deadlines remain possible. This changes
future writes, without deleting historical duplicates.
For a single uncoordinated action, the literal commitment quote also participates
in replay detection. Different generated action wording cannot mint another
obligation from the same quote. Quotes containing coordinated actions still need
the action comparison so distinct commitments remain possible.

## Claim wording and event-time precision

The ordinary extractor now distinguishes a reported paraphrase from a tentative
inference. Lexical backstops reject invented completion, unsupported frequency
and dropped situational conditions. Every batch candidate, including an inferred
one, requires a nonempty literal citation to an offered new turn from its owner.
Missing evidence produces a content-free `missing_evidence` refusal in the log;
it cannot bypass fidelity checks or retain a model-authored `event_time`. Such a
refusal is a lost candidate, not evidence of successful semantic abstention.
Inline provisional self notes retain their separate admission contract.

For both deliberate quoted notes and ordinary extraction, a paraphrase rejected
solely for dropping a condition can be replaced by an explicitly attributed,
literal source sentence. The rejected interpretation is discarded. The fallback
keeps the original whitespace, qualifications and owner and passes ordinary
admission again. It is bounded to four quotations of at most 1000 characters and
2000 characters of stored content. Relative wording cannot be copied into the
stored fallback. Proposed supersession, outcomes, relations and event times do
not carry over. Metadata marks `source_note_fallback=lost_condition`. Invalid
ownership, missing citations and other refusal reasons cannot use this fallback.

A plan plus a return report does not certify every planned action. A bare return
does not supply routine details or a new intention; the actual plan/report must
be cited, and a question remains a question. Numeric event clocks require stated
source timing, with equivalent 12/24-hour representations allowed. Recording
timestamps do not supply missing clocks. These conservative checks are not a semantic verifier;
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

Past-event weekday phrases resolve backward from their source. A bare weekday
can refer to the source day. Explicit “last Thursday” always refers to a
previous Thursday. Recall packets expose a day-only value as `event_date`
with day precision, give original messages their own sent times, and keep
`recorded_at` for the memory write. An unqualified legacy midnight does not
supply a clock time. An explicitly supported midnight retains minute precision.

## Original exchanges and reported outcomes

On recognizable questions or corrections about a past conversation, the
relevant-context builder first checks retained messages in SQLite. It reads at
most 400 rows from the user's own sessions, within the referenced date window
or the recent 14 days. Overlapping words in user messages and nearby dialogue
identify up to two likely exchanges, which are presented with actual local
message times in the existing prompt budget. The lookup uses no model call.
The trace records the original message IDs that fit. A bounded lookup can miss
paraphrases and older exchanges, which are still searchable through
`conversation_history` and semantic recall.

Routine retrieval gives a small ranking advantage to original user messages
and to past-event reports with current user-source receipts. A validated
conclusion linked to a prior plan weakens that plan's rank when both are
candidates, and the prompt labels the old entry as an intention with a later
report. This preserves distinct plans and their history. Second-thought cues
decline obvious unsupported clocks or parts of the day copied from the
assistant's reply, and the surfacing instruction asks for original evidence
before repeating factual details.

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
