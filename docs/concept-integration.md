# Concept integration contract (L24)

Concepts are the **upstream source of truth** for the durable views Aiko
holds of the user, herself, and the relationship. A deriver that overlaps
a concept kind **composes from active concepts** and falls back to its own
raw derivation only when the concept layer is sparse/immature. This file
is the contract: the one read path, the routing rules, and the
direction-of-truth table.

## The one read path: `ConceptView`

Every deriver / background worker / prompt-time consumer reads concepts
through a single facade — [`ConceptView`](../app/core/concepts/concept_view.py)
— never through `ConceptStore` directly, and never by re-resolving evidence
or cluster labels itself.

`ConceptView` is constructed from:

- a `ConceptStore` (required), plus
- an optional `topic_graph` and optional `memory_store`.

So a worker takes **one** dependency and gets both "which concepts?" and
"resolve their grounding". Build it with
[`concept_view_from(host)`](../app/core/concepts/concept_view.py), which
reads the host's `_concept_store` / `_topic_graph` / `_memory_store`, or
construct it directly.

| method | question it answers |
| --- | --- |
| `core(subject=, kind=, min_confidence=, limit=)` | "the high-confidence concepts about X" (always-on / who-they-are; turn-agnostic) |
| `relevant(embedding, subject=, kind=, k=, min_sim=)` | "the concepts nearest this turn" (wraps the one `ConceptStore.nearest` primitive) |
| `for_target(target, subject=, ...)` | "the concepts that feed *my* prompt block / subsystem" (the plug-in seam) |
| `for_cluster(rep_id)` | "the concepts spanning this topic cluster" (interest-map annotation seam) |
| `evidence_labels(concept_id, limit=)` | "the human-readable grounding behind this concept" |

Everything degrades to `[]` when the store is missing, and evidence /
cluster resolution returns only what it can when `topic_graph` /
`memory_store` are absent — a consumer that only needs concept lookup can
construct the view with the store alone.

## Routing: `surfacing_targets` is authoritative

A kind declares **where it surfaces**, and consumers ask **which kinds feed
me** — neither branches on kind names. In
[`concept_kinds.py`](../app/core/concepts/concept_kinds.py):

- `ConceptKind.surfacing_targets: dict[str, str]` maps `subject -> target`
  (with a `"*"` wildcard); `surfacing_target` is the subject-agnostic
  fallback. The same kind can feed different consumers per subject — e.g.
  `identity` (and `value`, L10) feed `profile_block` for `subject=user`.
  `subject=aiko` concepts have **no named for_target block** — they surface
  every turn through the T3 `relevant_context` path (core lane + relevance),
  so they carry no `surfacing_targets` entry. Since L11, `subject=aiko`
  concepts are mined in one combined pass over her aiko-dominant self-themes
  (clusters) **and** her self-memories, so they ground on `cluster` evidence
  like the user's concepts (`evidence_labels` resolves aiko cluster reps via
  the shared cluster-label map) — the `src_types=("cluster","concept")`
  grounding filter now renders real "…keeps surfacing around X/Y" for them;
  their `memory` evidence still counts toward confidence/promotion but is
  intentionally kept out of the trimmed grounding clause.
