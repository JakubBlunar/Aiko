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
