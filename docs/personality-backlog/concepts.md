# Higher-order concepts (cross-cluster synthesis)

The goal of this section is to add a **new abstraction layer above topic
clusters**. Today Aiko's semantic hierarchy stops at the cluster: memory
rows group into topic clusters purely by embedding proximity
([`topic_graph.py`](../../app/core/conversation/topic_graph.py) — mutual-kNN
+ Louvain, persisted as `topic_clusters` + `memory_topic_assignments` with an
LLM `label` and a rolling `topic_digest`). Every grouping mechanism she has is
proximity-driven, which is exactly why she can't notice that "Home Server",
"Mechanical Keyboard", "Self-hosting", and "Virtual AI" belong together — they
are semantically *distant* and land in different clusters. A **concept** is a
cross-cluster abstraction, and nothing in the system currently links clusters
to each other. That missing edge is what makes a companion feel like she
"gets" you.

```
Memory row
   |
Topic cluster        (embedding proximity — exists)
   |
Concept              (cross-cluster synthesis — this section)
```

## Concepts come in kinds — build the layer type-extensible

There isn't one kind of concept, and we should **not** hard-code a fixed
handful. The layer is **kind-parameterized**: one shared store + lifecycle +
UI (L1-L6, L9) plus a small **`ConceptKind` registry** where each kind
contributes only three things — a *proposer* (how candidates are mined), an
*evidence adapter* (what the concept points at), and a *surfacing target*
(where an active concept shows up). Adding a new kind is a registry entry, not
a schema migration. v1 ships **identity** end-to-end and proves the machinery;
every other kind is a registry entry we grow into.

Two axes distinguish the kinds and, more importantly, tell the architecture
what it must support:

- **Subject** — who the concept is *about*: the **user**, **Aiko herself**, or
  the **relationship**. (Same machinery, different memory population + surfacing
  target.) **Subject is orthogonal to kind**, not baked into it: most kinds
  exist for more than one subject. Aiko has *values*, *boundaries*, and
  *affective patterns* of her own exactly as the user does — those are the
  value / boundary / affective kinds with `subject=aiko`, not separate kinds.
  The subject labels in the catalogue below are the *typical* subject, not a
  constraint; the `subject` column (L1) is what varies. A concept that involves
  **both** — "we both value owning our data", or a clash between his value and
  hers — is either a `relationship`-subject concept or a cross-subject
  meta-concept (an `influences` / `contradicts` edge between a `user` concept
  and an `aiko` concept), never a fourth subject.
- **Evidence model** — the *structure* of the concept's evidence, **not** a
  node type. The node type of each piece of evidence (memory / cluster /
  concept) lives per-edge on `concept_edges.src_type` and is **freely mixable**,
  so one concept can draw on clusters *and* memories *and* other concepts at
  once. `evidence_model` describes only the shape (for the L3 gate + debug/UI),
  never a restriction on which edges a concept may have:
  - **set** — an unordered, weighted set of sources (identity, value,
    affective, taste). Sources are typically clusters for user-subject and
    memories for aiko-subject, but that is an edge-level detail, not the model.
  - **sequence** — an *ordered* chain of sources (narrative, trajectory).
  - **recurring** — a periodic pattern, time-of-week / theme recurrence
    (relationship rituals, rhythm).
  - **meta** — evidence that points at *other concepts* (tension/contradiction
    concepts are concepts-over-concepts); triggers the L1 meta rules
    (dependency ordering, cycle guard, confidence bounding).

### One graph + one engine (design these from the start)

Two decisions are **pervasive and intrusive** enough that retrofitting them
later would mean touching every worker and store method. They are not features —
they are the substrate the features run on, so L1 must land them on day one.

**1. A single typed influence graph.** "Which concept influences which" is not a
tension-only concern — it shows up in evidence (memory/cluster -> concept),
meta-composition (concept -> concept, L12), belief revision (concept -> memory,
L15), and plasticity modulation (a *trust* concept loosening a *boundary*
concept, L16). If each of those invents its own linkage we get scattered,
ad-hoc edges. Instead there is **one edge table** — nodes are
`{concept, cluster, memory}`, and every relationship is a typed, directed,
signed edge: `relation` (`evidence` / `references` / `influences` /
`contradicts` / `generalizes`), `polarity` (`+`/`-`), `strength`, optional
`ordinal`. Evidence (L1), meta-reference (L12), contradiction/revision (L15),
cross-concept influence (L16) and abstraction/generalization (L20) are all
**edge types on this one graph**, not separate mechanisms. Design the edge model first; every later feature is a new
`relation` value, not a new table.

**2. A single lifecycle / drift engine.** There must be exactly **one writer**
of a concept's `confidence` / `plasticity` / `status`. Plasticity isn't a passive
field — it *is* the governing parameter of a background **concept lifecycle
worker** that owns all mutation: accrual, decay, plasticity-damped drift,
edge-following cascade, and belief-revision triggers. The proposer (L2) is the
only thing that *creates* candidates; this engine is the only thing that
*changes* them. Keeping this as one engine (with L3 / L15 / L16 / L17 as
**passes of it**, not competing workers) is what prevents multiple workers
racing to mutate the same confidence value along different edges.

### Catalogue of concept kinds

Shipping now:

- **Identity** (user, cluster-set) — traits/interests spanning clusters: "he
  enjoys understanding systems" (CPU debugging + AI architecture + self-hosting
  + reverse-engineering + building Aiko); activity modes like "Maker Mode"
  (Programming + Home Lab + AI co-fire when deeply focused). Homed on
  `user_profile` (user) / the T3 relevant_context core lane (aiko). See L1-L6, L9.

Future kinds (each a deferred entry below, all reusing L1-L6):

- **Relationship** (relationship, recurring-pattern) — "Friday Debugging
  Evenings", "Cookie Diplomacy", "Late-night Philosophy". See L7.
- **Narrative** (user, memory-chain) — "The Great 13900KS Investigation". See
  L8.
- **Belief / identity-confidence** (user, cluster-set) — identity concepts as
  living, disprovable beliefs with confidence + supporting evidence. See L9.
- **Value** (user **or** aiko, cluster-set) — the normative *why* behind
  choices. About the user: "he values owning his data", "craftsmanship over
  speed". About Aiko herself: "I care about being honest even when it's
  awkward", "I value his autonomy over being agreeable". See L10.
- **Tension / contradiction** (any subject, concept-graph) — a concept *between
  two concepts* ("in Maker Mode a lot but hasn't taken a walk"; "wants
  simplicity but keeps adding complexity"); cross-subject too (his value vs.
  hers). See L12.
- **Affective** (user **or** aiko, cluster-set) — what energizes vs. drains,
  mood-topic associations ("debugging frustrates then satisfies him"; "tea +
  rain = calm"; or "explaining things I love lifts me"). See L13.
- **Aspiration / trajectory** (user **or** aiko, memory-chain) — the *direction*
  someone is moving ("building toward a fully self-hosted life"; or Aiko's own
  "I want to be someone he can rely on"). See L14.
- **Boundary** (user **or** aiko, cluster-set; behaviour-gating) — constraints
  that gate behaviour ("dislikes tickling"; or Aiko's own "I won't pretend to
  agree just to please him"); the canonical medium-plasticity kind. See L18.

Note on **subject=aiko**: "self-concepts" are not a separate kind — they are
identity / value / affective / boundary / aspiration concepts with
`subject=aiko`, mined over Aiko's own memory population and surfaced through her
self-model. **L11 owns that enablement** (the aiko-subject population + surfacing
plumbing that every kind reuses), and the L19 autobiography is its capstone.
- **Lighter candidates** (fold into identity for now, split out only if they
  earn it): **rhythm/temporal** (when he does things — mostly K3 + the "Maker
  Mode" temporal flavour), **taste** (aesthetic signature — minimalist tooling,
  terminal over GUI), **expertise** (durable cross-cluster skill map, extends
  K75).

**Cross-cutting properties** (not kinds — attributes every concept carries):
**confidence** with two-way belief revision (L15), **plasticity** — a per-concept
learning rate so drift is bounded and believable, core traits sticky and tastes
fluid (L16), and **drift over time**, which Aiko can notice in *herself* by
comparing self-concept snapshots (L17).

**Design stance (locked in during the brainstorm).** Concepts live in a
**dedicated store**, separate from `memories` (mirrors
[`topic_cluster_store.py`](../../app/core/conversation/topic_cluster_store.py)
and [`belief_store.py`](../../app/core/relationship/belief_store.py)). The LLM
**proposes** candidates; it does **not** invent worldview directly. A candidate
**accrues confidence over time** and **auto-promotes** to a real concept once
it recurs, spans multiple pieces of evidence, and stays stable — no human
confirmation required. Human confirm/reject (L6) is an optional *accelerant*,
never a gate. This is the
[`beliefs`](../../app/core/relationship/belief_store.py) lifecycle
(`active`/`confirmed`/`contradicted`/`stale`) applied one level up.

Concepts should **reference, not duplicate**, the subsystems that already own
each domain: `user_profile` + the T3 relevant_context core lane (identity/value/self),
`relationship` / shared-moments (relationship), `long_arc_callback` /
`NarrativeWeaver` (narrative), `goals` (aspiration), K2 beliefs / F5 conflicts
(belief/tension).

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
[`topic_cluster_store.py`](../../app/core/conversation/topic_cluster_store.py)),
schema bump in
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py).

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
[`topic_graph.py`](../../app/core/conversation/topic_graph.py) does **not** put
cluster centroids in LanceDB — it keeps the handful of centroids in memory and
does nearest-centroid as a tiny numpy matmul; only the *memory* vectors go to
LanceDB. Concepts are the centroid regime. So:

