# Concept grounding: 2026-11-05

The ordinary-use review found three specific overinterpretations: repetition
became development, compatible preferences became conflict, and a future promise
became completed progress. The proposer prompts encouraged finding a direction
or an interesting friction, supplied only positive examples, and called base
concepts settled beliefs. Ordered aspiration inputs also hid the distinction
between a memory's recording date and its future-plan status.

## Change

User and Aiko aspiration proposers now audit the observed change before naming
a direction. Their input includes each source's kind, temporal type and known
event time. Promise/plan records explicitly do not prove completed action.
Repeated mentions and record spacing do not prove increasing practice. Actual
steps toward a future ambition can still support a direction without claiming
the ambition has been achieved.

All three tension proposers now test whether both claims can be respected as
stated. Different contexts, complementary qualities and hypothetical unstated
conditions do not supply a collision. Base concepts are presented as held
interpretations rather than independent settled facts. The model emits a brief
factual source check before proposing labels, within the existing generation
call; this check is discarded by the existing concepts parser, not stored as a
new concept or treated as a semantic certificate.

Admission also requires an actual JSON boolean for a new aspiration's
`directional` flag. Tension citations must contain exactly two concepts of the
appropriate subjects, including reinforcement: user/user, Aiko/Aiko, or
user/Aiko for a relationship tension. Narrative rendering and its gate remain
unchanged. Existing concepts are not rewritten or removed.

## Bounded model comparison

The synthetic corpus in `tests/fixtures/concept_grounding_contrasts.json` was
frozen before changes. It contains 12 regression cases and six additional
validation cases, with genuine development/conflict and completed preparation
as positive controls. Rubrics and expected answers are excluded from prompts.

`scripts/evaluate_concept_grounding.py` captures the production proposer prompts,
calls the configured `worker_default` route, and replays the answer through the
production proposer admission path. It never reads live memories or writes
application state. This run used local Ollama
`baytout3/qwen3.5-uncensored:9b`, temperature 0.6, JSON output, 1,024 output
tokens, a 45-second provider timeout and explicit call/time budgets. Production
has a larger output allowance; the comparison used the same bounded allowance
on both sides.

| Measurement | Original baseline | Final source-first prompts |
| --- | ---: | ---: |
| Trials | 18 (one pass) | 36 (two passes) |
| Correct admission/abstention | 9/18 | 33/36 |
| Unsupported admissions among negative trials | 9/10 | 3/20 |
| Missed positive controls | 0/8 | 0/16 |
| Malformed, truncated or failed calls | 0 | 0 |
| Regression split correct | 6/12 | 21/24 |
| Additional validation split correct | 3/6 | 12/12 |

Intermediate revisions produced 15/18, 31/36 and 34/36 correct trials. The final
cleanup also removed residual positive examples that treated compatible
qualities as conflict. Its fresh repeated result is reported above;
the earlier better score was not substituted for it. The additional
validation outputs were inspected, so this is not a blind held-out result.
Repeated trials reuse the same small corpus and are not independent coverage
of ordinary use. Raw responses and frozen prompt snapshots are retained locally
under ignored `tmp/concept-grounding-2026-11-05/`.

All three final errors were compatible preferences mislabelled as tension. One
response even stated that both claims could be respected, then emitted a tension
anyway. The source check helps generation but does not mechanically establish
meaning. Positive outputs retained concrete progress and conflict, but some
rationales still embellished scope (for example, inferring frequency from
increasing skill). Admission accuracy is therefore not a semantic support score.
L22's broader model-quality acceptance remains open.

## Reuse and verification

Freeze prompts before editing a proposer:

```powershell
python scripts/evaluate_concept_grounding.py --prepare-only --output tmp/before.json
python scripts/evaluate_concept_grounding.py --prompts tmp/before.json --output tmp/before-results.json --max-calls 18
```

After editing, prepare a separate snapshot and run with the same route and
budgets. A snapshot's corpus digest and selected case coverage are checked before
inference. Malformed output and truncation count as failures, never successful
abstention. Inspect emitted meanings separately from the boolean scores. Frozen
snapshots preserve the rendered age phrases; preparing again on a later day
changes those phrases even though the synthetic source timestamps are fixed.

Focused regressions cover typed admission, subject pairs for new concepts and
reinforcement, citation order/unknown IDs, plan status versus completed sources,
narrative compatibility, answer leakage, stale snapshots, failure accounting and
positive-control loss. The affected selection covers aspiration, tension,
narrative/shared arcs, synthesis routing and evidence lineage; shared imports
also select unrelated session/voice tests, which are excluded by the focused
selection. Run `npm run lint` alongside these checks.

Validation passed: 106 focused tests, then 257 affected tests plus 20 subtests
(2,961 unrelated tests deselected). The final prompt-example cleanup also passed
the 106 focused checks and `npm run lint`. The full suite was not run.

Restart the backend to activate the new prompts. The next useful review is a
fresh source-linked ordinary-use cohort after activation, with particular
attention to compatible preferences and embellished rationales. Historical
concepts need explicit review; this change does not retroactively certify them.