- `affective` concepts (L13, both subjects) are the same story: they carry
  **no** `surfacing_targets`, so they surface only via the T3 `relevant_context`
  relevance path (they are **not** in the always-on core lane — tone guidance
  should appear when the turn's topic matches, not every turn). They ground on
  `cluster` evidence (topic reps annotated with the per-cluster affect map),
  plus — for `subject=aiko` — her affect-stamped self-memories; the affect
  *direction* lives in the concept label/rationale, not on the edges.
- `ritual` concepts (L7, `subject=relationship`) are the same story: they carry
  **no** `surfacing_targets`, so they surface only via the T3 `relevant_context`
  relevance path (not the always-on core lane — a ritual should colour the turn
  when the shared pattern is touched, not every turn). Their evidence is the
  constituent `shared_moment` memories (`memory` edges) grouped by single-link
  cosine ([`ritual_grouping`](../app/core/concepts/ritual_grouping.py)); the
  recurrence lives in the grouping, not on the edges, and rendering routes
  `family="ritual"` through `_concept_ritual_header`.
- `narrative` concepts (L8, `subject=user` **and** `aiko`) are the same story:
  **no** `surfacing_targets`, so they surface only via the T3 `relevant_context`
  relevance path (an arc should be called back to when the turn touches it, not
  pinned every turn). They are the first **`sequence`**-evidence kind — evidence
  is an *ordered* chain of `memory` edges carrying `ordinal` (0..n, temporal
  order), so `evidence_of` / `ConceptView` grounding returns the beats in order;
  the chain is derived from the candidate's `event_time` ordering at synthesis
  time. Rendering routes `family="narrative"` through `_concept_narrative_header`
  (first-person for aiko). A narrative is a *closed* arc, not a rolling recency
  digest (that stays the conversation summary's job); relationship + meta
  narratives are deferred to backlog L29.
- `aspiration` concepts (L14, `subject=user` **and** `aiko`) are the open-ended
  sibling of narrative — the second **`sequence`** kind (same `ordinal` chain,
  same relevance-only surfacing, **no** `surfacing_targets`), naming a
  *direction* someone is moving in rather than a closed arc. Rendering routes
  `family="aspiration"` through `_concept_aspiration_header` (momentum framing,
  first-person for aiko). Beyond the T3 relevance path they also feed a
  **proactive momentum ring**: [`AspirationMomentumWorker`](../app/core/proactive/aspiration_momentum_worker.py)
  reads active aspirations through `ConceptView` (the L24 contract — never the
  store directly), and — staleness-driven — drafts a private check-in cue into
  the `aiko.aspiration_momentum` kv ring; `_render_aspiration_momentum_block`
  surfaces it as a watermark-gated T6 hint the chat model phrases in-context.
- `boundary` concepts (L18, `subject=user` **and** `aiko`) are the
  behaviour-*gating* kind — soft, guiding lines, never refusals ("go gentler
  about his work"; first-person "I won't fake agreement just to please him").
  They are the first kind mined from a **hybrid of topic clusters + explicit
  remembered anchors** (`self_tagged` about the user / `self`+`reflection`+`diary`
  about her), and a **single deliberate anchor can seed one** — the proposer's
  composition rule (`>= 1` anchor OR `>= 2` clusters) lets the L3
  `boundary_evidence_gate` floor the source count at 1. They **join the always-on
  core lane** (`core_always_on=True`, `core_min_confidence=0.8`) yet carry **no**
  `surfacing_targets` (they route through the T3 `relevant_context` path, pinned +
  relevance, not `profile_block`). Rendering routes `family="boundary"` through a
  soft `_concept_boundary_header`.
- **Composite surfacing (per-kind, introduced with L18).** The turn-relevant
  concept fill in `build_relevant_context` no longer ranks by raw cosine alone: a
  `SurfaceWeights` field on each `ConceptKind` + the pure
  [`concept_surfacing.py`](../app/core/concepts/concept_surfacing.py) helper
  (`recency_boost` + `composite_score`) blend **context (cosine) + confidence +
  recency**. Defaults are context-only (so it reproduces the old cosine ranking —
  **opt-in per kind**); `boundary` opts into a recency-heavy blend so a line she
  was just reminded of outranks a stale one. Behaviour concepts weight recency
  higher than identity/value concepts, which barely care about it. The core lane
  + pinned path is unchanged (still confidence-ranked, turn-agnostic).
- `kinds_for_target(target, subject=None)` resolves the set of kind names
  routing to a target. `ConceptView.for_target` consumes it, so a new kind
  auto-flows to the matching consumer with **no consumer code change** —
  just declare its `surfacing_targets`.

## Direction of truth

Each row names the single authoring system for a claim, so the same thing
isn't derived twice.

| view / claim | source of truth | consumer / target | status |
| --- | --- | --- | --- |
| Aiko's self-model (who she is + what she values) | `subject=aiko` concepts (identity + value) | `build_relevant_context` -> T3 `relevant_context` (`yourself` headers) | **shipped (concepts-only)** |
| always-on core lane | `core_always_on` kinds (`identity`, `value`, `boundary`) | `build_relevant_context` | **shipped (migrated)** |
| concept recall tool | active concepts (any subject) | `recall_concept` | **shipped (migrated)** |
| user profile (who he is / what he values) | `subject=user` identity + value concepts | `user_profile` -> `profile_block` | **shipped (L28)** — concepts lead the block; SQLite is the floor and the `values` field is suppressed when a value concept exists |
| cluster annotation | concepts spanning a cluster | `cluster_activity` rows carry `representative_id`; readers resolve via `for_cluster` | **shipped (L28)** — the hot-path `interest_map` stays a bare `(label, size)` read; the rep id rides the mirror-joining `cluster_activity` instead |
| what a territory *means* (map-shape reflection) | concepts spanning the cluster | `KnowledgeMapReflectionWorker` -> `[mindmap]` reflection | **shipped (L28)** — top-`knowledge_map_reflection_concepts_per_cluster` by confidence, appended to each rich territory line |
| why a drifting interest matters | concepts spanning the cluster | `InterestDriftWorker` -> `interest_drift` journal -> inner-life cue | **shipped (L28)** — one most-confident concept, resolved only for the topic actually being drafted |
| transient mood / opinions | K2 beliefs | belief layer | stays transient (not migrated) |
| aspirations / trajectory (where they're heading) | `aspiration` concepts (`user` + `aiko`) | `build_relevant_context` -> T3 relevance + `AspirationMomentumWorker` -> `aspiration_momentum_block` | **shipped (L14)** |
| behaviour boundaries (soft guiding lines) | `boundary` concepts (`user` + `aiko`) | `build_relevant_context` -> core lane + T3 relevance (composite-scored) | **shipped (L18)** |
| abstraction / through-lines (the bigger pattern over several concepts) | `generalization` concepts (`user` + `aiko`) | `build_relevant_context` -> core lane + T3 relevance; children suppressed beneath a present parent | **shipped (L20)** — rides the L12 meta `evidence` rails; single-level only |

## Recipe for a new consumer

1. Take a `ConceptView` (a late-bound provider is fine — see
   `concept_view_from(self)`).
2. Read via `core` / `relevant` / `for_target` / `for_cluster`; resolve
   grounding via `evidence_labels`.
3. If the consumer feeds a named prompt block, declare the kind's
   `surfacing_targets` and read via `for_target(...)`.
4. Fall back to the legacy derivation when the concept result is
   sparse/immature (concepts upstream, raw derivation as the floor).
5. Add a row to the direction-of-truth table above.

The live `ConceptView` consumers are `build_relevant_context` (the T3 core
lane + relevance surfacing, including Aiko's `subject=aiko` self-model) and
the `recall_concept` tool — read those for the end-to-end pattern.

See also [`docs/personality-backlog/concepts.md`](personality-backlog/concepts.md)
(L24 contract, L28 rollout) and [`rules/code-conventions.md`](../rules/code-conventions.md).
