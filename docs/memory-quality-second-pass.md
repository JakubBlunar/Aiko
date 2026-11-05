# Memory quality second pass — 5 November 2026

Worker quality takes priority. Concepts may guide useful questions, but a worker
must never turn an existing interpretation into a new independent observation.

## Reflective worker output

The post-turn reflection prompt previously required an observation even when
nothing stood out. It also had no earlier-note context to discourage repetitive
general appreciation, and stored its own follow-up wishes as generic callbacks.
That generic kind could be read as a user observation downstream.

The worker now permits complete abstention, asks for meaningful new details or
uncertainty, and receives at most six short dated private notes for dedupe only.
It distinguishes its own feelings from user motives/actions and restricts
callbacks to specific unresolved threads. Nontext JSON fields are rejected.
All three output kinds retain explicit Aiko-owned origins; callbacks are
intentions, questions remain unanswered questions, and exchange receipts include
both known message IDs with `context_window` scope.

Legacy callbacks with the reflection worker's turn receipt are interpreted as
Aiko intentions, including rows stamped with the earlier generic kind contract.
Unlabelled callbacks remain subject-unknown; validated user admission still
takes precedence. No historical text or record is rewritten by this read policy.

Validation: 138 affected tests passed, 7,357 deselected; lint passed. The live
debug endpoint was unavailable, so there is no live generation-quality claim.

## Graph evidence observed read-only

The local SQLite snapshot had 3,325 assigned memories, including 99 marked as
absorbed by consolidation and five superseded rows. It also contained 733
archived members. Archival alone is retention policy and does not make a true
historical event unsuitable for topical organization. Absorbed aliases and
superseded claims should not independently shape active topic density.

Private count artifacts remain in the ignored second-pass audit directory.

## Topic workers and grouping coherence

Topic digests and generated labels were cached mainly by cluster size, which
missed changed source wording, corrections and equal-size membership changes.
Both now use semantic source fingerprints. They select bounded, recent and
role-balanced inputs; preserve event-time, subject, mode and provenance tags;
and recheck sources after generation. A source edit/deletion during a model call
defers that output. Digest receipts list the actual offered memory IDs and
revisions. Neither a question nor a hypothetical draft supplies factual summary
evidence. User-pinned labels retain their explicit policy; a changed source
creates a new derived digest instead of overwriting a pinned summary.

The topic graph now excludes absorbed aliases, superseded records, topic digests
and hypothetical prepared answers from topical density and novelty/coverage
checks. Ordinary archive history remains eligible. Warm-start reconciles old
assignments against current records. Memory-store update notifications refresh
memberships and centroids after edits/re-embedding, including writes from workers
that do not broadcast UI notifications. Deletes also refresh the remaining
centroid. This improves input coherence without replacing the clustering
algorithm or treating related sources as independent evidence.

Validation: the representative affected selection passed 886 tests and 86
subtests, with 6,744 deselected. Label/update follow-up passed 84 tests, with
6,366 deselected. Additional source/edit-race/pin contrasts passed; lint passed.
These tests use real SQLite stores and controlled model responses, not live
generation or a full-suite run.