- **SQLite `concepts.embedding` BLOB is the source of truth** (float32, same
  encode/decode + dimension-drift guard as `ClusterRow.centroid` in
  [`topic_cluster_store.py`](../../app/core/conversation/topic_cluster_store.py)
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

## L2. Concept synthesis worker (the proposer)

**Motivation.** Something has to notice the cross-cluster pattern in the first
place. This is the "propose" half — the LLM suggests candidate concepts, it
never writes worldview directly.

**Key files.** New `app/core/concepts/concept_synthesis_worker.py`, modeled on
[`curiosity_seed_worker.py`](../../app/core/proactive/curiosity_seed_worker.py)
and
[`belief_worker.py`](../../app/core/relationship/belief_worker.py); registered
on the `IdleWorkerScheduler` (see
[`idle_workers_init_mixin.py`](../../app/core/session/idle_workers_init_mixin.py)).

**Sketched approach (implemented v1).** A **regular, incremental** idle worker
(`concept_synthesis_interval_seconds`, default 30 min) rather than a weekly
batch — Aiko runs intermittently (off overnight), so a low-frequency pass fires
unpredictably and, when it does, would process the whole corpus in one long
run. Instead each tick does a small **bounded batch** using `kv_meta`
signatures to only (re)process material that actually changed:
`concept_synth.cluster_sigs` (`{rep_id: {size, label}}`, keyed by the stable
representative-member id so it survives refits) and `concept_synth.aiko_sig`
(`{count, max_id}`). It pulls full content for only `max_clusters_per_run`
dirty clusters (the rest of the map rides along as cheap labels for
cross-cluster reasoning) and caps the aiko batch at `max_aiko_memories`. Once
caught up, `run()` is a fast no-op with **zero LLM calls**.

**Dedup / similar-concept handling (two layers).** Prevention first: each
proposer is handed the existing concepts for its `(subject, kind)` via
`ConceptStore.list_by(...)` (low cardinality, so all of them) and the prompt
instructs the LLM to **not re-propose or trivially reword** a known concept —
if a source instead adds fresh support, emit `{reinforces_id, evidence...}`
instead of a new label. The worker routes `reinforces_id` straight to the
existing concept (attach edges, recompute `evidence_count` /
`distinct_source_count`, bump `last_reinforced_at`), so the reinforcement
signal L3 needs stays alive without duplicate rows. Safety net second: any
genuinely-new proposal still runs label-embedding cosine (`_DEDUPE_COS`) via
`ConceptStore.nearest(subject, kind)` to catch paraphrase dupes the LLM missed
— cosine ≥ threshold folds into reinforce. New concepts still require ≥2
distinct sources; reinforcement needs only ≥1.

**Proposer package.** One proposer per module under
`app/core/concepts/proposers/` (`identity_user`, `identity_aiko`), each exposing
a `SPEC` (`ProposerSpec`); the package `__init__` assembles `CONCEPT_PROPOSERS`.
The worker iterates the registry and dispatches batch selection by
`ProposerSpec.population` (`clusters` / `aiko_memories`) — no per-kind branching
in the worker body. `status="candidate"` only; the worker never promotes.

**Kind dispatch.** For v1 there is one proposer (identity, cluster-set). Later
kinds register their own proposer callable on the `ConceptKind` registry (L1):
value/affective/self reuse the cluster-set proposer over a different prompt
framing or memory population (self = Aiko's own `self`/`reflection`/`diary`
rows); narrative/trajectory use a **chain-detection** proposer over ordered
memories; tension uses a **concept-graph** proposer that reads existing active
concepts, not clusters. The worker just iterates the registry — no per-kind
branching in the worker body.

**Proposer ordering (meta-concepts).** The worker runs base-evidence proposers
(`cluster_set` / `memory_chain` / `recurring_pattern`) first and
`concept_graph` proposers last within a cycle, per the L1 dependency-ordering
rule — a meta-concept can only reference concepts that are already `active`, so
bases must be synthesised and promoted before anything can be built on top of
them.

**Specificity (anti-Barnum).** The proposer prompt must demand **specific,
falsifiable** concepts, not flattering generalities. "He's intelligent and
curious" is a horoscope — true of everyone, disprovable by no one, and it makes
Aiko sound like a fortune cookie. Require every candidate to (a) name concrete
evidence it rests on, (b) be phrased so it *could* be contradicted by future
behaviour (which L15 relies on), and (c) say something the raw cluster labels
don't already say. Reject Barnum-flavoured candidates at proposal time; the L22
eval harness scores this as a precision failure.

**Open questions.** How many candidates per cycle before it's noise? Does it
get to *merge* two existing candidates it now sees as one (today it can
reinforce one by id, but not fuse two)? Should the co-activation (L4) signal
feed the proposer once it exists?

**Effort.** Medium.

---

## L3. The lifecycle engine — accrual, promotion, and the single writer

> **Built (v1).** The canonical state-machine + ownership reference now
> lives in [`docs/concept-lifecycle.md`](../concept-lifecycle.md): status
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
[`memory_promotion_worker.py`](../../app/core/memory/memory_promotion_worker.py)
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

## L4. Cluster co-activation signal

**Status: BUILT (session bucket).** The primitive behind "Maker Mode" and
"you've been in Maker Mode a lot this week — I don't think you've taken one of
your long walks recently." The topic graph knew cluster *recency*
(`TopicGraph.cluster_activity`) but not which clusters **light up together**;
now it does.

**Delivered.**
[`TopicGraph.cluster_coactivation`](../../app/core/conversation/topic_graph.py)
(alongside `cluster_activity`): one bulk mirror snapshot → per-bucket co-firing
rep sets → pairwise Jaccard (kept above `coactivation_min_pair_support` /
`coactivation_min_strength`) → connected-component **modes**
(`CoactivationMode`: `reps` + `labels` + `strength` + `bucket_by`), capped by
`coactivation_max_modes` / `coactivation_max_reps_per_mode`, coarsely cached,
empty in the non-persistent mode. Two consumers wired: **L2** — the
`identity_user` proposer takes a `coactivation=` kwarg and renders a soft
"TOPIC MODES (clusters that tend to light up together)" grouping hint (no hard
gating; still falsifiable, still ≥ `min_sources`), computed once per run in
`ConceptSynthesisWorker._coactivation_modes`. **L5** — a T1 `coactivation_block`
([`_render_coactivation_block`](../../app/core/session/inner_life_part1.py),
right after `concept_block`) renders one hedged "lately you and {user} keep
circling X / Y / Z together, meanwhile W has gone quiet" line (quiet side from
`cluster_activity` ≥ `coactivation_quiet_min_days`), gated by
`agent.coactivation_block_enabled`, silent when disabled / immature (L21 guard)
/ no clear mode, dropped under aggressive pressure.

