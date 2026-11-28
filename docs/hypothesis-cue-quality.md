# Useful hypotheses and exploratory cues

This review, implemented on 5 November 2026, covers hypothesis invention and
the exploratory cue workers, their conversation providers, wants, and Live
delivery. It preserves the original distinction: memories record attributable
material, concepts generalize grounded knowledge, and hypotheses can propose
something new. A concept is **not** required before Aiko can wonder.

## Findings

The read-only runtime review found twelve live hypotheses, eleven open and one
supported. Recent inventions included hidden possessions, exact pauses and
subconscious behavior that chat could not establish. The old prompt required
multiple inventions and strongly rewarded reaching beyond familiar ground.
Novelty checks could reject repetition, but could not establish usefulness.

The cue review found personal scenes and commitments being described as things
Aiko did not understand. Sparse external research and nearby vector clusters
were being treated as ignorance and curiosity. Several workers also continued
producing during heartbeats despite sufficient pending inventory. Pool expiry
and surfacing counts describe delivery, not semantic quality or accuracy.

Some providers reduced a generated direction to its topic label. A topical
embedding could bypass a separate eligibility check. Seed-derived wants had
their own lifetime, allowing an older suggestion to remain available after
its pool row expired. These are separate production and delivery failures.

## Hypothesis admission

The ordinary proposer can abstain and proposes at most two entries. Each needs:

- A complete, qualified possibility with an explicit subject.
- An actual offered memory or topic ID and a literal source quote.
- The connection that inspired the leap and what finding out could add.
- A plausible competing explanation, a concrete non-leading question, and a
  fitting situation in which to explore it.

The worker first reads the dated sources and identifies what changed, under what
circumstances, and any reason the speaker gave. This short model call can mark
the observation as ordinary or settled. An unexplained clue can instead yield a
tentative reading. Only then does the worker make a second model call to decide
whether to keep it, what else could explain it, and what would distinguish the
two readings. In the proposal, Aiko drafts the source quote and an open question
before writing out the explanation. If the source reports a choice, the question
can ask what drew the person to it. Their method and motive stay open until the
person supplies them. With several sources, the evidence check accounts for each one
before drafting from those it found open. A settled reading needs a source
quote for the explanation. Both calls use the existing worker model with
thinking disabled.
A single choice can inspire a guess about that occasion. A broader preference
needs more evidence.
Admission rejects a statement copied from its source and an alternative that
repeats the statement after punctuation and case are normalized.

After a completed ordinary or settled reading, or an explicitly empty proposal,
the worker can skip
repeat inference for six hours. It compares the sources actually offered,
their contents and dates, the included concepts and earlier guesses, and
the model route and context budget. Changed inputs can be reconsidered on
the next pass. The last abstention lives in memory. Invalid, interrupted and
truncated replies and provider failures remain eligible for retry.

Schema 48 adds a JSON `hypotheses.quality` receipt. Only sources retained in the
packed prompt are eligible. Memory sources retain their date, subject and mode;
drafts, questions, summaries, unknown-origin and superseded rows are excluded.
Oversized memory sources are skipped instead of cutting off qualifications.
Topic summaries are explicitly themes, not observations.

Inspiration adds no support and is never copied into `answer_memory_ids`.
Existing answer evidence, adjudication and graduation rules remain authoritative.
The prompt rejects fabricated private objects, inaccessible physical observations
and subconscious explanations; admission also checks typed fields, stored-text
time, exact quotes and unsupported numerical precision. These narrow checks do
not prove that a model's leap is semantically sound.

Earlier ordinary guesses without a motivation receipt are held out of asking,
recall and the invented lane unless already supported by an answer. Normal
proposer runs retire unasked, unsupported legacy inventory as `expired`, keeping
the rows. This is neither a denial nor a historical motivation backfill.
Outcome-led introspection retains its separate attributable-correction contract,
reserved origin and no-eviction policy.

For example, a factual note about clearing a desk before drawing might inspire
the possibility that a small transition ritual helps switch activities. A useful
next question asks what helps with that transition; it does not present the
ritual as an observed habit. A playful shared experiment is also a valid benefit.

## Exploratory cue production

Curiosity seeds can abstain, name the supplied context that inspired them, and
retain a specific direction, connection and possible value. Familiar interests
can support a new angle; there is no mandatory subject quota or demand to find
an unrelated topic. Associations require an explicit accepted connection and a
concrete implication, rather than a generic similarity.

For new conversation-inspired seeds the worker copies a short phrase from the
supplied rolling summary into the existing connection field. Admission checks
the phrase against that summary. The worker can use a difference across
examples or a choice whose basis is still open. The phrase identifies the
source detail. The proposed explanation remains a guess. The chat handling
asks Aiko to leave the question unspoken when the user answers it or moves on.
The stance gate also recognizes explicit answer-format requests after a
question, such as “Which hall? Just the name.” It defers the curiosity before
claiming the cue. [Fictional replay and open checks](../backlog/personality/concepts.md#l87-wonder-about-the-exception-in-a-familiar-pattern)

Research gaps and density gradients use a conservative role/label filter.
Collections of scenes, promises and personal reflections do not become research
assignments just because they contain few external findings. Explicit open
questions can still justify exploring personal subjects; technical subjects such
as JavaScript promises remain eligible. Density is described as note coverage,
never proof of ignorance. This call-free heuristic cannot classify every topic.

Generated legacy seeds, associations and research/gradient cues lacking the new
contracts are held back and retired during normal worker runs. Explicitly
curated moves remain distinct. Inventory guards stop heartbeat overproduction
for the reviewed exploratory workers, including interest drift.
An otherwise full shelf with unreviewed automatic stock requests a normal
cleanup/replenishment run, so it cannot prevent its own replacement until TTL.

## Delivery and learning

The pool checks eligibility and source validity before topical ranking and again
when committing a staged claim. Cosine relevance and debug forcing cannot revive
a closed, changed, already-asked or unreviewed hypothesis. Current topic checks
join only the named clusters' memory roles on the conversation path.

Curiosity blocks and seed-derived wants retain the actual direction, connection
and value. Wants can outlive an otherwise valid seed's pool expiry, but must
still refer to a usable source. Missing, resolved or unreviewed sources are
removed. Live projection filters unreviewed automatic cues, and its eventual
claim uses the same pool source checks.

A non-leading exploration question may differ from the proposed explanation.
Ask receipts therefore match either the claim or its declared check question,
retain the actual question, and spend the one-ask allowance. The answer
adjudicator still receives both question and claim. Recognizing an ask never
confirms a hypothesis.

The hypothesis card shows motivation, alternative, fitting context, possible
question and source quotation explicitly marked as context rather than proof.
Older unreviewed guesses are identified without inventing their missing reason.

## Validation and follow-up

Regression coverage includes typed proposal admission, source quotes, zero
evidence persistence, schema-47 upgrades, legacy retirement, introspection,
source changes, question receipts, cue inventory, topic roles, wants and Live
projection. Affected-test integration covers the shared prompt and delivery
paths; related frontend tests cover the motivation card. Lint is required.

This validates the contracts and lifecycle, not the quality of future model
outputs. No live generation, settings change or data rewrite was used to stage
the review. Restart the backend and refresh the web UI to activate the changes;
normal enabled worker runs handle inventory retirement. After fresh runtime
data accumulates, inspect accepted and rejected proposals, rendered directions,
actual questions and answers together. Lower production volume alone is not
evidence of better understanding.
