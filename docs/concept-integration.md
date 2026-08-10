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
| `core_lane(limit=, openness_slots=, ...)` | "the concepts pinned into every turn" (balanced across `core_always_on` kinds, plus the openness reserve) |
| `relevant(embedding, subject=, kind=, k=, min_sim=)` | "the concepts nearest this turn" (wraps the one `ConceptStore.nearest` primitive) |
| `for_target(target, subject=, ...)` | "the concepts that feed *my* prompt block / subsystem" (the plug-in seam) |
| `for_consumer(consumer, subject=)` | "the concepts *I* get to think with" — the declared diet, budgeted and kind-balanced (the worker seam) |
| `for_cluster(rep_id, kinds=)` | "the concepts spanning this topic cluster" (interest-map annotation seam) |
| `evidence_labels(concept_id, limit=)` | "the human-readable grounding behind this concept" |

Everything degrades to `[]` when the store is missing, and evidence /
cluster resolution returns only what it can when `topic_graph` /
`memory_store` are absent — a consumer that only needs concept lookup can
construct the view with the store alone.

## The role axis: `anchor` / `guide` / `generative`

Every `ConceptKind` carries a `role` alongside its `importance` prior. The
axis exists because ranking concepts by strength converges on the kinds
that *constrain* Aiko — `boundary` (importance 0.9) and `value` (0.85) sit
at the top of the ladder — and a selection made entirely of those can
restate what she already holds but never reach past it. Naming the
distinction is what lets a mechanism act on it.

| role | what it carries | kinds |
| --- | --- | --- |
| `anchor` | what is true; stable ground | `identity`, `narrative`, `generalization`, `affective`, `ritual` |
| `guide` | what constrains action | `value`, `boundary`, `conduct`, `communication_style` |
| `generative` | what could move | `taste`, `pursuit`, `aspiration`, `tension` |

`kinds_by_role(role)` is the plug-in seam, mirroring `core_lane_kinds()`:
a new kind joins the balance mechanisms by declaring a role, with no
mechanism-side change.

## Diets: what one consumer gets to think with

A consumer declares a [`ConceptDiet`](../app/core/concepts/concept_diets.py)
once and reads `for_consumer(name)`. Before diets, a worker read concepts in
one of three ways — a single hardcoded `kind=`, every kind at once, or not
at all — and none of the three was a decision about what that worker needs
to *understand*, nor measurable: worker prompts have no input-token
accounting, so "read the concept layer" quietly meant "read all of it" as
the store grew.

```python
register_diet(ConceptDiet(
    consumer="belief_inference",
    kinds=("identity", "value", "affective", "taste", "aspiration"),
    subject="user",
    weight=1.0,
    rationale="...",
))
```

Four properties, each doing work a filter tuple wouldn't:

- **Budgeted.** `weight` scales a global allowance derived from the
  *worker* context window (`concept_diet_token_fraction` capped by
  `concept_diet_max_tokens`, floored by `concept_diet_min_tokens`), so a
  reflection pass can be given more room than a one-line cue worker
  without either being able to grow without bound. The window is resolved
  once inside `concept_view_from(host)`, so no call site knows about it.
- **Ranked by `importance x confidence`, within a kind.** Confidence alone
  buries the belief that matters more than it is established — the
  "attention gap" the L22 quality report already tracks. Because
  `importance` is a per-kind prior lifted by affect charge, the prior is
  constant *inside* a bucket, so the axis reorders purely on charge: the
  tastes she feels something about lead the ones she doesn't.
- **Balanced across kinds.** The draw is round-robin, which is what stops
  the previous property becoming the very problem it solves — a global sort
  on `importance x confidence` would return boundaries and values until the
  budget ran out, which is *worse* than confidence-only ranking because it
  would be more confident about being closed. A tight budget therefore
  trims every declared kind evenly instead of dropping the generative ones.
