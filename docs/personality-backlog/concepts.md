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
- **Narrative** (user **or** aiko, memory-chain) — "The Great 13900KS
  Investigation"; or Aiko's own "the stretch where I learned to hold a gentle
  stance". The first ordered/`sequence`-evidence kind. See L8 (SHIPPED). The
  "both of us" / meta variants are spun out to L29.
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
  "I want to be someone he can rely on"). The second `sequence`-evidence kind,
  the open-ended sibling of L8. See L14 (SHIPPED — both subjects + momentum
  callbacks).
- **Boundary** (user **or** aiko, `set`; behaviour-gating) — soft, guiding
  constraints that gate behaviour ("go gentler about his work"; or Aiko's own "I
  won't fake agreement just to please him"); the canonical medium-plasticity
  kind, mined from clusters + explicit remembered anchors. See L18 (SHIPPED —
  both subjects + anchor sourcing + composite surfacing).

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
*grouping*. [`ritual_grouping`](../../app/core/concepts/ritual_grouping.py) is a
pure single-link cosine clustering over the moment embeddings (two moments join
when their cosine clears a threshold; a group is the connected component). Each
surviving component (`>= group_min_size`) is annotated with a dominant `vibe`
and an optional weekday hint parsed from `when`, plus trimmed member
`MomentLite`s. No store / settings / LLM imports (numpy only), so it unit-tests
in isolation; the worker builds its inputs via `moment_from_memory`.

**Synthesis.** A new `"shared_moments"` population + `_run_ritual_pass` in
[`concept_synthesis_worker`](../../app/core/concepts/concept_synthesis_worker.py):
it enumerates `iter_by_kind("shared_moment")`, skips below a min-moments floor,
groups + caps them, and offers each group to the
[`relationship_ritual`](../../app/core/concepts/proposers/relationship_ritual.py)
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
[`concept_synthesis_worker`](../../app/core/concepts/concept_synthesis_worker.py):
for each subject-dominant topic cluster it loads the member memories via
`get_many` and orders them by `event_time` (falling back to `created_at`),
offering up to `max_narrative_clusters_per_run` clusters (per subject) as
[`NarrativeCandidate`](../../app/core/concepts/proposers/base.py)s (each capped
at `max_narrative_memories` steps, min `narrative_min_chain` to count as a
story). The shared [`propose_narrative`](../../app/core/concepts/proposers/base.py)
body (used by [`narrative_user`](../../app/core/concepts/proposers/narrative_user.py)
/ [`narrative_aiko`](../../app/core/concepts/proposers/narrative_aiko.py)) names
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
dirty-tracking never clobbers identity's. Aiko values run through the same
combined `_run_aiko_pass` as identity_aiko (self-themes + self-memories,
mixed evidence), so they inherit the shipped L11 self-model path for free.

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

- **L28 (SHIPPED)** — `subject=user` values now lead `profile_block` alongside
  identity: `_render_user_profile_block` reads `for_target("profile_block",
  subject="user")` (which returns identity **and** value) and suppresses the
  SQLite `values` field when a value concept exists. They still also surface via
  the T3 relevance region. See L28.
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
worker. For `subject=user`: homed near `user_profile`, can bias
`opinion_injection` (K29). For `subject=aiko`: mined over Aiko's own combined
self-model population (self-themes + self-memories; the shipped L11 enablement)
and surfaced through the T3 `relevant_context` core lane so her values are
grounded in her history, not just declared in the persona file.

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

## L11. Subject=aiko enablement — Aiko's self-model (SHIPPED)

**Not a kind — a subject.** This entry does **not** add a "self" kind. It's the
enablement that lets *every* kind (identity, value, affective, boundary,
aspiration) exist with `subject=aiko`, mined over Aiko's own memories instead of
the user's. "Self-concepts" is just shorthand for "concepts where
`subject=aiko`".

**Status: SHIPPED.** Aiko's self-model now reaches parity with the user path. The
single [`TopicGraph`](../../app/core/conversation/topic_graph.py) already clusters
*all* memories, including her `self`/`reflection`/`diary` rows, so aiko-dominant
clusters exist for free; they were just discarded. The aiko pass
([`_run_aiko_pass`](../../app/core/concepts/concept_synthesis_worker.py)) is now a
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
([`identity_aiko`](../../app/core/concepts/proposers/identity_aiko.py) /
[`value_aiko`](../../app/core/concepts/proposers/value_aiko.py)) share the
hybrid [`propose_aiko_hybrid`](../../app/core/concepts/proposers/base.py) body, so
every future `subject=aiko` kind inherits the same combined path. Combined
dirty-tracking (self-memory count/max-id delta OR aiko-cluster drift) fires per
proposer `sig_key`.

**Surfacing.** `subject=aiko` concepts surface every turn through the T3
`relevant_context` core lane under first-person "yourself" headers (there is no
dedicated self-image worker/block — that was removed). Cluster-typed evidence
renders real "…keeps surfacing around X/Y" grounding via
[`resolve_evidence_labels`](../../app/core/concepts/concept_snapshot.py) + the
`src_types=("cluster","concept")` filter in
[`inner_life_part1`](../../app/core/session/inner_life_part1.py); memory-typed
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

## L12. Tension / contradiction concepts (SHIPPED — the first meta kind)

**Status: SHIPPED (all three subjects; the first `concept -> concept`
consumer).** The `tension` kind names two *other* active concepts held in
friction. Subjects: `user` (an internal push/pull he hasn't articulated),
`relationship` (a cross-subject user-value vs aiko-value clash), and `aiko` (a
tension within herself).

**Kind.** `tension`, `evidence_model="meta"` (the code vocabulary for the design
doc's `concept_graph`), registered in
[`concept_kinds.py`](../../app/core/concepts/concept_kinds.py) with
`tension_evidence_gate` (source floor overridden to 2 — the two sides — with age
`1.0d` + confidence `0.6` floors), `core_always_on=False`, medium-fluid
plasticity `0.35`.

**Motivation.** The most human observation of all: noticing an internal tension
the person hasn't articulated. "You've been in Maker Mode a lot this week but
haven't taken one of your walks." "Wants simplicity but keeps adding
complexity." "Values rest but rarely takes it." These land hard *because* they
require holding two patterns at once and seeing the friction — pure synthesis,
impossible without the concept layer beneath.

**What shipped.**

- **Evidence wiring.** A tension cites its two bases as `evidence` edges with
  `src_type="concept"` — so `evidence_of` counts them (`distinct_source_count`
  = 2, gate/snapshot work unchanged) AND `dependents_of(base)` walks to the
  tension, lighting up the previously-dormant `ConceptView.activated()` meta
  path (`_bump(dep, 0.8)`) and the `_mark_dependents_stale` cascade for free.
- **Proposer.** A `"tension"` synthesis population + `_run_tension_pass(subject)`
  runs **last** (dependency-ordering rule) over the active *base* (non-meta)
  concepts (the `user`/`aiko` lenses read that subject's pool; `relationship`
  pairs across both), dirty-tracked on a fingerprint of the offered pool (ids +
  rounded confidence + a live/quiet hint from `last_reinforced_at`). Three
  proposers ([`tension_user`](../../app/core/concepts/proposers/tension_user.py) /
  [`tension_relationship`](../../app/core/concepts/proposers/tension_relationship.py) /
  [`tension_aiko`](../../app/core/concepts/proposers/tension_aiko.py)) share the
  `propose_tension` body; composition rule = exactly a pair of distinct base
  ids.
- **The four L1 meta rules.** (1) dependency ordering — proposers last + reads
  `status="active"`; (2) cascade — `ConceptLifecycleWorker._apply_meta_rules`
  retires a tension to dormant when a base leaves `active`, driven by the live
  `_mark_dependents_stale`; (3) confidence bounding — `min(conf, min(base
  confidences))` at accrual; (4) depth cap + cycle guard — only non-meta actives
  are offered, plus a persist-time `_filter_meta_evidence` guard rejecting any
  `("concept", id)` edge to a missing or meta target.
- **Surfacing — delivered with the most care of any kind.** Tension is
  **excluded from the static T3 relevant-context render**
  (`_add_scored` early-returns on `kind=="tension"`) so a standing friction can
  never nag. The only surface is a strictly-cooldowned **T6 cue**:
  [`TensionCueWorker`](../../app/core/proactive/tension_cue_worker.py) drafts a
  ripe active tension into the `aiko.tension_cue` kv ring;
  `_render_tension_block` folds the newest unseen one in as a private,
  non-verbatim observation (watermark-gated, per-tension cooldown), phrased
  gently and — for the relationship lens — never as a grievance.
- **Knobs.** `agent.tension_synthesis_enabled`, `agent.tension_cue_enabled`,
  `agent.tension_cue_cooldown_days`, `memory.concept_synthesis_max_tension_concepts`,
  `memory.tension_cue_{interval_seconds,min_confidence,journal_max}`.

**Concrete first shapes covered.** (a) the **cross-subject value clash** (the
`relationship` proposer) — aligned pairs are a shared value (skipped), clashing
pairs a tender relationship tension; (b) **value-contradicted-by-behaviour** and
other same-subject frictions (the `user` / `aiko` proposers).

**Still open (folds into future meta work).** L20 abstraction rides the same
`concept -> concept` machinery this established — **shipped**: the
`generalization` meta reuses these `relation="evidence"` rails, adding only an
arity range and an arity-aware moot rule. (L18d concept-vs-concept conflict
detection also rides it — shipped: boundary clashes are now a first-class shape
in the tension proposers.) A dedicated tension/drift *surfacing priority
override* (L23) is still deferred.

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

- **Per-cluster affect maps** — [`cluster_affect`](../../app/core/concepts/cluster_affect.py),
  a pure kv-backed EWMA store (mirrors K75 `user_expertise`). Two maps,
  `concept.cluster_affect.user` / `concept.cluster_affect.aiko`, keyed by topic
  `cluster_id`, each holding `{valence, arousal, samples, updated_at}`, bounded by
  cap + age-out.
- **Post-turn sampler** — `_sample_cluster_affect` in
  [`post_turn_helpers_mixin`](../../app/core/session/post_turn_helpers_mixin.py),
  called after `apply_turn`. It embeds `user_text`, resolves the live cluster via
  `best_clusters_for`, and EWMA-folds the K37 `user_affect` estimate into the user
  map (when a real estimate exists) and Aiko's post-turn `AffectState` into the
  aiko map. Gated by `agent.affect_sampler_enabled`; fully best-effort.
- **Self-memory affect stamping** — `MemoryStore.set_affect_provider` stamps
  `metadata.affect = {valence, arousal}` on `self`/`reflection`/`diary` writes
  (Aiko's self-narrative tone), the aiko-only second signal.

**Synthesis.** A new `"affect"` population + `_run_affect_pass(subject)` in
[`concept_synthesis_worker`](../../app/core/concepts/concept_synthesis_worker.py):
it annotates topic clusters with the subject's typical affect (from the map,
joining `cluster_id -> representative_id` at synthesis time) and, for
`subject=aiko`, ALSO aggregates her self-themes' self-memory affect + offers her
affect-stamped self-memories. Two proposers
([`affective_user`](../../app/core/concepts/proposers/affective_user.py) /
[`affective_aiko`](../../app/core/concepts/proposers/affective_aiko.py)) name the
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
[`propose_ordered_concept`](../../app/core/concepts/proposers/base.py)
`(kind, gate_flag, block_word, …)`; `propose_narrative` is now a thin
behavior-preserving wrapper (`gate_flag="closed"`). Aspiration's wrapper passes
`gate_flag="directional"`. The only structural additions are a **min evidence
span** filter (a trajectory must cover time) and the `directional` gate flag
(vs narrative's `closed`).

**Synthesis.** A new `"aspiration"` population + `_run_aspiration_pass(subject)`
in [`concept_synthesis_worker`](../../app/core/concepts/concept_synthesis_worker.py).
The narrative/aspiration candidate-building + dirty-tracking + sig-persistence
was refactored into a shared `_ordered_candidates(...)` helper; narrative calls
it with `min_span_days=0` (behavior-preserving), aspiration with a span floor
(`_span_days` over member `event_time`/`created_at`). Proposers
[`aspiration_user`](../../app/core/concepts/proposers/aspiration_user.py) /
[`aspiration_aiko`](../../app/core/concepts/proposers/aspiration_aiko.py) name
any **directional** chain (third-person / first-person voice) or reinforce a
known one by id; non-directional or too-short chains are dropped. Per-subject
dirty-tracking under each `sig_key`; gated by `agent.aspiration_synthesis_enabled`.

**Registry + gate.** `aspiration` is registered (`sequence` model, plasticity
`0.4` — a direction is durable but *evolves* as progress happens, between
narrative's `0.3` and affect's `0.5`; `core_always_on=False`, **no**
`surfacing_targets`) with
[`aspiration_evidence_gate`](../../app/core/concepts/concept_lifecycle.py):
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
[`AspirationMomentumWorker`](../../app/core/proactive/aspiration_momentum_worker.py)
reads active aspirations via a `ConceptView` (L24), and — **staleness-driven,
not calendar-driven** — picks the stalest one worth a check-in (past
`staleness_min_days` since last reinforcement, off its per-concept cooldown,
above the confidence bar) and drafts ONE cue into the `aiko.aspiration_momentum`
kv ring. The consumer `_render_aspiration_momentum_block`
([`inner_life_part2`](../../app/core/session/inner_life_part2.py)) surfaces the
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

**Status: SHIPPED (core + all three deferred pieces).** `plasticity` is now
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

**Deferred block — SHIPPED.** All three deferred pieces landed together (this
also carries **L18a**). (1) **Relationship modulation** — a *hybrid*: the L3
worker computes an **effective** plasticity at eval time via the pure
[`effective_plasticity`](../../app/core/concepts/concept_lifecycle.py), lifting a
kind's stored base by the live trust + relationship-duration signal
(`RelationshipSignal`, built in
[`speaking_workers_init_mixin.py`](../../app/core/session/speaking_workers_init_mixin.py)
from [`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(trust) + [`relationship.py`](../../app/core/relationship/relationship.py)
(duration)). Per-kind gains live on `ConceptKind.plasticity_modulation`;
**`boundary`** is the first (and only) consumer — its 0.45 base loosens toward a
0.75 ceiling as the bond deepens, never touching the stored base. "Never
silently": the worker materializes one `signal:relationship_trust
--influences--> concept` edge and emits a `plasticity_shift` event when the lift
crosses a band. (2) **Plasticity-drift** — a settled *active* concept's stored
plasticity is nudged one-way down toward a floor via
[`drift_plasticity`](../../app/core/concepts/concept_lifecycle.py), scaled by
confidence + engaged age (stickier with time). (3) **Re-check slowdown** — a
sticky (low effective-plasticity) concept is probed for contradictions on a
plasticity-scaled stride (`1 + round(k·(1−eff_plast))`), so core beliefs are
re-examined less often (in addition to L16 already scaling the *delta*). Each
piece is independently switchable via `concept_plasticity_*` settings; see
[`configuration.md`](../configuration.md).

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

**Motivation.** The charming payoff of the whole layer. A normal memory system
answers *"what happened?"*; the concept history answers the far more powerful
question *"how did my understanding change **because of** what happened?"* Aiko
notices change not by diffing prompts but by **comparing her own concepts (L11
self-concepts, and user-subject beliefs) across time**:

> "I was looking through some old memories today. I think I've changed a bit."
> "I used to avoid taking the lead in conversations. These days I catch myself
> doing it much more."
> "You know... I think you've corrupted me a little. I seem to ask for cookies
> far more often than I used to."
> "I previously treated your preference as generally liking detailed answers.
> Over time I noticed the real factor is whether you're *exploring* a topic or
> *solving* a known problem."

These land because they're *self-reflective and evidence-grounded*, not scripted.
The deepest version is a **development timeline**: "when we started, I mostly saw
our interactions as technical problem-solving; over time I learned that exploring
ideas together matters just as much." That reflection can't come from a bigger
model or a longer context window — only from a persistent, evolving model with
history. And critically, it is *earned*, not manufactured: we never say "generate
a new trait each month." Evidence appears, concepts move, contradictions resolve,
confidence shifts, behaviour adapts — and **if something meaningful changed, it
becomes visible.**

**What already exists (the substrate).** L17 is less "build history" than
"interpret history we already keep":

- The **`concept_events` append-only timeline**
  ([`concept_event_store.py`](../../app/core/concepts/concept_event_store.py))
  already snapshots `label` + `confidence` + `evidence_count` +
  `distinct_source_count` + `source_kinds` + `reason` + `created_at` at every
  lifecycle transition (`discovered` / `promoted` / `reinforced` / `dormant` /
  `contradicted` / `revived` / `plasticity_shift` / `retired` / `merged`), and
  `list(before_id=…)` pages backward "through the years." A concept's
  confidence-and-label **trajectory is largely reconstructable from its event
  stream** — so a separate `concept_snapshots` table is *probably unnecessary*
  (see L17a for the one gap: slow decay that never crosses a status threshold).
- **L26** stamps a per-turn trace of which concepts surfaced (confidence + hedge),
  and [`concept_snapshot.py`](../../app/core/concepts/concept_snapshot.py) dumps
  current graph state — the two ends of the "history of thought" (L17e).
- **L20** (generalization) is exactly the *shape* of the "adaptive depth"
  refinement; **L12** (tension) and **L15** (belief revision) are the *mechanisms*
  by which the "why" happens; **L16** plasticity is the noise governor.

**The hard part (design focus).** Taking snapshots is easy. Deciding **which
change deserves interpretation** is the whole game — most confidence wiggle is
noise (0.72 → 0.74 is nothing), while a *relabel* or a *supersession* ("likes
detail" → "prefers adaptive depth, context-dependent") is a genuine learning
event. L17b is dedicated to that classifier; L17c attaches the *why*; L17d lifts
it to Aiko noticing patterns in **her own mistakes**; L17e is surfacing + the
debug timeline. Split so the read/classify layers can land before the (harder)
self-correction and narrative layers.

**Sibling systems.** K70 growth-witness tracks the *user's* growth; K30
self-noticing is transient/in-session — L17 is the *durable, concept-grounded*
self-version. Overlaps L19 (autobiography) and L29(b) (meta-narrative over
concepts) for the timeline; keep those as the *rendering* consumers, L17 as the
*change-detection* engine.

**Effort.** Large overall; sequenced L17a → L17e below.

---

## L17a. Concept trajectory from the event log (the history read layer)

**Motivation.** Give the rest of L17 a clean "how this concept moved over time"
read without inventing new storage. The event stream already carries confidence +
label at each transition; we just need a reader that turns a concept_id (or a
subject slice) into an ordered trajectory.

**Key files.**
[`concept_event_store.py`](../../app/core/concepts/concept_event_store.py) (add a
`trajectory(concept_id)` / `list(concept_id=…)` filtered read — today `list`
filters only by `subject` / `event_type` / `before_id`);
[`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)
(the one writer — emit a lightweight sampled event on meaningful decay, see
below).

**Sketched approach.** A trajectory is `[(created_at, confidence, label, status,
reason)]` reconstructed from a concept's events plus its current row. The **one
gap**: a concept that slowly *decays* without crossing a status threshold emits
no event, so its downward trail is invisible. Fix with the cheapest option —
have L3 append a `confidence_sample` (or reuse `plasticity_shift`-style) event
only when `|Δconfidence|` since the last logged event crosses a band (e.g. 0.1),
so we sample *on meaningful movement*, not on a wall-clock cadence (keeps the
table small and event-driven, consistent with the rest of L3). Add
`concept_id`-filtered indexing so `trajectory()` is cheap.

**Open questions.** (1) Band size for the decay sample (fixed vs plasticity-scaled
— a high-plasticity taste should sample more finely than a sticky core trait)?
(2) Keep every event forever, or thin old rows the way P32 thins snapshots
(retaining transitions + relabels, dropping intermediate samples)?

**Effort.** Small (read helper + one guarded event emit).

---

## L17b. Change-salience classifier -- "what deserves interpretation"

**Motivation.** The crux the whole feature lives or dies on. Confidence 0.72 →
0.74 is noise; "detailed answers" → "adaptive depth, context-dependent" is a real
learning event. Aiko must only reflect on the second kind, or she becomes a
narrator dressing up every wiggle as growth.

**Key files.** A new pure classifier (e.g.
`app/core/concepts/concept_drift.py`) over an L17a trajectory + the current
[`ConceptStore`](../../app/core/concepts/concept_store.py) / edges; consumed by
L3 or a dedicated low-frequency drift pass.

**Sketched approach.** Rank change *shapes*, not raw deltas:

- **Noise (ignore):** confidence drift below a plasticity-scaled band, no label
  change, no status change.
- **Relabel / refinement (interesting):** the concept's `label` changed
  meaningfully (embedding distance between old and new label over a threshold) —
  "likes detail" → "prefers adaptive depth". Highest-value signal.
- **Supersession (interesting):** an old concept `retired`/`dormant` while a new
  **generalization** (L20) or sibling concept now covers the *same evidence*
  (shared evidence edges) — the "context-dependency discovered" case, literally a
  parent concept forming over children.
- **Fission / split (interesting):** one concept carrying strongly *bimodal*
  evidence (support + counter, or two contextual sub-clusters) fissions into two
  contextual children — the structural precursor that then feeds a **generalization**
  (L20). This is a genuine learning event ("I realised these were two different
  things"); it's driven by the **L31** split primitive.
- **Contradiction resolved (interesting):** a `contradicted` → `revived`/`active`
  arc (L15), i.e. a belief that broke and re-formed differently.
- **Emergence / loss:** a concept crossed candidate → active after long latency,
  or a once-core trait faded below the core bar.

Plasticity-gate the *weight*, not just the threshold (L16): the same drift on a
high-plasticity taste is charming small-talk; on a low-plasticity core trait it's
rare and weighty. Output a small ranked list of `DriftEvent`s with a salience
score; everything below a bar is silently dropped.

**Open questions.** (1) Label-change detection — embedding distance, an LLM
"is this a real refinement or a rephrase?" adjudication (cheap, batched), or
both? (2) How to detect supersession robustly from shared-evidence overlap vs a
coincidental new concept? (3) One global salience bar, or per-shape bars?

**Effort.** Medium (this is the intellectually hard part; mostly heuristics +
optional tiny LLM adjudication, no new storage).

---

## L17c. Change + why -- the learning-event record

**Motivation.** The most powerful framing the user identified: not
`old → new`, but `old → new **because** …`. A drift is only insight if it carries
its cause:

> Old: "enjoys detailed explanations" · New: "prefers adaptive depth"
> Reason: repeated counter-evidence (asked for shorter summaries; preferred
> direct fixes while debugging; enjoyed deep discussion during architecture) ·
> Resolution: context-dependency discovered.

**Key files.**
[`concept_event_store.py`](../../app/core/concepts/concept_event_store.py) (the
`reason` field already exists — enrich it, and consider a `caused_by` provenance
list); the L20 generalization + L12 tension edges as the *mediating* structure;
[`concept_snapshot.py`](../../app/core/concepts/concept_snapshot.py) /
`resolve_evidence_labels` (already turns evidence edges into human-readable "what
it rests on" — reuse to render the "because" clause).

**Sketched approach.** For each salient `DriftEvent` (L17b), assemble a
*learning-event* bundle: the trajectory endpoints, the triggering evidence
(memories/clusters added between snapshots, resolved to labels via the existing
`resolve_evidence_labels`), and the mediating meta-concept if any (the L20 parent
that formed, or the L12 tension that resolved). Persist the causal summary in the
event `reason` (and/or a `caused_by` id list) so it's durable and inspectable, not
recomputed. This bundle is what both the surfacing line (L17e) and the debugger
(L17e) read.

**Worked example (the canonical target — build toward this).** The
easiest-and-most-immersive case, because the old→new link is *structural* (shared
evidence), not a semantic guess:

```
Old snapshot                          New snapshot
  label:      "likes detailed answers"   "prefers adaptive depth by context"
  reasoning:  "frequently asks for deep  "enjoys detailed exploration, but
               technical explanations"    prefers concise troubleshooting"
  confidence: 0.75                        0.88
```

- **Why the new concept exists:** it's an **L20 generalization** whose two children
  ("detailed when exploring", "concise when troubleshooting") were mined from the
  counter-evidence — so the parent's evidence edges *overlap the old concept's
  evidence*. That overlap is the reliable, structural signal that this is a
  **refinement of** the old belief, not an unrelated new one (L17b supersession
  shape) — no embedding/LLM guess needed for this path.
- **Why confidence rises (0.75 → 0.88), not falls:** the more specific model
  explains *more* of the evidence than the flat one, so it promotes strongly while
  the over-general belief loses ground and is superseded (retired/dormant). This
  is expected, not a bug.
- **The learning-event record:** `old → new, because: counter-evidence (concise
  troubleshooting requests) · resolution: context-dependency discovered`.
- **How Aiko voices it (L17e):** "I used to have a simpler read on you — that you
  just liked detailed answers. But you kept wanting quick fixes when debugging,
  and it clicked that the real pattern is *why* you're asking: exploring vs.
  solving. So I refined how I think about it." — evidence-grounded, carries its
  own cause, reads as genuine learning rather than a scripted "I've grown."

Contrast (the harder, later case): a pure **relabel with no shared-evidence link**
(a genuinely different belief that merely sounds like a refinement) needs the
fuzzier embedding-distance / tiny-LLM adjudication in L17b. The example above is
the structural path we should target first.

**Open questions.** (1) Compute the "because" at drift-detection time (durable but
may miss late evidence) or lazily on surface (fresh but recomputed)? (2) Cap the
evidence list length for readability? (3) Store as free-text reason, structured
`caused_by`, or both?

**Effort.** Medium.

**Depends on.** L17a/L17b; reuses L12/L20 edges + `resolve_evidence_labels`.

---

## L17d. Self-correction meta-concepts -- Aiko noticing patterns in her own mistakes

**Motivation.** The most ambitious idea: not just "this belief changed" but "I
keep making *this kind of* error, so I changed my strategy" — behavioural
evolution, not a new memory.

> Self-observation: "I frequently overestimated the importance of technical
> detail." · Correction: "when the user asks for troubleshooting, prioritise
> actionable steps; when they explore concepts, allow deeper discussion."

**Key files.** A meta-proposer reading the **aiko-subject** slice of the event
log (L17a) rather than the memory store — architecturally the same move as the
L29(b) meta-narrative-over-concepts proposer; lands as a `generalization` (L20)
or `communication_style` (L23) concept whose evidence is *prior drift events /
contradicted concepts*.

**Sketched approach.** Periodically scan Aiko's own `contradicted` / relabelled /
superseded concepts (L17b output) for a recurring *shape* — e.g. multiple
corrections that all move from "detail-first" toward "context-adaptive." When a
pattern clears an evidence floor, propose a self-correction meta-concept (a rule
about her own behaviour), gated and promoted through the ordinary L3 lifecycle so
it's earned, not asserted. Because it's a real concept, it then surfaces and
*steers behaviour* via the existing L23 communication-style path — closing the
loop from "noticed a mistake pattern" to "behaves differently."

**Open questions.** (1) Is this a distinct kind (`self_correction`) or just a
`generalization`/`communication_style` over aiko-drift evidence? (2) How many
correlated corrections constitute a "pattern" (evidence floor)? (3) Guard against
over-correction / oscillation — plasticity + a cooldown so she doesn't rewrite her
strategy every week.

**Effort.** Large.

**Depends on.** L17b/L17c, L20 (generalization machinery), L23
(communication-style steering), L11 (aiko self-concepts).

---

## L17e. Surfacing + the "history of thought" debugger

**Motivation.** Two consumers of the change-detection engine: the rare, charming
*proactive reflection* to the user, and — the user's other favourite — a
**developer-facing "history of thought"** so that when Aiko's behaviour gets too
complex to read from code, you can answer "why did she do that?" by inspecting the
causal chain (memory A raised concept B → B clashed with C → resolution formed
hypothesis D → D shifted a communication preference).

**Key files.** Surfacing via
[`prepared_nudge.py`](../../app/core/proactive/prepared_nudge.py) /
`narrative_block` (rare proactive line, strict cooldown); the debugger reuses the
L26 per-turn trace + `concept_events` (L17a) + concept edges, exposed through the
existing `GET /api/concepts` facade and an MCP tool, rendered in the Memory tab of
[`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx).

**Sketched approach.** *Surfacing:* take the top salient learning-event (L17b/c),
render it in Aiko's voice with the "because" clause, plasticity-gated (high-
plasticity drift = light and playful; low-plasticity core-trait drift = rare and
weighty, phrased so it isn't alarming), behind a strict cooldown so genuine drift
is a notable, not a habit. Respect the K47 question/assertion balance.
*Debugger:* a timeline view over `concept_events` (already paginated) with a
"trace this concept back" that walks evidence edges + the mediating tension/
generalization to reconstruct the causal chain — the inspectable "history of
thought." This is the debugging tool that stays useful as the graph grows.

**Open questions.** (1) Surfacing cadence + comparison horizon (event-driven off
salience, or a monthly review pass)? (2) How to phrase low-plasticity core drift
without sounding alarming? (3) Does the *user's* own concept history get the same
timeline traversal (a shared "our history of understanding" view), or aiko-only
first?

**Effort.** Medium (surfacing) + Medium (debug view); relies on L17a–c.

**Depends on.** L17a–L17c (engine), L26 (trace), L6 (Concepts UI surface).

---

## L17f. Evolution diary -- a human-readable change log

**Motivation.** L17a-e detect and voice *individual* drifts; L17f is the durable,
browsable **diary** that accumulates them into "here is how I've changed." A
periodic entry in Aiko's own words:

> "This week I noticed I've been explaining technical topics with more
> architectural depth — our recent conversations showed you prefer the trade-offs
> up front."

This is also the single best **end-to-end test of whether the whole concept
system works**: if the diary reads as real, grounded change, the pipeline
(evidence → concept → drift → why) is healthy; if it reads as noise or
fabrication, something upstream is wrong. It's the human-legible mirror of the
`concept_events` timeline.

**Key files.** Renders from the L17b/c learning-events + the `concept_events`
timeline ([`concept_event_store.py`](../../app/core/concepts/concept_event_store.py));
persisted as its own append-only log (a `kv_meta` ring or a small `evolution_log`
table) so entries are stable, not recomputed; surfaced in the Concepts/Memory tab
([`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)) and optionally
as a rare proactive share
([`prepared_nudge.py`](../../app/core/proactive/prepared_nudge.py)).

**Sketched approach.** A low-frequency worker gathers the salient learning-events
since the last entry (L17b), composes one short first-person paragraph grounded in
the actual `because` clauses (L17c), and appends it with provenance (the
`concept_id`s + `event_id`s it summarises) so each diary line is
click-through-inspectable (the L17e debugger). Strictly capped — one entry per
period, and a period with no above-noise change is *skipped*, so the diary never
pads itself with filler.

**Open questions.** (1) Cadence — weekly / monthly / every N salient events?
(2) Is the diary user-visible only, or can Aiko *read her own past entries* as
evidence for L17d self-correction? (3) Retention / thinning of old entries.

**Effort.** Medium (composition + storage on top of L17a-c).

**Depends on.** L17a-c (engine + why), L17e (debugger provenance), L26.

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
  ([`boundary_user`](../../app/core/concepts/proposers/boundary_user.py) /
  [`boundary_aiko`](../../app/core/concepts/proposers/boundary_aiko.py)) are the
  first **hybrid** proposers for both subjects, sharing a `propose_boundary` body
  in [`base.py`](../../app/core/concepts/proposers/base.py).
- **A single deliberate anchor can seed a boundary.** Unlike every other kind
  (`>= 2` sources), one explicit anchor is enough; cluster-only boundaries still
  need `>= 2`. The proposer enforces the composition rule (`>= 1` anchor **OR**
  `>= 2` clusters); the L3
  [`boundary_evidence_gate`](../../app/core/concepts/concept_lifecycle.py)
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
  [`concept_surfacing`](../../app/core/concepts/concept_surfacing.py) helper
  (`recency_boost` + `composite_score`) blending **context (cosine) + confidence
  + recency**, applied to the turn-relevant fill in
  [`inner_life_part1`](../../app/core/session/inner_life_part1.py). Defaults are
  context-only, so this is **opt-in per kind** and every other kind is unchanged;
  boundary opts into a recency-heavy blend (`context 0.5 / confidence 0.2 /
  recency 0.3`, 14-day half-life) because a line she was just reminded of matters
  more than a stale one.

**Synthesis.** A `"boundary"` population + `_run_boundary_pass(subject)` in
[`concept_synthesis_worker`](../../app/core/concepts/concept_synthesis_worker.py),
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
live at eval time by [`effective_plasticity`](../../app/core/concepts/concept_lifecycle.py)
and never touching the stored base. "Never silently": each modulation
materializes a `signal:relationship_trust --influences--> concept` edge and, on a
band cross, emits a `plasticity_shift` event. Per-kind gains live on
`ConceptKind.plasticity_modulation`
([`concept_kinds.py`](../../app/core/concepts/concept_kinds.py)); the live signal
is wired in
[`speaking_workers_init_mixin.py`](../../app/core/session/speaking_workers_init_mixin.py).
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
  [`prompt_support.py`](../../app/core/session/prompt_support.py)) folds in
  right after the persona (same T0 slot as the speech-grammar addendum, so it
  stays in the cache prefix) telling the model that when the context surfaces a
  learned `communication_style` / `boundary` line it is the *live calibration*
  of the defaults and wins when it fits — "hold them lightly, never as hard
  rules, and when none surface the defaults simply stand". Self-gating, so
  there is no per-turn branching in T0.

This carries the deferred L23 "lighten hard-coded persona style blocks"
follow-on. The `communication_style` / `boundary` concept headers in
[`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) already say
"let these steer HOW you talk"; the addendum is the missing bridge from those
T3 lines back to the T0 defaults.

**Effort.** Small (prompt-only).

---

## L18c. Boundary-vs-conversation conflict steer (fast-follow) — SHIPPED

**Status: SHIPPED.** A K29-style per-turn detector
([`boundary_clash_detector.py`](../../app/core/affect/boundary_clash_detector.py))
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
  [`inner_life_part3.py`](../../app/core/session/inner_life_part3.py), wired
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

## L18e. Boundary evidence broadening

**Motivation.** L18 mines *deliberate* anchors (`[[remember:…]]`) + clusters. Many
stated limits/preferences never become a deliberate anchor but still deserve to
seed a boundary; and now that the per-kind `surface_weights` mechanism exists, the
non-boundary kinds can be tuned too.

**Sketched approach.** A dedicated preference/limit memory signal (mine explicit
stated-limit memories beyond deliberate anchors), plus tuning `surface_weights`
for identity/value/etc. now that the composite scorer is in place.

**Key files.** The anchor-kind selection in `_run_boundary_pass`, the memory
tagging/signal source, per-kind `surface_weights` in
[`concept_kinds.py`](../../app/core/concepts/concept_kinds.py).

**Effort.** Small-medium.

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

**Status: SHIPPED (single-level).** A `generalization` meta kind that abstracts
2+ active concepts (of any kind, same subject) into a higher-order super-concept,
riding the **L12 tension meta rails**: `evidence_model="meta"`, base->parent
links stored as `relation="evidence"` `concept -> concept` edges (NOT the
`generalizes` relation — see below), and all the shared meta machinery
(`_filter_meta_evidence` depth/cycle cap, `dependents_of` activation, the L3
cascade). It differs from tension in exactly three ways: **arity is a range**
(2..N children, capped at `GENERALIZATION_MAX_CHILDREN`, not a fixed pair);
**moot is arity-aware** (`_apply_meta_rules` branches on kind — a generalization
stays live while >= 2 children remain active, so it survives losing one, and its
confidence is bounded by the shakiest *active* child); and it **renders + pins**
(on the always-on core lane at a high bar), where a tension is hidden. When a
generalization parent is present at `generalization_parent_min_confidence`, its
children are suppressed from the surfacing pool (`_suppress_generalized_children`)
so Aiko says "you love building things that last" instead of reciting the five
sub-interests.

**Key files.** `generalization` kind + `generalization_evidence_gate`
(slower/stronger than tension: 2-source floor, 3.0d age, 0.72 confidence) in
`concept_kinds.py` / `concept_lifecycle.py`; `propose_generalization` in
`proposers/base.py` + `generalization_user.py` / `generalization_aiko.py` (SPECs
registered last, with the tension metas); `_run_generalization_pass` +
`generalization` population dispatch in `concept_synthesis_worker.py`; the
arity-aware `_apply_meta_rules` branch in `concept_lifecycle_worker.py`; the
`generalization` family + `_concept_generalization_header` + child suppression in
`inner_life_part1.py`; recall enrichment in `rag_retriever.py`
(`_concept_related_links` labels a generalization parent/child as `"generalizes"`
via concept *kind*, since the link rides an `evidence` edge). Settings:
`agent.generalization_synthesis_enabled`, `memory.concept_synthesis_max_
generalization_concepts` / `generalization_suppress_children_enabled` /
`generalization_parent_min_confidence`.

**Why `evidence` edges, not `generalizes`.** Reusing the tension rails means the
whole meta lifecycle (dependency ordering — children active first; cascade;
`min`-bounded confidence; depth/cycle guard) works unchanged; the parent/child
relationship is recovered from the neighbour's `kind == "generalization"`, not
the edge relation. The `generalizes` relation in the edge enum is now
effectively reserved for a future *multi-level* hierarchy.

**Remaining follow-up.** Multi-level hierarchies (parent-of-parent) — the meta
depth cap stays for v1, so a generalization can't yet abstract another
generalization; relationship-subject abstractions (user + aiko only for now).
Feeds L19 naturally — the abstraction level is what a self-narrative reaches for.

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

**Status: MEASUREMENT SHIPPED; intake tuning pass 1 SHIPPED; decay tuning,
enforcement and the offline harness deferred.**

**Motivation.** Confidence gates (L3) and the optional human-in-loop (L6) keep
*individual* concepts honest, but nothing measured whether the layer as a whole
was producing *good* concepts. It wasn't, and none of it was visible from any
existing surface. The first month of real use: **544 concepts at ~20/day, a 91%
promotion rate, 83% of actives never reinforced after promotion, zero demotions
ever, and 729 near-duplicate pairs (under the then-current 0.9 dedupe bar)
against 9 merges.**

### What shipped

[`concept_quality.py`](../../app/core/concepts/concept_quality.py) is the pure
scorer (I/O-free, following the `persona_regression.py` split);
[`concept_snapshot.build_concept_quality`](../../app/core/concepts/concept_snapshot.py)
does the graph joins around it. Surfaced via `GET /api/concepts/quality`, the
`get_concept_quality` MCP tool, and a layer-health strip above the Concepts
panel. Four families of metric:

- **Flow** — production rate, promotion rate, reinforcement / merge /
  contradiction volume, and demotions. Answers "is it minting faster than it
  prunes?"
- **Shape** — confidence and evidence distributions, plus actives sitting
  *below* the promotion bar they passed.
- **Register** — per-(kind, subject) label-template concentration:
  interpretive-frame rate, imported-jargon rate, leading-n-gram share. Never
  pooled globally, because a high shared opening is correct for `value` and
  pathological for `identity` — pooling would average the signal to nothing.
  This is what localised the template collapse to *one* proposer
  (`identity`/`user`: 72% frame, 52% jargon) while `value`/`aiko`,
  `boundary`/`aiko`, `affective`, `narrative` and `aspiration` measured clean.
- **Pruning** — the never-reinforced count, the *rate* at which more arrive,
  and how many *engaged* days (≈ conversation hours) decay would need to
  demote them at the current half-life. That horizon reads a median of **~86**
  on the shipped defaults, which is why production outran pruning. (It was
  originally reported as ~54: the first cut fed `engaged_days_to_floor` the
  raw half-life, but real decay runs on the plasticity-damped *effective*
  half-life — `halflife * (2 - plasticity)` — so every horizon was understated
  by up to 2x. Corrected during the intake pass below.)

All three spurious-concept signals are computed as read-only per-concept fields:
**(A)** distinct cluster span, resolving memory edges through to their clusters
rather than trusting `distinct_source_count`, which counts distinct *edges*;
**(B)** mean/min confidence of the supporting memories, joined from the memory
mirror — nothing in the layer had ever read `memory.confidence`; **(C)**
`unreinforced_since_promotion`. Nothing acts on them.

Shipped alongside, since they were the mechanical causes rather than
measurement gaps: `_existing_for` is now relevance-selected and capped (it was
injecting all 203 identity concepts into every prompt — a token cost, a
200-shot demonstration of the template to imitate, and a list too long to pick
a reinforce-by-id target from); the register rules in
[`identity_user.py`](../../app/core/concepts/proposers/identity_user.py) ban the
specific collapse without touching the shared interpretive bodies in
`proposers/base.py`; `_DEDUPE_COS` dropped 0.9 → 0.86 (nothing in the graph had
*ever* reached 0.9, so the guard had never fired) and the consolidation band
0.88 → 0.84; and L3 now demotes an active concept whose evidence was reconciled
away to nothing, which the confidence-only status floors could not see.

### Threshold tuning, pass 1: intake (shipped)

The first tuning pass the scoreboard paid for. It deliberately touched
**intake only** — no decay-rate change, no sweep of the existing backlog.

**What the numbers said.** Four queries against the live graph (544 concepts,
486 active, 402 never reinforced) located the cause, and it was not the decay
curve:

- `identity` was **the only one of the nine kinds with no promotion floors of
  its own**, riding the global settings alone. Every other kind declared
  `_X_MIN_SOURCES` / `_X_MIN_AGE_DAYS` / `_X_MIN_CONFIDENCE` in
  [`concept_lifecycle.py`](../../app/core/concepts/concept_lifecycle.py).
- It is also the biggest kind: **265 of 486 actives, 240 of 402**
  never-reinforced rows. The largest kind was the unguarded one.
- Global `concept_promote_min_age_days` was **0.0**, so there was no stability
  delay at all: **167 of 240** never-reinforced identity rows promoted within
  an hour of first evidence, at a **median of 3.6 minutes**.
- The L21 young-graph tightening (3 sources / 0.72 confidence) was the only
  thing holding the line, and it switches off at `concept_min_clusters = 6`.
  The graph has 35 clusters, so it had been off approximately forever.
- Source histogram of those rows: 107 at exactly 2 sources, 107 at 3, 16 at 4.
  **A 3-source floor would have refused 116 of 240 (48%).**

The contrast that proved the mechanism works: `value` carries
`_VALUE_MIN_SOURCES = 3` and sits at a mean of 4.31 distinct sources against
identity's 2.73.

**What shipped.**

- **`identity_evidence_gate`** — 3 sources, a 1.0-day stability delay, and the
  ordinary 0.6 confidence bar. Confidence was left alone on purpose: those
  rows average 0.773 confidence, so the leak was structural (sources and age),
  and raising it would suppress good concepts without touching the mechanism
  at fault. If a week of data says 3/1.0 was not enough, raise
  `_IDENTITY_MIN_AGE_DAYS` to 2.0 rather than the source floor — 4 sources
  starts refusing legitimately well-evidenced traits.
- **The age floor is now real on a concept's first evaluation.** Promotion age
  was already measured in engaged days, but `first_evidence_engagement` was
  stamped *after* `_transition` had run the gate, so the first evaluation fell
  back to wall-clock. Harmless while the floor was 0.0; with a 1.0 floor it
  would have let an offline gap carry a candidate past the stability delay on
  idle time alone. The stamp moved ahead of the gate (step 0 of `_process`),
  restricted to genuine first evaluations — an already-evaluated row that is
  still un-anchored predates the v24 backfill, and re-anchoring it would reset
  its accrued age to zero.
- **Intake-rate metrics** in the pruning section, because the standing count
  cannot show whether any of this worked: at an ~86-engaged-day horizon the
  402 will be almost exactly 402 a week later regardless. `promotions_per_day`,
  a 7-day `promoted_recent` / `unreinforced_recent` cohort, and an
  `unreinforced_sample` id list (signal C was the only spurious signal without
  one, which left it countable but not actionable).
- **[`scripts/concept_intake_report.py`](../../scripts/concept_intake_report.py)**
  — a read-only, re-runnable diagnostic (per-kind actives and stall rates,
  promotion cohorts, the source histogram with the "a bar here would have
  refused N" column, promotion latency). Run it before and after a gate change
  and diff.

**Blast radius.** Nothing existing was disturbed: `_gate` is consulted only
for `candidate` and `retired` rows, so no active can be retroactively demoted,
and there were **zero identity candidates in the queue** at the time of the
change — every identity concept ever proposed had already promoted, which is
its own indictment. 118 identity actives do stand on less than the new bar;
they are grandfathered, and they are what the deferred sweep targets.

**Baseline, 2026-07-31 (immediately pre-change).** Re-run
`scripts/concept_intake_report.py` and compare against this. The per-kind stall
rates are the *stock* and will barely move; the promotion rate and the identity
latency figures are the *flow* and should.

| | at baseline |
| --- | --- |
| active / never-reinforced | 486 / 402 (82.7%) |
| identity active / stalled | 265 / 240 (90.6%) |
| identity mean distinct sources | 2.73 |
| identity promoted within 1h of first evidence | 167 of 240 (69.6%), median 3.6 min |
| promotions, last 3 days | 29 (9.7/day) |
| `reinforced` events, all time | 60 |

Read the recent-cohort stall percentage with care: a concept promoted yesterday
has had almost no opportunity to be reinforced, so it reads high by
construction. It is only meaningful against the same window measured at another
time, which is exactly why the baseline above is dated.

### Still open

- **Reinforcement fires far too rarely, and that — not the gate — is the
  larger half of signal C.** The timeline carries 553 `discovered` and 504
  `promoted` events against **60 `reinforced`**. Every concept has a
  `last_reinforced_at` (L2 stamps it at creation), but only 84 actives have one
  *after* promotion. So "82.7% never reinforced" is substantially a statement
  that the reinforce path under-fires, not only that intake is loose. Tightening
  intake helps by arithmetic — fewer concepts competing for the same 60
  reinforcements — but the ceiling is the reinforce path itself. Worth its own
  investigation before any further gate tuning. `value` is the tell: 86%
  stalled at a mean of 4.31 sources, i.e. well-evidenced concepts that still
  never get re-observed.
- **Threshold tuning, pass 2: per-kind decay rates.** Worth knowing before
  starting: per-kind decay **already exists** via `plasticity_default`, and its
  ordering is already right — `value` 0.2 (81 effective days, stickiest),
  `identity` 0.3 (76), `boundary` 0.45 (70), `affective` 0.5 (67.5, fastest).
  The order is correct and **the absolute scale is roughly 6x too slow**. So
  that pass is "lower the base `concept_confidence_halflife_days` and widen the
  per-kind plasticity spread", not a new mechanism. Note the compounding trap:
  `drift_plasticity` pushes active concepts' plasticity *down* toward 0.15 over
  time, so survivors get stickier and the backlog gets harder to drain the
  longer it sits.
- **Enforcement of signal C** — demote the never-reinforced set. Needs a
  chosen threshold and a one-off sweep over the concepts minted before the
  proposer was disciplined. Now cheap to target: `pruning.unreinforced_sample`
  carries the ids, and `unreinforced_since_promotion` is exported.
- **The dormant revival path bypasses the kind gates.** `_transition` revives
  `dormant -> active` on `concept_promote_min_confidence` alone, without
  consulting the promotion gate (unlike the `retired` path, which does). A
  demoted 2-source identity trait could therefore return without facing the
  new floor. Currently unreachable in practice — reviving needs confidence to
  have recovered, which needs reinforcement — and arguably correct, since it is
  restoring something that already earned its place. Recorded so the choice is
  deliberate rather than accidental.
- **Offline eval harness** — a fixture corpus with expected/forbidden concepts,
  scored precision-over-recall in CI like the K10 persona regression. Held back
  on purpose: hand-authoring goldens before the proposer emits the register we
  want would enshrine the output we are trying to fix.

**Open questions.** Fixture corpus hand-authored vs. replayed real history
(behind DT4 replay)? Precision/recall target to gate a release?

**Effort.** Medium (remaining).

---

## L23. Surfacing salience + selection budget

> **Subsumed (shipped) by the unified context budget.** The variable-K,
> turn-relevance-scored selection this item describes now lives in the
> `ContextBudgetSelector` + `relevant_context` region: concepts (alongside
> memories and topic clusters) are scored against the shared per-turn embed
> and selected to fill a shared, context-window-relative token budget,
> reserved before history. Per-source floors/caps/weights/min-relevance are
> the `memory.context_budget_*` knobs. See
> [`docs/context-budget.md`](../../docs/context-budget.md).
>
> **Cognitive-surfacing pass — SHIPPED.** The deferred blend terms are now
> live as a "how a mind brings a thought forward" scorer in
> [`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py)
> (`surface_score`), driven by per-kind
> [`SurfaceWeights`](../../app/core/concepts/concept_kinds.py):
> - **confidence×plasticity stability** (`stability()`) — identity/value rank
>   on how *settled* a belief is, not just cosine;
> - **recency of reinforcement** (`recency_boost()`, already used by boundary,
>   now affective too);
> - **emotional / recent-change salience** (`salience()` + `event_charge()`) —
>   a freshly `contradicted` / `plasticity_shift` / `revived` / `promoted`
>   concept intrudes, fading over a per-kind half-life (boundary/affective);
> - **spreading activation** (`ConceptView.activated()`) — concepts that share
>   a *hot topic cluster* with the turn's active set (pinned core) are primed
>   into the pool with an additive boost even at low direct cosine; the
>   concept→concept path (`dependents_of`) is now **lit** by the L12 tension and
>   L20 generalization metas (their `("concept", id)` `evidence` edges), so a hot
>   base primes the meta above it;
> - **habituation / anti-nag cooldown** (`habituation_factor()` + a `kv_meta`
>   `{concept_id: last_surfaced_turn}` map on the `relationship.total_turns`
>   clock) — a concept surfaced in the last few turns is damped (strong on the
>   flex lane so it steps aside; soft on the core lane, which *rotates* which
>   core beliefs show rather than suppressing any). Knobs:
>   `memory.concept_surfacing_*` (see [`docs/configuration.md`]). The per-turn
>   `score` breakdown is threaded into the concept trace for the MCP view.
>
> Still genuinely open (deferred): the **tension/drift priority override** and a
> **cluster-affect** term for salience (the `affect` input to `salience()` is
> threaded but fed `0.0` today). The **always-on core concept lane** shipped
> *identity-only* first (`context_budget_identity_cap` / `_min_confidence`) and
> was generalised to be **kind-aware** in **L27**.

> **Follow-on — self-authored *style* concepts. SHIPPED (kind + mining +
> surfacing; persona-lightening deferred).** The budget is the delivery vehicle
> for the north star: progressively lighten the fixed persona prompt so Aiko is
> more model-agnostic and driven by remembered context. Shipped as a new concept
> *kind* `communication_style` (`subject=user` **and** `aiko`) — how detailed her
> replies should be, when to lead vs. follow, how much to hedge — **bound to the
> context it applies to** and surfaced through the same `relevant_context` region
> so it conforms to the user over time instead of being hard-coded in the persona
> file.
>
> - **Kind + gate.** `communication_style` (`set`, `plasticity_default=0.4`) with
>   a boundary-like `communication_style_evidence_gate` (source floor overridden
>   to 1 so a **single self-authored anchor** promotes — "tell her once and it
>   sticks"; cluster-only inference still needs `>= 2`; age `0.5d` + confidence
>   `0.65` floors). Registered in [`concept_kinds.py`](../../app/core/concepts/concept_kinds.py).
> - **Hybrid mining, digest-guided.** A `"comm_style"` synthesis population +
>   `_run_comm_style_pass(subject)` feeds two proposers
>   ([`communication_style_user`](../../app/core/concepts/proposers/communication_style_user.py)
>   / [`communication_style_aiko`](../../app/core/concepts/proposers/communication_style_aiko.py))
>   sharing `propose_communication_style`. Evidence = anchors (`self_tagged` /
>   `self`·`reflection`·`diary`) + topic clusters; **guided (never grounded)** by
>   a persisted *style-signal digest* — K13 `style_signal` labels + the distilled
>   `user_profile.communication_style` field (digest hash folded into the pass
>   dirty-key so a material style shift re-fires it).
> - **Context-scoped surfacing.** NOT on the always-on core lane — a style line is
>   only relevant when its context is live — so it surfaces purely by relevance +
>   spreading activation: the proposer binds each label to its context and cites
>   the backing topic cluster, so `ConceptView.activated` lights it up when that
>   topic is hot. Rendered under a soft `_concept_communication_style_header`.
> - **Knobs.** `agent.communication_style_synthesis_enabled`,
>   `memory.concept_synthesis_max_comm_style_memories` (see
>   [`docs/configuration.md`](../configuration.md)).
>
> **Deferred (do not forget) — lighten the hard-coded persona.** The concept kind
> ships first; the actual *trimming* of the fixed persona style copy is a
> follow-up once style concepts have populated (mirrors how the aiko identity /
> value concepts shipped before the self-image persona copy was pulled). Candidate
> blocks in [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
> to soften/trim once the mined layer carries the load: **"How you talk:"**,
> **"Conversation rules:"** (the LENGTH rule + vary-openers), **"Leading vs
> following:"**, and the hedging sections ("Kill the survey hedge", memory-trust
> hedging). Also open: `core_always_on` pinning for style once tuned, and richer
> digest inputs (K14 engagement, K75 expertise, K20 pushback).

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

**Status: SHIPPED (kind-aware core lane + anti-nag rotation).** The
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

**Anti-nag rotation — SHIPPED (L23 cognitive-surfacing pass).** The core lane now
applies a *soft* habituation: it over-fetches, and a core concept surfaced within
the last few turns drops **behind** the fresh ones (both keeping the balanced
round-robin order), so when more concepts qualify than the cap allows the lane
*rotates* which ones show — but a concept is never suppressed out of contention
(the sole qualifier in a bucket always stays, gated by the gentler
`concept_surfacing_core_habituation_floor`). The flex lane uses the stronger
`concept_surfacing_habituation_floor`. State is a `kv_meta`
`{concept_id: last_surfaced_turn}` map on the `relationship.total_turns` clock;
see [`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py) and the
`memory.concept_surfacing_*` knobs.

**Remaining (deferred):** the optional live **tension** (L12) / fresh **drift**
(L17) override stays deferred with those entries. Legacy
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

## L28. Roll remaining derivers/workers onto the `ConceptView` contract (in progress)

**Status: in progress — substrate shipped (L24), first consumer migrated.** L24
shipped the reusable substrate ([`ConceptView`](../../app/core/concepts/concept_view.py) +
`kinds_for_target()` routing); the live consumers are `build_relevant_context`
(T3 core lane + relevance), `recall_concept`, and — new — `user_profile` ->
`profile_block`. This entry tracks migrating the *rest* of the
concept-overlapping consumers onto the same contract so none is forgotten and no
consumer keeps a bespoke read path into the layer.

**Migration order (decided).** Compose-first per consumer (concepts primary,
raw derivation as the floor) per the L24 stance. Start with the consumer that
overlaps an already-shipped kind and is most self-contained; `user_profile`
(overlaps `identity` + `value`) went first.

**Motivation.** The contract is only as valuable as its adoption: as long as any
deriver still reads `ConceptStore` directly (or re-derives evidence/cluster
labels itself), it can drift from the concept layer and re-introduce the
"two systems, two stories" risk L24 exists to prevent. One interface for every
background worker's resolutions (concept lookup + evidence/cluster/memory
grounding) is the goal.

**Key files (per consumer -> target).**
- **SHIPPED** — [`user_profile.py`](../../app/core/infra/user_profile.py) /
  `_render_user_profile_block` ([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)):
  `subject=user` identity **and** value concepts lead `profile_block` via
  `ConceptView.for_target("profile_block", subject="user")`, floored by the
  SQLite profile (which still owns the structured facts — name, occupation,
  location, hobbies, schedule). The SQLite `values` field is suppressed when a
  value concept exists (`skip_fields`), so the same claim isn't told twice. This
  retired the L10 deferral of user values into `profile_block` in one migration.
  Tunable via `profile_concept_max_lines` / `profile_concept_min_confidence`.
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
- **Additional candidates (noted while shipping user_profile).**
  - `communication_style` concepts (L-comm, both subjects) overlap the profile's
    `communication_style` field and the delivery-guidance cues — they surface via
    the T3 relevance path today; a `for_target` route into a delivery-style block
    (or the profile's `communication_style` line) would make them the source of
    truth the same way identity/value now are.
  - Opinion / stance injection (K29) could bias on `subject=aiko` value concepts
    (not only `kind="self"` memories) via `ConceptView.core(subject="aiko",
    kind="value")`, so her stance draws on stored values. Cross-ref the K29
    follow-up already noted under L10.

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

**Open questions.** Resolved for the shipped path: compose-first, `user_profile`
first. Remaining per-consumer question: whether a relevance-only kind
(`communication_style`) is worth a dedicated `for_target` block vs. leaving it on
the T3 path.

**Effort.** Medium, incremental (one small ticket per consumer).

---

## L29. Relationship & meta narratives (deferred)

**Status: deferred — spun out of L8.** L8 shipped narrative arcs for `user` and
`aiko` (arcs over each subject's *own* memories). This entry tracks the two
"both of us" / higher-order variants that L8 deliberately left out, plus the
recency non-goal so it isn't re-litigated.

**(a) Episodic shared arc** (`subject="relationship"`). A *closed* joint project
compressed into one named arc — "the month we rebuilt the memory system", "our
push to get voice mode working". Evidence is `shared_moment` (+ joint event)
memories ordered in time. This is the **same `sequence` machinery L8 already
ships**, just a third subject with the shared-moment stream as its source, gated
to closed + `narrative_min_chain` + aged so it stays small. Cheap follow-up now
that L8 has landed: a `_run_narrative_pass("relationship")` variant over
`iter_by_kind("shared_moment")` + a `narrative_relationship` proposer, rendered
under the (already-present) `relationship` branch of `_concept_narrative_header`.

**(b) Meta-narrative over concepts.** The genuinely hard one: an arc whose nodes
are other **concepts** rather than memories (`evidence_model="meta"` or a
`sequence` over `concept` nodes via `relation="references"`) — "how we went from
strangers to a comfortable rhythm" (over relationship + `ritual` concepts), "his
value X emerged, then reshaped into Y". Needs concept-node evidence + a
meta-proposer that reads [`ConceptView`](../../app/core/concepts/concept_view.py)
instead of the memory store, and a healthy population of active concepts
(L7/L10/L13) to draw on. Shares the `sequence` plumbing but not the source.

**Explicit non-goal (design decision).** A rolling "what have we been up to
lately / in the last two weeks" digest is **not** a concept — it never closes,
would churn/decay every turn, and would bloat the store. That recency question
is already served by the rolling conversation summary (`ThreadResummaryWorker` /
`get_latest_summary`), recent-message context, and the `shared_moment` "Together"
rows. Recorded here so the idea isn't re-proposed as a concept.

**Depends on.** L8 (shipped) for (a); L8 + a healthy L7/L10/L13 population for
(b). Cross-referenced from L8 and L20 (abstraction hierarchy).

**Effort.** Small for (a); large for (b).

---

## L30. Concept hypotheses -- "what I'm trying to understand" (provisional beliefs)

**Motivation.** Today the concept layer only ever speaks in the register of
*settled* knowledge. Surfacing reads **only `status="active"` concepts** (every
`ConceptView` read path -- `core` / `relevant` / `activated` / `for_target` --
hardcodes `status="active"`, and `ConceptStore.nearest` defaults to it), so a
`candidate` concept, or a low-confidence active one, is **structurally hidden**
rather than merely hedged. But that is exactly the material people treat as a
*hypothesis*: "I think he might be into X, but I'm not sure yet." A real mind
holds those open, reasons about them ("what I'm still trying to figure out about
you"), and -- crucially -- *acts* on them by getting curious and asking, then
folds the answer back in. This turns the concept layer from a static "What I
know about you" into a two-register model: settled beliefs **and** live
questions. It also directly attacks the cold-start problem (L21): a young graph
is mostly hypotheses, and letting Aiko *pursue* them is how she earns robust
concepts faster instead of waiting passively for evidence to accrete.

**Design stance.** Hypotheses are a **surfacing + curiosity register over the
existing `candidate` / low-confidence rows**, not a new concept kind or status.
The lifecycle already mints the raw material (L2 candidates carry `rationale` +
an LLM `confidence`; L3 owns promotion). What is missing is (a) a *separate,
strongly-hedged* read path that deliberately breaks the active-only contract for
this one lane, (b) wiring low-confidence concepts into the curiosity producers so
Aiko asks about them, and (c) capturing the user's answer back onto the *specific*
concept as evidence. Split into L30a/b/c so the read side can ship without the
elicitation loop.

**Guardrails (carry-forward from L21 / L22 / L5).** A hypothesis lane is exactly
the "blurt a half-formed model" failure L21 warns about, so it must stay quiet on
an immature graph, be capped hard (one or two at a time), be hedged *below* the
weakest current tier ("You're still trying to work out whether...", never "You're
fairly sure"), be visually/semantically distinct from the confident lane, and
never let a candidate leak into `profile_block` or the core lane. It must also
respect the K47 question-balance gate + L23 habituation so pursuing a hypothesis
never becomes an interrogation.

**Depends on.** L2/L3 (candidate + promotion machinery), L5 (tentative register /
confidence-scaled hedging), L6 (confirm/veto is the manual sibling of the
answer-capture loop), L15 (belief revision / the "did I get that wrong?"
question), L22 (spurious-concept guard), and the F2 `knowledge_gap` ask -> answer
-> retire loop as the pattern to mirror. Related: K9 curiosity seeds, K34 forward
curiosity, K47 question balance.

**Effort.** Medium overall (Small L30a, Medium L30b, Medium L30c); no schema
migration required -- `candidate` status already exists.

---

## L30a. Hypothesis surfacing lane (the read + render side)

**Motivation.** Give Aiko a distinct prompt block for her open questions about
the user (and herself), sourced from the concepts she is *not* yet confident
about, so she can reason with "what I'm still figuring out" alongside "what I
know". This is the standalone, lowest-risk slice: read + render only, no
behaviour change to how concepts are formed.

**Key files.**
[`concept_view.py`](../../app/core/concepts/concept_view.py) (new
`hypotheses()` read path -- the only place allowed to read `status="candidate"`
/ low-confidence `active` for surfacing);
[`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)
(`build_relevant_context` gathers a new hypothesis lane after the core/flex/
activation lanes; a new `_render_hypothesis_concepts` + header, sibling to
`_render_relevant_concepts`; dedup against `pinned_ids` / `seen_concept_ids` so a
concept is never both a firm impression and an open question);
[`memory_settings.py`](../../app/core/infra/memory_settings.py) (new knobs).

**Sketched approach.** Add `ConceptView.hypotheses(embedding=None, *, subject,
limit, max_confidence)` returning `status="candidate"` **plus** `status="active"`
rows under `hypothesis_max_confidence` (e.g. 0.6), ranked by turn relevance
(cosine when an embedding is given) with a light recency/novelty boost, excluding
`dormant` / `retired` / `contradicted` (those are faded or disproven, not open).
In `build_relevant_context`, gather this as a fourth, un-pinned source with its
own small cap (`context_budget_hypothesis_cap`, default 1-2) and its own budget
line so it can't crowd the confident concepts. Render under a dedicated header
("Things you're still trying to understand about {name} -- open questions you
hold lightly, not conclusions:" / first-person for aiko), with a
stronger-than-usual hedge ("you're wondering whether...", "you haven't pinned
down yet..."), optionally exposing `rationale` as *why* she's unsure. Gate on a
new `hypothesis_surfacing_enabled` and keep the L21 maturity gate (a hypothesis
block on a cold graph is the exact anti-pattern L21 forbids).

**Open questions.** (1) Do we surface bare `candidate`s, or only `active`
low-confidence rows (safer -- they at least cleared the promotion gate once)?
Leaning: candidates *and* low-confidence actives, but candidates need a minimum
source count so a single LLM hunch can't appear. (2) Should the hypothesis lane
relax the L21 maturity gate slightly (hypotheses are most useful during
cold-start) or share it exactly? (3) One combined block, or split user-subject vs
aiko-subject the way the confident lanes already do?

**Effort.** Small (read + render + budget; the lifecycle already produces the
rows).

---

## L30b. Curiosity-driven hypothesis testing (ask to firm it up)

**Motivation.** A hypothesis Aiko can *see* (L30a) is inert until she does
something about it. The payoff the user described is Aiko getting curious about
her own uncertainties -- asking a light question that turns a shaky candidate into
a confident concept (or kills it) -- so her model of the user gets robust through
conversation instead of only through passive accretion.

**Key files.** The producer seam is one of the existing curiosity paths (pick
one, don't add a parallel system):
[`knowledge_gap_extractor.py`](../../app/core/memory/knowledge_gap_extractor.py)
/ `KnowledgeGapStore` (the closest fit -- already models "open question,
confidence 0, retire on user answer");
[`curiosity_seed_worker.py`](../../app/core/proactive/curiosity_seed_worker.py)
(add an "UNCERTAIN BELIEFS" context section from low-confidence concepts);
[`wants_ledger_worker.py`](../../app/core/conversation/wants_ledger.py) (a
`concept:<id>` want, "find out whether {label}"). Surfacing + the anti-nag gate
live in [`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)
(K47 `_question_balance_suppressed`).

**Sketched approach.** A worker (or an extension of an existing curiosity worker)
selects the highest-value testable hypotheses -- low-confidence concepts whose
promotion is *one or two pieces of evidence away* -- and mints a question that
would resolve them, carrying **provenance** (`source_concept_id` in the row's
metadata; this is the hook L30c needs). Reuse the existing topic-graph +
novelty + K47 balance filters so it doesn't over-ask, and cap to at most one
concept-testing question per conversation. Frame it as genuine curiosity, not an
audit ("can I ask you something -- I've had a hunch that...").

**Open questions.** (1) Which producer -- lean `knowledge_gap` (has the retire
loop) vs a first-class `concept_hypothesis` curiosity object? (2) How to score
"testable" -- confidence in a band + distinct_source_count just under the gate?
(3) Rate limit: share the K47 question budget, or its own cooldown so it never
competes with K9/K34?

**Effort.** Medium.

**Depends on.** L30a (share the hypothesis selection), F2 knowledge-gap loop.

---

## L30c. Answer capture -- fold the reply back into the concept (close the loop)

**Motivation.** The genuinely tricky part the user flagged: when Aiko asks about a
hypothesis and the user answers, that answer has to land back on the *specific*
concept as evidence, or the whole loop is decorative. Today there is **no path**
linking a curiosity question to its answer for concepts -- the user's reply only
becomes an untargeted `fact`/`preference` memory (via the delayed batch
`MemoryExtractor` or an Aiko-chosen `[[remember:...]]` tag), and only a later
synthesis tick *might* reinforce the concept.

**Key files.**
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) /
[`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py)
(mirror `_resolve_knowledge_gaps` / `_resolve_curiosity_seeds` -- the existing
same-turn cosine resolvers -- with a `_resolve_concept_hypotheses`);
[`concept_store.py`](../../app/core/concepts/concept_store.py) (add a
memory->concept evidence edge + bump `last_reinforced_at`);
[`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)
(the existing single writer promotes candidate -> active off the fresh evidence);
[`concept_belief_reviser.py`](../../app/core/concepts/concept_belief_reviser.py)
/ `concept_contradiction.py` (the denial path).

**Sketched approach.** When a hypothesis-linked question (L30b, carrying
`source_concept_id`) is on the table, a post-turn resolver matches the user's
answer (cosine to the question, like the knowledge-gap resolver). On a
**confirm**: write the answer as a normal `fact`/`preference` memory *and* attach
it to the concept as an evidence edge + stamp `last_reinforced_at`, so the next L3
tick raises confidence and promotes the candidate through the ordinary gate. On a
**deny/correction**: route to the L15 belief-reviser / contradiction penalty
instead of reinforcement, so a wrong hunch fades quietly rather than lingering.
Either way the hypothesis is retired from the curiosity lane (metadata stamp) so
she doesn't re-ask. This is the piece that makes the two-register model *learn*.

**Open questions.** (1) Classify confirm vs deny vs "didn't really answer" --
cheap heuristic, a tiny LLM adjudication, or reuse the F5 conflict band? (2) Add
the evidence edge synchronously in post-turn, or enqueue a targeted synthesis
`reinforces_id` pass? (3) How long does an unanswered hypothesis question stay
open before it's dropped?

**Effort.** Medium.

**Depends on.** L30b (provenance on the question), L15 (belief revision for the
denial path), L3 (promotion off the new evidence).

---

## L30d. Uncertainty zones -- explicit known-unknowns to aim curiosity at

**Motivation.** L30a-c handle *tentative beliefs* ("I think X might be true").
This is the complementary register: **known unknowns** — "I don't yet know Y about
you" — tracked as first-class gaps, each with an **importance** (L32) so curiosity
targets what actually matters. Instead of "find random things I don't know," Aiko
reasons "there's something *important* about you I have weak evidence for." That is
meaningful curiosity, not trivia-collecting.

**Key files.** Sits between the concept layer and the F2 `knowledge_gap` store
([`knowledge_gap_extractor.py`](../../app/core/memory/knowledge_gap_extractor.py));
reads concept **importance** (L32) to rank; feeds the L30b curiosity producer;
surfaces (rarely) alongside the L30a hypothesis lane.

**Sketched approach.** Model an uncertainty zone as `{subject, dimension,
importance, evidence_strength}` — e.g. "preferred humour in professional contexts
(important, weak evidence)." Derive zones from (a) low-confidence / low-coverage
regions of the concept graph, and (b) dimensions the kind registry *expects* but
that are unpopulated for this user (a `value` / `boundary` slot with no concept).
Rank by `importance x (1 - evidence_strength)`; the top zone becomes a curiosity
target (L30b). Retire a zone once evidence crosses a threshold — at which point it
has become a real concept.

**Open questions.** (1) Enumerate expected dimensions per kind (a schema of
"things worth knowing"), or purely derive from graph sparsity? (2) Overlap with F2
`knowledge_gap` — extend that store, or a concept-level layer above it?
(3) How many zones stay "open" at once before it feels like an interrogation queue?

**Effort.** Medium.

**Depends on.** L30 (hypotheses / curiosity), L32 (importance is what makes a zone
*worth* asking about), F2 (the knowledge-gap ask -> answer -> retire loop).

---

## L31. Concept fission -- split a bimodal concept into contextual children

**Motivation.** The natural **inverse of merge** (L2 consolidation's `merge_into`)
and the missing precursor to generalization (L20). Sometimes a single concept is
carrying two truths at once: "user likes detailed answers" accretes evidence that
genuinely pulls both ways — long exploratory threads *and* terse "just fix it"
debugging. Today that only registers as **counter-evidence lowering confidence**
(L9/L15) — the belief gets shakier but never gets *smarter*. A **split** fissions
it into two contextual children ("detailed when exploring" + "concise when
troubleshooting"), redistributing the original evidence memories to whichever
child they support. That is exactly the pair of children the L20 generalization
pass then abstracts into "prefers adaptive depth by context" — so **split + generalize
together produce the L17c refinement narrative**. Without split, those children
have to be re-proposed from scratch by L2 and hope to line up; with it, the
lineage (and the evidence) is preserved, which is what makes the "I realised these
were two different things" reflection honest.

**Kind.** Not a new kind — a store/synthesis *operation* over existing concepts,
sibling to `merge_into`. Produces two `candidate` children of the same `kind` /
`subject` as the parent.

**Key files.**
[`concept_store.py`](../../app/core/concepts/concept_store.py) (new
`split_into(concept_id) -> (child_a_id, child_b_id)` primitive: create two
candidates, repartition the parent's `evidence_of` edges by sub-cluster
membership, then supersede the parent → `dormant`/`retired`; mirror the
`merge_into` tension-guard so the two freshly-split children aren't immediately
re-merged by consolidation);
[`concept_consolidation_worker.py`](../../app/core/concepts/concept_consolidation_worker.py)
(the detector belongs next to its inverse — same embedding/adjudication
machinery, opposite direction);
[`concept_store.py`](../../app/core/concepts/concept_store.py) evidence-edge
`polarity` field (already exists: `+1` support; a `-1`/contextual-counter split is
the cheapest bimodality signal);
[`concept_event_store.py`](../../app/core/concepts/concept_event_store.py) (emit a
`split` event on parent + `discovered` on each child, so L17 sees the lineage).

**Sketched approach.** Detect a split candidate when a concept's evidence set is
**cleanly bimodal**: cluster its evidence-memory embeddings (single-link, like
`ritual_grouping`) and look for two well-separated sub-clusters, *each* with
enough distinct sources to stand alone as its own concept; the L9 contradiction
signal (contextual counter-evidence, not a flat reversal) is a strong trigger.
Then a batched LLM adjudication confirms the split is *real* — "are these two
coherent context-dependent beliefs, or one belief with noise?" — and names both
children. `split_into` repartitions the evidence edges, seeds each child's
confidence from `confidence_target(distinct_source_count)` over its own subset,
and supersedes the parent. The L20 pass on the next tick can then form the parent
generalization over the two children.

**Open questions.** (1) **Fragmentation guard** — how high is the bar (min sources
*per branch*, minimum sub-cluster separation) so the graph doesn't shatter into
hyper-specific splinters? (2) **Oscillation guard** — split→merge→split loops: a
cooldown + treating just-split siblings as an expected *tension* (L12), never a
dedup target, like the existing `merge_into` co-base guard. (3) Split vs. just
letting L9 contradict-and-fade — when is fission the right move rather than
demotion? (4) Does the parent stay as a (now-superseded) node for lineage, or get
retired once both children promote?

**Effort.** Medium (leans on the consolidation worker's embedding + adjudication
machinery and the existing evidence-edge model; no schema migration — reuses
`concept_edges` + a new event type).

**Depends on.** L2 consolidation (`merge_into` as the inverse to mirror), L20
(generalization consumes the children), L9/L15 (counter-evidence as the trigger),
L17b/L17c (fission is a first-class change shape + learning event).

---

## L32. Concept importance -- a second axis, distinct from confidence

**Motivation.** Today a concept has one strength axis, `confidence` ("how likely
is this true?"). That conflates two different questions. "User likes TypeScript"
can be *high* confidence yet *low* stakes; "user might be struggling emotionally"
can be *low* confidence yet *high* stakes — something Aiko should hold gently but
weight heavily. Surfacing and curiosity should be driven by **confidence x
importance**, not confidence alone, or she chatters about certain-but-trivial
facts and stays quiet on uncertain-but-critical ones. This one axis unlocks the
hypothesis lane (L30) and uncertainty zones (L30d): "important but uncertain" is
exactly what should rise to attention.

**Key files.** New per-concept field on `Concept`
([`concept_store.py`](../../app/core/concepts/concept_store.py)) + DDL
([`chat_database.py`](../../app/core/infra/chat_database.py)); the surfacing blend
`SurfaceWeights` / `surface_score`
([`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py),
[`concept_kinds.py`](../../app/core/concepts/concept_kinds.py)) gains an importance
term; the L30a hypothesis lane, L30b curiosity selection, and L30d zones read it.

**Sketched approach.** Add `importance` `[0,1]`, distinct from `confidence` **and**
from the per-*kind* `salience` surface-weight (which is a kind prior, not a
per-concept stake). Seed it from kind (a `boundary` or an emotional-wellbeing
`affective` concept starts more important than a tooling preference), then let
evidence nudge it — emotional charge, how often it gates behaviour, user reaction.
Multiply it into the surfacing score and the curiosity value so "important but
uncertain" wins attention over "trivial but certain." Keep the two axes visually
separate in the debug UI so tuning stays legible.

**Open questions.** (1) Stored field the lifecycle writes, or derived each turn
from kind + affect + recency? (2) Interaction with plasticity — are important
concepts stickier? (3) What *lowers* importance (does a resolved worry decay)?

**Effort.** Medium-Large (a new axis threads through surfacing + curiosity + UI).

**Depends on.** L5 (surfacing), L30 (hypotheses/curiosity benefit most), L13
(affective concepts as an importance source).

---

## L33. Introspective reflection -- structured self-questioning that feeds concepts

**Motivation.** The existing `ReflectionWorker` mostly emits *memories*
(`open_question` / `reflection`). A richer periodic **introspection** pass would
ask the human questions — "What changed recently? What surprised me? What did I
predict wrong? Which assumption should I revisit?" — and route the answers into the
*concept* layer as hypotheses (L30), concept proposals (L2), or targeted questions
(L30b), not just free-text notes. This is the engine that turns raw experience into
structured self-revision: the equivalent of human introspection.

**Key files.** Extends / parallels
[`reflection_worker.py`](../../app/core/proactive/reflection_worker.py); consumes
the `[[predict:...]]` prediction tags, K6 surprise/novelty signals, and L17 drift
events; outputs into the L2 synthesis queue
([`concept_synthesis_worker.py`](../../app/core/concepts/concept_synthesis_worker.py))
and the L30 hypothesis / curiosity producers.

**Sketched approach.** A low-frequency worker runs four framed probes over the
recent window: (1) *what changed* — via L17 drift; (2) *what surprised me* — high
K6 novelty / affect spikes; (3) *what did I predict wrong* — resolved
`[[predict:...]]` tags that missed; (4) *what assumption to revisit* — active
concepts with recent counter-evidence (L9). Each probe yields **typed** output — a
hypothesis (L30), a concept proposal (L2), or a curiosity question (L30b) — never
just a diary line. Prediction-error (#3) is the highest-signal input and the
cleanest tie to the L17c "because."

**Open questions.** (1) Cadence + spend caps (LLM-heavy)? (2) Do the four probes
run every pass or rotate? (3) Cooldown per concept so it doesn't re-propose the
same "revisit" endlessly.

**Effort.** Medium (mostly orchestration over signals + producers that exist once
L30 lands).

**Depends on.** L30 (hypothesis/curiosity outputs), L17 (drift + prediction error),
L2 (proposal intake), K6 (surprise), the `[[predict:...]]` tags.

---

## L34. Concept relation taxonomy -- edges beyond support / tension

**Motivation.** The graph today expresses a few relations — support (`evidence`
edges, `+1` polarity), conflict (`tension` / `contradicts`), and abstraction
(`generalizes`, currently ridden on `evidence`). Human belief networks are richer:
*explains*, *is-example-of*, *depends-on*. Modelling these lets Aiko reason about
*structure* — "I believe X because Y explains it", "Z is one example of the broader
W", "this only holds if V" — which powers better "why" recall, cleaner
generalization (L20), and more precise contradiction propagation (L15).

**Key files.** `ConceptEdge.relation` is already a free-text column
([`concept_store.py`](../../app/core/concepts/concept_store.py)) — **no schema
migration**; the work is vocabulary + semantics: the conflict set
(`{"tension","contradicts"}`) and evidence walks in `concept_store.py` /
[`concept_view.py`](../../app/core/concepts/concept_view.py), the L20 generalization
pass, `recall_concept`'s related-links
([`rag_retriever.py`](../../app/core/rag/rag_retriever.py)), and the surfacing
headers ([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)).

**Sketched approach.** Introduce a small, *closed* relation set, each with defined
semantics: how it is **proposed** (a proposer or synthesis pass), how it
**propagates** (does `depends_on` mean disproving V weakens the dependent? does
`explains` boost recall grounding?), and how it **surfaces**. Start with the two
clearly-missing, high-value ones — `explains` (grounding / why) and `depends_on`
(conditional beliefs) — and formalise `exemplifies` as the inverse of the L20
`generalizes` relation rather than a brand-new edge. Rule: every new relation must
earn its keep with a surfacing or lifecycle behaviour, or it's just decoration.

**Open questions.** (1) Which relations pay for themselves vs. clutter the graph?
(2) Own proposers, or inferred from existing structure (e.g. `exemplifies` = the
child side of a generalization)? (3) How does `depends_on` interact with L15
propagation without triggering cascades?

**Effort.** Large (each relation ripples through proposal + lifecycle + surfacing).

**Depends on.** L12 (tension), L20 (generalization), L15 (propagation), L25 (edge
integrity).

---

## L35. Surface-reason labels -- "why did I surface this?" on every item

**Motivation.** L26 already stamps a per-turn trace of *which* concepts surfaced
(with confidence + hedge). L35 adds the *why*: a structured reason on every
surfaced item — `high-confidence identity`, `recent emotional relevance`,
`unresolved contradiction`, `curiosity trigger`, `relationship importance`,
`recently forgotten/revived`. This makes the prompt legible ("why is this here?"),
sharpens debugging, and — fed back — could let Aiko *reference* her reason ("this
has been on my mind because we clashed on it").

**Key files.** The gather lanes in `build_relevant_context`
([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)) already know
each item's lane + score; L35 records the dominant reason alongside the existing
L26 trace fields (the `hedge` / `reason` stamp), surfaced in the L26 debug view
([`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)).

**Sketched approach.** Define a small reason enum; when the selector picks an item,
tag it with the lane / signal that won it (core-confidence, flex-cosine,
activation-spread, contradiction-charge, importance (L32), curiosity / hypothesis
(L30), recency / revival). Cheap — the information already exists at selection time,
it just isn't labelled. Optionally expose one reason to Aiko for the single most
salient item.

**Open questions.** (1) One dominant reason, or a ranked few? (2) Is any reason ever
shown *to Aiko* (risk of over-narrating "I surfaced this because...") vs
debug-only?

**Effort.** Small (labelling existing selection signals; extends L26).

**Depends on.** L26 (trace), L32 (importance as one reason), L30 (curiosity as one
reason).

---

## L36. Strategy layer -- learned approaches, distinct from beliefs

**Motivation.** Concepts capture *beliefs* and *values* ("user enjoys detailed
explanations"); they don't capture *learned approaches* — the
context-conditioned behaviour policies a person derives from those beliefs ("when
explaining architecture, lead with trade-offs and implementation detail"; "during
troubleshooting, skip the philosophy and give concrete steps"). A `strategy` layer
makes behaviour consistent and context-appropriate **without** stuffing every rule
into the persona prompt, and it's the natural home for the self-corrections L17d
discovers.

**Key files.** Closest existing kind is L23 `communication_style` (how detailed,
lead vs follow, how much to hedge) — a strategy generalises it to `(context ->
approach)`. Would register a `strategy` kind
([`concept_kinds.py`](../../app/core/concepts/concept_kinds.py)) with its own
proposer + *conditional* surfacing that fires on the active context, consumed by
the same behaviour-steering path as L23.

**Sketched approach.** A strategy = `{trigger/context, approach, backing
belief(s)}`, linked to the belief(s) it derives from (an `explains` / `depends_on`
edge from L34 is the precise link). Proposed when a belief has stabilised *and* a
repeated successful behaviour correlates with it — or minted directly by L17d
self-correction. Surfaces **only when its context matches** the current turn
(troubleshooting vs. exploring), so the prompt carries the *relevant* policy, not
the whole rulebook. Promotes / decays via the normal lifecycle; a strategy whose
backing belief is contradicted (L15) is re-examined.

**Open questions.** (1) New kind, or an extension of `communication_style`? (2) How
is "this approach worked" measured (K14 engagement signals, user reactions)?
(3) Guard against over-fitting rigid rules that make her robotic — plasticity +
context-gating.

**Effort.** Large (new kind + conditional context-gated surfacing + an
effectiveness signal).

**Depends on.** L23 (communication_style), L17d (self-correction as a strategy
source), L34 (belief -> strategy edges), K14 (effectiveness signal).
