# Shipped -- Higher-order concepts (L-series)

Part of the [shipped log index](../shipped.md). The concept layer's landed
foundations: the store and lifecycle engine, the kinds that reached
production, and the integrity / observability work underneath them. Open items
from this family -- the L17 self-drift chain, the L30 hypothesis family, and
the remaining tuning in L22 -- still live in [`concepts.md`](../concepts.md),
which also carries the design preamble for the whole layer.

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