- **Never all rails.** A diet naming any `guide` kind must also name at
  least one `generative` kind — a registry invariant (`diet_problems` /
  `registry_problems`), not a convention, because the failure it prevents
  is silent: a cue worker fed boundaries and values produces cues that keep
  her exactly as she is, and nothing about that looks like a bug from the
  outside. The invariant is deliberately one-directional; a generative-only
  diet needs no guide, since a worker that reads only what could move is
  open by construction.

**The exclusion principle.** *Producers* of concepts do not get diets.
Feeding `ConceptSynthesisWorker` or `HypothesisProposerWorker` the existing
concept set lets the layer confirm itself — new concepts proposed in the
shape of the old ones. Their direct reads for novelty and dedupe answer a
different question ("does this already exist?") and stay as they are.
Workers that produce something *else* — cues, goals, beliefs, wants — are
ordinary consumers and do get diets.

## Openness on the brain line

Diets govern what workers read. Two further mechanisms govern the two
surfaces that reach the chat prompt itself, each of which narrowed her for
a different reason.

**The pinned core lane was structurally closed.** `core_lane_kinds()`
returns only `core_always_on` kinds — `identity`, `value`, `boundary`,
`generalization`: two anchors and two guides, zero generative kinds. With a
core cap of 15, up to 15 concepts were pinned into *every* turn and not one
of them *could* be an aspiration, taste, pursuit or tension. No amount of
tuning reached this. `core_lane(..., openness_slots=)` reserves slots for
the strongest generative-role concepts drawn from kinds that are otherwise
ineligible for the lane (`concept_core_openness_slots`, default 2), gated on
`concept_core_openness_min_confidence` (0.5) so a half-formed aspiration is
not pinned forever. An unfilled reserve falls back to the normal lane, so
nothing is wasted. Reserved picks use the same banded rank and `concept_id`
tiebreak as the rest of the lane, because the lane sits in a
cache-prefix-sensitive tier: the selection moves only when the underlying
concept moves.

Measuring the reserve on a real graph (L28m) corrected three things about
*how* it draws, none of which showed up in the unit tests: it rotates kinds
before subjects (two `aspiration` subject buckets were taking every slot, so
no other generative kind was reachable), it skips kinds that declare
`static_render = False` (a `tension` cannot render in this block, so a slot
spent on one is spent on nothing), and it takes the caller's habituation read
through `openness_rest` so the pinned generative concept rests and rotates
like everything else on the lane.

**The per-turn flex lane was tilted, not closed.** Generative kinds can
reach it, but `surface_score` ends in `boosted * habituation *
importance_factor(...)`, which at the default `concept_importance_strength`
of 0.4 is ~1.16 for `boundary` against ~0.92 for `taste` — a 26 percent head
start on every comparison, only partly offset by habituation and only for
concepts shown recently. `concept_flex_generative_floor` (default 1) adds a
*floor* rather than a reweight: after the ranked pick, if the selection
contains no generative concept and at least one generative candidate cleared
the relevance floor, the weakest selected **guide** is swapped for the
strongest generative one. A floor for the same reason `importance_factor` is
a modulator rather than a summed term — it leaves the blend and its single
tuning knob intact and fires only in the one case that matters, a turn where
the tilt shut generative kinds out entirely. Lowering
`concept_importance_strength` instead would buy the same diversity by
weakening stakes everywhere, including where high stakes should win. The
swap never targets an `anchor`: losing a boundary from one turn is
recoverable, losing the identity concept that says who she is talking to is
not.

