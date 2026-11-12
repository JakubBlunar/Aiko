# Memory and topic correctness review — 5 November 2026

This pass follows [memory-quality-second-pass.md](../rollouts/memory-quality-second-pass.md).
The aim remains grounded concepts over memories: preserve what happened, who it
concerned, when it applied and what the sources actually establish. More generated
notes are not automatically more knowledge.

## Read-only findings

Runtime diagnostics reported 127 topic clusters and 3,731 assignments. The SQLite
review found 5,475 memories, 105 retained consolidated records outside topic
digests, and 112 absorbed aliases. In 21 consolidated records, retained original
sources had recorded observation roots absent from the surviving note's lineage.
That is lost accessible provenance, not proof that those notes were false.

The sampled 72-hour cohort had 247 rows, including 36 user observations, 50 Aiko
reflections, 56 Aiko intentions and 30 user intentions. There were 62 admission
receipts and 21 explicit event times. These are different roles, not a common
denominator for a quality score. Durable preferences need not have an event date;
intentions and diary prose cannot substitute for observed actions. The recent
cohort spans several implementation versions and is not a controlled evaluation.

Three owning-source findings matter:

- The topic update listener removed and re-added a memory on every update,
  including metadata/salience changes. The last member of a small topic could
  lose its cluster identity even though its embedding had not changed.
- Incremental addition used a normalized centroid multiplied by member count as
  if it were the original vector sum. Replayed add notifications could also
  reweight an already-present point. Both can drift topic geometry.
- Both consolidation paths retained memory IDs/revisions but did not preserve
  all observation roots from absorbed inputs. They also lacked a final source
  recheck against corrections or deletion during model generation.

## Implemented

### Stable geometric membership

The graph caches small, content-free vector fingerprints for assigned and newly
pending points. Metadata-only updates clear derived coactivation caches but keep
membership and topic identity. Eligibility changes still remove superseded,
absorbed or otherwise excluded rows. A changed vector still goes through genuine
reassignment. Replayed add notifications neither reweight an existing member nor
inflate pending-refit demand.

Warm-start and full rebuild seed the fingerprints. A genuinely new assigned point
gets an exact centroid recomputation from current members, rather than accumulating
renormalization error. This reads one affected cluster's vectors; metadata-only
notifications avoid those reads. It does not change clustering thresholds or
reorganize the live database during development.

### Historical observation continuity

Both consolidators freeze a bounded `consolidation_lineage` receipt from the actual
input records before modifying them. It carries existing message/URL roots, source
categories, completeness and visible truncation. Repeated compaction carries prior
receipts forward. Unknown legacy origins remain unknown; original records remain
available as archived sources. No source ID is invented for missing testimony.

The receipt is explicitly `scope=historical_inputs`. It is separate from the
`support_observations` used for genuinely arriving duplicate observations and is
excluded from `memory_revision`. Merely packaging existing roots must not refresh
a concept's observation clock. There is no global revision-hash migration, new
confidence vote, promotion change or historical support-manifest backfill.
Existing frozen concept manifests retain their historical scope; future captures
can include the survivor's preserved history. Source separation still does not
prove independence or that every root entails every clause of a merged note.

Consolidators now recheck source content, role/time/provenance, tier, pin status
and relevant lineage metadata after generation/re-embedding. A changed or deleted
source defers the merge. A failed primary write cannot archive the other inputs.
A victim changed after the primary commit is retained instead of being absorbed.
This narrows generation races; it is not a database-wide transaction across a
long-running model call.

No existing live memory or topic assignment was rewritten during this review.
The fixes load after restart and apply through normal update/consolidation paths.

## Highest-value next data

1. **Per-topic evidence profiles.** Expose known observation groups separately
   from note counts, with actor/source-role mix, actual event-time span, recording
   span, unresolved questions and unknown provenance. Use these in focus packets
   for concept synthesis. A cluster title or a digest is an organizing summary,
   not another observation. Counts should describe their limitations instead of
   feeding a new confidence formula immediately.
2. **Situation and outcome receipts.** Preserve an explicitly supported context,
   action, reported outcome and relevant exception alongside a source quote.
   For example, a request for shorter replies while tired and a preference for
   detail while debugging can coexist. Unknown fields remain empty; the worker
   must not invent a trigger, motive, outcome or event date to complete a schema.
3. **Follow-through over time.** Link a later reported action/completion or changed
   plan to the original intention. Keep planned, reported-completed, cancelled and
   unknown distinct. This supplies actual development to aspiration/narrative
   concepts instead of mistaking repeated promises or elapsed time for progress.
4. **Counterexamples and changed applicability.** Retain a later correction or a
   different situational preference as useful evidence, rather than forcing one
   universal trait or treating every difference as a contradiction. A lone episode
   should remain a lone episode until additional observations justify a pattern.

The first step is mostly a read model over existing records, so it can improve
what the model sees without another generative worker. The second and third add
meaningful longitudinal data when the conversation actually supplies it. They
should be evaluated with paired examples before changing concept promotion gates.

## Validation

Focused contrasts cover singleton identity, warm-start, duplicate notifications,
exact centroids, pending demand, preserved roots, repeated compaction, reflections,
unknown/truncated history, stale merge inputs and failed primary writes. The
representative affected selection covers topic organization, consolidation,
concept synthesis, grounding, source lineage, dependencies and applicability.
Lint remains required. These checks verify contracts and lifecycle, not the
semantic quality of future model output or the independence of real-world events.
