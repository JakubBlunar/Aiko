# Memory source meaning and recall improvements

Implementation of the [6 Nov boundary audit](memory-boundary-audit-2026-11-06.md).
SQLite remains authoritative. Historical rows are not bulk rewritten.

## Recall

Direct and topic-expanded hits carry subject, source mode and domain from the
authoritative memory. Personal reports, Aiko's inner life, relationship memories
and world knowledge have distinct headings. A vector record without an
authoritative join remains explicitly unresolved. Catchphrases are labelled as
recurring quotations rather than personal claims or proven original authorship.
Associative notes retain whole qualifications and the ordinary temporal tags;
oversized notes are omitted rather than cut to a misleading prefix.

Focused recall regressions: 110 passed. The combined verification is recorded
below. Live-model wording remains to be checked after a backend restart and
ordinary use. Legacy image rows with only an uploader/event-kind fallback now
resolve to an unknown depicted subject at read time; the rows are not rewritten.

## Hypothesis answers and world graduation

Answer memories index the literal reply and actual question labelled as context,
explicitly anchored to the source recording date. The proposed interpretation,
exact verdict, question and target identity remain in a separate receipt.
Recall and worker packets expose the
question as target-specific context. General concept synthesis cannot repurpose
that contextual answer as an independent claim; the dedicated verdict and
graduation paths still attach it to the tested target.

Known source messages are checked before and after embedding. Answer time does
not become the occurrence time of a reported event. Oversized quotes are refused
whole. World graduation retains actual confirmation-memory revisions and remains
a world interpretation, without implying external verification. Missing, edited
or withdrawn supports invalidate it; failed writes leave the hypothesis live.

Focused hypothesis checks: 164 tests and 3 subtests passed, plus 31 receipt,
longitudinal and recall regressions. Lint passed. These deterministic checks do
not validate the model's semantic judgement of an answer.

## Shared images

Queued image jobs capture the caption's original message, session and timestamp.
The worker receives whole bounded, age-tagged inputs and the stored-text time
rule. Structured notes distinguish caption quotes, visual interpretation,
depicted subject/domain and caption-supported timing. Plain legacy responses
remain interpretations of unknown subject. Oversized inputs/outputs are refused
whole. Visual uncertainty and caption qualifications remain available.

Image notes have an explicit sharing-date anchor; upload or processing time
never fills the depicted event date. A separately recorded caption date is not
external verification of occurrence. Caption edits invalidate the receipt, and
an edit during embedding defers the write. Visual notes can be recalled but do
not independently establish personal concepts; caption testimony retains the
ordinary extraction path.

Focused image/recall checks: 62 passed, plus 18 image-evidence and receipt checks.
Lint passed. Live visual identification and summary entailment remain model
quality questions, not proven by literal citation checks.

## Manual knowledge-gap answers

The manual path validates the gap before writing and rechecks the question after
embedding. Supplied answers retain the question, manual source, gap identity and
recording date. Their subject remains explicitly unknown: knowing who entered an
answer does not identify what it describes or independently verify world facts.
Occurrence dates are not inferred from entry time.

Unavailable embedding, failed writes and changed questions report a retryable
error without closing the gap. A deliberately blank answer still dismisses it
without creating a memory. Invalid/non-gap IDs create no answer. Already closed
gaps do not create another answer. Focused gap/REST checks: 70 passed; lint passed.

## Pursuit episodes and research continuity

Pursuit notes retain a stable episode identity, dated virtual/reflection scope,
activity clauses and world-state receipts. Imagined outing clauses remain
distinct from simulated activity clauses. Replaying one episode cannot add a
second note; separate dated visits do not collapse their timing. Whole overlong
notes are refused rather than losing their final qualification.

Hobby milestones and wrap-ups retain a bounded chain of research-result
identities/revisions. Only recorded curiosity findings with research evidence
can supply that chain. Changed results defer a queued note and invalidate its
research support later. Such notes are Aiko's research reflections, not physical
practice or additional independent observations. Unbacked project ideas remain
reflections. These source distinctions also reach recall and worker packets.

Focused pursuit/activity checks: 186 tests and 4 subtests passed; lint passed.
Virtual-state receipts establish simulated effects, not physical-world proof.

## Phrase occurrences and shared-joke exchanges

The miner retains bounded literal occurrence receipts, window counts and the
first observed speaker. It marks authorship unknown rather than treating the
first speaker in a sample as the original author. Voice adoption cannot upgrade
that sampled observation through its legacy origin lookup. Quotations do not
independently establish personal concepts.

The fast joke path verifies the actual assistant line and user echo, retains
both sources and anchors the shared exchange to the echo's verified timestamp.
It describes the observed amused echo rather than asserting a recurring joke
from one exchange. Missing dates remain unknown. Edited sources invalidate
receipts and defer writes; registry, recall and voice consumers omit stale
evidence. Legacy records without receipts are not retroactively certified.

Focused phrase/moment/voice checks: 90 passed. Another 14 tests cover occurrence
receipts and the direct-retrieval SQLite join. Lint passed before the final
integration sweep. Amusement detection and usefulness still need ordinary use.

## Receipt revisions, merging and prompt budgets

The new versioned receipts participate in source revisions and merge scope.
Changing a question, support link or episode receipt invalidates the earlier
support even if the memory wording remains identical. Identically worded answers
from different contexts cannot silently merge. Rows without these v1 contracts,
including legacy unversioned hypothesis answers, retain their original support
fingerprints.

World graduation checks that each confirmation concerns the current statement
and target, and rechecks the hypothesis after embedding. A target edit cannot
reuse confirmations for an older statement. Malformed research-support packets
fail closed.

Memory and learned-pattern candidates are costed using their whole rendered
content, including source labels, question context and timing. Standalone headers
provide conservative allowance for shared section overhead. A qualified note is
kept whole or omitted when it cannot fit; formatting failures omit that candidate.

## Validation and ordinary-use follow-up

The initial unfiltered affected selection covered 336 files: 8,187 tests and
1,135 subtests passed; seven timing tests were skipped under parallel execution.
Four assertions failed: two expected the previous recall layout, and two checked
older invention wording. The invention renderer was verified unchanged from the
audit baseline; its assertions now check the existing stronger source boundary.
All four failing modules passed in focused follow-up.

After the optional receipt/hash and budget refinements, a representative
integration set passed 791 tests and 45 subtests, covering all six intake paths,
source lineage, consolidation, patterns and prompt assembly. The seven timing
checks passed serially. The interrupted final selector run was stopped when
Codex closed; its abandoned managed scratch was removed only after verifying
the Python processes had stopped. The final affected-selector rerun used the
explicit representative module set for the remaining optional-contract and
budget changes: 886 tests and 45 subtests passed. `npm run lint` passed, all
changed text files retain LF, and managed scratch was cleaned after completion.

After restarting the backend, inspect fresh ordinary-use rows and surfaced
context for subject/domain preservation, target-specific answers, image caption
uncertainty and depicted dates, useful manual answers, research/virtual episode
continuity and genuine shared phrases. Check missed useful notes as well as
overclaims. L22 remains the ordinary-use checkpoint; no live worker was forced
and no historical memory was bulk rewritten.