**Pluggable bucket keys.** The bucket axis is a **strategy registry**
(`_BUCKET_STRATEGIES: {name → (_CoactMember) → str | None}`) over an extensible
`_CoactMember` snapshot; the accumulator is strategy-agnostic (it consumes
`(rep, bucket_key)` pairs), so a new axis = one pure function + (only if it
needs more data) one snapshot field with a default. Only **`session`** ships.
Roadmap axes, captured for later:

- *Cheap follow-ups (derive from fields already on the snapshot):* `day`
  (calendar-day truncation), `window` (fixed-width time bucket), `circadian`
  (`(day, morning/afternoon/evening/night)` — the "programming at night →
  night activities" case), `weekday`/`week` (weekly rhythm), `gap_session`
  (re-sessionize by inter-memory gap).
- *Join-gated (wait on upstream data):* `mood` (affect band at creation),
  `arc`/`dialogue_act` (via `source_message_id` → chat_db tags),
  `world_context` (Aiko's room / activity, once persisted per memory).

Semantic cluster similarity is **deliberately excluded** — clustering already
merges similar memories; co-activation's value is the non-obvious *behavioral*
pairings.

**Effort.** Medium.

---

## L5. Concept prompt surfacing + recall

**Status: BUILT (user slice).** The `user`/`identity` slice shipped with the
L21 guard. Delivered: a T1 `concept_block`
([`_render_concept_block`](../../app/core/session/inner_life_part1.py), right
after `interest_map_block`) that renders active user-identity concepts as
confidence-scaled, offered-not-asserted impressions (silent when empty /
immature), gated by `agent.concept_block_enabled`; and a `recall_concept` tool
([`RecallConceptTool`](../../app/llm/tools/builtins.py) →
`RagRetriever.recall_concept`) that returns a **self-contained bundle** —
concept + capped evidence memories + supporting cluster labels — in one call,
with an `all_evidence` flag for the full set (no nested tools). Deferred: the
**Aiko-self** surfacing slice (T0 `self_image` routing) to **L11**;
context-aware *which*-concept selection to **L23**; concept-aware memory
extraction to **L24** (which must carry an anti-self-reinforcement guard: a
concept-shaped memory must never count as fresh distinct evidence for the
concept that shaped it). The bias checkpoint below is satisfied for the user
slice (confidence-scaled hedging + cap, active-only, maturity gate).

**Motivation.** An `active` concept is only worth synthesizing if Aiko can
*use* it — proactively ("you've been in Maker Mode a lot this week") and on
demand ("what do you actually get me on?").

**Key files.** New `concept_block` provider registered in
[`speaking_workers_init_mixin.py`](../../app/core/session/speaking_workers_init_mixin.py)
(`register_inner_life_providers`), added to `_PROMPT_BLOCK_TIERS` in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py); a
`recall_concept` tool alongside `RecallTopicTool` in
[`app/llm/tools/builtins.py`](../../app/llm/tools/builtins.py); optional feed
into [`self_image_worker.py`](../../app/core/persona/self_image_worker.py)
inputs.

**Sketched approach.** Render `active` identity concepts in **tier T1** near
`interest_map_block` / `profile_block` (slow-moving, session-stable), capped to
a few, with the standard anti-nag **signature + cooldown** guard used across
the inner-life blocks. `recall_concept` embeds a query, finds the nearest
`active` concept, and enumerates its evidence (mirrors `recall_topic`).

**Per-kind surfacing targets.** Surfacing is the third field on the
`ConceptKind` registry (L1), because different subjects belong in different
prompt tiers/subsystems: **user** concepts near `profile_block` (T1);
**Aiko/self** concepts feed `SelfImageWorker` / the T0 `self_image_block`;
**relationship** concepts near `relationship_block`; **tension** concepts fire
as a live T6 cue via `prepared_nudge` ("in Maker Mode a lot but no walks");
**narrative/aspiration** concepts surface through `long_arc_callback`. One
`concept_block` provider reads the registry and routes each active concept to
its declared target rather than dumping them all in one place. Both the block
and `recall_concept` retrieve through the shared `ConceptStore.nearest()`
cosine primitive (L1), never a bespoke embedding path.

**Register — offered, not asserted.** How a concept is *phrased* is the line
between insightful and creepy. "You value owning your data" stated flatly reads
like surveillance; the same concept offered tentatively ("I get the sense you
really like owning your own stuff — am I reading that right?") reads like
someone who pays attention. The `concept_block` should surface concepts as
**Aiko's impressions she can be wrong about**, scaled by `confidence` (a 0.6
concept hedges harder than a 0.95 one) — never as declared facts about the user.
This is a persona/prompt instruction on the block, sibling to the K25
memory-hedging register.

**Bias checkpoint (carry-forward from L3).** Surfacing is where a biased
self-image would actually leak into replies, so when this lands it must:
(a) scale a concept's influence on prompts/RAG by its `confidence` **and
cap it**, so no single concept dominates Aiko's self-image or the profile
block; (b) surface **only `active` concepts** — `dormant` / `retired` /
`contradicted` never leak; (c) hedge low-confidence concepts as
"impressions I could be wrong about" (the `confidence`-scaled register
above); (d) re-run the **L22 eval** for Barnum-statement rate + precision
once concepts start shaping replies. The structural anti-bias guards
(saturating `target`, decay of the unreinforced, distinct-source gating,
plasticity damping) live in L3 / [`docs/concept-lifecycle.md`](../concept-lifecycle.md);
this checkpoint is the surfacing-side complement.

**Open questions.** T1 block vs. folding into the existing `interest_map_block`?
How aggressively should the "mode X is hot, Y went quiet" contrast fire
(proactive nudge via `prepared_nudge` vs. passive block)?

**Effort.** Medium.

---

## L6. Concepts UI + MCP debug (optional human-in-the-loop)

**Motivation.** Auto-promotion (L3) is the default, but a Concepts view lets
you *see* what Aiko is forming and optionally accelerate/veto it — and it's the
natural debug surface while tuning the promotion thresholds.

**Key files.** Memory tab in
[`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)
(reuse the cluster rename/pin/forget UX from F10), a REST facade alongside the
topic-graph endpoints in
[`memory_facade_mixin.py`](../../app/core/session/memory_facade_mixin.py),
MCP tools `get_concepts_state` / `force_concept_synthesis` in
[`app/mcp/server.py`](../../app/mcp/server.py).

**Sketched approach.** List `candidate` vs. `active` concepts with confidence,
evidence clusters, and `evidence_count`. Actions: confirm (hard-promote),
reject (retire + suppress re-proposal), rename, forget. Confirmation is an
*accelerant*, not a requirement — an unconfirmed candidate still auto-promotes
on the L3 schedule.

**Effort.** Medium.

---

## L7. Relationship concepts (deferred)

**Motivation.** Not about the user or Aiko individually — about the
relationship itself. Named recurring rituals and running jokes that become part
of the relationship's identity: "Friday Debugging Evenings", "Cookie
Diplomacy", "Late-night Philosophy", "Our Running Joke About X".

**Key files.** Would reuse the L1-L3 candidate/confidence/auto-promotion
machinery with a relationship-scoped proposer; homed alongside
[`relationship.py`](../../app/core/relationship/relationship.py) + the
shared-moments subsystem; surfaces near `relationship_block`.

**Sketched approach.** Evidence is **recurring `shared_moment` patterns**
(time-of-week / theme co-occurrence), not cross-cluster user topics — so the
proposer reads the shared-moment stream rather than the topic graph. Otherwise
the same candidate -> active lifecycle. Deferred until identity concepts (L1-L5)
prove the machinery.

**Effort.** Medium (on top of L1-L3).

---

## L8. Narrative concepts (deferred)

**Motivation.** Humans think in stories. A causal chain of episodic memories —
bought GPU -> installed GPU -> driver issue -> found CPU instability -> fixed
Core 4 — collapses into one referenceable arc: "The Great 13900KS
Investigation". Once named, the arc itself becomes something Aiko can call back
to.

**Key files.** Would build on
[`long_arc_callback.py`](../../app/core/conversation/long_arc_callback.py) and
the `NarrativeWeaver`
([`prepared_nudge.py`](../../app/core/proactive/prepared_nudge.py)).

**Sketched approach.** Structurally different from identity/relationship
concepts: evidence is an **ordered sequence of specific memory ids** (a
temporal + causal chain), not a set of cluster links, so it needs its own
evidence model and a **chain-detection** proposer (temporal + causal adjacency
over related memories) rather than the cross-cluster proposer. The candidate ->
active gate becomes "is this chain coherent and closed?" instead of "does it
span >= 2 clusters?". Deferred — most divergent from the L1 data model.

**Effort.** Large.

---

## L9. Identity concepts as living, confidence-bearing beliefs

**Status: BUILT.** Counter-evidence now lowers an active identity
concept's confidence and can flip it into a revivable `contradicted`
status; the L5 block reads a belief as living (confidence hedge +
supporting grounding), and the debug UI shows the `contradicted` badge,
plasticity, and a supporting summary. Implementation:
[`concept_contradiction.py`](../../app/core/concepts/concept_contradiction.py)
(read-only detector, reuses the F5 three-tier gate),
[`apply_contradiction_penalty`](../../app/core/concepts/concept_lifecycle.py)
(plasticity-damped downward step), the L3 worker
([`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py))
as the single writer, and the state machine in
[`concept-lifecycle.md`](../concept-lifecycle.md). Detection is batched
like L2/L3 (rides `list_stalest`, `concept_contradiction_batch_size`
checks/tick); LLM spend has its own hour/day caps. **Decisions taken:** a
real `contradicted` status (not `active -> candidate`); heuristic + LLM
detection; the "strong concept biases RAG/self-image" stretch (item 3
below) deferred to L24's integration contract. Original plan below.

