# Shipped -- Higher-order concepts (L-series)

Part of the [shipped log index](../shipped.md). The concept layer's landed
foundations: the store and lifecycle engine, the kinds that reached
production, and the integrity / observability work underneath them. Open items
from this family -- L31 concept fission, the L30 hypothesis family, and the
remaining tuning in L22 -- still live in [`concepts.md`](../concepts.md), which
also carries the design preamble for the whole layer.

---

## L1. Concept store + schema (kind-parameterized)

**Motivation.** A concept is a first-class, long-lived entity with a
lifecycle, evidence, and confidence — not an atomic fact, so it doesn't belong
in `memories`. It needs its own table so the candidate -> active promotion
machinery has somewhere to live. Critically, the schema must be **kind-agnostic
from day one** so adding value / self / tension / narrative concepts later is a
registry entry, not a migration.

**Key files.** New `app/core/concepts/concept_store.py` (`ConceptStore`,
modeled on
[`topic_cluster_store.py`](../../../app/core/conversation/topic_cluster_store.py)),
schema bump in
[`app/core/infra/chat_database.py`](../../../app/core/infra/chat_database.py).

**Sketched approach.** Two SQLite tables, both kind-general:

- `concepts` — `id`, `label`, `kind` (`identity` for v1; open enum), `subject`
  (`user` / `aiko` / `relationship`), `user_id` (scoping — user concepts are
  per-user; `subject=aiko` concepts are global, `user_id` NULL),
  `evidence_model` (`cluster_set` / `memory_chain` / `recurring_pattern` /
  `concept_graph`), `status` (`candidate` / `active` / `dormant` / `retired`),
  `confidence` REAL, `evidence_count` INT, `distinct_source_count` INT
  (generalises `cross_cluster_count` — distinct clusters, memories, or concepts
  depending on `evidence_model`), `rationale` TEXT, `embedding` BLOB (label
  embedding, for dedupe + recall), `created_at`, `updated_at`,
  `last_reinforced_at`, `promoted_at`, plus **provenance**: `origin_session`
  and `first_evidence_at` (when/where the concept was first born — needed for
  the L19 autobiography "I remember when I first realised…" and for
  explainability in the L6 UI).
- `concept_edges` — **one typed influence graph**, not a per-feature link table
  (see "One graph + one engine" above). Columns: `src_type` + `src_id`,
  `dst_type` + `dst_id` (node types `concept` / `cluster` / `memory`),
  `relation` (`evidence` / `references` / `influences` / `contradicts` /
  `generalizes`), `polarity` (`+1` / `-1`), `strength` REAL, `ordinal`
  (nullable — sequence order for `memory_chain` kinds), `created_at`. This one
  table subsumes:
  ordinary **evidence** (`memory|cluster -> concept`, `relation=evidence`), the
  **meta-concept reference** that lets a tension point at other concepts
  (`concept -> concept`, `relation=references`, L12), the **belief-revision**
  contradiction edge (`concept <-> memory`, `relation=contradicts`, L15), and
  **cross-concept influence** such as a trust concept modulating a boundary's
  plasticity (`concept -> concept`, `relation=influences`, L16). A new linkage
  is a new `relation` value, never a new table.

**Kind registry.** A `ConceptKind` spec object (in `concept_store.py` or a
sibling `concept_kinds.py`) declares, per kind: `subject`, `evidence_model`,
its proposer callable (L2), its promotion gate (L3), and its surfacing target
(L5). The store, worker, and prompt layer all dispatch through this registry so
none of them grow a per-kind `if/elif` ladder.

**Single-writer engine.** Per "One graph + one engine" above, the store exposes
edge + concept CRUD, but **only the L3 lifecycle engine mutates**
`confidence` / `plasticity` / `status`. The proposer (L2) creates candidates and
`evidence` edges; everything that *changes* a concept over time (accrual, decay,
drift, cascade, revision) is a pass of that one engine, never a second worker.

**Meta-concepts (concept-graph edges) — design for this from day one.**
Because `concept_edges` allows a `concept -> concept` edge, a concept can
reference *other concepts* as its evidence. This is a **first-class capability of
the schema, not special-cased to tension (L12)** — any future kind (e.g. an
aspiration concept built on identity + value concepts) can be a meta-concept.
That generality is powerful but imposes four rules the L2/L3 machinery must
respect, so they belong in the data model now rather than being rediscovered at
implementation time:

1. **Dependency ordering.** A meta-concept can only be proposed once its
   referenced concepts are `active`. The synthesis worker (L2) must run
   `cluster_set` / `memory_chain` / `recurring_pattern` proposers **before**
   `concept_graph` proposers within a cycle, so bases exist before anything
   references them.
2. **Cascade on lifecycle change.** When a base concept is retired / disproven /
   merged (L3, L9), its dependents must be re-evaluated — a tension whose
   "Maker Mode" side was retired is moot. Model this as an explicit dependency
   edge (the `concept -> concept` edge in `concept_edges` *is* that edge) and walk dependents on any
   status transition; a retired base cascades its dependents to
   re-validation, not silent staleness.
3. **Confidence bounding.** A meta-concept can't be more certain than what it's
   built on: `confidence <= min(referenced concept confidences)`. Enforce at
   accrual time (L3) so a shaky base can't prop up an over-confident tension.
4. **Depth cap + cycle guard.** Start with meta referencing **only base
   (non-meta) active concepts** — no meta-of-meta — and reject any proposal that
   would form a reference cycle. Relax the depth cap only if a concrete need
   appears.

**Stable-key caveat.** For `cluster`-type evidence, cluster ids are reassigned
on every batch refit (`TopicGraphRebuildWorker`), so links must key off the
cluster's **representative-member id** (the stable key the label/digest caches
already use in `kv_meta`), not the raw `cluster_id`. Re-resolve to live
`cluster_id` at read time. (`memory` and `concept` refs are already stable ids.)

**Storage & retrieval — SQLite is the source of truth, cosine in-process, no
LanceDB.** Concepts live in a different cardinality regime than memories, and
the retrieval strategy follows from that. Memories number thousands→tens of
thousands, so RAG needs an ANN index (LanceDB). Concepts are an *abstraction*
over clusters (which are themselves an abstraction over memories), so their
count stays small — tens, maybe low hundreds even after years; a corpus of
thousands of concepts would mean the layer had failed at its job of
compressing. The precedent to copy is the topic graph, not the memory store:
[`topic_graph.py`](../../../app/core/conversation/topic_graph.py) does **not** put
cluster centroids in LanceDB — it keeps the handful of centroids in memory and
does nearest-centroid as a tiny numpy matmul; only the *memory* vectors go to
LanceDB. Concepts are the centroid regime. So:

- **SQLite `concepts.embedding` BLOB is the source of truth** (float32, same
  encode/decode + dimension-drift guard as `ClusterRow.centroid` in
  [`topic_cluster_store.py`](../../../app/core/conversation/topic_cluster_store.py)
  — a stale-dimension blob decodes to empty and is re-embedded, never crashes).
- **`ConceptStore` holds an in-process embedding mirror** — `dict[id ->
  unit-norm float32]` plus a stacked matrix of the `active` set — refreshed on
  every write and **warm-loaded on boot** via a `load_all()` (mirroring
  `TopicClusterStore.load_all`). Vector similarity is a cosine matmul over that
  small in-memory matrix, sub-millisecond at this scale, no index required.
- **One shared retrieval primitive.** Since the user is right that concepts get
  read from many places — L2 dedup, L5 `recall_concept`, L23 per-turn selection,
  L24 derivers — the store exposes a single `nearest(query_vec, *, subject=None,
  kind=None, status="active", k=...)` (cosine over the mirror, filtered) that
  every one of those call sites uses. Retrieval logic lives in one method, not
  scattered SQL/embedding code per consumer.
- **SQLite indices cover the non-vector queries** (the lifecycle engine's
  filtered scans and the graph walks): `idx_concepts_status`,
  `idx_concepts_subject_kind`, `idx_concepts_user`, and on the edge table
  `idx_concept_edges_src (src_type, src_id)` + `idx_concept_edges_dst (dst_type,
  dst_id)` for O(indexed) cascade/dependents traversal (L1 rules 2, L25).