**Measurement.** Both lanes report a role mix (anchor / guide / generative
counts, `constraint_ratio = guide / (guide + generative)`, and whether the
floor fired) on the concept trace, and the L22 report in
[`concept_quality.py`](../app/core/concepts/concept_quality.py) carries a
store-wide `roles` section. A floor that fires most turns is itself the
finding: it means the tilt is shutting generative kinds out as a rule rather
than an exception. This is not habituation — habituation fights *repetition*
of one concept, this fights *composition* skew across kinds.

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
- `narrative` concepts (L8, `subject=user` **and** `aiko`; L29a adds
  `relationship`) are the same story:
  **no** `surfacing_targets`, so they surface only via the T3 `relevant_context`
  relevance path (an arc should be called back to when the turn touches it, not
  pinned every turn). They are the first **`sequence`**-evidence kind — evidence
  is an *ordered* chain of `memory` edges carrying `ordinal` (0..n, temporal
  order), so `evidence_of` / `ConceptView` grounding returns the beats in order;
  the chain is derived from the candidate's `event_time` ordering at synthesis
  time. Rendering routes `family="narrative"` through `_concept_narrative_header`
  (first-person for aiko, third-person plural for `relationship`). A narrative is
  a *closed* arc, not a rolling recency digest (that stays the conversation
  summary's job). **L29a** adds the `relationship` subject — a closed joint
  project whose beats are `shared_moment` rows, cut into episodes by topical
  coherence *and* temporal contiguity rather than sourced from topic clusters
  (`"shared_arc"` population). The meta-narrative over other concepts stays
  deferred as backlog L29b.
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
| always-on core lane | `core_always_on` kinds (`identity`, `value`, `boundary`, `generalization`) + the openness reserve (strongest `generative`-role concepts, otherwise ineligible) | `build_relevant_context` -> `core_lane(openness_slots=)` | **shipped (L28)** — the reserve is what keeps a pinned lane of two anchors and two guides from being the whole of what she carries every turn |
| per-turn flex concepts | `surface_score` ranking + the generative floor | `build_relevant_context` -> T3 relevance | **shipped (L28)** — the floor swaps the weakest selected guide for the strongest generative concept, only on a turn whose pick is generative-free |
| concept recall tool | active concepts (any subject) | `recall_concept` | **shipped (migrated)** |
| user profile (who he is / what he values) | `subject=user` identity + value concepts | `user_profile` -> `profile_block` | **shipped (L28)** — concepts lead the block; SQLite is the floor and the `values` field is suppressed when a value concept exists |
| cluster annotation | concepts spanning a cluster | `cluster_activity` rows carry `representative_id`; readers resolve via `for_cluster` | **shipped (L28)** — the hot-path `interest_map` stays a bare `(label, size)` read; the rep id rides the mirror-joining `cluster_activity` instead |
| what a territory *means* (map-shape reflection) | concepts spanning the cluster, scoped to the `knowledge_map_reflection` diet | `KnowledgeMapReflectionWorker` -> `[mindmap]` reflection | **shipped (L28)** — capped by `knowledge_map_reflection_concepts_per_cluster` and spread across kinds before going deep into one, so two slots aren't both spent on rails |
| why a drifting interest matters | concepts spanning the cluster, scoped to the `interest_drift` diet | `InterestDriftWorker` -> `interest_drift` journal -> inner-life cue | **shipped (L28)** — one most-confident concept, resolved only for the topic actually being drafted; the diet is what stops a `boundary` winning the "why she cares" slot and turning a pull into a rail |
| which tension to raise | `tension` concepts (`tension_cue` diet) | `TensionCueWorker` -> tension cue ring | **shipped (L28)** — same single kind as before, now *declared*: a hardcoded `kind=` is invisible to the registry audit |
| whether a trajectory has gone quiet | `aspiration` concepts (`aspiration_momentum` diet) | `AspirationMomentumWorker` -> `aspiration_momentum_block` | **shipped (L28)** — declared for the same reason |
| things of hers she could offer | `subject=aiko` `pursuit` concepts (`wants_ledger` diet) | `WantsLedgerWorker` -> wants ledger | **shipped (L28)** — the last direct `ConceptStore` reader; it had its own copy of the status / subject / kind filter and its own confidence sort, neither kept in step with the view |
| what she could wonder about next | the `forward_curiosity` diet (`aspiration`, `affective`, `taste`, `pursuit`) | `ForwardCuriosityWorker` -> curiosity cue ring | **shipped (L28)** — a fourth candidate pool keyed `concept:{id}`, riding the existing `oq:` dedupe and subject quota; before it, a written-down memory row was the only thing she could be curious about |
| Aiko's quiet long-term goals | `subject=aiko` `aspiration` concepts lead; K1 `kind="goal"` rows are the floor | `_render_goals_block` | **shipped (L28)** — composed in the renderer, so `GoalStore`'s write path and cosine dedupe are untouched; an aspiration is who she is *becoming*, a goal is a to-do, and the block keeps them apart |
| transient mood / opinions | K2 beliefs | belief layer | **stays transient (decided, L28)** — a belief is a prediction about right now; what shipped is the *bias*, a `concept_hint` prior on the extractor (one-directional: nothing writes back, or the layer confirms itself) |
| her stance when he contradicts one | `kind="self"` memories **and** the `stance` diet (`subject=aiko` `value` / `taste` / `pursuit`) | `_render_opinion_injection_block` -> K29 cue | **shipped (L28)** — concept candidates skip the opinion-shape regex (their kind already establishes them as stances) and carry a `stance_origin` so the cue can't claim she *wrote* one |
| how to talk *now* (delivery style) | `communication_style` concepts (both subjects) | `build_relevant_context` -> T3 relevance only | **resolved (L28): stays on T3** — no legacy derivation worth flooring, style is inherently turn-dependent, and it is a `guide` kind, so pinning it would add a fourth standing guide surface |
| aspirations / trajectory (where they're heading) | `aspiration` concepts (`user` + `aiko`) | `build_relevant_context` -> T3 relevance + `AspirationMomentumWorker` -> `aspiration_momentum_block` | **shipped (L14)** |
| behaviour boundaries (soft guiding lines) | `boundary` concepts (`user` + `aiko`) | `build_relevant_context` -> core lane + T3 relevance (composite-scored) | **shipped (L18)** |
| abstraction / through-lines (the bigger pattern over several concepts) | `generalization` concepts (`user` + `aiko`) | `build_relevant_context` -> core lane + T3 relevance; children suppressed beneath a present parent | **shipped (L20)** — rides the L12 meta `evidence` rails; single-level only |

## Recipe for a new consumer

1. Take a `ConceptView` (a late-bound provider is fine — see
   `concept_view_from(self)`).
2. Declare a `ConceptDiet` for it in
   [`concept_diets.py`](../app/core/concepts/concept_diets.py) — kinds,
   subject scope, `weight`, and a `rationale` a reader can argue with — and
   read via `for_consumer(name)`. Skip this only for a *producer* of
   concepts (the exclusion principle above) or a consumer whose question is
   genuinely "the concepts nearest this turn" / "spanning this cluster",
   which are `relevant` / `for_cluster`. Never a hardcoded `kind=`: the
   registry can only audit reads it can enumerate.
3. Resolve grounding via `evidence_labels`.
4. If the consumer feeds a named prompt block, declare the kind's
   `surfacing_targets` and read via `for_target(...)`.
5. Fall back to the legacy derivation when the concept result is
   sparse/immature (concepts upstream, raw derivation as the floor).
6. Add a row to the direction-of-truth table above.

The live `ConceptView` consumers are `build_relevant_context` (the T3 core
lane + relevance surfacing, including Aiko's `subject=aiko` self-model) and
the `recall_concept` tool — read those for the end-to-end pattern; for a
diet-shaped worker read `ForwardCuriosityWorker` or
`_render_opinion_injection_block`.

See also [`docs/personality-backlog/concepts.md`](personality-backlog/concepts.md)
(L24 contract, L28 rollout — shipped), [`docs/configuration.md`](configuration.md)
for the diet / openness knobs, and
[`rules/code-conventions.md`](../rules/code-conventions.md).