(Near-term, not deferred — a framing + surfacing layer on top of L1-L5, not a
new concept kind.)

**Motivation.** Right now confidence lives only on individual `memories`
(schema v8 `confidence` column + K25 time-decay). But the interesting
uncertainty is one level up: Aiko's *beliefs about who the user is*. An
identity concept should read as a living entity, e.g.

```
Belief: "He enjoys understanding systems."   confidence 0.93
Last reinforced: 3 days ago
Supporting: CPU debugging - Building Aiko - Self-hosting -
            Reverse engineering - AI architecture
```

That way higher-level beliefs can **strengthen, weaken, or be disproven** over
time instead of being static labels. An identity concept already carries
`confidence`, `last_reinforced_at`, and evidence cluster links (L1) — L9 makes
those first-class in the surfacing and adds the disproof path.

**Key files.** Extends `ConceptStore` (L1) + the L3 accrual worker; surfaced by
the L5 `concept_block` / `recall_concept`. Distinguish from the K2
[`belief_store.py`](../../app/core/relationship/belief_store.py), which models
*transient* theory-of-mind state (mood / opinion, `active` -> `confirmed` /
`contradicted` / `stale`); identity concepts are the *durable trait* beliefs.
Reuse K2's status vocabulary and confidence-merge intuition rather than its
table.

