# Concept and pattern rollout, 28 October 2026

Implementation is split into five independently committed phases. Live model
quality will be assessed over subsequent ordinary use; deterministic tests
establish the data contracts, not naturalness.

1. Grounded hunches (L30a / bounded RS2): source-linked recall and a compact
   hypothesis basis with an explicit unresolved point. Existing T3 budget,
   candidate isolation and invented-hypothesis wording remain authoritative.
2. Context-specific patterns (L50): extend source-cited applicability and
   preserve exceptions and temporal validity.
3. Shared shorthand (K106): explicitly adopted, source-linked meanings with
   context, correction and deletion behavior.
4. Learned approaches (L36): outcome-backed strategy transfer with constraints,
   exceptions and source invalidation.
5. Changes in understanding (L29b): temporal concept-change arcs for the user,
   Aiko and the relationship, with source and revision validation.

## Phase 1

Implemented a bounded read-only evidence projection. Recall retains memory and
concept IDs/revisions, source authority, source timestamps, support coverage,
applicability and tracked support validity. Full evidence is capped at 16
excerpts. A grounded hypothesis receives one short basis and an unresolved
point; the selector budgets that extra text without increasing T3 capacity.
Existing hypothesis trace rows record basis IDs and the uncertainty reason.
Stored rationale remains an interpretation, separate from supporting excerpts.

## Phase 2

Scoped communication patterns now accept literal, source-cited activity names
alongside learning, troubleshooting and casual conversation. The existing
proposer, correction, contradiction and T3 paths share these names. A fresh,
compatible situation can resolve a short follow-up; stale situations cannot.
Changed/deleted qualifying sources suppress the rule. Exceptions and validity
windows remain enforced. This extends communication-style learning for both
subjects; arbitrary factual concept scoping remains future work.

Validation: phase 1 focused affected selection passed 197 tests; phase 2 passed
211 tests plus 72 subtests. The import selector reaches shared session hubs, so
these runs explicitly select recall, context-budget, hypothesis, applicability,
contradiction and dependency behavior rather than unrelated UI/voice tests.
`npm run lint` passed for both phases.

## Phase 3

The existing memory extractor can emit explicitly adopted shared terms. Exact
definition/adoption spans and message revisions are validated before writing
through MemoryStore; queued writes recheck sources. Names default to session
scope. Explicit relationship-wide or named-activity scope and ISO expiry are
supported. New adopted meanings supersede earlier meanings in the same scope;
retirement, edited/deleted sources, other users/sessions and ambiguous equal
scopes do not yield a meaning. Definitions are capped at 256 recent records.

Validated definitions compete inside the existing T3 memory budget only when
the user uses the term. They provide referent context and offer no stance or
unsolicited topic. Generic vector retrieval cannot bypass their validation.
The existing concept trace records structured-pattern IDs and actual text
inclusion; the memory outcome ledger records subsequent use. Inclusion and
echo remain different from helpfulness. No additional model call is added.

Validation: 197 affected tests passed, followed by a 144-test focused run
including an actual extractor-to-SQLite round trip. Lint passed.

## Phase 4

Learned approaches use source-cited goal, approach, constraints, exceptions and
an explicit user-reported result. Success/failed outcomes are separate records;
an explicit source-linked failure correction suppresses the earlier strategy.
Edited/deleted premises invalidate reuse. This pilot stores evidence through
MemoryStore and does not create a second concept/dependency truth store.

At most one strategy competes in the existing memory budget. Transfer requires
matching goal words and every recorded constraint; exceptions veto it. A fresh
working-set question/constraints can resolve a short follow-up, while substantial
new requests and explicit pivots stand alone. This conservative lexical matcher
does not claim semantic entailment or causal proof from one reported outcome.

Both optional extractor schemas are included only when the offered transcript
has a corresponding naming/outcome signal. They share the existing call and
output budget; routine extraction retains its original context capacity.

Validation: 310 focused affected tests passed, including goal contrasts,
exception/failure/retraction behavior, prompt budgeting and extractor-to-SQLite
strategy intake. `npm run lint` passed.

## Phase 5

Understanding arcs are read projections of connected learning events and their
concept versions. They cover the user, Aiko and the relationship. The drift
worker captures bounded source-revision receipts when it records a new change;
the read path checks original dialogue, memories, frozen cluster members,
supporting concepts and the exact lifecycle event. Adding later cluster members
does not rewrite an earlier observation set. A missing, edited or deleted
premise, an unrecorded label revision or a broken chain leaves a thin record.
Legacy learning events without receipts are not retroactively certified.