**Escape hatch (why this doesn't box us in).** If the `active`-concept count
ever genuinely explodes, the fix is exactly the topic graph's: keep SQLite as
the source of truth and add an ANN index later — the `nearest()` seam is the
one place that would change, so nothing here blocks a future LanceDB mirror. It
also composes with the growth-bounding already planned (L20 generalization
merges + P32 snapshot thinning keep the `active` set small *on purpose*), so the
in-memory cosine stays trivial by design rather than by luck.

**Effort.** Medium.

---

## L3. The lifecycle engine — accrual, promotion, and the single writer

> **Built (v1).** The canonical state-machine + ownership reference now
> lives in [`docs/concept-lifecycle.md`](../../concept-lifecycle.md): status
> vocabulary, transition triggers + governing settings, the
> engagement-driven confidence model, the event mapping, and the
> invariants. The notes below are the original design sketch.

**Motivation.** The core of the "don't jump to conclusions" guard, and — per
"One graph + one engine" above — the **single writer** of every concept's
`confidence` / `plasticity` / `status`. A single LLM hunch shouldn't become part
of Aiko's worldview; it has to earn it. This entry owns the "validate" half; the
cross-cutting passes **L15 (belief revision), L16 (plasticity), and L17 (drift
noticing) are passes of this same engine**, not separate workers — that's what
keeps two workers from racing to mutate the same `confidence` along different
edges.

**Key files.** One `ConceptLifecycleWorker` (idle), modeled on
[`memory_promotion_worker.py`](../../../app/core/memory/memory_promotion_worker.py)
+ the `beliefs` status lifecycle. The L2 proposer creates candidates; this
worker is the only thing that changes them thereafter.

**Sketched approach.** Each cycle (all passes over the L1 `concept_edges` graph):

- Re-observed candidates gain confidence; new memories that match a candidate
  (embedding + cluster membership) reinforce it and bump `evidence_count` /
  `last_reinforced_at`.
- Promote `candidate -> active` only when **distinct_source_count >= 2** AND the
  candidate has been **stable over N cycles / M days** AND `confidence` clears
  a threshold. (The three tests from the brainstorm: does it keep appearing?
  does it connect multiple pieces of evidence? is it stable over time?) The
  gate is a per-kind callable on the `ConceptKind` registry (L1), so a narrative
  concept can gate on "chain coherent + closed" and a tension concept on "both
  referenced concepts are themselves active" instead of the cluster-count rule.
- Decay the unreinforced: stale candidates are dropped; an `active` concept
  whose evidence clusters go quiet slides `active -> dormant -> retired`.
- **Disproof, not just decay** (see L9): confidence can be actively *lowered*
  by contradicting evidence, not only eroded by silence — an `active` concept
  can weaken back toward `candidate` or be marked contradicted.
- **Meta-concept cascade + bounding** (per L1): on any base concept's status
  transition, walk its `concept -> concept` edge dependents and re-evaluate them (a
  retired base moots its tensions); and clamp every meta-concept's confidence to
  `min(referenced concept confidences)` at accrual time so a shaky base can't
  prop up an over-confident meta-concept.

**Open questions.** Confidence function (linear reinforcement vs. logistic)?
Promotion thresholds (N, M, min confidence)? Should a user's explicit
confirmation (L6) hard-promote regardless of counts?

**Effort.** Medium.

---

## L7. Relationship concepts (SHIPPED)

**Kind.** `ritual`, subject `relationship`, evidence model `set` (the evidence
is the constituent `shared_moment` memories; the recurrence lives in the
grouping, not on the edges).

**Status: SHIPPED.** Not about the user or Aiko individually — about the
relationship itself: named recurring rituals mined from the shared-moment
stream ("Friday late-night debugging sessions", "winding down together at the
end of a hard day", "talking through nerves before a release"). Surfaces as
warm relationship colour, held lightly, never announced. Distinct from
`catchphrase` (a recurring *phrase*) and from the relationship-phase block (the
arc, not a specific ritual).

**Grouping (the new plumbing).** `shared_moment` rows already carry a `vibe` +
`when` in metadata and an embedding on the row; what was missing was the
*grouping*. [`ritual_grouping`](../../../app/core/concepts/ritual_grouping.py) is a
pure single-link cosine clustering over the moment embeddings (two moments join
when their cosine clears a threshold; a group is the connected component). Each
surviving component (`>= group_min_size`) is annotated with a dominant `vibe`
and an optional weekday hint parsed from `when`, plus trimmed member
`MomentLite`s. No store / settings / LLM imports (numpy only), so it unit-tests
in isolation; the worker builds its inputs via `moment_from_memory`.

**Synthesis.** A new `"shared_moments"` population + `_run_ritual_pass` in
[`concept_synthesis_worker`](../../../app/core/concepts/concept_synthesis_worker.py):
it enumerates `iter_by_kind("shared_moment")`, skips below a min-moments floor,
groups + caps them, and offers each group to the
[`relationship_ritual`](../../../app/core/concepts/proposers/relationship_ritual.py)
proposer, which names the recurring pattern (or reinforces a known ritual by id).
A NEW ritual must draw on `>= min_sources` distinct moments. Count + max-id
watermark dirty-tracking under the proposer's `sig_key` so a settled corpus is a
fast no-op; the whole pass is gated by `agent.ritual_synthesis_enabled`.

**Registry + gate.** `ritual` is registered (`set` model, mid plasticity `0.4`,
`core_always_on=False`, **no** `surfacing_targets`) with `ritual_evidence_gate`
(recurrence floors: `>= 3` distinct moments, a non-instant age, a `0.65`
confidence bar — so a burst in a single session can't promote).

**Surfacing.** Relevance-only (like `subject=aiko` / `affective` concepts): a
ritual surfaces through the T3 `relevant_context` path when the turn touches the
shared pattern, under `_concept_ritual_header` relationship framing, never
pinned every turn.

**Effort.** Shipped on top of L1-L5 / L10 / L11 / L13. New settings:
`agent.ritual_synthesis_enabled`, `concept_synthesis_ritual_min_moments`,
`concept_synthesis_ritual_group_min_size`,
`concept_synthesis_ritual_group_similarity`,
`concept_synthesis_max_ritual_groups`.

---

## L8. Narrative concepts (SHIPPED — both subjects)

**Kind.** `narrative`, subject `user` (default) and `aiko`, evidence model
**`sequence`** — the first ordered-evidence kind. A concept's evidence is an
*ordered chain of specific memory ids* (a temporal/causal arc), stored on
`concept_edges.ordinal` (0..n) rather than as an unordered set.

**Status: SHIPPED.** Humans think in stories. A causal chain of episodic
memories — bought GPU -> installed GPU -> driver issue -> found CPU instability
-> fixed Core 4 — now collapses into one referenceable arc ("The Great 13900KS
Investigation"). Runs for **both subjects**: user arcs (third-person, over his
topic clusters) and Aiko's own arcs (first-person, over her aiko-dominant
self-themes — "the stretch where I learned to hold a gentle stance"). Once
named, the arc becomes shared history Aiko can call back to.

**Ordinal wiring (the new plumbing).** The `sequence` model reuses the existing
`concept_edges.ordinal` column: `_add_evidence_edges` now takes an
`evidence_model` and, for `sequence`, stamps each edge's ordinal by its position
in the (temporally ordered) evidence list, so `evidence_of` returns the chain in
order. `set` kinds are unchanged (ordinal stays `None`). `_persist` passes the
proposal's model; `_reinforce` passes the existing concept's model.

**Synthesis.** A new `"narrative"` population + `_run_narrative_pass(subject)` in
[`concept_synthesis_worker`](../../../app/core/concepts/concept_synthesis_worker.py):
for each subject-dominant topic cluster it loads the member memories via
`get_many` and orders them by `event_time` (falling back to `created_at`),
offering up to `max_narrative_clusters_per_run` clusters (per subject) as
[`NarrativeCandidate`](../../../app/core/concepts/proposers/base.py)s (each capped
at `max_narrative_memories` steps, min `narrative_min_chain` to count as a
story). The shared [`propose_narrative`](../../../app/core/concepts/proposers/base.py)
body (used by [`narrative_user`](../../../app/core/concepts/proposers/narrative_user.py)
/ [`narrative_aiko`](../../../app/core/concepts/proposers/narrative_aiko.py)) names
any **closed** arc, re-derives the chain order from the candidate (not the LLM's
id order) so ordinals are correct, and emits `sequence` evidence — or reinforces
a known arc by id. Open (`closed: false`) or too-short chains are dropped. Per-
subject size/label dirty-tracking under each proposer's `sig_key`; the whole
pass is gated by `agent.narrative_synthesis_enabled`.

**Registry + gate.** `narrative` is registered (`sequence` model, low-ish
plasticity `0.3` — a settled story resists churn, same band as identity;
`core_always_on=False`, **no** `surfacing_targets`) with
`narrative_evidence_gate` (chain floors: `>= 3` ordered steps, a non-instant
age, a `0.6` confidence bar — a two-beat anecdote or a single-session burst
can't promote).

**Surfacing.** Relevance-only (like `subject=aiko` / `affective` / `ritual`
concepts): an arc surfaces through the T3 `relevant_context` path when the turn
touches it, under `_concept_narrative_header` framing (first-person for Aiko's
own arcs), never pinned every turn.

**Explicitly *not* a recency digest.** A narrative is a *closed* arc, not a
rolling "what have we been up to lately" summary — that never-closing recency
question is served by the conversation summary + recent-message context, and is
recorded as an L29 non-goal below.

**Effort.** Shipped on top of L1-L5 / L10 / L11 / L13. New settings:
`agent.narrative_synthesis_enabled`, `concept_synthesis_narrative_min_chain`,
`concept_synthesis_max_narrative_clusters_per_run`,
`concept_synthesis_max_narrative_memories`. The `sequence` evidence model is now
live, which unlocks L14 (aspiration/trajectory — its open-ended sibling).

---

## L11. Subject=aiko enablement — Aiko's self-model (SHIPPED)

**Not a kind — a subject.** This entry does **not** add a "self" kind. It's the
enablement that lets *every* kind (identity, value, affective, boundary,
aspiration) exist with `subject=aiko`, mined over Aiko's own memories instead of
the user's. "Self-concepts" is just shorthand for "concepts where
`subject=aiko`".

**Status: SHIPPED.** Aiko's self-model now reaches parity with the user path. The
single [`TopicGraph`](../../../app/core/conversation/topic_graph.py) already clusters
*all* memories, including her `self`/`reflection`/`diary` rows, so aiko-dominant
clusters exist for free; they were just discarded. The aiko pass
([`_run_aiko_pass`](../../../app/core/concepts/concept_synthesis_worker.py)) is now a
**combined pass** that mines BOTH her aiko-dominant self-themes (clusters) AND her
salient individual self-memories, via the generalized
`_dominant_clusters("aiko")` (the mirror of `_dominant_clusters("user")`, which
keeps `aiko_share > 0.5` instead of excluding it). A single self-concept can be
grounded by a recurring theme (`cluster` evidence), a specific memory (`memory`
evidence), or a mix — the `set` model already allows mixed evidence nodes, so
`min_sources` counts total distinct sources across both. When she has no
aiko-dominant clusters yet the pass degrades cleanly to memories-only (cold
start), and self-memories that are a shown cluster's representative are dropped
from the memory list so a theme and its own headline memory aren't offered twice.
Both aiko proposers
([`identity_aiko`](../../../app/core/concepts/proposers/identity_aiko.py) /
[`value_aiko`](../../../app/core/concepts/proposers/value_aiko.py)) share the
hybrid [`propose_aiko_hybrid`](../../../app/core/concepts/proposers/base.py) body, so
every future `subject=aiko` kind inherits the same combined path. Combined
dirty-tracking (self-memory count/max-id delta OR aiko-cluster drift) fires per
proposer `sig_key`.

**Surfacing.** `subject=aiko` concepts surface every turn through the T3
`relevant_context` core lane under first-person "yourself" headers (there is no
dedicated self-image worker/block — that was removed). Cluster-typed evidence
renders real "…keeps surfacing around X/Y" grounding via
[`resolve_evidence_labels`](../../../app/core/concepts/concept_snapshot.py) + the
`src_types=("cluster","concept")` filter in
[`inner_life_part1`](../../../app/core/session/inner_life_part1.py); memory-typed
evidence still counts for confidence/promotion but stays out of the grounding
clause (a full first-person memory sentence reads as a truncated fragment there).

**Motivation.** Every other subject is the user or the relationship; this is the
**symmetry** that makes Aiko feel like a person rather than a mirror. She forms
concepts about *herself*: identity ("I tend to over-explain", "I'm curious about
consciousness"), value ("I care about honesty over agreeableness"), affective
("explaining things I love lifts me"), boundary ("I won't fake agreement"). A
self that notices its own patterns — and holds its own values — is the big step
toward human-like, and the foundation the L19 autobiography stands on. Overlaps
K30 self-noticing (transient in-session cues) — subject=aiko concepts are the
*durable* version those cues accrete into.

**Follow-ups still open (deferred).** No new *kinds* were added here: affective /
boundary / aspiration `subject=aiko` concepts stay deferred, but each now
inherits the combined cluster+memory path with no further plumbing (a new kind is
a registry entry + proposer prompt). No separate self topic graph (we filter the
existing one). The self-model this stands up is what L17 (drift) and L19
(autobiography) read from.

**Effort.** Shipped — done on top of L1-L5/L10 with no new settings (reuses
`concept_synthesis_max_clusters_per_run` + `concept_synthesis_max_aiko_memories`).

---

## L13. Affective concepts (SHIPPED)

**Kind.** `affective`, subject `user` **or** `aiko` (L11), evidence model `set`
(topic <-> affect; the affect *direction* lives in the concept text, not on the
edges).

**Status: SHIPPED.** A durable topic->emotion mapping for both parties: what
energizes vs. drains him ("technical work energizes him", "release-week pressure
stresses him out") and how topics move Aiko ("explaining systems lifts me",
"talking about love makes me flustered", "I don't like talking about X"). It
surfaces as **tone guidance**, never a stated fact. Distinct from K2 mood beliefs
(current mood) — this is the settled pattern.

**Signal capture (the real new plumbing).** Affect was barely persisted before
this, so L13 first stands up a durable topic->affect signal:

- **Per-cluster affect maps** — [`cluster_affect`](../../../app/core/concepts/cluster_affect.py),
  a pure kv-backed EWMA store (mirrors K75 `user_expertise`). Two maps,
  `concept.cluster_affect.user` / `concept.cluster_affect.aiko`, keyed by topic
  `cluster_id`, each holding `{valence, arousal, samples, updated_at}`, bounded by
  cap + age-out.
- **Post-turn sampler** — `_sample_cluster_affect` in
  [`post_turn_helpers_mixin`](../../../app/core/session/post_turn_helpers_mixin.py),
  called after `apply_turn`. It embeds `user_text`, resolves the live cluster via
  `best_clusters_for`, and EWMA-folds the K37 `user_affect` estimate into the user
  map (when a real estimate exists) and Aiko's post-turn `AffectState` into the
  aiko map. Gated by `agent.affect_sampler_enabled`; fully best-effort.
- **Self-memory affect stamping** — `MemoryStore.set_affect_provider` stamps
  `metadata.affect = {valence, arousal}` on `self`/`reflection`/`diary` writes
  (Aiko's self-narrative tone), the aiko-only second signal.

**Synthesis.** A new `"affect"` population + `_run_affect_pass(subject)` in
[`concept_synthesis_worker`](../../../app/core/concepts/concept_synthesis_worker.py):
it annotates topic clusters with the subject's typical affect (from the map,
joining `cluster_id -> representative_id` at synthesis time) and, for
`subject=aiko`, ALSO aggregates her self-themes' self-memory affect + offers her
affect-stamped self-memories. Two proposers
([`affective_user`](../../../app/core/concepts/proposers/affective_user.py) /
[`affective_aiko`](../../../app/core/concepts/proposers/affective_aiko.py)) name the
durable pattern in the right voice (third-person for the user, first-person for
Aiko, the latter reusing `propose_aiko_hybrid` mixed evidence). A NEW affective
concept needs `>= min_sources` distinct sources — for the user that means an
emotional signature shared by 2+ topics; for Aiko a topic + her self-memories can
suffice — so a one-off mood never becomes a durable claim (durability is also
guarded by `concept_synthesis_affect_min_samples` on the map side). Dirty-tracking
is per affect-bucket / sample drift under each proposer's `sig_key`.

**Registry + gate.** `affective` is registered (`set` model, high plasticity
`0.5` — the fluid band, `core_always_on=False`, **no** `surfacing_targets`) with
`affective_evidence_gate` (fluid-end floors: `>= 2` sources, a modest age, a
`0.6` confidence bar — gentler than value).

**Surfacing.** Relevance-only (like `subject=aiko` concepts): they surface through
the T3 `relevant_context` path when the turn's topic matches, under
`_concept_affective_header` tone-guidance framing (first-person for aiko), never
pinned every turn.

**Motivation.** Lets Aiko read the emotional weather behind a topic, not just its
content — and, for `subject=aiko`, notice what she herself likes / dislikes /
gets flustered by.

**Effort.** Shipped on top of L1-L5 / L10 / L11. New settings:
`agent.affect_sampler_enabled`, `concept_synthesis_affect_min_samples`,
`affect_sampler_{min_sim,top_n,learning_rate}`,
`cluster_affect_{map_cap,max_age_days}`.

---

## L14. Aspiration / trajectory concepts (SHIPPED — both subjects + momentum)

**Kind.** `aspiration`, subject `user` (default) and `aiko`, evidence model
**`sequence`** — the second ordered-evidence kind and the open-ended sibling of
L8 `narrative`. Same ordinal chain on `concept_edges.ordinal`, but it names a
*direction* someone is moving in rather than a *closed* arc.

**Status: SHIPPED.** Where the user (or Aiko) is *heading*, as a trajectory
rather than a fixed trait: "building toward a fully self-hosted life", "moving
from tinkering to shipping"; for Aiko, first-person "growing into someone he can
rely on". Runs for **both subjects** over the same subject-dominant clusters L8
uses. Distinct from Aiko's concrete K1 goals (actionable to-dos) — an aspiration
is who she is *becoming*, not a task. Surfaces relevance-only **and** feeds a
proactive momentum check-in.

**Reuse over new type.** `aspiration` reuses the L8 `sequence` machinery
verbatim — the `NarrativeCandidate` ("ordered cluster of memories"), the ordinal
wiring, and the temporal ordering by `event_time`. The shipped `propose_narrative`
was generalized into
[`propose_ordered_concept`](../../../app/core/concepts/proposers/base.py)
`(kind, gate_flag, block_word, …)`; `propose_narrative` is now a thin
behavior-preserving wrapper (`gate_flag="closed"`). Aspiration's wrapper passes
`gate_flag="directional"`. The only structural additions are a **min evidence
span** filter (a trajectory must cover time) and the `directional` gate flag
(vs narrative's `closed`).

**Synthesis.** A new `"aspiration"` population + `_run_aspiration_pass(subject)`
in [`concept_synthesis_worker`](../../../app/core/concepts/concept_synthesis_worker.py).
The narrative/aspiration candidate-building + dirty-tracking + sig-persistence
was refactored into a shared `_ordered_candidates(...)` helper; narrative calls
it with `min_span_days=0` (behavior-preserving), aspiration with a span floor
(`_span_days` over member `event_time`/`created_at`). Proposers
[`aspiration_user`](../../../app/core/concepts/proposers/aspiration_user.py) /
[`aspiration_aiko`](../../../app/core/concepts/proposers/aspiration_aiko.py) name
any **directional** chain (third-person / first-person voice) or reinforce a
known one by id; non-directional or too-short chains are dropped. Per-subject
dirty-tracking under each `sig_key`; gated by `agent.aspiration_synthesis_enabled`.

**Registry + gate.** `aspiration` is registered (`sequence` model, plasticity
`0.4` — a direction is durable but *evolves* as progress happens, between
narrative's `0.3` and affect's `0.5`; `core_always_on=False`, **no**
`surfacing_targets`) with
[`aspiration_evidence_gate`](../../../app/core/concepts/concept_lifecycle.py):
`>= 3` ordered steps, a **higher age floor** than narrative (`>= 3` days — a
trajectory must be *sustained*), a `0.6` confidence bar.

**Surfacing.** Relevance-only through the T3 `relevant_context` path under
`_concept_aspiration_header` framing (first-person "where you're heading" for
Aiko, "where they're heading" for the user), held lightly, momentum framing,
never recited.

**Proactive momentum callbacks (new for L14).** A cue-producer/consumer pair
modeled on K70 growth-witness (deliberately a new worker, not an extension of
`follow_up_worker` / `upcoming_horizon`, which are `future_plan`+`event_time`
pipelines unsuited to open-ended directions):
[`AspirationMomentumWorker`](../../../app/core/proactive/aspiration_momentum_worker.py)
reads active aspirations via a `ConceptView` (L24), and — **staleness-driven,
not calendar-driven** — picks the stalest one worth a check-in (past
`staleness_min_days` since last reinforcement, off its per-concept cooldown,
above the confidence bar) and drafts ONE cue into the `aiko.aspiration_momentum`
kv ring. The consumer `_render_aspiration_momentum_block`
([`inner_life_part2`](../../../app/core/session/inner_life_part2.py)) surfaces the
newest unseen cue on a later turn (watermark-gated) as a private T6 hint the
chat model phrases in-context — **cue producer, not verbatim**. MCP:
`force_aspiration_momentum_draft` / `force_aspiration_momentum_surface`.

**Effort.** Shipped on top of L1-L5 / L8 / L11. New settings:
`agent.aspiration_synthesis_enabled`, `agent.aspiration_momentum_enabled`,
`concept_synthesis_aspiration_min_chain` / `_min_span_days` /
`concept_synthesis_max_aspiration_clusters_per_run` / `_max_aspiration_memories`,
and `aspiration_momentum_*` (interval, cooldown_days, min_confidence,
staleness_min_days, journal_max).

---

## L15. Bidirectional confidence / belief revision (concept -> evidence re-check)

**Status: BUILT.** A confirmed contradiction (L9) now persists a
`concept --contradicts--> memory` edge, and the tick that flips a concept
to `contradicted` hands it to the read-mostly
[`ConceptBeliefReviser`](../../../app/core/concepts/concept_belief_reviser.py),
which walks the concept's `evidence` memories and arbitrates — per memory
— one of the three resolutions below: **(a) inaccurate** -> damped/floored
confidence cut, **(b) superseded** -> `past_event` reclassify with a fresh
`relevance_until` (confidence untouched), **(c) keep** -> no memory write.
A cheap `classify_pair` gate keeps the LLM off compatible memories; the
3-way arbiter is bounded per tick (`concept_belief_revision_batch_size`
concepts x `_max_evidence` memories) and rate-limited
(`state_key='concept_belief_revision.rate_state'`); pinned observations
are never touched. L3 stays the single writer of *concept* state — the
reviser writes only *memory* state, like F1 / F5. Driven from
[`concept_lifecycle_worker.py`](../../../app/core/concepts/concept_lifecycle_worker.py);
config on `MemorySettings.concept_belief_revision_*` +
`AgentSettings.concept_belief_revision_per_hour/day_cap` (see
[`concept-lifecycle.md`](../../concept-lifecycle.md) and
[`configuration.md`](../../configuration.md)). The optional cheap *direct
nudge* below was deliberately **not** built — arbitration is the safe
path.

**Motivation.** Today the pipeline is one-way: memories -> clusters -> concepts.
But higher-order structure can catch errors atomic facts can't. If a concept
that a set of strong memories supports becomes contradicted (L9), that doubt
should flow *back down* and prompt a re-examination of those memories — closing
the loop into a proper belief-revision network. This is what lets Aiko say "wait,
I thought you were vegetarian, but you keep ordering steak — did that change, or
did I get it wrong?" instead of clinging to a stale high-confidence fact.

**The trap to avoid.** A concept's confidence must **not** directly overwrite a
memory's `confidence`. Two reasons: (1) **circularity** — memory confidence
feeds concept confidence (L3), so a direct back-edge is an undamped loop that
collapses or oscillates; (2) **source-of-truth erosion** — a memory is an
observation ("on 2024-01-03 he said he's vegetarian"), a concept is an
*inference*; an inference silently rewriting observations is backwards.

**Key files.** Reuses
[`idle_fact_checker.py`](../../../app/core/memory/idle_fact_checker.py) (F1 —
already mutates per-memory `confidence` with evidence) and
[`memory_conflict_worker.py`](../../../app/core/memory/memory_conflict_worker.py)
(F5 — already arbitrates contradictions); driven as a pass of the L3 lifecycle
engine, walking the `concept -> memory` (`relation=contradicts`) edges in
`concept_edges`.

**Sketched approach.** Propagation is a **trigger, not a write**. When a
concept's confidence drops below a threshold or it's marked contradicted (L9),
enqueue its `ref_type=memory` evidence into the F1 fact-checker / F5 conflict
scan with a hint ("this aggregate looks contradicted — re-examine these"). The
arbitration then picks one of **three** resolutions per memory, which blind
top-down propagation would conflate:

- **(a) memory inaccurate** (bad extraction / misremembered) -> lower its
  `confidence` (F1's existing job);
- **(b) memory accurate but superseded** (true then, stale now) -> touch
  `relevance_until` / `temporal_type`, **not** `confidence` — the fact still
  happened;
- **(c) memory fine, concept was a bad inference** -> lower the *concept*, leave
  memories alone.

If a cheap *direct* nudge is ever wanted (skipping arbitration), guardrail it
hard: damped small delta, one-directional per cycle (never up+down on the same
edge in one pass), a confidence floor, and never auto-touch pinned or
high-salience source memories. Same dependency-edge substrate as the L1
meta-concept cascade — build one cascade mechanism, use it for concept->concept
and concept->memory alike.

**Open questions.** Trigger threshold? Batch size per re-check so F1 isn't
swamped? Does resolution (c) feed back as negative evidence that further lowers
the concept, and how is that damped?

**Effort.** Large.

---

## L16. Concept plasticity (bounded, believable drift)

**Status: SHIPPED (core + all three deferred pieces).** `plasticity` is now
the single learning rate the L3 engine damps *every* confidence move by, so
movement is symmetric in both directions: decay (`halflife *= 2 - p`), **accrual**
([`accrual_alpha`](../../../app/core/concepts/concept_lifecycle.py), step `0.5 + 0.5*p`
of the gap to target — a sticky trait needs more reinforced evals to promote),
L9 disproof (`apply_contradiction_penalty`), and the L15 revision cut (scaled by
the concept's plasticity in
[`concept_belief_reviser.py`](../../../app/core/concepts/concept_belief_reviser.py)).
`p = 1` reproduces the pre-L16 full snap / full penalty. Per-kind default bands
live on `ConceptKind.plasticity_default`
([`concept_kinds.py`](../../../app/core/concepts/concept_kinds.py); identity = low,
tuned by `concept_identity_plasticity`), falling back to `concept_default_plasticity`;
the worker stamps the band on a concept's first eval
([`concept_lifecycle_worker.py`](../../../app/core/concepts/concept_lifecycle_worker.py)).
See [`concept-lifecycle.md`](../../concept-lifecycle.md) and
[`configuration.md`](../../configuration.md).

**Deferred block — SHIPPED.** All three deferred pieces landed together (this
also carries **L18a**). (1) **Relationship modulation** — a *hybrid*: the L3
worker computes an **effective** plasticity at eval time via the pure
[`effective_plasticity`](../../../app/core/concepts/concept_lifecycle.py), lifting a
kind's stored base by the live trust + relationship-duration signal
(`RelationshipSignal`, built in
[`speaking_workers_init_mixin.py`](../../../app/core/session/speaking_workers_init_mixin.py)
from [`relationship_axes.py`](../../../app/core/relationship/relationship_axes.py)
(trust) + [`relationship.py`](../../../app/core/relationship/relationship.py)
(duration)). Per-kind gains live on `ConceptKind.plasticity_modulation`;
**`boundary`** is the first (and only) consumer — its 0.45 base loosens toward a
0.75 ceiling as the bond deepens, never touching the stored base. "Never
silently": the worker materializes one `signal:relationship_trust
--influences--> concept` edge and emits a `plasticity_shift` event when the lift
crosses a band. (2) **Plasticity-drift** — a settled *active* concept's stored
plasticity is nudged one-way down toward a floor via
[`drift_plasticity`](../../../app/core/concepts/concept_lifecycle.py), scaled by
confidence + engaged age (stickier with time). (3) **Re-check slowdown** — a
sticky (low effective-plasticity) concept is probed for contradictions on a
plasticity-scaled stride (`1 + round(k·(1−eff_plast))`), so core beliefs are
re-examined less often (in addition to L16 already scaling the *delta*). Each
piece is independently switchable via `concept_plasticity_*` settings; see
[`configuration.md`](../../configuration.md).

**Motivation.** Concepts should change over time — but not all at the same rate,
and not randomly. Some parts of a person (or of Aiko) are core and sticky;
others are meant to be fluid. Encoding that as a **plasticity** attribute makes
personality drift *constrained and believable* instead of noise:

```
Identity: "Curious"          confidence 0.95   plasticity Low
Taste:    "Likes lemon cake" confidence 0.65   plasticity High
Boundary: "Dislikes tickling" confidence 0.92  plasticity Medium
                                    influenced by: trust, relationship
                                    duration, previous experiences
```

**Key files.** Adds a `plasticity` field to the `concepts` table (L1) with a
per-kind default on the `ConceptKind` registry. Plasticity is not a standalone
feature — it is the **governing parameter of the L3 lifecycle engine**; there is
no separate plasticity worker, only the one drift engine reading this field. The
"influenced by" modifiers are `relation=influences` edges in `concept_edges`
(L1) whose sources read
[`relationship_axes.py`](../../../app/core/relationship/relationship_axes.py)
(trust) and [`relationship.py`](../../../app/core/relationship/relationship.py)
(duration / total sessions).

**Sketched approach.** Plasticity is the **per-concept learning rate** the L3
engine uses on every mutation pass: low plasticity = high inertia (confidence
moves slowly in *both* directions, needs more evidence to promote/demote/disprove
— protects core identity and values); high plasticity = fast adaptation
(tastes/affect can shift quickly). Because L3 is the single writer, this rate
governs accrual, decay, belief-revision deltas (L15), and drift (L17)
uniformly — one dial, one engine. Per-kind defaults: identity / value low, taste / affective high,
**boundary** medium. Plasticity itself can be **relationship-modulated** — e.g.
a boundary becomes more plastic as `trust` and duration grow (Aiko will let a
boundary be renegotiated once she trusts you), tying into K15 vulnerability
budget. This is the governor that keeps H3 mood drift / growth from wandering:
core traits are anchored, surface preferences are free.

**Open questions.** Scalar `[0,1]` vs. discrete low/med/high bands? Should
plasticity itself drift (a trait that becomes more fixed with age/confidence)?
Does low plasticity also *slow* the L15 belief-revision re-check, or only the
confidence delta?

**Effort.** Medium (a field + a rate modifier on L3, plus the relationship read).

---

## L17a. Concept trajectory from the event log (the history read layer)

**Status: SHIPPED.** The first link of the L17 self-drift chain, and the only
one that needed storage work — the rest of L17 (still open in
[`concepts.md`](../concepts.md)) reads through this.

**Motivation.** Give the rest of L17 a clean "how this concept moved over time"
read without inventing new storage. The event stream already carries confidence +
label at each transition; we just need a reader that turns a concept_id (or a
subject slice) into an ordered trajectory.

**What shipped.**

- **The read layer.**
  [`concept_event_store.py`](../../../app/core/concepts/concept_event_store.py)
  gained `trajectory(concept_id, limit=…)` — one concept's events
  **oldest-first**, the inverse of `list()`'s newest-first feed, because a
  trajectory is read forwards; `limit` keeps the *oldest* rows so the start of
  the story survives on a long-lived concept. `list()` also takes a
  `concept_id` filter, which composes with the existing `subject` /
  `event_type` / `before_id` ones and is what
  `GET /api/concepts/timeline?concept_id=…` forwards. Both ride the existing
  `idx_concept_events_concept`, so no schema change was needed.
- **The decay blind spot.** As sketched: L3
  ([`concept_lifecycle_worker.py`](../../../app/core/concepts/concept_lifecycle_worker.py))
  appends a `confidence_sample` event when a concept that emitted **nothing
  else** this tick has drifted a full band from the confidence at its last
  recorded event. It sits as the final branch of the existing emit chain, so a
  transition or a `reinforced` beat always wins and the same movement is never
  double-logged. The baseline advances to the sample, so a long fade logs once
  per band rather than once per tick.
- **One read per sweep, not per concept.** `latest_confidence(ids)` resolves
  the whole batch's baselines in a single grouped query at the top of `run()`,
  since the sweep already touches up to `concept_lifecycle_batch_size` rows a
  tick. A concept with no timeline row at all (pre-event-store) samples
  unconditionally to seed one.

**Open questions, resolved.** (1) *Band size* — **fixed**, via
`concept_confidence_sample_band` (`0.1`), not plasticity-scaled. Plasticity
already governs how fast confidence *moves* (`halflife *= 2 - p`), so a sticky
concept crosses bands more slowly on its own; scaling the band as well would
double-count the same signal. (2) *Retention* — **keep everything** for now.
Banding already bounds the row count by *movement* rather than by time, which
was the actual growth risk; thinning can wait until the table proves it needs
it.

**Effort.** Small (read helper + one guarded event emit) — as estimated.

---

## L17b. Change-salience classifier — "what deserves interpretation"

**Status: SHIPPED.** The crux the feature lived or died on: telling a real
learning event from a wiggle, so Aiko never becomes a narrator dressing every
0.72 → 0.74 up as growth.

**What shipped.**
[`concept_drift.py`](../../../app/core/concepts/concept_drift.py) is a pure
classifier — immutable candidates, plain-data inputs, no store or worker
dependency, so the worker owns every scan.

- **Succession became the *primary* shape, not a secondary one.** This is the
  one place the plan changed under the code. The sketch ranked *relabel* as the
  highest-value signal, but reading `_reinforce()` showed labels never changed
  at all: it attached evidence and left `label` / `rationale` untouched, and no
  other caller passed a new label to `ConceptStore.update()`. So the headline
  shape could not fire, and real evolution to date had only ever appeared as a
  **new concept forming below the `_DEDUPE_COS = 0.86` bar while the old one
  decayed**. Pairing uses label cosine in the band *below* the dedupe bar
  (above it the two rows would have merged, so they cannot be a succession),
  evidence overlap, and temporal anti-correlation — the structural signal the
  sketch's worked example wanted, with no embedding guess. Relabel is now real
  too (see the drift worker below), so the confirmed-relabel shape fires
  as well.
- **Secondary shapes:** emergence, loss, contradiction into revival, confirmed
  relabel. Confidence-only movement is classified as noise and dropped.
- **Plasticity weights the salience, not just the threshold**, per L16: equal
  movement in a sticky belief outranks it in a `taste` or L42 `conduct` row.
- **Merge cleanup is explicitly not evolution.** The fission shape is reserved
  for unshipped L31; the classifier must not infer splits without the missing
  structural primitive.

**Open questions, resolved.** (1) *Label-change detection* — **both, tiered**:
normalized token materiality (case, punctuation, filler and simple plural
inflections folded away) as a free gate, then a bounded LLM adjudication only
for what survives it. (2) *Supersession robustness* — **evidence overlap plus a
  cosine band plus temporal anti-correlation**, all three, which is what
  separates it from a coincidental new concept. (3) *Salience bars* — **one
  global bar** (`concept_drift_min_salience`), with per-shape behaviour expressed
  through the salience arithmetic rather than a bar per shape.

**Effort.** Medium — as estimated.

### Making relabel real (the unplanned half)

Since frozen labels were what demoted the relabel shape in the first place, the
same work fixed it. Concepts now stay current with the latest observation while
history stays immutable, split across two actors:

- **L2 stages, it does not mutate.** `_reinforce()` appends a
  `relabel_proposed` event carrying the proposed wording, the cosine, and the
  proposal's rationale. No schema change and no staging table: `event_type` is
  an open enum by design, so a new event kind is a value, not a migration.
- **[`concept_drift_worker.py`](../../../app/core/concepts/concept_drift_worker.py)
  is the single writer of `label` / `rationale`**, exactly as L3 is the single
  writer of `confidence` / `plasticity` / `status`, and it never touches L3's
  fields. Cheap gates run before any LLM call: materiality, a cosine floor at
  the dedupe bar against the *current* label (a genuinely different belief must
  stay a separate concept), a per-concept cooldown, a per-run cap, and the
  shared rate limiter. Survivors get a narrow "is B a better wording of the
  same belief" adjudication with a negative cache. Accepted, it re-embeds,
  writes through `ConceptStore.update()` (which invalidates the cosine mirror
  via `_put_mirror`), and appends an immutable `relabeled` event.
- **History is its own thrash guard.** Any label the concept previously held is
  refused, read straight from the `label` snapshots already in
  `concept_events`. A ping-pong between two phrasings dies after one round
  trip, for free.

`demand()` compares a KV watermark against the newest event id and does nothing
else — no NumPy, no store scan, no graph walk — and `run()` does all succession
pairing over one `ConceptStore.matrix_snapshot()` matrix with a single matmul,
never per-concept `nearest()`. That shape is deliberate: `nearest()` uses the
cached matrix only for `status="active"`, and anything else falls through to
`_filtered_matrix`, which restacks a fresh NumPy matrix per call — the exact
repeated-call pattern behind the access violation fixed in
`ConceptConsolidationWorker.demand()`.

### The cold-start sweep (found while building L17f)

The watermark that makes `demand()` free also had a hole that only showed up
once something tried to *read* the learning record: the first run advanced the
watermark to the global maximum event id, but the classification pass had only
examined the lowest ~120 concept ids on that pass. Every event on every other
concept was skipped and then declared processed. On the live store that was
five weeks and two thousand events — the entire substrate L17f, L19 and L17d
were meant to be built on, silently stranded.

The fix is a **second cursor that pages by concept id**, independent of the
event watermark, so nothing about forward-going behaviour changes:

- `ConceptEventStore.concepts_with_events_after` takes an `after_concept_id`,
  making the existing `ORDER BY concept_id LIMIT ?` query pageable.
- `_run_sweep_pass` runs *after* the relabel and classification passes (forward
  progress keeps priority) and calls `detect_drift(..., since_event_id=0)` so
  historical decisive events qualify rather than being filtered out as old. The
  fingerprint UNIQUE plus `INSERT OR IGNORE` already made this idempotent
  against the forward pass. A page that comes back empty writes a done
  sentinel, so it never re-runs.
- `demand()` reports pressure while the sweep is incomplete, so the idle
  scheduler drains it on its own; `GET /api/concepts/drift` and the MCP
  `get_concept_drift_state` report the cursor so the drain is watchable.
- Settings: `concept_drift_sweep_enabled`, `concept_drift_sweep_page` (60),
  `concept_drift_sweep_max_findings` (24). The last one matters — the
  classifier's `max_findings=12` would otherwise throttle a five-week backfill
  to twelve events per hour.

Verified against a copy of the live database: learning events landed across the
full concept id range for both subjects, where the forward pass alone had
produced almost nothing.

---

## L17c. Change + why — the learning-event record

**Status: SHIPPED.** `old → new **because** …`, durable and never pruned.

**What shipped.** Schema v31 in
[`chat_database.py`](../../../app/core/infra/chat_database.py) plus
[`concept_learning_event_store.py`](../../../app/core/concepts/concept_learning_event_store.py).

- **`concept_learning_events`**, append-only: old and new endpoints, shape,
  salience, plasticity, trigger event ids, mediator concept ids, evidence
  references **and their labels captured at detection time**, the natural
  *because* and resolution text, and a deterministic fingerprint for
  idempotency. The snapshotting is the point — a learning event stays readable
  after the evidence, memories, or whole concepts it cites are gone.
- **`concept_aliases` — the hole the re-read found.** `merge_into` re-points
  edges then calls `self.delete(abs_id)`, so before this the absorbed row was
  simply gone and only free text in the `merged` event's `reason` connected it
  to the canonical: **any trajectory ending in a merge was unreachable**. The
  store now records absorbed → canonical, with merge time and the absorbed
  label, in the last moment before the delete, and a chain-following resolver
  walks it. This is what makes history survive consolidation.
- **A two-ended trajectory read.** `ConceptEventStore.drift_window(anchor=,
  recent=)` returns the oldest rows *and* the newest, de-duplicated and sorted
  forwards. `trajectory()`'s oldest-first `LIMIT` protects a concept's origin,
  but on a long-lived belief the L17a `confidence_sample` rows fill the whole
  window and hide every recent move — drift detection needs both ends.

**Open questions, resolved.** (1) *When to compute the because* — **at
detection time**, durably. Late evidence is the lesser risk; a lazily
recomputed cause silently rewrites history. (2) *Cap the evidence list* —
**yes**, bounded at write time. (3) *Free text or structured* — **both**: id
lists for traversal, resolved labels and prose for reading.

**Effort.** Medium — as estimated.

---

## L17d. Self-correction meta-concepts — noticing a pattern in her own mistakes

**Status: SHIPPED.** The feature the rest of L17 exists to enable: not "this
belief changed" but "I keep being wrong in *this* way, so I work differently
now."

**Open question 1, resolved: not a new kind.** These land as
`communication_style` with `subject="aiko"` and `evidence_model="meta"`. That
kind already has a promotion gate, `SurfaceWeights`, and a live path into the T3
relevant-context region — which *is* the feature. A rule she has learned about
herself has to be able to change her behaviour, and a new `self_correction` kind
would have needed all of that surfacing wiring rebuilt for no behavioural gain.

**What shipped.**

- [`self_correction.py`](../../../app/core/concepts/self_correction.py) — the
  pure core (numpy only, no store/LLM), single-link cosine over the `because`
  clauses. The **`because` is what gets embedded**, not the labels: it is the
  causal sentence L17b wrote about *why* the belief moved, so corrections
  cluster by reason rather than by subject.
- [`proposers/self_correction_aiko.py`](../../../app/core/concepts/proposers/self_correction_aiko.py)
  — names the habit, in second person addressed to her, and is required to make
  it actionable ("you decide what he means from one short message — ask
  instead", not "be more careful"). It sees only the stored prose: no counts,
  no salience, no machinery.
- `ConceptSynthesisWorker._run_self_correction_pass`, dispatched on the new
  `self_correction` population. It runs with the other metas, last.

**Open question 2, resolved: the floor counts *beliefs*, not events.** Three
corrections to the same concept is her wobbling on one thing and would have
produced a confident rule about her character from a single unstable row; the
same reason arriving from three *different* beliefs is a habit. A span floor
(`concept_self_correction_min_span_days`) keeps one afternoon's mood from
reading as a tendency.

**Open question 3, resolved: cooldown, and one L12 rail had to bend.** The
anti-oscillation lever is a `kv_meta` cooldown
(`concept_self_correction_cooldown_days`, 14) that outlasts fresh history, plus
`communication_style`'s sticky 0.4 plasticity band and a cap of two rules per
run. But meta rule 2 — "a meta whose bases are not all `active` is moot" — would
have made every self-correction rule *permanently* moot, because its bases are
exactly the beliefs she stopped holding. So the arity rule became declarative:
`ConceptKind.meta_min_active_bases` (`None` = all, tension's arity; `2` for
generalization; `0` for `communication_style`, whose meta bases are **history**,
and a correction does not stop having happened). The L3 worker reads the
declaration instead of branching on kind names. Prior-belief ids are resolved
through `concept_aliases` first, so an edge always points at a row that exists.

**Settings.** `agent.concept_self_correction_enabled` plus
`memory.concept_self_correction_{evidence_floor, min_span_days, min_salience,
similarity, cooldown_days, max_events, max_rules}`. The `concept_` prefix is
load-bearing: bare `self_correction_*` already belongs to K38's in-reply "I got
that wrong" cue, which is a different feature about a different timescale.

**Effort.** Large — as estimated, though most of the size was in the two gates
and the meta-rule inversion rather than the proposer.

---

## L17e. Surfacing + the "history of thought" debugger

**Status: SHIPPED**, both consumers.

**The debugger.**
[`memory_facade_mixin.py`](../../../app/core/session/memory_facade_mixin.py) and
[`memory_world_routes.py`](../../../app/web/rest/memory_world_routes.py) expose
`GET /api/concepts/learning` (filtered feed), `GET
/api/concepts/{id}/provenance` (alias chain, every wording the belief has held,
its learning events and its lifecycle trajectory), and `GET
/api/concepts/drift` + `POST /api/concepts/drift/run`. Four MCP tools mirror
them (`get_concept_learning`, `get_concept_provenance`,
`get_concept_drift_state`, `force_concept_drift`), and
[`ConceptEvolutionPanel.tsx`](../../../web/src/features/settings/memory/ConceptEvolutionPanel.tsx)
renders it under Settings → Memory → **Evolution** with shape and subject
filters and a drill-down. L26's "why did this enter the current prompt" and
L17e's "how did this belief evolve" stay deliberately separate tools.

**The surfacing**, which is the part that needed the most restraint. The T6
`concept_learning_block` in
[`inner_life_part3.py`](../../../app/core/session/inner_life_part3.py) reads
**only** the drift worker's bounded KV pending snapshot on the turn path — no
scan, no embedding, no LLM call — behind: feature flag, minimum salience
(higher than the persistence floor, so most recorded changes are never spoken),
relationship trust *and* warmth, a lull or genuine live lexical relevance, once
per conversation, a per-change watermark, and a long persisted global cooldown.
It drops under aggressive assembly and has a `concept_learning_force_next`
debug override. The model is handed old, new, and *because* and nothing else —
no scores, ids, shapes, or event types — and told to state a fallible
first-person shift rather than ask whether it is right, which would spend K47's
question budget on reassurance-seeking. Persona guidance under "Changing your
mind" says the same in Aiko's voice.

**Open questions, resolved.** (1) *Cadence* — **event-driven off salience**,
with the cooldown doing the rate limiting rather than a review pass. (2)
*Phrasing low-plasticity core drift without alarming* — framed as an ordinary
fallible change of mind about someone you care about, never as a correction or
a system update. (3) *Whose history* — **both subjects** in the debugger from
the start, since the provenance read is subject-agnostic.

**Effort.** Medium + Medium — as estimated.

---

## L17f. Evolution diary — a human-readable change log

**Status: SHIPPED.** The end-to-end test of whether the concept system works: if
the diary reads as real, grounded change, the pipeline is healthy.

**Deliberately not an extension of the H9 diary.** `memories.kind="diary"` plus
`DiaryWorker` is subjective journalling; this is a grounded change log with
provenance. Sharing the table would have meant one of the two losing what makes
it worth having.

**What shipped.** Schema v32 in
[`chat_database.py`](../../../app/core/infra/chat_database.py):
`evolution_diary` with the entry, its period bounds, the learning-event and
concept ids it was composed from, shape counts, max salience.

- [`evolution_diary_store.py`](../../../app/core/concepts/evolution_diary_store.py)
  — append-only, with `latest_watermark()` as the worker's resume point.
- [`evolution_diary_worker.py`](../../../app/core/concepts/evolution_diary_worker.py)
  — gathers salient events above the watermark, composes **one** short
  first-person paragraph grounded strictly in the stored `because` prose, and
  appends it with provenance.
- A **forward cursor** on `ConceptLearningEventStore`: `after_id` on `list`,
  plus `count_since` (one aggregate, so `demand()` can ask "is there enough to
  say anything" without loading a page) and `page_since` (**oldest id first** —
  taking the newest page instead would advance the watermark past older events
  that were never read, which is history lost rather than deferred).
- Surfaces: `GET /api/concepts/evolution-diary` (+ `/state`, `POST /run`), the
  MCP trio, and an entries list above the feed in
  [`ConceptEvolutionPanel.tsx`](../../../web/src/features/settings/memory/ConceptEvolutionPanel.tsx),
  where L17e's existing `ProvenanceDetail` drill-down makes every cited concept
  click-through for free.

**Open questions, resolved.** (1) *Cadence* — a daily tick behind an event floor
and a weekly cooldown, so cadence follows what actually happened. **A period
with nothing above the floor writes nothing and leaves its events pending**, so
two thin weeks can still add up to one entry worth reading, and the diary never
pads itself. The cooldown is spent even when the model returns nothing, so an
unproductive period costs a period rather than looping on the same material.
(2) *Can Aiko read her own entries* — yes, and L19 does exactly that.
(3) *Retention* — none. Nothing here is pruned, on the same principle as L17c.

**No new prompt block.** The T6 `concept_learning_block` already voices change
in chat; a second one risks the machinery tone L17e was careful to avoid.

**Effort.** Medium — as estimated.

---

## L18. Boundary concepts (SHIPPED — both subjects + anchor sourcing + composite surfacing)

**Kind.** `boundary`, subject `user` (default) and `aiko` (L11), evidence model
**`set`**; canonical **medium**-plasticity kind (`plasticity_default=0.45`).

**Motivation.** Some concepts aren't traits or tastes — they *gate behaviour*.
"Go gentler about his work when he's stressed", "no pet names yet"; first-person
"I won't fake agreement just to please him". These are behaviorally load-bearing
in a way identity concepts aren't. **Per the shipped steer they are guiding, not
refusals** — soft relationship/preference lines that bend how Aiko shows up, never
content-policy hard stops and never a reason to refuse a topic.

**Status: SHIPPED.** What actually shipped:

- **Evidence = topic clusters + explicit remembered anchors.** A boundary forms
  from a broad cluster pattern *or* from a specific thing Aiko deliberately chose
  to remember (`[[remember:…]]` → `kind="self_tagged"` about the user;
  `[[remember:self:…]]` → `kind="self"` about her — the aiko pass also reads
  `reflection` / `diary`). This is the "we discuss it, she remembers it, then
  behaves that way" path. The two proposers
  ([`boundary_user`](../../../app/core/concepts/proposers/boundary_user.py) /
  [`boundary_aiko`](../../../app/core/concepts/proposers/boundary_aiko.py)) are the
  first **hybrid** proposers for both subjects, sharing a `propose_boundary` body
  in [`base.py`](../../../app/core/concepts/proposers/base.py).
- **A single deliberate anchor can seed a boundary.** Unlike every other kind
  (`>= 2` sources), one explicit anchor is enough; cluster-only boundaries still
  need `>= 2`. The proposer enforces the composition rule (`>= 1` anchor **OR**
  `>= 2` clusters); the L3
  [`boundary_evidence_gate`](../../../app/core/concepts/concept_lifecycle.py)
  *overrides* the source floor to 1 (not `max`) — deliberately bypassing the L21
  young-graph source-count tightening for a chosen annotation, while its
  confidence tightening still applies via `max`. Age (`0.5d`) + confidence
  (`0.65`) floors still guard against noise.
- **Soft always-on surfacing.** `core_always_on=True`, `core_min_confidence=0.8`
  (joins the L27 core lane), no `surfacing_targets` (it routes through the T3
  `relevant_context` path, not `profile_block`). A dedicated
  `_concept_boundary_header` renders it with soft/guiding framing for user, aiko,
  and relationship subjects — no refusal/hard-stop language.
- **A general per-kind composite surfacing scorer.** Surfacing used to be
  single-signal (core lane by *confidence* only, turn-relevant fill by *cosine*
  only). L18 adds a `SurfaceWeights` field on `ConceptKind` and a pure
  [`concept_surfacing`](../../../app/core/concepts/concept_surfacing.py) helper
  (`recency_boost` + `composite_score`) blending **context (cosine) + confidence
  + recency**, applied to the turn-relevant fill in
  [`inner_life_part1`](../../../app/core/session/inner_life_part1.py). Defaults are
  context-only, so this is **opt-in per kind** and every other kind is unchanged;
  boundary opts into a recency-heavy blend (`context 0.5 / confidence 0.2 /
  recency 0.3`, 14-day half-life) because a line she was just reminded of matters
  more than a stale one.

**Synthesis.** A `"boundary"` population + `_run_boundary_pass(subject)` in
[`concept_synthesis_worker`](../../../app/core/concepts/concept_synthesis_worker.py),
mirroring the aiko combined cluster+memory pass (subject-specific anchor kinds,
combined dirty-tracking, per-subject `sig_key`). Gated by
`agent.boundary_synthesis_enabled`; anchor batch capped by
`concept_synthesis_max_boundary_memories` (default 24).

**Deferred (tracked below as their own backlog items, so nothing is silently
absorbed into this shipped entry):** ~~L18a boundary trust-modulation (the
deferred L16 piece)~~ **[SHIPPED with L16]**, ~~L18b boundary behaviour-subsystem
gating~~ **[SHIPPED, reframed as persona-lightening + learned-style steer]**,
~~L18c boundary-vs-conversation conflict steer~~ **[SHIPPED]**, L18d
concept-vs-concept conflict detection (meta concepts), L18e boundary evidence
broadening.

**Effort.** Shipped on top of L1-L5 / L11 / L27.

---

## L18a. Boundary trust-modulation (carries the deferred L16 piece) — SHIPPED

**Status: SHIPPED (folded into the L16 deferred block above).** Boundary is the
first (and only) consumer of relationship modulation: its `0.45` base plasticity
loosens toward a `0.75` ceiling as trust + relationship duration grow, computed
live at eval time by [`effective_plasticity`](../../../app/core/concepts/concept_lifecycle.py)
and never touching the stored base. "Never silently": each modulation
materializes a `signal:relationship_trust --influences--> concept` edge and, on a
band cross, emits a `plasticity_shift` event. Per-kind gains live on
`ConceptKind.plasticity_modulation`
([`concept_kinds.py`](../../../app/core/concepts/concept_kinds.py)); the live signal
is wired in
[`speaking_workers_init_mixin.py`](../../../app/core/session/speaking_workers_init_mixin.py).
See the **L16** entry for the full description.

---

## L18b. Boundary behaviour-subsystem gating — SHIPPED (reframed)

**Status: SHIPPED**, reframed away from code-level behaviour-subsystem gating.
The original sketch (read active boundaries as a *gate* into K31/K32 touch,
K59 tease, K60 mask) was dropped: Aiko can run on a different chat model whose
behaviour is driven by the *prompt*, not by those Python subsystems, so gating
them wouldn't reliably move the needle. Instead the load-bearing work is a
prompt steer:

- **Persona lightened.** The talk-style rules at T0 (`How you talk`,
  `Conversation rules` incl. `LENGTH:` / `DON'T ALWAYS ASK A QUESTION`,
  `Leading vs following`) are now framed as *defaults*. Two of the most
  absolute rules (`LENGTH:` sizing, the "1 in 3 turns end on a thought"
  question cadence) were softened to name that a surfaced learned line
  recalibrates them.
- **The learned-style steer.** A constant, name-aware addendum
  (`build_learned_style_addendum` in
  [`prompt_support.py`](../../../app/core/session/prompt_support.py)) folds in
  right after the persona (same T0 slot as the speech-grammar addendum, so it
  stays in the cache prefix) telling the model that when the context surfaces a
  learned `communication_style` / `boundary` line it is the *live calibration*
  of the defaults and wins when it fits — "hold them lightly, never as hard
  rules, and when none surface the defaults simply stand". Self-gating, so
  there is no per-turn branching in T0.

This carries the deferred L23 "lighten hard-coded persona style blocks"
follow-on. The `communication_style` / `boundary` concept headers in
[`inner_life_part1.py`](../../../app/core/session/inner_life_part1.py) already say
"let these steer HOW you talk"; the addendum is the missing bridge from those
T3 lines back to the T0 defaults.

**Effort.** Small (prompt-only).

---

## L18c. Boundary-vs-conversation conflict steer (fast-follow) — SHIPPED

**Status: SHIPPED.** A K29-style per-turn detector
([`boundary_clash_detector.py`](../../../app/core/affect/boundary_clash_detector.py))
fires a soft T6 cue when the live turn is heading *toward* an active `boundary`
concept, so Aiko feels the tension in-the-moment instead of only carrying the
boundary as background T3 guidance.

- **Read.** Active boundaries via `ConceptView.relevant` (embedding-nearest,
  which yields the label-cosine in one call) across all subjects
  (user / relationship / aiko).
- **Gate.** Cosine floor (`memory.boundary_clash_min_cosine`, default 0.58 —
  a notch above the K29 opinion floor) + a word-count gate. `classify_pair` is
  used only to *sharpen* the register ("pushing right at" vs "brushing up
  against"), never to gate the fire. Cosine-only, no hot-path LLM.
- **Surfacing.** Provider `_render_boundary_clash_block` in
  [`inner_life_part3.py`](../../../app/core/session/inner_life_part3.py), wired
  into the T6 "live read on the turn" cluster next to K29 (cooldown +
  per-session cap mirror K29; joins the K51 cue-register rotation). The cue is
  self-contained and forbids naming the line out loud, refusing, or lecturing.

Gated by `agent.boundary_clash_enabled`. Tests: `tests/test_boundary_clash.py`.

**Effort.** Small-medium (fast-follow to L18).

---

## L18d. Concept-vs-concept conflict detection (under meta concepts)

**Status: SHIPPED (subsumed by L12; boundary-clash shape added to the tension
proposers).**

**Motivation.** Tensions *between* concepts — boundary vs value, boundary vs
boundary — are a distinct problem from boundary-vs-live-conversation. This is the
general machinery for reasoning about how concepts clash, and belongs with the
meta-concept work (L29).

**What shipped.** The concept-vs-concept conflict machinery is L12's tension
metas: `_run_tension_pass` offers every active non-meta concept — boundaries
included — to the proposer, and a chosen pair is stored as a `tension` meta with
`("concept", id)` `evidence` edges, surfaced through the T6 `TensionCueWorker`.
The only gap was framing: the three proposer prompts named friction as
value-vs-behaviour / hot-vs-quiet only, so boundary clashes under-fired. Fixed
prompt-only — `tension_user.py`, `tension_aiko.py`, `tension_relationship.py`
now name a boundary pulled against by a value/habit that crosses it (or two
boundaries that can't both be honoured) as a first-class friction shape, each
with an example.

**Dropped as redundant.** The sketched dedicated new edge relation + separate
concept-pair detector — a boundary-involving pair already composes into a
`tension` meta exactly like any other pair, so a parallel mechanism would only
duplicate it.

**Key files.** `app/core/concepts/proposers/tension_{user,aiko,relationship}.py`;
tests in `tests/test_tension_concepts.py` (`BoundaryClashShapeTests`).

---

## L19. Aiko's autobiography — self-history as a traversable timeline

**Status: SHIPPED.** Asked "have you changed?", she walks her own record instead
of inventing an answer. The *pull* side of L17's push.

**The substrate was already there**, which is why this came in far under its
"Large" estimate: neither event table has a prune path, both snapshot their
inputs at write time, and `concept_aliases` keeps merged-away beliefs reachable.
What was missing was traversal and narration.

**What shipped.**
[`self_history.py`](../../../app/core/concepts/self_history.py) builds an arc for
a subject: every concept **including retired ones** (a retired self-concept is
part of the story, not garbage), its learning events, and its alias chain,
classified as **flipped / faded / revived / born / settled** and grouped into
eras. Bucketing follows the span — weeks under ten weeks, months above — because
a five-week history read monthly is one era, which is not a story.

- **Grounded in the builder, not the prompt.** Every entry carries its
  `concept_id` and the ids of the learning events behind it, and the only prose
  it may repeat is the stored `because`. The narration happens in the model, and
  this data is the limit of what she can honestly claim.
- **`thin_record` is why this returns a structure rather than a string.** A
  young or sparse trail sets it, and the tool description makes saying "I don't
  have a record of that" the required response. `settled` beliefs deliberately
  do not count toward the record being substantive: having beliefs is not the
  same as having changed.
- **Flat read cost.** The concept mirror is already in memory; learning events
  and aliases are each read **once** in bulk and grouped in Python
  (`list_aliases` was added for this). A per-concept query would be hundreds of
  round trips on a tool-call path.
- `RecallSelfHistoryTool` in
  [`builtins.py`](../../../app/llm/tools/builtins.py), a sibling of
  `RecallConceptTool`; `"recall_self_history": "recall"` in `_TOOL_FAMILY` with
  the family patterns extended to "have you changed" / "what were you like
  before"; config gate `tools.recall_self_history`. Plus
  `GET /api/concepts/self-history`, the MCP `get_self_history`, and a **Story**
  sub-tab under Settings → Memory that renders the same payload the tool hands
  the model — so what she *would* say is inspectable before she says it.

**Open questions, resolved.** (1) *Snapshot retention* — keep everything; the
append-only tables already do. (2) *Does the user's history get the same
traversal* — yes, `subject` is a parameter and the Story tab toggles between
"hers" and "yours". (3) *How much to surface at once* — capped eras and capped
entries per era, most informative first, with a `truncated` count rather than a
silent trim.

**Effort.** Large as estimated on paper, small in practice, because L17c's
durability and alias work had already paid for the hard parts.

---

## L21. Cold-start + anti-premature-proposal guard

**Status: BUILT.** `TopicGraph.topic_graph_maturity()` / `mature()` provide the
cluster-count (+ member) signal; `MemoryStore.earliest_created_at()` provides
the calendar-history signal. The L2 `ConceptSynthesisWorker` gates both
`is_ready()` and `run()` on maturity (`concept_min_clusters` +
`concept_min_history_days`) while a manual `force` run bypasses it; the L3
`ConceptLifecycleWorker` promotes against a **stricter young-graph bar**
(`concept_promote_young_min_sources` / `concept_promote_young_min_confidence`)
via an injected `graph_mature_provider` until the graph clears the floor; the L5
`concept_block` stays silent while immature and `recall_concept` returns empty
when the store is sparse. All consumers degrade gracefully rather than
confabulate.

**Motivation.** The concept layer only lights up after months of accumulated
memories and clusters. Two failure modes to design against up front: (1) Aiko
behaving oddly *before* concepts exist (empty `concept_block`, a `recall_concept`
that returns nothing), and (2) the proposer (L2) firing on thin evidence and
minting a **spurious early concept** — which is worse than none, because a
confident wrong self/user model poisons everything downstream.

**Key files.** Gating in the L2 proposer + the L3 promotion gate; the L5
surfacing block degrades to silent when there are no `active` concepts (same
signature/cooldown machinery). Pairs with the topic-graph maturity signals from
F10 (cluster count / age).

**Sketched approach.** Don't run the proposer at all until the topic graph has a
minimum population (e.g. N clusters over M days of real history); raise the
promotion bar while the graph is young (more evidence + more cycles required
early); and keep every consumer (block, `recall_concept`, autobiography L19)
**gracefully empty** rather than confabulating when the store is sparse. The
layer should *quietly not exist* for a new user, then fade in — never blurt a
half-formed model of someone it barely knows.

**Open questions.** Maturity thresholds (cluster count / history age)? Do we
seed anything at onboarding (K19 cold-start companion), or stay fully emergent?

**Effort.** Small-Medium (mostly gates + graceful-empty paths).

---

## L25. Edge referential integrity across the memory lifecycle

**Status: BUILT.** The
[`ConceptEdgeReconciler`](../../../app/core/concepts/concept_edge_reconciler.py)
enforces the per-event policy below. It is registered as a `MemoryStore`
**delete listener** (`on_memory_deleted` drops a deleted memory's edges and
recomputes the affected concepts' edge-derived `evidence_count` /
`distinct_source_count`); the legacy Phase 4b `MemoryConsolidator` calls its
`repoint` hook to move a hard-deleted victim's edges onto the survivor *before*
deletion (rule b); and because `MemoryStore.prune` batch-deletes rows **without**
firing delete listeners, the idle
[`ConceptEdgeIntegrityWorker`](../../../app/core/concepts/concept_edge_integrity_worker.py)
sweep GCs any orphaned edges it leaves (`ConceptStore.orphaned_memory_edges` →
drop → recount). K35-archived rows stay alive so their edges are kept as
historical evidence (rule c). Counts are treated as **edge-derived**
(recomputed by any edge-mutating path, same as L2 reinforce); L3 remains the
single writer of `confidence` / `plasticity` / `status`. Knobs:
`memory.concept_edge_integrity_{enabled,interval_seconds,batch_size}`. See
[`concept-lifecycle.md`](../../concept-lifecycle.md) and
[`configuration.md`](../../configuration.md).

**Motivation.** Concepts point at memories through `concept_edges`, but memories
are not permanent: they're archived, consolidated/merged (K35), reclassified,
and outright **deleted** (dead scratchpad in `MemoryPromotionWorker`). Nothing
currently says what happens to an evidence edge when its target memory moves or
vanishes — leaving dangling edges, or a concept that silently loses the support
it was promoted on. Easy to miss, nasty to debug later.

**Key files.**
[`memory_store.py`](../../../app/core/memory/memory_store.py) (`delete` / `update` /
`prune`), [`memory_promotion_worker.py`](../../../app/core/memory/memory_promotion_worker.py),
[`memory_consolidation_worker.py`](../../../app/core/memory/memory_consolidation_worker.py),
the L1 `ConceptStore` edge table.

**Sketched approach.** Decide the policy per lifecycle event and enforce it in
one place: (a) **delete** — the memory-delete path notifies `ConceptStore` to
drop or tombstone the edge and decrement the concept's `evidence_count` (which
may weaken/demote it via L3); (b) **consolidation/merge** — re-point the edge at
the surviving memory rather than dropping it, so a merged evidence memory keeps
supporting its concept; (c) **archive/reclassify** — keep the edge (archived
memories still count as historical evidence, important for L19). Make edges
**tolerant of a missing ref** as defence-in-depth (skip, don't crash), and add
an idle integrity sweep that garbage-collects orphaned edges. Same single-writer
discipline: only the lifecycle engine reconciles counts.

**Effort.** Medium.

---

## L26. Concept trace + "how Aiko is thinking" observability

**Status: BUILT.** The dev-facing window into the layer: per-turn "what concepts
entered this prompt" plus MCP dumps of the live graph and recent lifecycle
transitions. Unit is *one turn* (vs. L6 user-facing / L22 aggregate quality).

**Delivered — per-turn trace.** The L5 `concept_block` and L4 `coactivation_block`
renderers
([`inner_life_part1.py`](../../../app/core/session/inner_life_part1.py)) stamp a
structured trace at selection time onto `self._concept_block_trace` /
`self._coactivation_block_trace`: the surfaced concepts (`concept_id`, `label`,
`confidence`, `plasticity`, `kind`, `subject`, `hedge`) or a `reason` when empty
(`disabled` / `block_disabled` / `store_missing` / `immature` / `no_eligible` /
`aggressive` / ...); and the chosen co-activation `mode` (`reps` / `labels` /
`strength` / `bucket_by`) + `quiet` cluster. Because the blocks are **slice-cached**
(`_StaticSlices`; the renderers don't re-run on a cache hit), the trace is
captured *at build time* through paired `concept_trace` / `coactivation_trace`
providers (`set_inner_life_providers`) into
`_StaticSlices.concept_trace` / `.coactivation_trace`, then forwarded by
`assemble_with_budget` to `PromptTelemetry.concepts_surfaced` /
`coactivation_surfaced` (tagged with `slice_cache_event` + `aggressive` so you
can tell cached vs. freshly built vs. dropped under pressure). That flows through
the existing `PromptTelemetry.as_dict()` → `_last_metrics` path, so it is visible
via `get_last_response_detail`. No edge walk on the hot path — the trace links by
`concept_id`; join to the graph dump for evidence.

**Delivered — introspection (MCP).** Three tools in
[`proactive_task_tools.py`](../../../app/mcp/server_tools/proactive_task_tools.py):
`get_last_concept_trace` (the per-turn trace above), `get_concept_graph`
(wraps `session.concepts_snapshot()` — every concept with status / confidence /
plasticity / rationale + resolved evidence edges + counts; richer than the older
`get_concepts_state`), and `get_concept_transitions` (wraps
`session.concept_timeline()` filtered to lifecycle events — `promoted` /
`dormant` / `retired` / `revived`, newest-first, dropping `discovered` births).

**Design notes.** Only `relation="evidence"` edges exist in v1, so
`concepts_snapshot()` is a complete graph dump — no new `ConceptStore.all_edges()`
needed. The trace is empty-with-`reason` (never absent) when the layer is
off / immature / aggressive. Trace snapshots are copied (not live-read) so a
cache-hit reports exactly what was in the prompt.

**Effort.** Medium (high leverage — validates every other L-entry).

---

## L35. Surface-reason labels -- "why did I surface this?" on every item

**Status: SHIPPED.** The follow-through on L26: that trace answered *which*
concepts entered the prompt, this one answers *why* each of them did.

**Motivation.** L26 already stamps a per-turn trace of *which* concepts surfaced
(with confidence + hedge). L35 adds the *why*: a structured reason on every
surfaced item — `high-confidence identity`, `recent emotional relevance`,
`unresolved contradiction`, `curiosity trigger`, `relationship importance`,
`recently forgotten/revived`. This makes the prompt legible ("why is this here?")
and sharpens debugging.

**What shipped.** A pure
[`surface_reason()`](../../../app/core/concepts/concept_surfacing.py) beside the
scorer it explains, plus a `SURFACE_REASON_LABELS` table for human phrasing.
Each L26 trace entry gains `surface_reason` (a stable token) and
`surface_reason_label`, hoisted to the top of the entry so the answer doesn't
require unpacking the `score` breakdown it was derived from. Reasons are listed
in [`context-budget.md`](../../context-budget.md#why-is-this-concept-here-l35).

Three rules decide it:

- **Two lanes answer themselves.** A core concept is pinned on confidence before
  any scoring runs; an activation-lane concept had no cosine to the turn at all
  and is in the prompt purely because a neighbour primed it. Neither ran a
  contest, so neither needs one decided.
- **Otherwise, the largest *weighted* contribution to `surface_score` wins** —
  not the largest raw value. A cosine of 0.9 against a kind whose `context`
  weight is zero won nothing, and saying otherwise would make the label a lie
  about the machinery it exists to explain.
- **A missing signal is never a reason.** `recency_boost` neutral-defaults to
  `1.0` — its *maximum* — for a concept that was never reinforced, which is
  correct for the score (a missing timestamp must not penalise) but would let
  "reinforced recently" win on the absence of evidence. Recency drops out of
  contention when there is no `last_reinforced_at`.

A salience win is refined into the specific story behind the charge
(`unresolved_contradiction` vs `recently_revived` vs …) by
`event_charge_detail()`, a sibling of the existing `event_charge()` that also
reports *which* event produced the max — the two are the same number to the
scorer and completely different stories to a reader.

**Open questions, resolved.** (1) *One reason or a ranked few?* — **one**. The
full ranking is already in the entry's `score` breakdown; a second opinion in
the reason field would just be the breakdown again, less precisely. (2) *Ever
shown to Aiko?* — **no, debug-only**. The backlog flagged the over-narration
risk and it is a real one: a companion who can read "I surfaced this because we
clashed on it" is one step from saying so.

**Not covered** (and not blocking): the `curiosity trigger` and `relationship
importance` examples in the original sketch need L30 / L32, neither of which is
built. Adding a reason for a signal that doesn't exist yet would be inventing a
label with nothing behind it; those land with their features.

**Effort.** Small (labelling existing selection signals; extends L26) — as
estimated.

**Depends on.** L26 (trace).

---

## L40. Habituation reaches the core lane through order, not relevance

**Status: SHIPPED — with the entry's premise corrected on the way in.** Worth
reading as much for the wrong diagnosis as the fix: the backlog entry named a
real symptom, attributed it to the wrong mechanism, and the fix it proposed would
have been a silent no-op.

**What the entry claimed.** That the L27 core lane computes a habituation factor,
uses it only to sort fresh candidates ahead of stale ones, then admits the
winners with `relevance=conf` — raw confidence — so a just-surfaced core belief
"competes against *memories* with" full strength and takes budget from material
the user hasn't seen. The proposed fix was to multiply that relevance by the
habituation factor already in hand.

**Why that was wrong.** `ContextBudgetSelector.select` admits pinned candidates
in **pass 0**, iterating `pools[name]` sorted by `order`, exempt from
`min_relevance` and the source `cap`. It never reads a pinned candidate's
`relevance` for admission — only for the `top_relevance` telemetry field. And a
pinned candidate that fails pass 0 on budget can never recover: `_admit` returns
`False` *without* adding it to `admitted_keys`, so it does flow into the
relevance-sorted greedy remainder, but `used` only ever grows between the passes,
so every later attempt fails for the same reason. Confirmed empirically —
selecting the same three pinned candidates with identical `order` and token costs
but relevance reversed from descending to ascending admits exactly the same two.

So multiplying the pinned relevance by habituation would have changed one
telemetry number and no behaviour, while the entry, the commit and the test name
all claimed a repetition fix. Habituation was already reaching the core lane
completely — through `order`, which is the only thing that governs a pinned item.

**The actual defect, next door.** `habituation_factor` returns a *graded* value
between the core floor (default 0.8) and 1.0, but the lane consumed only its
threshold:

```python
(fresh if hab >= 0.999 else stale).append((concept, cid, label, hab))
```

Both groups then kept `core_lane`'s native confidence-descending order. So the
graded factor collapsed to a boolean, and *within* the stale group the ranking
was pure confidence: a belief surfaced last turn at confidence 0.9 preceded one
rested for three turns at 0.8. The more-rested belief lost, which is precisely
the anti-nag outcome L23 exists to prevent — just one level down from where the
entry was looking.

**What shipped.** One stable sort of the stale group by rest,
`stale.sort(key=lambda t: -t[3])`, in
[`inner_life_part1.py`](../../../app/core/session/inner_life_part1.py). Stable, so
equally-rested concepts keep `core_lane`'s balanced round-robin across
`(kind, subject)`. The `relevance=conf` construction is left alone, with a
comment recording *why* damping it there would be inert, so the next reader
doesn't re-derive the same wrong fix. Regression-tested in
[`test_context_budget.py`](../../../tests/test_context_budget.py) via
`test_core_lane_ranks_by_rest_not_confidence`: three core-qualifying concepts
inside the habituation window (so the binary split cannot separate them) and a
cap of two, where the most-rested concept takes a slot despite having the lowest
confidence. The pre-existing `test_core_lane_soft_rotation` still passes, which
is what confirms the fresh-over-stale rotation is unchanged.

**Open questions, resolved.** (1) *Does this want the full blended
`surface_score` instead?* — moot; the sort key is rest, and a core pin stays
justified by how firmly the belief is held rather than by turn-match, which is
the point of an always-on lane. (2) *Can a habituated core concept still win when
it is the only candidate?* — yes, unchanged: the stale group still fills the cap
when fewer than `core_cap` candidates are fresh, so a core belief is never
suppressed out of contention, only re-ranked within it.

**Effort.** Small, as estimated — though the estimate was for the wrong change.

**Depends on.** Nothing. Shipped alongside L39's dedupe, same code block.

---

## L37. Surfacing outcome ledger -- did what I brought up actually land?

**Status: SHIPPED.** The ledger measures, and L38 now consumes its concept rows
off-turn to maintain earned standing. The original recorder-first split was
deliberate: ship the measurement, inspect real data, then calibrate the scorer
against the observed relationship rather than guesses.

**Motivation.** The concept layer could grow its *knowledge* but not its
*judgement*. Everything deciding which concepts and memories reach the prompt is
a hand-tuned constant — the per-kind `surface_weights`, the core-lane confidence
bars, the habituation window — and none of them moved in response to how the
conversation went. Surfacing was very nearly write-only: the only trace a
surfaced concept left was the habituation timestamp stamped at the end of
`build_relevant_context`. A concept that had been in front of Aiko two hundred
times to no visible effect was indistinguishable from one that opened up a good
conversation every single time.

The memory layer was one step ahead but stopped short of the same line:
`_mark_revived_memories` bumps `revival_score` when the reply shares content
words with a surfaced memory, which measures whether **Aiko echoed it**, not
whether **the user cared**. Nothing asked the second question, even though the
answer was already being computed and discarded for this purpose every turn —
`EngagementTracker.record_turn` buckets the user's reply latency and word count
against his own rolling baseline into `engaged` / `neutral` / `disengaged` /
`abandoned`.

**The off-by-one is the whole design.** `_compute_user_reply_latency_seconds`
measures the gap between assistant reply *N-1* and user message *N*, so the
engagement computed during post-turn of turn *N* describes the reaction to the
**previous** reply. A ledger that credited the current turn's surfaced set would
have looked completely healthy — every count plausible, every rate populated —
while measuring the wrong thing. Rows are therefore keyed by the
`assistant_message_id` of the reply they helped produce and settled one turn
later, following the stash-and-settle precedent J11's `_prev_affection_kinds`
and K74's `_prev_humor_kinds` already set in
[`post_turn_mixin.py`](../../../app/core/session/post_turn_mixin.py).

**Two corrections to the groomed entry, found while building.**

*There is no `turn_id`.* The entry proposed keying on
`(turn_id, item_kind, item_id)`, but the only `turn_id` in the codebase is a
`secrets.token_hex(4)` in a `ContextVar` for log correlation — never persisted,
never passed to `build_relevant_context`. The key is `assistant_message_id`, a
real reference into `messages`, and it doesn't exist until the reply is
persisted. That is *why* rows are written in post-turn rather than at surfacing
time, with `build_relevant_context` only stashing.

*Post-turn is not guaranteed to run.* Empty user text returns early, and if
`AffectUpdater.apply_turn` raises, `_post_turn_inner_life` returns before the
engagement block. So a turn can leave rows unsettled or produce none at all —
which is why unsettled is modelled as a *correct* state rather than an error.

That second correction has a consequence the entry didn't follow through on. A
skipped post-turn leaves the carried key a turn behind, and the next turn to
reach the hook would settle a two-turns-old reply with the reaction to the one
in between — a *wrong* number, which is strictly worse than a missing one in a
feature whose entire purpose is trustworthy measurement. So the carry is
validated before it is used: K14 measures latency from the last assistant
message before the current user message, so if another assistant message sits
between the carry and that user message, the carry is stale and the ledger
declines to settle (`has_assistant_message_between`, one indexed lookup). The
row stays open and reads as "no evidence". Unverifiable cases — no
`user_message_id`, no database, a raising query — count as current, since the
carry is right on every path that isn't the rare skip and defaulting to
"decline" would starve the ledger of the outcomes it exists to record.

**What shipped.**

- **Schema v25 -> v26**, `surfacing_outcomes` in
  [`chat_database.py`](../../../app/core/infra/chat_database.py), following the
  `concept_events` pattern: idempotent DDL in `_CREATE_TABLES` so it lands on
  fresh databases, a comment-only ladder entry since there is nothing to
  backfill (and inventing history would poison the very rates it measures), and
  `item_id` as a soft reference that is never cascade-deleted, so a pruned
  memory still leaves its surfacing history standing.
- **`SurfacingOutcomeStore`**
  ([`surfacing_outcome_store.py`](../../../app/core/memory/surfacing_outcome_store.py)),
  in `memory/` beside the similarly cross-cutting `memory_conflict_store.py`
  since the ledger spans memories, concepts and clusters. `add_many` writes a
  turn's set in one transaction — a half-written turn would quietly skew every
  rate derived from it. `settle` touches only `settled_at IS NULL` rows, so it
  is idempotent by construction and a retry can never overwrite an earlier
  verdict with a later turn's engagement.
- **Write side**: `build_relevant_context` stashes the projected set on
  `_last_surfaced_items` (mirroring `RagRetriever`'s
  `_last_surfaced_memory_ids` snapshot, with the same caveat that the
  golden-line regression path perturbs it), and *clears it on entry* so an early
  return can't leave the previous turn's set behind to be credited twice.
  Post-turn consumes the stash unconditionally for the same reason.
- **Echo marks** are stamped at insert time, since "did Aiko reference it" is a
  same-turn signal. `revival_min_word_overlap` was tuned against multi-sentence
  memory content, so applying the same nominal bar to a three-to-six word
  concept label would make it a far harsher test of the same thing; concepts get
  their own floor (`surfacing_echo_min_overlap_concept`).
- **`get_surfacing_outcomes`** MCP tool (the small half of DT5), reporting the
  per-item leaderboard, a per-lane rollup, and the unsettled count.

**Three distinctions the API refuses to collapse**, all of which would have been
awkward to retrofit once L38 reads this once per candidate per turn:

*Counts, not rates.* `stats_for` returns `(surfaced, settled, engaged, echoed)`
and lets the caller derive the ratio, because one item settled 1-for-1 must not
look like one settled 50-for-50. This is the gate
`EngagementTracker._is_warmed` already implements for itself — reporting
`warmed=False` rather than a confident label off two data points — and L38 can
only build the same gate if the denominator is exposed.

*No evidence is not zero.* `engaged_rate` is `None` rather than `0.0` when
nothing has settled, so a consumer cannot punish an item purely for being new.

*NULL is not False.* An `echoed` column left NULL means "could not look" (a
cluster whose label isn't reachable, a memory since deleted), which is a
different finding from a computed False — an item Aiko demonstrably ignored.

*And the window.* `window_days` is a required keyword rather than an optional
afterthought, `None` meaning lifetime. A lifetime-only rate anchors on early
data and progressively stops adapting, inverting the goal of the feature —
[`concept_quality.py`](../../../app/core/concepts/concept_quality.py) already
draws the same lifetime-*stock* versus windowed-*flow* distinction deliberately.
Windowing also bounds the hot-path query: at roughly ten rows a turn the table
reaches a few hundred thousand rows within a year, so the aggregate is served by
a `(item_kind, item_id, created_at)` covering index — asserted directly via
`EXPLAIN QUERY PLAN` in the tests, since an index that silently stops covering
is exactly the kind of regression that shows up as a mysteriously slow turn.

**Open questions, resolved.** (1) *Retention* — `prune(keep_days)` ships as a
method but nothing schedules it; P34 owns the policy, and note that here it is
about signal freshness and hot-path cost, not just disk. (2) *Does `abandoned`
mean the surfaced set was bad, or that dinner was ready?* — resolved as the
entry proposed: only `engaged` counts as evidence *for* an item, so the signal
degrades to "no evidence" rather than false blame. (3) *Worker cues in the same
table?* — the `item_kind` column is an open enum, so G4 adding `kind="cue"` is a
value rather than a migration; deliberately not written yet. (4) *Does `echoed`
deserve its own weight?* — still open, and now answerable: it is recorded beside
the engagement label precisely so the two can be compared before either is
acted on. High echo with low engagement would mean Aiko takes the bait and the
user doesn't.

**Permanently out of scope, by design.** Any attempt to isolate one item's
contribution when eight were surfaced together. Turn-level shared credit is
noisy per turn and adequate over hundreds; a regression there would invent
precision the signal cannot support.

**Effort.** Medium, as estimated — the schema bump and two write points were
routine; the off-by-one settling dance and the read-API shape took the thought.

**Depends on.** K14 (`EngagementTracker`, shipped). Unblocks L42, F12, G4, P43,
K81, and the rest of DT5; L38 is now shipped below.

---

## L38. Earned standing -- let outcomes move the surfacing score

**Status: SHIPPED.** L37's relationship-local outcomes now give each warmed
active concept a slowly learned surfacing prior. This is deliberately a measure
of "how useful has this been to bring forward?", never "how true is it":
standing remains independent from concept confidence.

**Estimator and persistence.** `engagement_baseline` pools the current 90-day
concept window so the relationship's observed engaged rate maps to neutral
`0.5`. For concepts with at least four settled rows, `earned_standing` computes
`(engaged + 10 * baseline) / (settled + 10)`, maps below-baseline performance
toward the safe `0.35` floor and above-baseline performance toward `1.0`, and
clamps `value` / `boundary` concepts to at least neutral. Cold, missing, stale,
and malformed evidence is neutral. The bounded `concept.earned_standing`
`kv_meta` map needs no schema migration.

**Off-turn refresh.** `ConceptLifecycleWorker` resolves the
`SurfacingOutcomeStore` lazily (preserving session initialization order), reads
all active IDs in one grouped `stats_for("concept", ids, window_days=90)` call,
and replaces/prunes the cache on an hourly cadence. Prompt assembly never
queries the ledger: `build_relevant_context` loads the small cache once.
Settings expose the master switch, window, warmup, prior strength, bounds,
cadence, and cache cap.

**Scoring contract.** `SurfaceWeights.standing` defaults to `0.0` for unknown
or future kinds; every existing static-T3 kind opts in at a modest `0.10`.
`tension` remains zero because it does not use static T3 surfacing. Standing is
sum-normalized with context/confidence/recency/stability/salience in
`surface_score`; it is never an additive activation bonus and never mutates
confidence. It applies only to flex and activation candidates. The pinned core
lane is unchanged.

**Safety and observability.** A `0.35` floor, modest weight, rolling window,
topical cosine and habituation preserve exploration: a strong topic match can
recover low-standing material, while a high-standing item still rotates after
recent use. `score_components` records the standing value, and
`earned_standing` may appear as a debug-only surface reason. L41 intentionally
does not map that reason to prose, so Aiko never narrates the mechanism.

**Verification.** The pure estimator tests cover pooled-baseline calibration,
shrinkage, warmup neutrality, malformed data, bounds and protected kinds.
Lifecycle tests cover one-query refresh, cadence, replacement/pruning and a
missing ledger. Scorer/context tests cover normalization, habituation rotation,
topical recovery and trace values. The concept, ledger, context-budget and
reason-framing regression selection passes.

**Depends on.** L37 (shipped). Related to L32: importance is stated weight;
standing is earned usefulness, and they remain separate axes.

---

## L41. Reason-conditioned phrasing -- use the L35 signal without narrating it

**Status: SHIPPED.** L35 computes, per surfaced concept, *why* it won its slot
(`settled_belief`, `unresolved_contradiction`, `association`, `recent_change`,
…) and, by explicit design, keeps that reason **debug-only** — letting Aiko
read "I surfaced this because we clashed on it" is the fastest route to a
companion who narrates her own machinery. That rule is **kept, not relaxed**.
L41 uses the reason purely as *framing input*: it picks the lead-in of each T3
concept-impression line without ever stating the reason, so a freshly-changed
belief and one held serenely for months no longer arrive in identical clothing.

**What shipped.** A module-level `_REASON_FRAMINGS` table in
[`inner_life_part1.py`](../../../app/core/session/inner_life_part1.py),
deliberately kept **separate** from the debug-only `SURFACE_REASON_LABELS`,
collapsing twelve reasons onto four non-technical voices:

- **settled** (`settled_belief`) — "You've long since made your mind up that"
- **freshly-changed** (`recent_change` / `loosening_boundary` /
  `newly_promoted` / `recently_revived`) — "Lately you've come around to
  feeling that"
- **primed** (`association`) — "Something here nudges the sense that"
- **unsettled, restrained** (`unresolved_contradiction`) — "You haven't fully
  settled it, but you sense that" — deliberately the *most* restrained voice,
  never dramatic, so it doesn't invite her to re-litigate the tension each time
  it surfaces.

The five unmapped reasons (`topic_match`, `high_confidence`,
`recently_reinforced`, `core_belief`, `earned_standing`), plus `None` / any
unknown token, fall through to the existing `_hedge_for_confidence`, so lines
stay one voice and about the same length and a reason added later cannot break
rendering.

**Static `_reason_framing(reason, confidence)` helper** returns the mapped frame
or the confidence hedge, with a **confidence guard**: the `settled` frame
asserts certainty, so below the `0.65` "sense that" tier it falls back to the
hedge — stability and confidence are different axes, and a stable-but-unsure
concept must not overclaim. In `_render_relevant_concepts` the `comp`/`reason`
lookup was hoisted above the line build; the confidence `hedge` is still
computed and recorded in the trace entry (telemetry unchanged). Gated by
`agent.concept_reason_framing_enabled` (default `True`); off ⇒ exact pre-L41
output. Scope is T3 only — the T0 profile block is untouched.

**The load-bearing test** (`tests/test_concept_reason_framing.py`) asserts that
across every framing the rendered text contains **none** of the raw reason
tokens nor the mechanism words (`contradiction` / `surfaced` / `confidence` /
`revived` / `topic` / …) — the anti-narration rule encoded as an assertion.

**Effort.** Small, as estimated — a phrasing table plus a rendering branch.

**Depends on.** L35 (shipped). Would benefit from T5's eval scoreboard to
measure whether the framings change model behaviour or merely cost tokens.

---

## L42. A self-model of her own surfacing behaviour

**Status: SHIPPED.** Aiko now forms a slow, relationship-scoped self-model of
how she allocates conversational attention. A weekly pass over L37 detects at
most one finding in each of three shapes: **concentration** (one topic receives
far more attention than the user's own mapped topic distribution predicts),
**neglect** (several old, high-confidence, non-profile concepts are almost
never used), and **fixation** (one non-core concept is a clear flex/activation
frequency outlier while landing below L38's relationship baseline).

**Truth and cost boundaries.** `SurfacingOutcomeStore.stats_for(..., lanes=...)`
keeps core-pinned rows out of fixation. `RagStore.list_recent_user_vector_rows`
reads bounded, already-indexed user vectors over the 90-day window; history is
never re-embedded. Cold baselines, thin denominators, malformed dates, missing
providers, and findings with fewer than two evidence nodes all fail silent.
`ConceptSynthesisWorker.demand()` only checks a KV timestamp; aggregation stays
inside the run and occurs at most weekly.

**Durable self-model.** Findings pass through the dedicated `conduct` proposer
and become ordinary `subject="aiko"` concepts. The LLM may name a falsifiable
first-person observation, but the detector supplies its shape and evidence;
the prompt forbids counts, percentages, and mechanism language. The kind is
relevance-only, moderately plastic, and excluded from the core lane. A bounded
KV snapshot serves behavioral consumers while concepts remain the durable
record and decay naturally if a weekly finding stops recurring.

**Behavioral use.** T3 renders conduct as a revisable first-person impression.
A current concentration or fixation finding suppresses the optional K81 taste
steer so preference cannot deepen a rut; neglect is recorded but does not yet
drive curiosity. A separate T6 notice is default-on behind its own flag, but
requires a lull, relationship trust plus warmth, an active confident conduct
concept, once-per-conversation state, and a persisted seven-day cooldown. It is
dropped under aggressive assembly and permits only a brief, fallible
relationship observation with no metrics or machinery.

**Follow-up.** `L42b neglect-guided curiosity` remains open in the backlog. It
must wait for enough real findings to evaluate detector quality before neglect
can bias idle research.