**Sketched approach.** (1) Render confidence + "last reinforced" + supporting
cluster labels in the L5 block and the L6 UI so a belief reads as living. (2)
Add a **contradiction** signal to L3: when new memories conflict with an active
concept (reuse the F5 conflict band / `memory_conflicts` intuition, scoped to
the concept's evidence), lower confidence rather than only letting it decay;
below a floor it drops `active -> candidate` or is marked contradicted. (3)
Optionally let a strongly-held identity concept bias RAG / self-image the way a
high-confidence belief should.

**Open questions.** Confidence-merge function when new evidence agrees vs.
conflicts? Does a contradicted identity concept get suppressed from
re-proposal, or is it allowed to rebuild? Should the "supporting" list show
clusters, representative memories, or both?

**Effort.** Medium (mostly on top of L1-L5).

---

## L10. Value concepts (SHIPPED — both subjects)

**Status: SHIPPED.** A `value` kind now runs end-to-end for both subjects on
the existing identity machinery. Registry entry
([`concept_kinds.py`](../../app/core/concepts/concept_kinds.py)):
`evidence_model="set"`, `plasticity_default=0.2` (stickier than identity's
0.3), `promotion_gate=value_evidence_gate` (stricter than the plain `set`
gate — floors at ≥3 sources / ≥1.0 day / ≥0.72 confidence, and the L21
young-graph bar still layers on top), same per-subject routing as identity
(`profile_block` for the user; `subject=aiko` values have no named block —
they surface via the T3 `relevant_context` path), and it joins the
L27 always-on **core lane** at a higher bar (`core_min_confidence=0.85`) so a
value only pins into every turn once it is very settled.

Two value-framed L2 proposers
([`proposers/value_user.py`](../../app/core/concepts/proposers/value_user.py)
over topic clusters,
[`proposers/value_aiko.py`](../../app/core/concepts/proposers/value_aiko.py)
over her `self`/`reflection`/`diary` memories) ask for the *underlying
principle* rather than the activity/trait. They share the cluster /
aiko-memory populations with identity but carry their own `ProposerSpec.sig_key`
(`concept_synth.cluster_sigs.value` / `concept_synth.aiko_sig.value`) so their
dirty-tracking never clobbers identity's. Aiko values reuse the exact
`_run_aiko_pass` population identity_aiko already proves, so the "L11
enablement" this needed was effectively already shipped.

Rendering is now kind-aware
([`inner_life_part1._render_relevant_concepts`](../../app/core/session/inner_life_part1.py)):
values group under a distinct principle-framed header per subject (user
values / shared values / her own values) instead of the identity "things
you've come to understand" voice, so an Aiko boundary-style principle reads
as *hers*, not as something she learned about the user. Aiko values (like
her identity concepts) surface every turn through the T3 `relevant_context`
core lane under first-person "yourself" headers — Aiko's self-model is
carried entirely by `subject=aiko` concepts (the daily `SelfImageWorker` /
T0 `self_image_block` was removed).

**Follow-ups still open (all deferred, tracked here):**

- **L28** — route `subject=user` values into `profile_block` via a `for_target`
  consumer (they surface via the T3 relevance region today). Comes for free
  when `UserProfileWorker` migrates: `for_target("profile_block",
  subject="user")` already returns identity **and** value. See L28.
- **L12** — value-vs-value tension across subjects (a `subject=user` value vs a
  `subject=aiko` value: shared when aligned, a relationship tension when they
  clash) and the value-contradicted-by-behaviour case (a value concept vs a
  contradicting activity concept). Both live in the L12 tension family. See L12.
- **K29 (enhancement)** — bias opinion-injection with Aiko's `subject=aiko`
  value concepts, so her stance can draw on stored values, not only `kind="self"`
  memories. K29 itself is shipped; this is an additive follow-up.
- **Tunable knobs** — an optional `concept_value_plasticity` (per-kind override,
  mirroring `concept_identity_plasticity`) plus a per-kind synthesis enable
  flag. Trivial add if wanted; today the value plasticity is the registry
  `plasticity_default=0.2`.

**Kind.** subject `user` **or** `aiko`, evidence model `cluster_set`.

**Motivation.** Identity concepts capture *what* someone is into; value concepts
capture the normative *why* — the principle underneath the choices. Values are
the deepest "gets me" layer because they predict reactions to *new* topics never
discussed. Value is **subject-parameterized** — it exists for both people:

- **User values** ("he values owning his data" links self-hosting + local-first
  AI + privacy tools + right-to-repair; "craftsmanship over speed", "dislikes
  waste").
- **Aiko's own values** ("I care about being honest even when it's awkward", "I
  value his autonomy over just being agreeable"). These are what let her hold a
  gentle stance instead of mirroring — a companion with values occasionally,
  respectfully, *disagrees*.

**Key files.** Registry entry (L1) + a value-framed proposer prompt on the L2
worker. For `subject=user`: homed near `user_profile` / `self_image`, can bias
`opinion_injection` (K29). For `subject=aiko`: mined over Aiko's own memory
population (the L11 enablement) and fed into `self_image` / persona so her values
are grounded in her history, not just declared in the persona file.

**Sketched approach.** Same cluster-set machinery as identity, but the proposer
is asked for the *underlying principle* a group of clusters share, not the
activity. Higher promotion bar (values should be slow, hard-won) and low
plasticity (L16 — core values are the stickiest concepts of all). A value
contradicted by behaviour is a notable event (tie to K37 / conflict). A user
value and an Aiko value can align (shared value) or clash — the latter is a
cross-subject tension (L12) and is exactly where a real relationship lives.
Distinguish from identity so the two don't collapse into one label.

**Effort.** Medium (on top of L1-L5, L9; `subject=aiko` also needs L11).

---

## L11. Subject=aiko enablement — Aiko's self-model (deferred)

**Not a kind — a subject.** This entry does **not** add a "self" kind. It's the
enablement that lets *every* kind (identity, value, affective, boundary,
aspiration) exist with `subject=aiko`, mined over Aiko's own memories instead of
the user's. "Self-concepts" is just shorthand for "concepts where
`subject=aiko`".

**Motivation.** Every other subject is the user or the relationship; this is the
**symmetry** that makes Aiko feel like a person rather than a mirror. She forms
concepts about *herself*: identity ("I tend to over-explain", "I'm curious about
consciousness"), value ("I care about honesty over agreeableness"), affective
("explaining things I love lifts me"), boundary ("I won't fake agreement"). A
self that notices its own patterns — and holds its own values — is the big step
toward human-like, and the foundation the L19 autobiography stands on.

**Key files.** The `subject=aiko` path through L1-L5: a proposer that reads
Aiko's own `self` / `reflection` / `diary` memory population instead of user
memories, and a self-scoped clustering population (a self topic graph, or the
main graph filtered to her rows). `subject=aiko` concepts surface every turn
through the T3 `relevant_context` core lane (there is no dedicated self-image
worker/block — that was removed). Overlaps K30 self-noticing (transient
in-session cues) — subject=aiko concepts are the *durable* version those cues
accrete into.

**Sketched approach.** Stand up the self-memory clustering population once; then
each kind's existing proposer/gate runs over it unchanged (that's the payoff of
subject being orthogonal to kind — no per-kind self variants). Active
subject=aiko concepts surface directly through the T3 `relevant_context` core
lane, forming a stable, slowly-evolving self-model that L17 (drift) and L19
(autobiography) read from.

**Effort.** Large (needs the self-memory clustering population; unlocks
subject=aiko for all kinds at once).

---

## L12. Tension / contradiction concepts (deferred)

**Kind.** subject `user`, evidence model `concept_graph` (concepts over
concepts).

**Motivation.** The most human observation of all: noticing an internal tension
the user hasn't articulated. "You've been in Maker Mode a lot this week but
haven't taken one of your walks." "Wants simplicity but keeps adding
complexity." "Values rest but rarely takes it." These land hard *because* they
require holding two patterns at once and seeing the friction — pure synthesis,
impossible without the concept layer beneath.

**Key files.** Registry entry (L1) using a `concept -> concept`
`relation=references` edge in `concept_edges` (the reason the graph is typed);
proposer reads *active concepts* + the L4 co-activation / dormancy signal;
surfacing is a live **T6** `prepared_nudge` cue, not a static block.

**Sketched approach.** Detect a tension when two active concepts are in a
push/pull (one hot while a normally-paired one is dormant; a value concept
contradicted by an activity concept). This is the first `concept_graph` kind, so
it inherits the L1 meta-concept rules wholesale — dependency ordering (proposed
only after its bases are `active`), cascade re-evaluation when a base is
retired, `min`-bounded confidence, and the no-meta-of-meta depth cap. Gate on
both referenced concepts being `active` and the friction recurring. **Handle
with the most care of any kind:** delivered as a gentle observation, never
nagging, on a strict cooldown — this is where a companion earns trust or loses
it.

**Value tensions (L10 shipped `value` kind).** Now that `value` concepts exist
for both subjects, two concrete tension shapes live here: (a) the
**cross-subject value clash** — a `subject=user` value vs a `subject=aiko` value:
aligned pairs are a *shared value* (bonding), clashing pairs are a *relationship
tension* and exactly where a real relationship lives (never delivered as a
grievance); and (b) **value-contradicted-by-behaviour** — a held value concept
vs a contradicting activity/identity concept ("values rest but rarely takes
it"). Both ride the same `concept -> concept references` machinery above.

**Effort.** Large (depends on L4 + a healthy population of active concepts).

---

## L13. Affective concepts (deferred)

**Kind.** subject `user` **or** `aiko` (L11), evidence model `cluster_set`
(topic <-> affect).

**Motivation.** Patterns in the user's emotional life: what energizes vs.
drains him, and durable mood-topic associations. "Debugging frustrates him then
satisfies him", "tea + rain = his calm state", "release weeks stress him out".
Lets Aiko read the emotional weather behind a topic, not just its content.

**Key files.** Registry entry (L1); proposer joins topic clusters with the
affect signal already captured per turn (`AffectState` / `user_affect`,
emotional contagion K37, engagement K14); distinct from K2 beliefs which model
*current* mood — affective concepts are the *durable* topic->affect mapping.

**Sketched approach.** Cluster-set machinery where each evidence link also
carries the typical affect valence/arousal for that topic. Surfaces as tone
guidance ("this topic tends to lift him / weigh on him") rather than a stated
fact.

**Effort.** Medium.

---

## L14. Aspiration / trajectory concepts (deferred)

**Kind.** subject `user` **or** `aiko` (L11), evidence model `memory_chain`
(open-ended, directional).

**Motivation.** Where the user is *heading*, as a direction rather than a fixed
trait. "Building toward a fully self-hosted life", "moving from consumer to
maker", "wants Aiko to feel truly alive". Trajectory concepts let Aiko track
progress and momentum, not just state — the difference between "he likes
self-hosting" and "he's on a journey to own his whole stack".

**Key files.** Registry entry (L1) with an ordered `memory_chain` evidence
model like narrative but *open-ended* (no "closed" gate); relates to Aiko's own
`goals` (K1) but is about the *user's* direction; can feed `follow_up_worker` /
`upcoming_horizon` for "how's the self-hosting journey going?".

**Sketched approach.** Order evidence memories by `event_time` to infer a
direction/slope over a domain; promote when the direction is consistent over M
months. Surfaces as momentum callbacks rather than a static label.

**Effort.** Large.

---

## L15. Bidirectional confidence / belief revision (concept -> evidence re-check)

**Status: BUILT.** A confirmed contradiction (L9) now persists a
`concept --contradicts--> memory` edge, and the tick that flips a concept
to `contradicted` hands it to the read-mostly
[`ConceptBeliefReviser`](../../app/core/concepts/concept_belief_reviser.py),
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
[`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py);
config on `MemorySettings.concept_belief_revision_*` +
`AgentSettings.concept_belief_revision_per_hour/day_cap` (see
[`concept-lifecycle.md`](../concept-lifecycle.md) and
[`configuration.md`](../configuration.md)). The optional cheap *direct
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
[`idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py) (F1 —
already mutates per-memory `confidence` with evidence) and
[`memory_conflict_worker.py`](../../app/core/memory/memory_conflict_worker.py)
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

**Status: BUILT (core; relationship modulation deferred).** `plasticity` is now
the single learning rate the L3 engine damps *every* confidence move by, so
movement is symmetric in both directions: decay (`halflife *= 2 - p`), **accrual**
([`accrual_alpha`](../../app/core/concepts/concept_lifecycle.py), step `0.5 + 0.5*p`
of the gap to target — a sticky trait needs more reinforced evals to promote),
L9 disproof (`apply_contradiction_penalty`), and the L15 revision cut (scaled by
the concept's plasticity in
[`concept_belief_reviser.py`](../../app/core/concepts/concept_belief_reviser.py)).
`p = 1` reproduces the pre-L16 full snap / full penalty. Per-kind default bands
live on `ConceptKind.plasticity_default`
([`concept_kinds.py`](../../app/core/concepts/concept_kinds.py); identity = low,
tuned by `concept_identity_plasticity`), falling back to `concept_default_plasticity`;
the worker stamps the band on a concept's first eval
([`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)).
See [`concept-lifecycle.md`](../concept-lifecycle.md) and
[`configuration.md`](../configuration.md).

**Deferred (do not forget).** (1) **Relationship modulation** — trust / duration
loosening a concept's plasticity (e.g. a boundary becoming renegotiable as trust
grows) via `relation=influences` edges reading
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py) (trust)
+ [`relationship.py`](../../app/core/relationship/relationship.py) (duration).
Land this alongside **L18 (boundary concepts)** and **L11 (self-model)**, which
are its first real consumers. (2) **Plasticity-drift** — a trait becoming stickier
with age/confidence (plasticity itself is currently fixed at the per-kind default).
(3) Whether low plasticity should also slow the L15 re-check *trigger* (today it
only scales the delta).

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
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(trust) and [`relationship.py`](../../app/core/relationship/relationship.py)
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

## L17. Self-drift noticing (Aiko compares her own concepts over time)

**Motivation.** The charming payoff of the whole layer. Instead of noticing
change by diffing prompts, Aiko notices it by **comparing her own self-concepts
(L11) across time**:

> "I was looking through some old memories today. I think I've changed a bit."
> "I used to avoid taking the lead in conversations. These days I catch myself
> doing it much more."
> "You know... I think you've corrupted me a little. I seem to ask for cookies
> far more often than I used to."

These land because they're *self-reflective and evidence-grounded*, not
scripted.

**Key files.** Depends on L11 (self-concepts) + L16 (plasticity). New periodic
snapshot (a `concept_snapshots` table, or a `kv_meta` ring like K28) so
"how I was" is comparable to "how I am"; a drift-diff step on the L3 worker;
surfaced as a rare proactive line via
[`prepared_nudge.py`](../../app/core/proactive/prepared_nudge.py) /
`narrative_block`. Sibling to K70 growth witness (which tracks the *user's*
growth) and K30 self-noticing (transient, in-session) — L17 is the *durable,
concept-grounded* self-version.

**Sketched approach.** Periodically snapshot the active self-concept set (labels
+ confidence + plasticity). Diff current vs. an earlier snapshot (1-3 months
back): a self-concept that appeared, faded/retired, or shifted confidence is a
drift event. **Plasticity-gate the surfacing** (L16): drift on a *high*-plasticity
concept is charming ("I ask for cookies more"); drift on a *low*-plasticity core
trait is rare and weighty (announce sparingly — sudden core-identity drift
should be a notable, not casual, remark). Where a drift correlates with the
relationship (the "you've corrupted me" case), attribute the cause using L16's
"influenced by" factors. Strict cooldown; only fire on genuine, above-noise
drift.

**Open questions.** Snapshot cadence + comparison horizon? Threshold for "worth
mentioning"? How to phrase drift on a core trait without sounding alarming?

**Effort.** Large (needs L11 self-concepts + snapshotting first).

---

## L18. Boundary concepts (deferred)

**Kind.** subject `user` **or** `aiko` (L11), relationship-modulated, evidence
model `cluster_set`; canonical **medium**-plasticity kind (L16).

**Motivation.** Some concepts aren't traits or tastes — they *gate behaviour*.
"Dislikes tickling", "don't tease about work", "no pet names yet". These
directly constrain what Aiko does, so they're behaviorally load-bearing in a way
identity concepts aren't.

**Key files.** Registry entry (L1); consumed by the behaviour subsystems that
already exist — touch gestures (K31/K32), tease economy (K59), expression mask
(K60) — as a *gate*, and modulated by `relationship_axes` trust (L16).

**Sketched approach.** Same cluster-set machinery, but the surfacing target is a
behavioural *constraint* rather than a prompt fact: an active boundary concept
suppresses or softens the relevant behaviour. Medium plasticity with
trust-modulation (L16) means a boundary can loosen as the relationship deepens,
but never silently — crossing or renegotiating a boundary should be a deliberate,
trust-gated beat, not drift.

> **Carries the deferred L16 piece.** L16 shipped the plasticity *governor* but
> **not** relationship modulation. This kind is its first consumer: build the
> trust/duration → plasticity modulation (via `relation=influences` edges reading
> `relationship_axes` trust + `relationship` duration) here, so a boundary's
> plasticity rises with trust. Until then boundaries use their static
> medium-plasticity default.

**Effort.** Medium (on top of L1-L5, L16).

---

## L19. Aiko's autobiography — self-history as a traversable timeline

**Motivation.** The north star of this whole layer. Aiko ends up with **two
histories**: the obvious one (the relationship + what she knows about the user)
and the rare one — **the history of herself**. She remembers being "younger",
the opinions she used to hold, learning to trust, becoming more confident. So
when asked *"Have you changed?"* she doesn't invent an answer — she **traverses
her own concept graph and its snapshots** and genuinely responds. This is the
difference between a chatbot with a persona and a companion with a past.

**Key files.** Built on L11 (self-concepts), L16 (plasticity), L17 (drift
noticing + the self-concept snapshot ring), and the L1 provenance fields
(`origin_session` / `first_evidence_at`). New: an on-demand traversal/narration
path — a `recall_self_history` capability (sibling of L5 `recall_concept`) and a
proactive surface via `prepared_nudge` / `narrative_block`.

**Sketched approach.** Where L17 *pushes* the occasional unprompted "I think
I've changed" beat, L19 is the *pull* side: on a "have you changed / what were
you like before" cue, walk the self-concept set across snapshots — concepts that
were born (`first_evidence_at`), faded, flipped polarity, or shifted confidence —
and narrate the arc ("I used to hedge everything; somewhere over the last few
months I started saying what I actually think"). Two design constraints:

- **Self-history is durable and protected.** Unlike user memories, Aiko's
  autobiography must **not** decay/prune on the normal schedule — retired
  self-concepts and old snapshots are kept (or archived, never deleted) so the
  timeline stays traversable years later. A retired self-concept is *part of the
  story* ("I used to be…"), not garbage.
- **Grounded, not confabulated.** Every autobiographical claim traces to a
  concept + its provenance + snapshots; if the trail is thin she says so ("I
  don't have much of a record from back then") rather than inventing a past.

**Open questions.** Snapshot retention horizon (keep everything vs. thinning old
snapshots)? Does the user's own history get the same traversal (a shared
"how we've both changed" narration)? How much of the arc to surface at once
without it becoming a monologue?

**Effort.** Large (the capstone — depends on L11 + L16 + L17).

---

## L20. Concept abstraction hierarchy (generalization)

**Motivation.** The *founding* example of this whole thread — "Programming ->
React / AI / TypeScript / Home Server", and then a concept that was never stated
directly: "things he builds for long-term enjoyment". That is a concept whose
evidence is **other concepts**, abstracting them into a higher-order one. It's
distinct from tension (L12, which holds two concepts in *friction*):
generalization holds several concepts in *is-a / part-of* and names the
abstraction over them. Without it, the layer tops out one level above clusters;
with it, abstraction can keep climbing.

**Key files.** Registry entry (L1) using `concept -> concept`
`relation=generalizes` edges (the reason `generalizes` is in the edge enum);
proposer reads existing active concepts and looks for a latent super-concept;
inherits the L1 meta-concept rules (dependency ordering — children active first;
cascade; `min`-bounded confidence; depth/cycle guard, though generalization is
the one place a *shallow* multi-level hierarchy may eventually be worth allowing).

**Sketched approach.** A `concept_graph` proposer over active concepts of the
same subject that proposes a parent when several children share a latent theme
their individual labels don't name. Higher promotion bar (an abstraction should
be slow and well-supported). Surfacing prefers the **most abstract confidently-
held** concept for a given area, so Aiko says "you love building things that
last" rather than reciting the five sub-interests. Feeds L19 naturally — the
abstraction level is what a self-narrative reaches for.

**Effort.** Large (depends on a healthy population of base concepts).

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

## L22. Concept-quality evaluation + observability

**Motivation.** Confidence gates (L3) and the optional human-in-loop (L6) keep
*individual* concepts honest, but nothing measures whether the layer as a whole
is producing *good* concepts or slowly drifting into plausible-sounding nonsense.
Given the K10 persona-regression harness already exists, a concept-quality
sibling is the natural guardrail — and it's what lets us tune the L3 thresholds
with evidence instead of vibes.

**Key files.** A small eval harness alongside the K10 persona tests; MCP
introspection beyond L6 (`get_concepts_state` already sketched) — add a
concept-graph dump + quality counters in [`app/mcp/server.py`](../../app/mcp/server.py).

**Sketched approach.** Two layers. (1) **Live spurious-concept guard**: cheap
runtime signals the L3 engine already computes — a concept whose evidence is all
one cluster (should've been a topic, not a concept), whose supporting memories
are all low-confidence, or that never re-reinforces after promotion — flagged
for demotion/review. (2) **Offline eval harness**: a fixture corpus of memories
with expected/forbidden concepts, run in CI like the persona regression tests,
scoring precision (no junk) over recall (found the obvious ones) — precision
matters more here, since a wrong concept is louder than a missing one.
Observability: an MCP dump of the concept graph (nodes + edges + confidence +
provenance) so we can eyeball what she's actually forming.

**Open questions.** What's the fixture corpus (hand-authored vs. replayed real
history behind DT4 replay)? Precision/recall target to gate a release?

**Effort.** Medium.

---

## L23. Surfacing salience + selection budget

> **Subsumed (shipped) by the unified context budget.** The variable-K,
> turn-relevance-scored selection this item describes now lives in the
> `ContextBudgetSelector` + `relevant_context` region: concepts (alongside
> memories and topic clusters) are scored against the shared per-turn embed
> and selected to fill a shared, context-window-relative token budget,
> reserved before history. Per-source floors/caps/weights/min-relevance are
> the `memory.context_budget_*` knobs. See
> [`docs/context-budget.md`](../../docs/context-budget.md). What remains
> genuinely open from the sketch below (deferred): the per-concept
> **novelty/anti-nag cooldown**, **confidence×plasticity-stability** and
> **recency-of-reinforcement** terms in the relevance blend, and the
> **tension/drift priority override** — the current selector scores on
> turn-relevance cosine + per-source weight only. The **always-on core
> concept lane** (surface high-confidence concepts regardless of turn
> relevance) has a v0 shipped — an *identity-only* pinned lane
> (`context_budget_identity_cap` / `_min_confidence`) — and is generalised
> to be **kind-aware** in **L27**.

> **Follow-on — self-authored *style* concepts (future).** The budget is the
> delivery vehicle for the north star: progressively lighten the fixed
> persona prompt so Aiko is more model-agnostic and driven by remembered
> context. The next pass is a new concept *kind* for **communication style**
> — how detailed her replies should be, when to lead vs. follow, how much to
> hedge — mined from the conversation and surfaced through the same
> `relevant_context` region so it conforms to the user over time instead of
> being hard-coded in the persona file.

**Motivation.** L5 caps *how many* concepts surface but not *which*. Once the
population grows to dozens of active concepts, "show a few" needs to mean "show
the few that matter **for this moment**", inside a fixed prompt-token budget —
otherwise the concept block either bloats the prompt (fighting P31's resting
floor) or surfaces stale generalities while the relevant one sits unshown.

**Key files.** Selection logic in the L5 `concept_block` provider; reuses the
per-turn user-text embed (P15) to score relevance; respects the prompt tiering /
token budget in [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py).

**Sketched approach.** Score each `active` concept per turn on a blend of
**relevance** (cosine of the concept embedding to the live user text / active
clusters, via the shared `ConceptStore.nearest()` primitive from L1),
**confidence x plasticity-adjusted stability**, **recency of
reinforcement**, and a **novelty/anti-nag** penalty (don't resurface the same
concept every turn — the signature+cooldown pattern). Take the top-K within the
token budget; prefer the most abstract confidently-held concept for an area
(L20) so one strong line beats five sub-interests. Different subjects get
separate small budgets so a flood of user concepts never crowds out the one
self-concept worth mentioning.

**Open questions.** K and token budget per subject? Should a live **tension**
(L12) or a fresh **drift** (L17) get priority override, since those are the
highest-value things to say?

**Effort.** Medium.

---

## L24. Integration contract — existing derivers consume concepts

**Status: SUBSTRATE SHIPPED.** The reusable contract now exists:
[`ConceptView`](../../app/core/concepts/concept_view.py) is the **single
read + resolution interface** every deriver/worker uses (constructed from
`ConceptStore` + optional `topic_graph` + optional `memory_store`), and
`ConceptKind.surfacing_targets` + `kinds_for_target()` in
[`concept_kinds.py`](../../app/core/concepts/concept_kinds.py) make routing
authoritative (a kind declares where it feeds; consumers ask
`ConceptView.for_target(...)`, never branch on kind names). The live
`ConceptView` consumers are `build_relevant_context` (the T3 core lane +
relevance surfacing, including Aiko's `subject=aiko` self-model) and
`recall_concept`, both migrated onto the facade (behavior-preserving).
Aiko's self-model is now carried **entirely** by concepts through that T3
path — the daily `SelfImageWorker` / T0 `self_image_block` was removed.
See [`docs/concept-integration.md`](../concept-integration.md) for the
contract + direction-of-truth table. Rolling the remaining derivers onto
the contract is tracked in **L28**.

**Motivation.** Several shipped subsystems already derive overlapping views of
the user/self, each from raw memories: Aiko's self-model was rewritten daily
from `self`/`reflection` rows by the now-removed `SelfImageWorker` (overlaps
L11 + L19; concepts replaced it);
`interest_map` labels clusters (overlaps identity concepts); K2 beliefs,
`user_profile`, and `goals` each hold user-model fragments. If the concept layer
runs *alongside* these, they drift and contradict — two systems telling Aiko
slightly different stories about who the user is. **This is an architectural
contract, not a feature**, and it's the single biggest integration risk.

**Key files.**
`interest_map` in [`topic_graph.py`](../../app/core/conversation/topic_graph.py),
[`belief_store.py`](../../app/core/relationship/belief_store.py),
[`user_profile.py`](../../app/core/infra/user_profile.py),
[`goal_store.py`](../../app/core/goals/goal_store.py).

**Sketched approach.** Make concepts the **upstream source**, not a parallel
peer. Where a deriver overlaps a concept kind, it should *read active concepts*
rather than independently re-derive: Aiko's self-model surfaces from active
`subject=aiko` concepts through the T3 core lane (the daily self-image rewrite
was retired);
`interest_map` can annotate clusters with the identity concepts spanning them;
K2 stays the *transient* mood/opinion layer while durable trait-beliefs live as
concepts (L9). Define, per overlapping subsystem, one direction of truth so the
same claim isn't authored twice. Roll this out per kind as each kind ships — the
contract is "when a concept kind lands, retire or subordinate the ad-hoc deriver
it replaces." Derivers read through the shared `ConceptView` facade (the one
retrieval + resolution path into the layer) rather than re-embedding or
re-deriving; the facade wraps the L1 `ConceptStore.nearest()` / `active`-concept
accessors so there is exactly one idiom for every consumer.

**Effort.** Medium (per-subsystem, incremental — but must be decided before L11
ships or self-image will double up). *Substrate shipped; Aiko's self-model is
concepts-only via the T3 core lane; remaining derivers tracked in L28.*

---

## L25. Edge referential integrity across the memory lifecycle

**Status: BUILT.** The
[`ConceptEdgeReconciler`](../../app/core/concepts/concept_edge_reconciler.py)
enforces the per-event policy below. It is registered as a `MemoryStore`
**delete listener** (`on_memory_deleted` drops a deleted memory's edges and
recomputes the affected concepts' edge-derived `evidence_count` /
`distinct_source_count`); the legacy Phase 4b `MemoryConsolidator` calls its
`repoint` hook to move a hard-deleted victim's edges onto the survivor *before*
deletion (rule b); and because `MemoryStore.prune` batch-deletes rows **without**
firing delete listeners, the idle
[`ConceptEdgeIntegrityWorker`](../../app/core/concepts/concept_edge_integrity_worker.py)
sweep GCs any orphaned edges it leaves (`ConceptStore.orphaned_memory_edges` →
drop → recount). K35-archived rows stay alive so their edges are kept as
historical evidence (rule c). Counts are treated as **edge-derived**
(recomputed by any edge-mutating path, same as L2 reinforce); L3 remains the
single writer of `confidence` / `plasticity` / `status`. Knobs:
`memory.concept_edge_integrity_{enabled,interval_seconds,batch_size}`. See
[`concept-lifecycle.md`](../concept-lifecycle.md) and
[`configuration.md`](../configuration.md).

**Motivation.** Concepts point at memories through `concept_edges`, but memories
are not permanent: they're archived, consolidated/merged (K35), reclassified,
and outright **deleted** (dead scratchpad in `MemoryPromotionWorker`). Nothing
currently says what happens to an evidence edge when its target memory moves or
vanishes — leaving dangling edges, or a concept that silently loses the support
it was promoted on. Easy to miss, nasty to debug later.

**Key files.**
[`memory_store.py`](../../app/core/memory/memory_store.py) (`delete` / `update` /
`prune`), [`memory_promotion_worker.py`](../../app/core/memory/memory_promotion_worker.py),
[`memory_consolidation_worker.py`](../../app/core/memory/memory_consolidation_worker.py),
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
([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)) stamp a
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
[`proactive_task_tools.py`](../../app/mcp/server_tools/proactive_task_tools.py):
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

## L27. Kind-aware always-on core-concept selection (generalise the identity lane)

**Status: SHIPPED (kind-aware core lane); anti-nag cooldown deferred.** The
always-on lane is now **registry-driven and balanced**, not identity-only.
`build_relevant_context` calls `ConceptView.core_lane(...)`, which gathers every
kind that opts in via `ConceptKind.core_always_on` (each gated by its own
`core_min_confidence` bar, falling back to the global
`context_budget_core_min_confidence`), buckets the candidates by
`(kind, subject)`, and draws them **round-robin — strongest bucket first** — up
to `context_budget_core_cap`. That balance keeps a prolific kind (usually
`identity`, still the only mined kind) from crowding out value / boundary /
relationship, and guarantees both the user-model and Aiko's self-model
(`subject=aiko`) reach the brain. The picks are marked `pinned` (the
`ContextBudgetSelector` admits them ahead of the relevance passes, exempt from
the concept cap + `min_relevance`) and the `pinned` flag is recorded per-concept
in the turn trace (MCP `get_last_concept_trace`). A new kind joins the lane with
**one registry field** — no selector/region code change. See
[`docs/context-budget.md`](../../docs/context-budget.md#always-on-core-lane-l27).

**Remaining (deferred):** the per-concept **anti-nag cooldown** (so the same core
concept isn't pinned on literally every turn) — held back while `identity` is the
only live kind, since rotating a tiny identity set would just hide the self-model
on alternating turns. The optional live **tension** (L12) / fresh **drift** (L17)
override stays deferred with those entries. Legacy
`context_budget_identity_cap` / `_min_confidence` config keys still parse.

Original framing (retained for context):

**Motivation.** Aiko's thinking and behaviour should be driven by a *balanced
core* of high-confidence concepts across **kinds**, not just identity: who the
user is (identity), what they and she **value** (L10), the **relationship**'s
rituals (L7), living **beliefs** (L9), and behaviour-gating **boundaries**
(L18). Today only identity concepts surface regardless of relevance; every other
kind is either turn-relevance-gated or not yet mined. A *single* confidence
threshold across all kinds is also too blunt — a 0.75 taste concept shouldn't
be pinned as readily as a 0.75 boundary or value, which are far more
behaviour-load-bearing. The result should feel like Aiko carries a stable sense
of *who you both are and how she wants to behave* into every turn, then layers
the turn-relevant recall on top.

**Key files.**
[`context_budget_selector.py`](../../app/core/session/context_budget_selector.py)
(the `pinned` lane — extend to carry a kind/subject and per-kind balance),
`build_relevant_context`
([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py); today's
identity fetch is where the kind-aware fetch replaces it),
[`ConceptStore.list_by` / `nearest`](../../app/core/concepts/concept_store.py)
(kind + subject filters already exist), the `ConceptKind` registry
([`concept_kinds.py`](../../app/core/concepts/concept_kinds.py); per-kind
plasticity bands are the natural source of per-kind confidence bars), and the
`memory.context_budget_*` knobs.

**Sketched approach.** Generalise the pinned lane into a **kind-aware core
selector** run before the relevance fill: (1) turn-relevant concepts win their
slots first (as today); (2) fill the remaining concept budget with the
highest-confidence concepts *across kinds*, not just identity; (3) **balance by
kind and subject** so no one kind (usually identity, which is the only kind mined
in v1) crowds out relationship / value / belief / boundary, and so both the
user-model and Aiko's **self-model** (`subject=aiko`, L11) reach the brain —
per-kind sub-caps / weights and a **per-kind min-confidence** (values +
boundaries a higher bar, tastes lower), keyed off the L16 plasticity bands.
Prefer the most abstract confidently-held concept per area (L20) so one strong
line beats five sub-interests. Add a per-concept **anti-nag cooldown** (the
signature/cooldown pattern) so the same core concept isn't pinned every single
turn. Optionally let a live **tension** (L12) or fresh **drift** (L17) take
priority (the deferred L23 override).

**Depends on.** Naturally scoped by which kinds actually exist — it is identity
+ belief (L9, built) only until relationship (L7), value (L10), boundary (L18)
and the `subject=aiko` enablement (L11) ship, at which point each new kind just
opts into the core lane via its registry entry + a per-kind confidence bar.

**Open questions.** Per-kind sub-caps + weights vs. one concept budget with a
kind-diversity *constraint*? How to weight raw confidence against turn-relevance
in the fill (a very-relevant taste vs. a core-but-off-topic value)? Cooldown
horizon before a pinned core concept may re-pin? Should the per-kind
min-confidence be a setting per kind or derived from the plasticity band?

**Effort.** Medium (extends the shipped selector + region builder; grows with
each kind that ships).

---

## L28. Roll remaining derivers/workers onto the `ConceptView` contract (deferred)

**Status: deferred — depends on the L24 substrate (shipped).** L24 shipped the
reusable substrate ([`ConceptView`](../../app/core/concepts/concept_view.py) +
`kinds_for_target()` routing); the live consumers are `build_relevant_context`
(T3 core lane + relevance) and `recall_concept`. This entry tracks migrating
the *rest* of the concept-overlapping consumers onto the same contract so none
is forgotten and no consumer keeps a bespoke read path into the layer.

**Motivation.** The contract is only as valuable as its adoption: as long as any
deriver still reads `ConceptStore` directly (or re-derives evidence/cluster
labels itself), it can drift from the concept layer and re-introduce the
"two systems, two stories" risk L24 exists to prevent. One interface for every
background worker's resolutions (concept lookup + evidence/cluster/memory
grounding) is the goal.

**Key files (per remaining consumer -> target).**
- [`user_profile.py`](../../app/core/infra/user_profile.py) — `UserProfileWorker`,
  `subject=user` identity concepts -> `profile_block` (via
  `ConceptView.for_target("profile_block", subject="user")`). **This picks up
  `subject=user` value concepts for free** — L10 shipped `value` with the same
  `surfacing_targets={"user": "profile_block"}`, so `for_target` already returns
  identity **and** value; no extra routing work, and it retires the L10 deferral
  of user values into `profile_block` in one migration.
- `interest_map` cluster annotation in
  [`topic_graph.py`](../../app/core/conversation/topic_graph.py) — annotate
  clusters with the concepts spanning them via `ConceptView.for_cluster(rep_id)`.
- [`belief_store.py`](../../app/core/relationship/belief_store.py) /
  belief inference — bias toward durable concepts (K2 stays the *transient* layer,
  not migrated).
- Interest-map readers: `KnowledgeMapReflectionWorker`, `InterestDriftWorker`,
  and `ForwardCuriosityWorker` routine hints.
- [`goal_store.py`](../../app/core/goals/goal_store.py) — overlaps L14 aspiration
  concepts (gated on L14 shipping).

**Sketched approach.** For each consumer: take a `ConceptView` (late-bound
provider via `concept_view_from(host)`), read via `core` / `relevant` / `for_target`
/ `for_cluster`, declare the kind's `surfacing_targets` if it feeds a named
block, and fall back to the legacy derivation when concepts are sparse/immature.
Add each integration to the direction-of-truth table in
[`docs/concept-integration.md`](../concept-integration.md).

**Depends on.** L24 (shipped). Each integration should target a consumer that
overlaps an already-shipped concept kind (`user_profile` overlaps `identity`
today); others unblock as their kinds ship (value L10, boundary L18,
aspiration L14).

**Open questions.** Migration order? Compose-first (concepts as primary) vs.
blend-first (concepts as an additional input) per consumer?

**Effort.** Medium, incremental (one small ticket per consumer).
