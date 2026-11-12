# Ordinary-use review, 12 November 2026

This resumes the checkpoints that were waiting for several days of conversation:
L22 memory/concept quality, reflective workers, L29b understanding arcs,
L36/K106 structured learning, second-thought output and F17/cue delivery.
The review uses read-only SQLite transactions and live MCP diagnostics. No
historical memory, concept, setting, cue or task was repaired or forced.
Private sources, exports, annotations and model responses are in ignored
`tmp/ordinary-use-2026-11-12/`; personal dialogue is not a repository fixture.

## Cohort and activation boundary

The snapshot contains 233 user messages over 9-12 November and 285 new memory
rows: 35 promises, 23 goal notes, 14 diary entries, seven reflections, seven
research findings, three topic digests and other extraction/pursuit records.
The live scheduler reported no worker errors. Source receipts demonstrate that
the newer producer paths were exercised; this is not a recorded deployment hash
proving that every fresh row came from exactly the same checkout.

An additional concept sample used the deterministic reviewer with cutoff
`2026-11-10T14:00:00Z`, seed `ordinary-use-v1`: 18 active/candidate records from
131 eligible records, balanced across subject/kind/status. Four support judgments
remain unknown. Of the 14 inspected support sets judged, three supported the
label and 11 did not justify its full interpretation. This deliberately balanced
sample is a diagnostic, not a prevalence estimate for the graph. Other quality
axes remain unscored; source availability is not substituted for a judgment.

## Evaluate the relation, not just the citation

Each review must distinguish source availability from these separate questions:

1. Who speaks, who is described and who is expected to act?
2. Is the source advice, capability, an offer, an intention, a definite
   commitment, a reported action, an outcome or a subjective interpretation?
3. Which conditions, exceptions, negations and uncertainties apply? Were they
   retained, and is there evidence that a condition was satisfied?
4. Does the cited source support this particular claim and causal relationship?
   A shared topic, plausible motive or nearby timestamp does not supply support.
5. Did useful information get omitted? A malformed output or an admission
   refusal is not a successful semantic abstention.

For example, advising someone to bring headphones does not promise to bring
them. Offering to remind them if wanted is not an unconditional commitment to
remind them. An explicit commitment does not become a mere offer because the
other person has not requested it. These distinctions also apply to summaries,
shared moments, goals and diaries, not only the promise ledger.

## Findings and implemented corrections

### Promises and other source-linked claims

The fresh promise cohort included advice to the user recorded as Aiko's own
obligation, anticipation of somebody else's action recorded as a commitment,
and ordinary immediate exchanges saved as follow-ups. Some citations were real
and correctly attributed to their speaker; the inferred relation was wrong.

Promise admission now inspects the sentence surrounding a quote, including an
omitted leading or trailing condition. It refuses clear advice, capability
offers, negated requests and conditional arrangements as unconditional ledger
obligations. The prompt distinguishes later follow-up from immediate enactment.
Conditional arrangements can remain ordinary qualified memories; the ledger
does not implement condition satisfaction or infer acceptance of old offers.

Shared claim checks now cover `if` conditions and obvious unsupported
commitment interpretations. Extraction and shared moments inspect the source
sentence rather than trusting a condition-free fragment; digest clauses use
the same fidelity checks. Positive controls retain definite commitments and
qualified conditional arrangements in ordinary memory. These are conservative
English backstops, not a general semantic entailment engine.

### Temporal wording

New extracted rows still contained floating current-state/next-year wording;
one new digest contained week-relative history. Ordinary extraction only added
the stored-text rule on turns offering optional learning schemas. It now adds
that rule unconditionally. Extraction rejects unanchored generated wording with
a content-free refusal reason; evidence quotes retain their original wording.
The existing time detector also recognizes next/last/this year/month/week and
numbered or spelled-out intervals such as "four weeks ago", protecting digest
clauses and other existing consumers. Unknown event time remains unknown.

### Hobby research

A sketching project accumulated six findings about roofing manufacturers,
including repeated answers to essentially the same question. Real URLs and
persisted findings did not establish relevance to drawing or new progress.

Research generation now receives bounded, dated question/finding history and
keeps the original project as its purpose. Repeated questions are refused, and
near-repeated findings cannot increment project progress. Numeric/negation
changes remain distinct. The repetition guard is lexical; semantic relevance
and useful novelty remain model-quality checks. Existing progress and findings
were not rewritten.

### Concepts

