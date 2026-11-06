# Reflective producers and source meaning

These changes follow the 6 Nov producer review. The aim is grounded knowledge
over Aiko's memories, while preserving subjective reflection and creative inner
life. Source links explain how something was generated; they are not independent
observations or a new confidence formula.

## Goal reflection

The reflection worker receives up to six whole, relevant memory records from a
bounded recent pool, plus deliberate notes on the goal. Existing generated
reflections provide continuity and dedupe context, not new evidence. Candidate
selection uses existing local vectors and lexical overlap, without another model
call. Source roles, temporal scope, recording age and qualifications remain visible.

Notes distinguish reflection, intention, reported step and reported outcome.
Reported steps cannot cite plans or earlier generated reflections as execution;
outcomes require a recorded outcome/completion receipt. Reflection and intention
can still stand as Aiko's own thoughts. These structural checks do not prove that
the wording follows semantically from the cited source.

The worker records an assessment of the goal and offered source revisions.
Unchanged context does not produce another model call or progress memory. The
assessment timestamp rotates attention across goals without incrementing progress
count. New relevant evidence, a goal change or a deliberate note can reopen
reflection. Token-overlap novelty checks reject close rewordings across goal
notes, while preserving changed negation, numbers and conditions. This is a
conservative heuristic, not a semantic equivalence test.

Source and goal revisions are checked after generation and again at persistence.
`reflective_derivation` retains actual input IDs/revisions and cited IDs under
`generation_context` scope. It never supplies new observation roots. Confidence,
promotion policy and deliberate tool/manual note retention remain unchanged.

## Shared moments

The detector returns one to three supported clauses, narrow literal message
citations and an event scope: exchange, recalled event or shared virtual scene.
Only those clauses are stored; an additional unchecked summary cannot add detail.
Unknown IDs, fabricated quotes, unsupported frequency/completion and missing
qualifiers are refused. Recalled-event clauses require user-report citations.

Exchange time comes from the cited exchange lines, not the last unrelated line
in the window. Recalled event time requires a literal timing phrase; recording
time does not fill an unknown occurrence date. Day-only timing remains day-only.
Unknown event dates remain explicitly unknown and cannot trigger anniversaries.
Shared virtual scenes retain their virtual domain and relational meaning.

`moment_evidence` preserves source quotes, revisions, timestamps, clause scope and
time basis. Persistence rechecks sources after embedding; downstream source
checks reject edited/deleted receipts. Differently evidenced moments cannot be
silently merged. Semantic revisions include this field only when present, keeping
ordinary legacy hashes stable. A deliberate description/date edit preserves the
old receipt as historical context instead of certifying new wording with it.
The detector has a 512-token minimum for the citation-bearing answer, without an
extra model call. Legacy anonymous tuples cannot authorize invented source IDs.

## Dream derivation

Production dream providers pass memory rows instead of clipped content strings.
The worker packs whole, dated, role-labelled records within a bounded packet,
skipping records that do not fit rather than cutting off an exception. It records
the actual offered IDs/revisions and rechecks them after generation/embedding.
A changed input defers the output and releases an episode reservation.

Legacy text and derived summaries can still supply association, with unknown
input identity recorded explicitly. Oversized text is omitted as a whole. Topic
labels provide orientation, never evidence that a subject recurred. The dream
remains an associative reflection, not testimony that the imagined event happened.
Derivation metadata does not create observation roots or refresh support clocks.
Sleep-episode ownership and existing model-call/retention limits remain intact.

## Activation and evaluation

No live memories were rewritten and no worker was forced during implementation.
Restart is required. The existing L22 post-restart review checklist also covers
these producers: sample actual goal changes, repetitions, narrow moment sources,
unknown/past event dates and preserved dream qualifications after ordinary use.
Check useful output and valid abstention alongside refusals. Controlled regressions
verify these boundaries; future model-output quality still needs a fresh cohort.

Focused producer integration passed 104 tests. The representative affected
selection passed 795 tests and 79 subtests, covering goals, moments, anniversaries,
dreams/sleep, source revisions, post-turn routing and concept admission. Final
targeted contrasts also passed. Full lint passed; the full suite was not run.
