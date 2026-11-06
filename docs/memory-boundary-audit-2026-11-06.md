# Remaining memory boundaries: source, subject and time

Read-only review on 6 Nov 2026, after the goal, shared-moment and dream changes.
The next useful work is preserving meaning across secondary writers and recall.
Adding more generated memories or concept context would not repair these gaps.

This is a source audit with a small historical SQLite check, not an assessment
of post-restart worker quality. No worker was forced, no model was called, and
no historical row was changed. The existing L22 ordinary-use review remains open.

## 1. Recall drops the source role even when storage knows it

**Priority: first.** `RagRetriever.format_block()` routes only `self` and
`self_tagged` to the self section. Other direct memory hits go under
"What you know about <user>". It does not route by `memory_origin`; associative
expansion bullets also lose those distinctions. `RagHit` does not carry the
origin contract, although retrieval already joins other fields from SQLite.
Knowledge and curiosity suffixes help with their intended use, but do not
correct the enclosing subject label or preserve a virtual domain.

A read-only probe passed three existing rows to the actual formatter: image
memory 2719, hypothesis answer 10946 and pursuit note 11095. All appeared in the
user section. The pursuit note resolves correctly to Aiko / virtual event /
virtual in `memory_origin()`, yet the rendered line retained none of that scope.
This verifies formatting given selected hits; it does not prove that a real
chat selected or acted on those rows.

**Improve:** carry authoritative SQLite origin into the hit and preserve
subject, source mode and domain in direct and associative recall. Use explicit
unknown source meaning for unresolved records. Keep recording age distinct from
event occurrence. Do not turn reflective derivations into new observations.

**Acceptance:** user reports, Aiko intentions/reflections, virtual activities,
world knowledge and unknown records remain distinguishable through normal and
topic-expanded recall. A virtual breakfast never appears as the user's event.
Source admission and selection budgets retain their existing behavior.

Sources: `app/core/rag/rag_retriever.py` (`format_block`, SQLite hydration),
`app/core/rag/rag_store.py` (`RagHit`),
`app/core/memory/memory_origin.py`.

## 2. Hypothesis answer memories include more than the supported answer

**Priority: next.** `_store_hypothesis_answer()` already requires a literal
answer span and retains its message, question and answer-event receipt. However,
it embeds the full hypothesis alongside that span in an ordinary `fact` memory.
This introduces Aiko's proposed interpretation into the searchable evidence text,
including when the reply contradicts it. The stored metadata distinguishes only
confirmation from a generic response. The current writer also uses answer receipt
time as `event_time`, without classifying the quoted claim's temporal scope.
Its default origin is a user observation regardless of the target's subject.

Historical row 10946 contains a narrow reply plus a substantially broader
behavioral interpretation. Its source quote is genuine; that does not validate
all the surrounding interpretation. This is a scope problem, not missing quotes.

The world-graduation exit is a second boundary: `_anchor_world_hypothesis()`
receives only a statement and writes it with `source=hypothesis`. That source
already resolves to world knowledge, but loses the hypothesis ID and support
receipts from the resulting memory. `_anchor()` can close graduation even when
the memory write fails. No world-anchor rows were found in the inspected DB.

**Improve:** store the bounded reply with its question/target context and exact
verdict; keep proposed interpretations as interpretations. Preserve target
subject, reported versus intended/hypothetical scope, and separate answer time
from event time. Carry validated support identities/revisions through world
graduation and make write failure an explicit retryable outcome.

**Acceptance:** a short assent cannot become a broader preference or permission;
a denial cannot make the denied belief a positive fact; a recollection of an old
event is not dated to the reply. Public knowledge and statements about Aiko do
not become personal user observations. Retraction can find the actual supports.

Sources: `app/core/session/hypothesis_resolve_mixin.py`,
`app/core/concepts/hypothesis_graduation.py`.

## 3. Image memory mixes seeing a picture with the depicted event

The distillation prompt already resolves caption pronouns using both names and
warns against guessing an unidentified subject. Those safeguards should remain.
However, it has no today anchor or stored-text time rule, requests past tense
and dropping hedges, and returns an unstructured line. The writer stores it as
an `event` with `event_time=now`, without explicit subject/domain/scope. The live
caller passes processing time and `source_message_id=None`, even though the
attachment reference is preserved.

The DB contains two historical image memories, both linked to attachments and
neither linked to a message. Both describe Aiko's avatar but fall back to a user
observation through their kind. This is not a measure of recent image frequency.

**Improve:** retain the actual caption message and its timestamp at scheduling;
separate the sharing/seeing event from depicted content. Preserve user-caption
claims versus visual interpretation, uncertainty, identified subject and
real/virtual/unknown context. An unknown photo date stays unknown; upload time
does not fill it. Reject or regenerate overlong content rather than cutting off
its qualifications.

