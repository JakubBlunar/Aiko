# Companion continuity and ordinary-use quality, 17 November 2026

## Scope and evidence

The user reports the latest prior changes are loaded. Read-only MCP status,
idle-life summary, worker status and hobby state were collected on 17 November.
SQLite reads used `mode=ro`. No conversation message was sent, no worker was forced,
and no historical memory, concept, model route or live setting was changed.
Runtime capture and source receipts are present; this is not a byte-for-byte build
attestation. Changes from this review require another backend restart.

The fresh memory window starts at 15 November, 15:00 UTC. It contains five diary
entries, seven promises, 22 pursuit notes, ten goal notes and other records. Last
saved dialogue is 16 November, 02:01 UTC. Detailed inspection concentrated on the
five diaries, seven promises, their actual message sources, both newly active
concepts, the current hobby and the idle-life trace. Cohort totals are not a claim
that every row was semantically reviewed or that omissions were exhaustively found.

## 1. Source to memory to later understanding

There are useful positives. Memory 11668 retains the user's fan replacement and
the qualification that one still makes noise. Several later event/preference rows
have usable source message links. Source presence alone still does not establish
that the inference preserves the exchange.

Diary 11669 introduces a no-teasing agreement that is absent from its fresh message
packet. Its earlier diary inputs contain related imagery. Both new active concepts,
13690 and 13691, depend on this diary; one turns the unsupported agreement into a
general stance about temporary promises. This is a concrete downstream path for
an invented detail, not merely awkward diary style. Historical rows were retained.

The promise cohort also retains interpretation problems. Memory 11685 converts an
alternative about reading or watching into a definite sequential obligation.
Memory 11696 names the wrong recipient for a carrying intention. The later reply
also says carrying is unnecessary. General actor/modality interpretation and
reconciliation remain open; this pass does not claim to repair that ledger.

The new diary source review uses only the current frozen transcript. It permits
personal feelings, uncertainty and intentions, but requires support for concrete
claims about actions, commitments and other people's motives. Missing, malformed
or contradictory review results cannot admit a diary entry. The source is checked
again before storage. This prevents a prior diary from being the review's evidence
for its own repeated claim; a model can still misjudge semantic support.

## 2. Diary novelty and useful hobby research

### Reproducible diary identity bug

Diaries 11691 and 11707 have identical prose. Their rendered source suffixes contain
12 and 13 messages respectively, ending at the same latest message. Entry 11724
later receives 14 messages, still without any new dialogue. As age labels shorten,
more old rows fit in the character limit. The old fingerprint was calculated from
that changing rendered subset and therefore mistook time passing for new material.

The production diary snapshot now fingerprints the bounded raw message rows while
retaining exact offered-source receipts separately. Real content edits or new rows
still change identity. Exact repeats of the three latest diary entries are refused.
Numeric event clocks require testimony rather than a message timestamp; this catches
the unsupported clock in both identical entries. A switch from the old fingerprint
format may cause one additional composition attempt after restart; the duplicate,
clock and source-review guards still apply.

### Hobby research purpose was lost at distillation

The current skyline project has seven older research findings and no recorded
advance since 14 November. Its latest result concerns structural roof geometry;
that alone does not teach perspective sketching. Three idle days without progress
are not sufficient to call the existing seven-day stall rule broken.

Question generation already receives project purpose, but the answering worker
received only the derived question and snippets. It could correctly answer the
wrong question. New questions now carry local project purpose into distillation.
Pending legacy questions acquire missing purpose when reused by the hobby worker.
Purpose is never added to the outbound search query. The first model decision is
usefulness for that original activity; a strict positive relevance verdict and
normal excerpt attribution are required before a scoped finding is saved or its
question resolved. Purpose edits invalidate an in-flight result. Historical applied
findings and project counters were not rewritten or retrospectively certified.

## 3. Independent-life observation

The MCP 72-hour summary reports 27 recorded autonomous steps across 19 action keys,
including rest, household actions, reading, looking outside and thinking about
drawing. Three steps fall in the review's maintenance category. There are no current
loop flags or incomplete runs in that summary. The two `doodle` steps explicitly
report thinking about a possible drawing, with no saved drawing receipt; they are
not two completed sketches or advances of the skyline project.

Recorder heartbeat spans cover approximately 54.4 hours. Forty-three gaps are
unconfirmed, including a roughly 16-hour early gap; these are not evidence of either
activity or inactivity. Holds include cooldown, sleep, shared conversation,
intentional placement and garden visits. Counts of compressed admission observations
do not measure how long an activity lasted.

Nine of ten model-planning attempts in the 72-hour summary fell back. Eight of those
fallbacks fall within the fresh memory cutoff. The old event says only
`model_failed_or_invalid_plan`; attribution events establish that some replies
parsed, but cannot identify the rejection. A disposable world-only copy produced
one valid model rest/thinking plan. This neither reproduces nor resolves the high
historical fallback rate.

New failures record bounded, content-free reasons at the rejection site. The model
also receives the actual drawing coordinate/stroke limits; the earlier prompt
referred to instructions "above" without providing those bounds. No selection
weights, rest allowances, context sampling ratios or activity quotas were tuned.
Recent chat remains optional inspiration.

## Bounded local-model checks

All probes use the configured local worker model,
`baytout3/qwen3.5-uncensored:9b`, without web searches or live memory writes.

- Eight fictional diary contrasts: rejected an inherited invented agreement,
  advice promoted to a promise, an alternative promoted to a sequence, an
  unaccepted offer and an action contradicted by the reply. Retained feelings,
  an explicit commitment and a qualified uncompleted plan: 8/8 intended verdicts.
- Initial four research contrasts: the unscoped path accepted both useful drawing
  techniques and both off-purpose supplier/structural-engineering answers. Merely
  adding purpose in the user payload still accepted structural engineering.
- A capped-reasoning experiment did not resolve that error and incurred answer
  starvation/retries in three cases. It was not adopted in production.
- Making relevance the primary system instruction, with the verdict before the
  answer, retained both drawing positives and refused both negatives: 4/4.
- Four held-out painting/plant-care contrasts retained both intended positives
  and rejected unrelated tea marketing. It accepted a watercolor pigment supplier
  fact that had been labeled negative: 3/4 against the frozen intended labels.
  Art-supply information could be useful, making this boundary debatable; the
  result was retained rather than relabeled or used for further prompt tuning.

These are small diagnostic samples, not naturalness or reliability estimates.
Prompt examples overlap the four development research cases. No extra reasoning
allowance or new model call was added to research distillation. Diary grounding
adds one bounded call for an otherwise eligible entry, off the conversation path.

## Validation and remaining checks

The representative backend integration sets pass 465 tests plus six subtests. They cover diary
identity/grounding, source edits during review and embedding, research ownership,
household execution, drawing data, idle history, coverage and isolated simulation.
The full affected-source graph selects 350 test modules through shared session and
evidence imports, so a representative integration set was chosen instead of the
full suite. The final affected-selector run passes 77 tests (overlapping those
sets). `npm run lint`, whitespace and LF checks pass. Managed test scratch is cleaned
after each run, and the manually owned read-only review/probe scratch is removed
after its helper processes exit. No full-suite or ordinary-use quality pass is claimed.

After restart, inspect only newly written outputs for novelty, concrete source
support and missed useful material. Check project-question inconclusive rates and
whether accepted findings actually help the project. Collect new rejection reasons
before changing model planning or deterministic activity weights. Existing records
still need a separately scoped, source-aware historical repair decision; they were
not silently rewritten here.