Inspected failures included repeated intentions treated as development,
unreported completed investigations, incompatible book identities treated as one
pursuit, and compatible preferences treated as conflicts. Some fresh concepts
also depended on older unsupported promise or activity records.

A bounded evaluation on the configured `worker_default` route,
`baytout3/qwen3.5-uncensored:9b`, produced 11/12 correct regression admissions.
Its false tension explicitly said `source_conflict: none: both can be respected`
and nevertheless emitted a concept. The production parser discarded that check.
Explicit `none` source-change/conflict verdicts now suppress inconsistent
concept arrays, and evaluation uses the same rule. Replaying the frozen outputs
gives 12/12; a separate six-case held-out run gives 6/6 with no transport or
truncation errors. This is a narrow admission fix, not proof of semantic quality
in the fresh graph, and existing concepts were not changed.

### Goals, diary and other reflective output

Fresh goal notes retained derivation packets and stayed predominantly reflections
(22 reflection notes, one intention; no reported-step/outcome positives). That
does not establish their correctness: inspected prose sometimes added event
dates or an unsupported interpretation of a commitment. New diary entries also
reused earlier imagery as if it were a fresh action. A sampled dream remained
an associative reflection with recorded inputs; that does not verify its
historical premises. Source-scoped map output remained an interpretation.

The goal and diary prompts now explicitly preserve speech acts, actor ownership,
conditions and the difference between no request and refusal. Earlier diary
imagery cannot establish another occurrence. Subjective feelings, uncertainty
and new intentions remain allowed.

Fifteen synthetic prompt probes across promise extraction, ordinary extraction,
digests, goals and diary were inspected, then repeated with the same sources and
650-token output allowance after prompt changes. These use production prompt
builders but are not full worker executions or natural conversation trials.
Actor reversal, a commitment downgraded to an offer, unsupported event dates,
and schema mistakes still occurred. A citation/valid JSON is not scored as a
semantic pass, and a guard rejecting a wrong schema can still mean a missed
important memory. No overall worker-quality pass rate is claimed. Model-level
meaning fidelity remains open; stronger prompts alone do not close it.

## Checkpoints that remain open

| Checkpoint | Evidence from this review | Next acceptance check |
| --- | --- | --- |
| L22 / worker meaning | Enough fresh rows to expose interpretation failures | Repeat actor/modality/condition/entailment contrasts and fresh source review, including omissions |
| L29b understanding arcs | No supported arc among eight sampled endpoints per subject; 13 lacked usable receipts and 11 lacked enough verified changes | Inspect newly receipt-backed connected changes; do not manufacture history |
| L36 / K106 patterns | Zero stored shared terms or strategies; one historical shorthand rejection has no reason | Wait for genuine adoption/outcome opportunities; review misses before relaxing gates |
| Second thought | Seven scheduled drafts queued; no recorded output failures/truncation in the process snapshot | Check usefulness, repetition and correct interpretation separately from output validity |
| F17 surfacing | 14,860 item rows over 233 recent measured replies; 190 unsettled rows | Item-level paraphrase versus topical leakage review; no retention/cooldown tuning from shared-turn engagement |
| Cue delivery | 128 retained final traces for 233 measured replies; 105 older traces outside retention | Inspect committed cue-to-actual-utterance/answer relations; missing traces stay unknown |
| Harness ordinary use | Only one recent workflow row | Inspect representative work/result delivery; conversation volume alone cannot validate it |

The surfacing report also mixed lifetime header totals with windowed breakdowns:
241,230 total / 10,149 unsettled appeared beside recent rates. Header counts now
honor the requested window; lifetime remains explicitly selectable. The correct
recent denominator does not itself establish usefulness or a settling failure.

## Validation and next restart

The representative integration run passed 740 tests and 94 subtests, covering
promise/source admission, extraction, shared moments, summaries, goals, diary,
hobby research, concept synthesis/history, time and surfacing. Cross-worker
contrasts deliberately give negative cases real source citations and retain
positive controls. Shared imports expand the affected selector to 490 modules;
the representative set avoids collecting unrelated voice/UI suites. The full
suite was not run.

The final affected-selector run on the seven changed regression modules passed
183 tests, including the added numeric/negation contrasts. `npm run lint`, local
documentation-link, whitespace and LF checks passed. Managed test scratch was
cleaned; the two interrupted collection runs' directories were removed only
after verifying their processes had stopped.

Backend restart is required to load the fixes. The next cohort must be separated
from this review's input cohort. No worker was forced, no live conversation was
used for synthetic probes, and no historical record or runtime route was changed.