**Acceptance:** an old holiday photo, Aiko render, third-party photo and ambiguous
screenshot retain their different subjects and temporal meanings. A delayed job
does not change the time of sharing. No fictional scene becomes a physical event.

Sources: `app/core/vision/image_memory.py`,
`app/core/session/speaking_window_jobs_mixin.py`.

## 4. Manual gap answers bypass source-aware admission

`MemoryFacadeMixin.resolve_knowledge_gap()` writes an answer as a generic `fact`
without the original question, an answer-origin receipt or explicit subject.
The resulting default is a user observation even for impersonal answers.
The gap links to the answer when it succeeds, but the answer has no reciprocal
gap context. Saving can fail and the gap still closes; validation that the target
is a gap occurs after the answer write, so a bad ID can leave an orphan answer.

The UI deliberately allows an empty answer to dismiss a gap. That is valid and
must remain separate from successfully saving a supplied answer. Automated gap
and fact-checker paths have their own receipts; this finding is about the manual
REST/UI path.

**Improve:** validate the gap first, retain its question and manual-answer source,
route the answer by its supported subject/scope, and report supplied-answer write
failure without closing the gap. Preserve the explicit dismiss-without-answer
action. Manual testimony is not independent external verification.

**Acceptance:** a world answer cannot become a personal user fact; an answer
failure leaves the gap available; an invalid ID produces no orphan memory;
blank dismissal still works deliberately.

Sources: `app/core/session/memory_facade_mixin.py`,
`app/core/memory/knowledge_gap_extractor.py`,
`web/src/features/settings/memory/KnowledgeGapsPanel.tsx`.

## 5. Pursuit notes need episode and research continuity

`PursuitNoteWriter` already maps to Aiko / virtual event / virtual, and activity
writers require a meaningful trace rather than every ambient beat. Hobby wrap-up
wording also distinguishes result-backed progress from an unfulfilled idea.
These are useful existing boundaries.

The writer retains `noted_at` and source-specific extras but leaves ordinary
event scope unspecified and slices text at 400 characters. A hobby milestone can
retain one research-memory ID; the wrap-up does not retain that research chain.
Away notes retain activity keys and changed effects, without a stable episode
receipt. These limits make later source review and timing less precise.

The inspected DB has 116 pursuit notes. These are historical counts, not 116
errors. The formatter probe above demonstrates one actual loss of their already
known virtual source meaning.

**Improve after recall:** keep episode identity/time and bounded activity or
research receipts. Preserve imagined, intended, simulated and researched clauses
separately when their meanings differ. Label known virtual-event time as virtual;
do not manufacture a physical occurrence. Preserve qualifications at length caps.

**Acceptance:** repeated garden visits are traceable episodes; researched learning
does not become physical practice; imagined outings do not become completed
trips. Deliberate creative inner life remains available as such.

Sources: `app/core/memory/pursuit_notes.py`,
`app/core/proactive/hobby_worker.py`,
`app/core/world/idle_activity_worker.py`,
`app/core/world/garden_visit_worker.py`.

## 6. Catchphrase provenance means first seen, not necessarily coined

The slow miner has recurrence and balanced-use gates. It nevertheless stores
only the phrase and first speaker from the sampled window, without occurrence
IDs/counts. That supports "first observed here", not original authorship or a
personal belief. All 42 historical catchphrase rows inspected lacked a source
message ID; this does not show whether every phrase is unhelpful.

The fast inside-joke path knows an origin message and an echo message. Its shared
moment keeps them in different fields and does not supply an event timestamp.
The new moments store therefore safely uses recording-time-only semantics and
skips anniversary inference. This is missing usable evidence, not an unsafe
anniversary generated by the current fallback.

**Improve:** retain bounded occurrence receipts, first-observed scope and counts;
avoid promoting an ordinary recurring fragment into a personal claim. Preserve
both sides of a new joke's exchange and use its verified exchange timestamp when
available. Missing timestamps must remain unknown.

**Acceptance:** moving the mining window cannot change claimed original
authorship; repeated quotation is not a personal belief; a new joke retains the
actual exchange and honest timing.

Source: `app/core/memory/catchphrase_miner.py`.

## Delivery order and validation boundary

Start with recall source meaning, then hypothesis answers and image memory.
Manual gap answers are a small independent correctness fix. Pursuit continuity
and catchphrase receipts follow. These are implementation candidates, not shipped
changes. Keep each change reviewable and test its negative cases, including
unknown timing, changed sources, denials and qualified claims.

Source inspection and the read-only formatter probe establish the deterministic
gaps above. They do not establish live-model phrasing, retrieval frequency,
post-restart behavior or semantic entailment. Historical repairs require their
own conservative review; this audit does not authorize bulk rewriting.