At least two verified changes are required. At most three changes render per
arc. The current endpoint comes through ConceptView. User-requested retrospective
context and concept recall can include the arc, costed inside the existing T3
budget. The record distinguishes an earlier belief from a fact about a person:
changing Aiko's understanding does not prove that the user or relationship
changed. These are request-responsive context, not unsolicited cues or stance
offers. No new named prompt block or model call is introduced.

The final review also tightened source-quote validation for hypothesis bases,
literal activity-scope admission, explicit term retirement, in-flight owner
identity and short-follow-up strategy context. Structured feature assembly lives
in its own module rather than expanding the large inner-life provider.

## The ordinary-use trial

Restart the backend to load the implementation. Existing extraction/concept/
history settings continue to govern their respective paths; no user settings
or live memories were modified by implementation or tests.

Use Aiko normally for several days. Useful observations include a hunch that
fits or misses, a context-specific preference, an adopted shared expression,
a correction to its meaning, and a genuinely helpful/failed approach. Definitions
default to session scope; an explicitly permanent shared name can use language
such as "let's always call small experiments lab mode". Activities and expiry
must be stated, not guessed. Historical arcs accumulate from newly verified
changes and can remain thin during a short trial.

Read-only debug tools:

- `get_pattern_learning_state`: content-free, owner-scoped schema/proposal/
  admission/rejection counters, bounded record/source-validity counts and the
  last turn's structured-pattern/arc inclusion.
- `get_last_concept_trace`: hypothesis basis IDs, uncertainty reasons and the
  selected/included structured-pattern and arc receipts.
- `get_understanding_arcs(subject="all", limit=3)`: explicit inspection of
  personal historical belief text; default results rotate across the three
  subjects. Unsupported records remain counted as thin.
- `get_surfacing_outcomes`: existing subsequent-use/echo evidence. Interpret
  this separately from user-confirmed helpfulness or causal attribution.

The literal admission and goal/constraint matchers are deliberately conservative.
The trial should establish missed opportunities as well as unwanted callbacks,
incorrect references and unsupported certainty before broadening them. No live
model-quality claim follows from deterministic tests.

## Final validation

The final behavior-focused affected selection passed 1,141 tests and 99 subtests,
including prompt/cache, extraction, retrieval, lifecycle/history, source
dependencies, diagnostics and session setup. A subsequent 61-test run covered
the final owner-scoping and durable-definition intake adjustments. Explicitly
adopted shared terms enter long-term memory; conditional strategy records retain
the existing scratchpad/promotion policy. `npm run lint`, documentation link
checks (83 files), whitespace and LF checks passed. No full-suite or live-model
run was performed. Managed test scratch was cleaned up.

## Fictional local comparison, 22 November 2026

Run `python -m scripts.companion_pattern_replay --seed 71` from the project
environment. The [runner](../../scripts/companion_pattern_replay.py) uses an
installed loopback Ollama model and local embeddings. Each history has its own
disposable SQLite database. Seeds 71 and 73 used
`baytout3/qwen3.5-uncensored:9b` and `qwen3-embedding:0.6b`. Both used the
same fictional cases. The second run also guarded Python file opens against
the installed data directory and user config, SQLite connections against paths
outside replay scratch, and socket connections against destinations other than
loopback Ollama. It completed with zero guarded access violations.

### Scoped preference (L22/L48/L50)

The user asked for compass bearings during map drawing. Three supplied concept
labels included two invented explanations. An alternative used one cited
request, gated by the existing applicability functions. The third arm supplied
no memory. This comparison controls the material offered to the reply model.
Natural synthesis, ranking and multi-turn retrieval remain open.

| Later turn | Three labels | One scoped request | Empty context |
| --- | --- | --- | --- |
| Place a pier above a tower on a north-up map | North (90 input tokens) | Due north (67) | South (51) |
| Troubleshoot a silent metronome | Check power (81) | Check power (42) | Check power (42) |
| Recall the earlier request | Extended it to discussing maps (83) | Preserved the drawing condition (60) | Invented a request to draw a world map (44) |

