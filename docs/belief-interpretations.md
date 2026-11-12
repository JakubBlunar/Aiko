# Beliefs as tentative interpretations

Implemented 5 November 2026, following
[the investigation](../backlog/reviews/belief-quality-proposal-2026-11-05.md).

Beliefs are **Aiko's tentative, situation-specific interpretation of what an
exchange means now**. They can introduce a new possibility or conversational
idea before any concept exists. Requiring grounded concepts for admission would
defeat that purpose. Actual user words anchor the interpretation; they do not
prove the inference. Concepts are optional baselines for avoiding rediscovery,
not prerequisites or corroborating sources. Beliefs do not become primary
evidence for concepts.

## Representation and production

The existing `beliefs` table and mood/opinion API remain compatible; no schema
migration or bulk historical rewrite is needed. New worker rows put the full
interpretation in `predicted_state`, with `interpretation_version: 2` metadata:

- reason, situation, uncertainty/alternative, revision condition and novelty;
- optional conversational response idea;
- one to three exact user-message quotes, IDs and source timestamps;
- session, applicability scope, last genuine support and expiry;
- bounded support-ID bookkeeping and three previous interpretation receipts
  when a canonical subject is revised.

Authored prose must use explicit dates or omit timing. Literal source quotes
retain their original wording, including relative words, beside their timestamps.
The worker uses `format_transcript`, a date anchor and `STORED_TEXT_TIME_RULE`.
Adjacent assistant turns can resolve references but cannot supply user evidence.
Dangling subjects, missing explanatory fields and nontext fields are refused.

One generation proposes zero to two useful interpretations. Empty output is
normal. The worker offers relevant recent readings (up to eight, from a bounded
80-row candidate scan), interests and optional ConceptView baselines in the same
call. Budget packing sheds optional hints before sacrificing recent transcript
evidence. Matching can only reference baseline IDs actually included in the final
prompt. Source quotes must occur in the corresponding actual budgeted message,
not merely somewhere in a fetched window or in another source.

A persisted user/session watermark prevents re-mining unchanged windows,
including after restart. An accepted reading must quote at least one new user
message. Older turns resolve context; appending an unrelated turn does not turn
an old quote into new evidence. Valid empty results advance the processed
boundary; failed calls, unusable-format answers and cancellation do not. Failed attempts remain eligible for
retry despite the scheduler's attempt timestamp. A session/user switch during
generation discards the result.

The old `[[predict:...]]` shorthand could not carry this information. It is no
longer prompted or persisted; compatibility stripping still keeps legacy tags
out of chat and speech. Manual API creation and historical records retain their
legacy representation and remain available for explicit review.

## Meaning, repetition and authority

The extractor compares full meanings and qualifiers, not positive tone or topic
labels. For a paraphrase or fresh support of an existing reading, it nominates
an offered `existing_id` and copies its canonical interpretation, situation,
uncertainty and revision condition. Those fields must match exactly; the store
must still have the same current record. Different wording is thus adjudicated
by the model as the *same canonical reading*, not blindly merged by topic
embedding. Exact canonical-field preservation also works when an embedding is
unavailable or its model has changed. A changed
qualifier is a revision or separate reading, never confirmation of the old one.
Exact repeated full readings also reuse their canonical subject within a batch.

New structured rows bypass the old fuzzy-topic merge. Affection, security and
considering somebody's comfort can remain separate, even with identical topic
vectors. One current reading per canonical `(user, kind, topic)` remains the
store contract; a changed interpretation or scope resets its authority and keeps
three previous receipts. This is a bounded current-understanding store, not an
unlimited historical belief timeline.

Distinct source IDs increase a support-update counter. **That is not independent
episode corroboration or confirmation.** Reprocessing the same support IDs does
not increase the counter or extend applicability. No number of extractions
automatically confirms a reading. Model confidence remains a diagnostic, not an
evidence-strength label. Only explicit review records `confirmation_basis=user`.

Post-turn maintenance no longer adjudicates user mood using Aiko's `AffectState`,
and lexical overlap/antonyms no longer confirm or contradict interpretations.
The old gap type and renderer remain compatible, but this hook only ages records.
Revisions happen through the source-linked worker or user correction; this
implementation does not claim to semantically verify beliefs automatically.

## Applicability and conversation

Default maximum applicability, anchored to supporting message time:

| Scope | Window |
| --- | --- |
| Exchange | 2 hours |
| Situation | 24 hours |
| Explicitly ongoing circumstance | 7 days |

These are conservative initial defaults, not measured optimal durations. Old
unscoped moods have a 24-hour fallback; old opinions use 90 days. Confirmed rows
expire too. Unrelated checks and manual confirmation do not extend support time.
Changing circumstances can restart a scoped reading without a permanent penalty
for an old contradiction.

The existing `trusted_beliefs_block` name remains registered, but it now belongs
to **T6** and consumes the live user query. It offers **no stance**: this is
context for understanding a matching exchange, not an instruction to introduce
a topic or ask a question. It can supply relevant *tentative* version-2 readings;
requiring confirmation first would make new thinking unusable. It preserves
unknowns and revision conditions and says that actual user words take priority.
It never instructs Aiko to assume an internal state or perform repeated insight.
Same-session and lexical relevance gates limit applicability. This deliberately
cheap gate can miss indirect references; ordinary-use review should measure that.

Legacy automatic statuses do not certify old fragments for prompt use. A legacy
row needs explicit new user confirmation and current relevance/applicability to
enter context. Existing outcomes remain historical method counts, not accuracy
scores. No historical outcome is deleted or reclassified by this change.

## Review screen and validation

The default screen lists version-2 interpretations; earlier readings remain
available through an opt-in filter. Filtering happens before database pagination,
and status counts use the same generation filter. At the active-pool cap, older
legacy fragments yield before structured interpretations; excess rows become
stale instead of being deleted. High legacy model confidence cannot suppress a
fresh tentative idea. Cards show the full reading,
why, situation, uncertainty, revision condition, expiry and expandable supporting
words. Model confidence is tucked into diagnostics. Users can correct wording,
situation or uncertainty without a binary verdict; current user corrections are
protected from worker replacement and retain the previous receipt.

Focused regressions exercise replay/restart, failure retry, offered-source and
actor validation, genuine support updates, qualifier preservation, topic-vector
collisions, manual corrections, expiry of confirmed moods, query/session gating,
legacy authority, REST corrections and rendered cards. Broader representative
checks cover prompt assembly/cache ordering, post-turn integration, affect
concepts and response-tag stripping. These are structural checks, not proof that
every generated reading is insightful or justified. After restart, review fresh
ordinary exchanges for novelty, support, restraint, uncertainty and whether the
interpretation actually helps Aiko respond.

Validation completed: 735 representative affected Python tests (8,895 deselected;
7,779 subtests), followed by 120 focused belief tests after final intake/date/cap
refinements; 100 related web tests across ten files; `npm run lint`; and
`git diff --check`. Managed runners cleaned their scratch directories. No live
worker generation or historical-data migration was forced for this change.