Both seeds gave the same replies. The next comparison should use the ordinary
concept selector and a second activity. Check whether it delivers the narrower
note when the user refers back without naming the activity.

### When a curiosity has been answered (H35)

Six independent histories started with the same pending question about the
lining material of a violin case. Aiko's settlement path consumed both
volunteered answers, including a paraphrase about a satin-lined fiddle carrier.
It also consumed three of four unanswered controls: the case arrived, a coat
had a fleece lining, and the user explicitly said they could not identify the
case lining. The unrelated choir update left the question open. All six had
zero prompt appearances. This measures whether the specific question was
answered; curiosity seeds currently also retire on topical conversation.

An offline 9B reviewer asked for an exact answer span got four of six right
at each seed, compared with three of six for settlement. It kept the delivery
case open, but credited the coat and “Maybe the maker knows.” Verbatim words
alone did not identify the object or establish a known answer. A subsequent
pilot should distinguish a topic being discussed from a resolved question
and retain naturally volunteered answers under paraphrase. Neither method
earned a change to the live completion policy on this comparison.

### Shared names and learned approaches (K106/L36/K100)

Each seed exercised the real extractor and storage path on four pre-labelled
opportunities. We compared the current optional instructions with a short
clarification about naming versus scheduling and copying exact strategy spans.

| Opportunity | Current instructions | Clarified instructions |
| --- | --- | --- |
| Adopt a name for a dark rehearsal | 0/2 definitions. Both stored an invented scheduling agreement. | 0/2 definitions. Both proposed the right kind without its required payload. |
| Decline the name | 0/2 definitions | 0/2 definitions |
| Report that blotting a stamp fixed bleeding ink under a supply constraint | 1/2 strategies | 0/2 strategies |
| Plan to try blotting after lunch | 0/2 strategies | 0/2 strategies |

The one admitted strategy retained the goal, constraint, approach and reported
result. The naming case exposes two different failures: inventing a planned
event, and losing a valid definition at structured output. Keep the source
checks. Trial an example of the nested payload on fresh names, with a declined
name and a separately agreed appointment as controls. Ordinary-use quality
and cross-topic transfer remain open.

### Runtime follow-up on the same local models

Seeds 71 and 73 exercised ordinary chat concept selection, cue settlement and
memory extraction in fresh databases. The replay guard blocked access to the
installed data tree and user config, restricted SQLite to replay scratch and
allowed model traffic only to loopback Ollama. All assertions passed at both
seeds. Repeating a fixture at a second seed measures repeatability.

| Path | Observed behavior at each seed |
| --- | --- |
| Ordinary preference selection | Seven turns across map drawing and block printing. Each relevant turn received the single source note, and each unrelated turn received zero. Both subject-only recall questions recovered the activity qualifier. |
| Replies | Aiko placed the pier due north and ordered the inks yellow, orange, navy. She recalled each request within its original activity. A shorter rendering removed an earlier reply's scope expansion to describing maps. |
| Pending curiosity | Two volunteered answers settled the violin-case question, including the paraphrase. Four unanswered controls stayed pending. All six had zero prompt appearances. The offline model reviewer credited two unanswered controls. |
| Shared names | The extractor stored “lantern round” and “cloud pass” with the intended meanings. Declining a name produced zero memories. The independently booked rehearsal room retained December 8. |
| Strategy | The ink-blotting report yielded one successful strategy with the existing-supplies constraint. The plan to try it after lunch remained a plan. Neither seed stored a clock time for it. |

Use `--section preferences`, `--section curiosity` or `--section extraction`
to repeat one part. The preference replay seeds concept edges and isolates the
concept lane. Learning those edges from a new conversation and their interaction
with other recall sources still need ordinary-use validation. The curiosity
cases exercise one paraphrased referent and do not measure general semantic
accuracy across questions.

The affected test run surfaced a small-context regression: the optional
extraction instructions could crowd out the transcript on a 4,096-token route.
The packer now falls back to the ordinary format when it fits more turns.
Both capability modes passed the source-receipt round trip, and the final
extractor-focused selection passed 152 tests. Three other failures reproduced
on the unchanged revision `568d8d76`. Inline-memory admission refused a
literal fallback with `lost_condition`, and two forced hypothesis-verdict
tests expected a graduated concept. The follow-up fixed the literal retry
and verified that only confirmations backed by distinct current user messages
supply graduation evidence. All three failing cases now have passing coverage.
