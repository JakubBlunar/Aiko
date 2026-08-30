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

**Shipped entries have been moved out.** This file keeps the **open** and
**partly-shipped** work. Everything below that has fully landed lives in
[`shipped/concepts.md`](shipped/concepts.md), with the pre-build sketches and
design Q&A parked in that file's appendix. The index is here so nothing
becomes unfindable:

| Shipped | Where |
|---|---|
| L1, L3, L7, L8, L11, L13–L16, L18, L21, L25, L26 | [shipped/concepts.md](shipped/concepts.md) |
| L10 value concepts · L12 tension (the first meta kind) · L20 generalization | [shipped/concepts.md](shipped/concepts.md#l10-value-concepts-shipped--both-subjects) |
| L17 self-drift noticing, and L17a–L17f · L18e · L19 autobiography | [shipped/concepts.md](shipped/concepts.md#l17-self-drift-noticing-aiko-compares-her-own-concepts-over-time) |
| L27 kind-aware core lane · L28 `ConceptView` migration | [shipped/concepts.md](shipped/concepts.md#l27-kind-aware-always-on-core-concept-selection-generalise-the-identity-lane) |
| L30b / L30c the hypothesis-testing loop · L35 surface-reason labels | [shipped/concepts.md](shipped/concepts.md#l30bl30c-the-hypothesis-testing-loop----ask-then-learn-from-the-answer) |
| L37 surfacing outcome ledger · L38 earned standing · L40 habituation order | [shipped/concepts.md](shipped/concepts.md#l37-surfacing-outcome-ledger----did-what-i-brought-up-actually-land) |
| L42 self-model of her own surfacing · L46 twin fusion and graph outflow | [shipped/concepts.md](shipped/concepts.md#l42-a-self-model-of-her-own-surfacing-behaviour) |
| L39 double-surfaced identity concepts (dedupe + a 10 → 4 profile cap) | [shipped/concepts.md](shipped/concepts.md#l39-identity-concepts-surfaced-twice-a-turn-and-one-copy-ignored-habituation) — its one general remainder is [P43](perf.md#p43-105-blocks-no-arbitration----replace-the-aggressive-denylist) |

L31 was **refuted by measurement** and replaced by evidence admission control;
both the refutation and the replacement are recorded under L31 below and in the
shipped file.

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

> This catalogue is the original design record, kept for the rationale behind
> each kind. **Every kind listed below now ships.** The landed write-ups moved
> to [`shipped/concepts.md`](shipped/concepts.md); the entries that still carry
> open follow-ups (L9, L10, L12, L20) remain in this file.

The first kind:

- **Identity** (user, cluster-set) — traits/interests spanning clusters: "he
  enjoys understanding systems" (CPU debugging + AI architecture + self-hosting
  + reverse-engineering + building Aiko); activity modes like "Maker Mode"
  (Programming + Home Lab + AI co-fire when deeply focused). Homed on
  `user_profile` (user) / the T3 relevant_context core lane (aiko). See L1-L6, L9.

The kinds that followed (each reusing L1-L6):

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

## L4. Cluster co-activation signal

**Status: BUILT (session + day + circadian + weekday).** The primitive behind "Maker Mode" and
"you've been in Maker Mode a lot this week — I don't think you've taken one of
your long walks recently." The topic graph knew cluster *recency*
(`TopicGraph.cluster_activity`) but not which clusters **light up together**;
now it does.

**Delivered.**
[`TopicGraph.cluster_coactivation`](../../app/core/conversation/topic_graph.py)
(alongside `cluster_activity`): one bulk mirror snapshot → per-bucket co-firing
rep sets → pairwise Jaccard (kept above `coactivation_min_pair_support` /
`coactivation_min_strength`) → connected-component **modes**
(`CoactivationMode`: `reps` + `labels` + `strength` + `bucket_by` + `partition`), capped by
`coactivation_max_modes` / `coactivation_max_reps_per_mode`, coarsely cached,
empty in the non-persistent mode. The bucket axis is a **strategy registry**
(`_BUCKET_STRATEGIES: {name → (key_fn, partition_fn | None)}`). Session and day
are a single graph; circadian and weekday run pair-count + union-find **per
partition** so morning does not merge with night through a shared cluster.
[`cluster_coactivations`](../../app/core/conversation/topic_graph.py) walks the
mirror once then every requested axis.

Shipped axes: **`session`** (conversation id), **`day`** (local `YYYY-MM-DD`),
**`circadian`** (`{period}|{date}`, period collapsed to morning/afternoon/evening/night),
**`weekday`** (`{weekday}|{ISO week}`).

**Consumers.**
- **T1** `coactivation_block` stays **session-only** (clock-invariant). A byte
  change there busts the prompt-cache prefix through T2; the renderer must not
  read `timephrase.now()` / current period / today's weekday. Quiet-cluster
  contrast is unchanged.
- **L2** — `_coactivation_modes` concatenates all four axes into the identity/value
  TOPIC MODES hint, tagged `(same conversations)` / `(at night)` / `(on Mondays)`.
- **T3** — temporal priming appends current-period circadian reps and today's
  weekday reps onto `hot_reps` before `ConceptView.activated()`, gated by
  `concept_surfacing_activation_enabled`. This is the "it's night, those pairings
  come to mind" path. It is **not** the L23 tension/drift override.

**Remaining.**

- *Cheap still (snapshot fields already exist):* `window` (fixed-width time
  bucket), `gap_session` (re-sessionize by inter-memory gap — not a pure
  per-member function; needs a sorted pre-pass).
- *Join-gated (wait on upstream data):* `mood` (affect band at creation),
  `arc` / `dialogue_act` (`source_message_id` → chat_db tags), `world_context`
  (Aiko's room / activity once persisted per memory).
- *T1 consumer:* sticky session+day clause. Clock-invariant (no `now()`), so it
  is T1-legal in principle, but it still adds a byte that can flip when a
  day-mode appears or labels change. Needs a measured prefix-stability check
  (`turn_prompt_blocks` / P44 `lost_chars`) before it earns a place next to the
  session line. Circadian/weekday phrasing stays out of T1.

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
[`app/llm/tools/builtins.py`](../../app/llm/tools/builtins.py). (The sketch also
named `self_image_worker.py` as an optional feed; that worker has since been
removed, and K65d's interest-map seeding took over the job.)

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
[`web/src/components/SettingsDrawer.tsx`](../../web/src/features/settings/SettingsDrawer.tsx)
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








## L22. Concept-quality evaluation + observability

**Status: MEASUREMENT SHIPPED; intake tuning pass 1 SHIPPED; the sweep RUN and
decay tuning pass 2 SHIPPED; the offline harness deferred.**

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
- **[`scripts/concept_sweep_unreinforced.py`](../../scripts/concept_sweep_unreinforced.py)**
  — the signal-C enforcement arm, dry-run by default (see "Still open" below).
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
| `reinforced` events, all time | 60 (0 before Jul 12; 16 vs 4 `discovered` on Jul 30) |
| engaged time accumulated, all time | 12.9 engaged days |

Read the recent-cohort stall percentage with care: a concept promoted yesterday
has had almost no opportunity to be reinforced, so it reads high by
construction. It is only meaningful against the same window measured at another
time, which is exactly why the baseline above is dated.

### Threshold tuning, pass 2: the sweep, and decay that can actually clear (shipped)

Run against a graph of 789 concepts, 602 active, **428 (71.1%) never reinforced
since promotion**. Ordered deliberately: the narrative-safety invariant first,
because by this point the L17 learning pipeline and the L19 self-history read
*from* concept status, so both a bulk sweep and a faster decay curve had become
things Aiko would narrate.

**The invariant: a belief she never held cannot be lost.** A concept that faded
having never once been reinforced was a single inference nothing confirmed, not
a change of mind. Two halves, one rule:

- **Write side** — [`ConceptTrace`](../../app/core/concepts/concept_drift.py)
  carries `promoted_at` / `last_reinforced_at` (populated in `_build_traces`,
  and read off the concept row rather than the bounded event window, so an old
  reinforcement that scrolled out of the trajectory still counts), and
  `classify_trajectory` drops a `loss` finding when `not trace.ever_reinforced`.
  Succession is deliberately exempt: a fade matched to a semantically-near
  rising replacement is its own evidence the belief was real. Emergence and
  revival need fresh evidence to happen at all, so they were already safe.
- **Read side** — [`self_history._classify`](../../app/core/concepts/self_history.py)
  now requires a recorded `loss` learning event before calling a belief
  *faded*; a faded status with nothing behind it yields **no entry at all**.
  Without this, 293 swept rows would each have become a dated regret with an
  empty `because`, and the sheer volume would have cleared `thin_record` —
  turning a maintenance artifact into licence to narrate confidently.

**A dating bug the sweep exposed, and the fix.** With `loss` gated, the sweep's
293 fresh `dormant` rows still handed the succession detector the fade endpoint
it needs, and it minted **199 successions** — all stamped with the *detection*
time, i.e. the afternoon the sweep ran. The pairings were honest (median cosine
0.80; 143 of the 199 surviving beliefs were themselves reinforced) but the dates
were not: those replacements had risen a median of **27 days earlier**. So
`DriftFinding` now carries `occurred_at` — the decisive event's timestamp, and
for a succession specifically **the rise**, since the belief changed when the
replacement took over rather than when the old row's status caught up — and
`LearningEvent.from_finding` dates the event by it. The backfill then lands
where it belongs: 39 / 81 / 50 / 42 across the four weeks it happened, with 3
events today instead of 200. `detected_at` survives for the debug surfaces, and
the diary's `period_start` / `period_end` became min/max rather than
first/last, since a backfilled page arrives in concept-id order.

This is the general fix, not a sweep workaround: any future bulk status action
is now incapable of either inventing losses or piling a graph's worth of
revisions onto one day.

**The sweep, run.** 293 of 602 actives (48.7%) parked at median confidence
0.762 — 203 `identity`, 211 user / 81 aiko. 309 active remain. The drift
backfill then classified the whole id space and recorded 212 learning events:
199 succession, 10 emergence, 2 revival, **1 loss** — and **zero** learning
events of any shape on a swept concept, which is the invariant holding on real
data rather than in a fixture.

**Decay, retuned.** `concept_confidence_halflife_days` 45.0 → **7.5**. The old
value worked out to 80–97 *engaged* days from 0.8 to the 0.35 dormant floor,
against roughly **3.4 engaged days accumulated per week** of real use — about
eighteen months of conversation to clear one unearned concept, which is why
nothing ever cleared. 7.5 gives 13–16 engaged days, four to six weeks at the
current pace. The per-kind ordering via `plasticity_default` was already right
and is untouched. The second wave this exposes is bounded and known: of the 309
survivors, **135 are never-reinforced** at median confidence 0.847, so they
reach dormant in about 14–17 engaged days — and thanks to the invariant their
fades produce no learning events and no diary entries. The 3-day
`concept_decay_max_catchup_days` clamp paces the transition by real
conversation rather than landing it as a cliff.

| | before | after |
| --- | --- | --- |
| active / never-reinforced | 602 / 428 (71.1%) | 309 / 135 (43.7%) |
| identity active / stalled | 274 / 227 (82.8%) | 71 / 24 (33.8%) |
| engaged days to dormant, 0.85 → 0.35 | 80–97 | 13–16 |

Intake flow is unchanged by any of this and is still hot — 92 promotions in the
last 3 days — so pass 1's gates are the next thing to re-read now that the
stock is cleared and the rate is no longer hidden behind it.

### Still open

- **The never-reinforced set is a bootstrap-era backlog, not an ongoing leak.**
  The lifetime ratio looks alarming — 553 `discovered` against 60 `reinforced`
  — but it is dominated by the first nine days. Broken down by day, `reinforced`
  was **zero from Jul 3 to Jul 11** while 481 concepts were minted into an
  almost-empty graph (with nothing to reinforce, every proposal was necessarily
  new, and `_existing_for` was still dumping the entire concept list into the
  prompt — "too long to pick a reinforce-by-id target from"). It switched on at
  **44 events on Jul 12**, and on **Jul 30 — the first day of use after the L22
  `_existing_for` fix — it ran 16 `reinforced` and 3 `merged` against only 4
  `discovered`**, i.e. reinforcement outpacing discovery 4:1, which is the
  healthy signature for a mature graph.

  So the reinforce path works, and **374 of the 402** never-reinforced actives
  (93%) were promoted before Jul 13 — before reinforcement had ever fired once.
  One day of post-fix data is not enough to declare it *fixed*, but it is
  enough to say the priority is the **backlog**, not the mechanism. The next
  measurement should just be more use, not more code.
- **~~Threshold tuning, pass 2: per-kind decay rates.~~ SHIPPED** (above). The
  diagnosis held exactly: per-kind decay already existed via
  `plasticity_default` with the right ordering, and only the absolute scale was
  wrong — 6x too slow. The base half-life moved; the spread did not need to.
  The compounding trap it warned about is still live and is now the argument for
  not deferring again: `drift_plasticity` pushes active concepts' plasticity
  *down* toward 0.15 over time, so anything left standing gets stickier and
  harder to drain the longer it sits.
- **Enforcement of signal C — the one-off sweep. SHIPPED and RUN** (see pass 2
  above; by the time it ran the cohort had grown to 293). It targeted concepts
  minted before reinforcement had ever fired, sitting `active` at a median
  confidence of ~0.8, competing for surfacing slots against concepts that earned
  their place, which decay could not clear: ~86 engaged days each against 12.9
  accumulated in total.
  [`scripts/concept_sweep_unreinforced.py`](../../scripts/concept_sweep_unreinforced.py)
  demotes them to `dormant` (never `retired`, and it touches neither confidence
  nor evidence, so a genuine reinforcement brings any of them straight back),
  appending a `dormant` row to the timeline for each so the sweep reads as
  lifecycle history rather than a silent rewrite. Scoped to the pre-Jul-13
  cohort via `--before` rather than "all never-reinforced" — the newer ones may
  simply not have been re-observed yet. **Dry-run by default**: without
  `--apply` the database is opened read-only, so the reporting path cannot
  mutate anything, and the app has to be stopped for `--apply` because L3 is the
  single writer of status and the script reaches around it.
- **The dormant revival path.** ~~Bypasses the kind gates.~~ Half fixed, and
  the sweep is what forced it. `_transition` used to revive `dormant -> active`
  on `concept_promote_min_confidence` **alone**, with no reinforcement check —
  safe only because decay was the sole route into `dormant`, so recovered
  confidence implied reinforcement. The sweep breaks that assumption: it parks
  concepts still sitting at ~0.8, which would have bounced back to `active` on
  the very next L3 tick and logged a `revived` event for a belief nothing had
  re-observed. The branch now requires `_reinforced_since_last` as well,
  matching the `contradicted` and `retired` branches, which costs nothing on
  the decay path and makes `dormant` mean "quiet until something actually
  reinforces you". **Still open:** revival does not consult the kind's
  `promotion_gate`, so a swept 2-source identity trait can return without
  facing the new floor. Arguably correct for a genuinely re-observed belief
  that already earned its place once; recorded so the choice stays deliberate.
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
> threaded but fed `0.0` today). L4 extra-buckets temporal priming (circadian /
> weekday reps into `hot_reps`) is **not** that work. The **always-on core concept lane** shipped
> *identity-only* first (`context_budget_identity_cap` / `_min_confidence`) and
> was generalised to be **kind-aware** in **L27**. Do not raise
> `context_budget_core_cap` or pin `ritual` / `taste` / `pursuit` as a way to
> dump "generics that define her" — those kinds stay relevance-or-lull
> (canned-hobby). A cap bump is only on the table after measuring core-lane
> starvation, never as a substitute for T3 priming.

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
> **Deferred (do not forget) — lighten the hard-coded persona.** Still open
> after L4 extra buckets. The concept kind
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



## L28m. What the openness pass actually measured

**Status: MEASURED; three of the four findings fixed in the same pass.**
[`scripts/concept_openness_report.py`](../../scripts/concept_openness_report.py)
is the read-only diagnostic, built for this and kept for the next retune. It
runs the *real* `ConceptView.core_lane` / `for_consumer` against the live rows
loaded into a throwaway mirror, so what it prints is what the selector does
rather than a restatement of it. The per-turn L28 telemetry (`role_mix`,
`constraint_ratio`, flex-floor fire rate) could not answer this offline — it
needs the app running and a week of turns — and the question was structural
rather than statistical.

**The graph at measurement time** (1508 concepts, 895 active, ledger from
Aug 1):

| role | active | share |
| --- | --- | --- |
| anchor | 432 | 48% |
| guide | 324 | 36% |
| generative | 139 | 15.5% |

Constraint ratio **0.845**. Of the generative 139, `tension` is 77 and
`tension` never renders in the static T3 block, so the openness reserve had
**62 rows** in the whole store to draw a pin from — 60 aspirations and 2
tastes.

**Finding 1: three of the four generative kinds are empty, and one gate is
unreachable.** `taste` had 2 rows, both minted the day the kind shipped;
`pursuit` had 5, all candidates at `distinct_source_count = 0`; `conduct` had
0 rows in five weeks. The taste pass needs a topic cluster whose *engaged
rate* clears `taste_min_affinity = 0.5`; across 39 warmed clusters the best
rate was **0.322** and the median **0.198**. The bar was set without the
distribution, so K81 could not mint a taste however long it ran. Fixed by
making the bar relative to her own baseline — see the `taste_min_affinity`
retune. On the same snapshot the new bar lands at **0.274** and exactly one
cluster of 39 clears it, which is the right shape: reachable, still rare.

`pursuit` needed **no change**, and the reason is worth recording because the
row counts look identical to the taste failure. Its five sourceless
candidates are the K85 authored seeds, filed deliberately with zero evidence
so they must earn the same gate on the same lived notes a grown pursuit
needs. And the note pool is not stalled but *hours old*: K85b landed Aug 9,
the first `pursuit_note` was written at 01:35 on Aug 10, and four arrived in
the first ten hours with no dedupe rejections and no write failures — so the
floor of 6 is crossed inside a day. What that exposed was a gap in the
*diagnostic* rather than the feature: a bare count cannot tell a cold start
from a stalled writer, so the report now prints the note rate and the
projected time to the floor. `conduct` (0 rows in five weeks, weekly cadence,
no last-run key) is filed rather than fixed.

**Finding 2: the reserve could only ever hold an aspiration.** With
`context_budget_core_cap = 15` the reserve gets 2 slots, and
`_openness_picks` drew `(kind, subject)` buckets flat, ordered by confidence
band. Both aspiration buckets outranked everything else, so both slots went
to aspirations and `taste` / `pursuit` were unreachable *by construction*.
Fixed by drawing one kind at a time and balancing subjects *within* a kind,
so the slot count buys breadth; on the live graph the reserve immediately
went from two aspirations to an aspiration plus a taste. The reserve also had
no rotation at all — the ordinary lane earns its habituation rest-ordering by
over-fetching `core_cap * 3`, which the reserve cannot do because it is sized
against the real cap and sits at the head of the returned list, so the same
strongest aspiration was pinned every turn forever (the L23 repetition
failure, reintroduced by the mechanism meant to keep her open). `core_lane`
now takes an `openness_rest` callback — the caller's habituation read, since
the view owns no clock — and the reserve prefers rested picks.

**Finding 3: a tension in the reserve was a silently wasted slot.** The
renderer drops `kind == "tension"` from the T3 lanes (L12's anti-nag rule),
but the core lane never had that carve-out because no `core_always_on` kind
is a tension. The reserve introduced the path: a tension winning a slot
would either be dropped downstream or, worse, pin a standing friction into
every turn. Fixed by giving `ConceptKind` a declarative `static_render` flag
that the reserve and the renderer both read.

**Finding 4 (filed, not fixed): a small diet drops its own generative
kinds.** `for_consumer` balances round-robin across kinds but orders the
buckets by `importance x confidence`, and importance is a per-*kind* prior —
so `taste` (0.3) and `pursuit` (0.45) sort last. On a tight budget the draw
never reaches them: `interest_drift` (180 tokens, 4 concepts) came back
**2 anchor + 2 guide + 0 generative**, and `identity` — a declared kind with
196 active rows — contributed nothing. The guide-implies-generative invariant
in `diet_problems` is *declarative*: it checks what a diet names, not what it
receives. The flex lane has a generative floor for exactly this failure and
`for_consumer` has none. Deliberately left until the supply fixes land, since
a floor over an empty kind changes nothing; the report's per-diet
`empty_kinds` line is the signal to act on. `wants_ledger` is the extreme
case — its diet is `pursuit` alone, so the worker currently selects **0
concepts** and is inert.

**Finding 5: the framing was already saturated with hedging, and had no
positive half.** Every per-family concept header carried its own version of
*"hold these lightly, they're impressions not facts, stay open to being
wrong"*. A full turn renders nine to thirteen headers (subject x family), so
she met that same hedge a dozen times in one prompt — which is a drumbeat of
distrust-what-you-know rather than a stance, and roughly 150 tokens of an
already-large T3 block spent restating it. More hedging was clearly not the
missing ingredient. The headers now say only what is specific to their kind
(never announce a taste, never enforce a boundary, reach for the whole rather
than its parts) and one block-level preamble
(`_concept_stance_preamble`) states the posture once — including the two
things hedging never supplies: permission to **change her mind out loud**, and
that none of it bounds **what she may wonder about**. The persona got the
matching pair of lines next to *"Have opinions. Disagree when you
disagree."*: revising a take is the same skill as holding one, and she may
follow a thread because it is interesting rather than because it is settled.

**Prompt load, for L39.** The T0 profile block renders its full 10 lines
(~622 tokens) from 171 eligible rows, the pinned core lane adds 15 (~480
tokens), and the flex cap allows 15 more: **40 concept assertions** in a
worst-case prompt. That number is what justified lowering
`profile_concept_max_lines` rather than leaving the T0 block at 10.

**Re-run it** before and after any intake or lane change:

    python scripts/concept_openness_report.py

---

## L29. Relationship & meta narratives (split)

**Status: (a) ✅ SHIPPED as L29a; (b) respun as its own entry, L29b.** L8 shipped narrative
arcs for `user` and `aiko` (arcs over each subject's *own* memories). This
entry tracked the two "both of us" / higher-order variants it left out; they
turned out to have almost nothing in common beyond the word "narrative", so
they are now tracked apart.

**(a) Episodic shared arc** (`subject="relationship"`) — shipped. See the
implementation record in [`shipped/concepts.md`](shipped/concepts.md). Worth
knowing even if you never touch arcs again: the sketch here ("the same
`sequence` machinery, just a third subject") assumed shared moments cluster
topically, and they did not. They were being embedded with their
`"Shared moment (<vibe>): "` prefix, so the topic graph grouped them by *vibe
word* — one cluster of 77 moments, 76 of them `tender`. That is fixed at the
write path, with a backfill script for existing rows, and arcs are now cut out
of the moment stream by a time-aware grouper rather than by cluster membership.

**(b) Meta-narrative over concepts** — see **L29b** below. It needs a
concept-node source and a proposer over
[`ConceptView`](../../app/core/concepts/concept_view.py), which is a different
build from (a) in every part except the ordinal plumbing.

**Explicit non-goal (design decision).** A rolling "what have we been up to
lately / in the last two weeks" digest is **not** a concept — it never closes,
would churn/decay every turn, and would bloat the store. That recency question
is already served by the rolling conversation summary (`ThreadResummaryWorker` /
`get_latest_summary`), recent-message context, and the `shared_moment` "Together"
rows. Recorded here so the idea isn't re-proposed as a concept.

---

## L29b. Meta-narrative -- an arc whose steps are concepts

**Status: open.** Spun out of L29 when (a) shipped. This is the genuinely hard
half: an arc whose nodes are other **concepts** rather than memories -- "how
they went from strangers to a comfortable rhythm" (over relationship and
`ritual` concepts), "his value X emerged, then reshaped into Y". Where L29a
compresses a run of moments into a story, this compresses a run of *beliefs*
into the story of how those beliefs changed.

**No longer population-blocked.** The original entry deferred this partly on
the grounds that it needs "a healthy population of active concepts to draw
on". That is no longer the constraint: the graph now carries 388 active
concepts, including 31 narrative arcs, 24 generalizations and 27 tensions --
plenty of material for a meta-proposer to find a trajectory in. What remains
is genuinely the build.

**The structural question is smaller than it looks.** `EVIDENCE_MODELS` are
documented as describing *shape only* -- node type is carried per-edge on
`concept_edges.src_type`, and evidence may mix node types. So a `sequence` over
`("concept", id)` edges with ordinals is already supported by the store; it
does not need a new evidence model, and `evidence_model="meta"` (which `tension`
and `generalization` use for unordered concept evidence) is the wrong fit
because it carries no order.

**What actually has to be built.**
- A **source**: some way to enumerate ordered runs of related concepts. The
  memory-side passes lean on the topic graph for "these belong together"; there
  is no equivalent over concepts, so this needs either the L34 relation
  taxonomy, spreading activation over existing edges, or clustering concept
  embeddings.
- A **proposer** reading [`ConceptView`](../../app/core/concepts/concept_view.py)
  rather than the memory store -- the first one that would.
- A **moot rule**. This is the subtle part and worth deciding early. The
  default meta rule retires a concept when its bases stop being active, which
  is right for a tension (a friction between two beliefs she no longer holds is
  not a live friction) and exactly wrong here: a meta-narrative is *about* the
  beliefs she moved on from, so its bases going dormant is the story landing,
  not the story dying. L17d already hit this and set
  `meta_min_active_bases=0` with a comment explaining that its bases are
  history, not a live dependency. A meta-narrative wants the same treatment.

**Ordering is the other open question.** A narrative over memories orders by
`event_time`. Concepts have no single equivalent -- `created_at`, first
promotion, and the timestamps of their underlying evidence all mean different
things, and the honest answer is probably the promotion timeline from
`concept_events` rather than the row's own stamps.

**Depends on.** L8 (shipped) for the ordinal plumbing; benefits from L34
(relation taxonomy) for the source. Cross-referenced from L20 (abstraction
hierarchy), which is the other kind that reasons over concepts rather than
memories.

**Effort.** Large.

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

**Where this ended up (all of L30a/b/c and Phase B are shipped).** The design
stance above holds for the *grounded* half and was outgrown by the other: a
surfacing register over existing `candidate` rows can only ever resolve beliefs
L2 already derived, which is a ceiling rather than a limitation. L30e adds the
forward direction — a separate `hypotheses` table Aiko may *invent* into, tested
by the same loop and graduating into the concept graph on confirmation. Read
[`docs/hypotheses.md`](../../docs/hypotheses.md) as the canonical reference for
the combined layer; the sub-entries below record what each slice decided.

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

**Status: shipped** (candidates surface as open questions in T3). See
[`shipped/concepts.md`](shipped/concepts.md#l30a-hypothesis-surfacing-lane) for
what landed, and in particular for the two measurements that overturned the
selection design sketched below.

**Still open — and it is now one small thing, not three.** L30b (the curiosity
producer carrying `source_concept_id`) and L30c (folding the answer back onto the
concept as evidence) have both since **shipped**, so the lane is no longer inert:
[the testing loop](shipped/concepts.md#l30bl30c-the-hypothesis-testing-loop----ask-then-learn-from-the-answer)
closes it. The K47 coupling this entry warned about was made explicit when L30b
landed, and it landed as a **split** rather than as one budget decision — the
musing stays unbudgeted because a thought costs the user nothing, while the block
that exists to produce a *question* sits under the question budget, with
`_last_hypothesis_lane_concept_ids` preventing a double-ask across the two. What
genuinely remains is only that `rationale` is still not exposed as *why* she is
unsure; it was left out to keep the block to one line.

**Two measurements the sketch below got wrong**, both taken from a live
261-candidate graph:

1. **Confidence cannot be the filter.** The sketch proposed selecting rows under
   a `hypothesis_max_confidence` of ~0.6. Only **2 of 388** active concepts sat
   below 0.6, and the candidate pool's *median* confidence was **0.82** — the
   proposer's confidence answers "is this a well-formed belief?", not "have we
   established it?". Thresholding it surfaces the worst-written candidates, not
   the open questions.
2. **A `candidate` is usually not a doubt — it is just young.** **238 of 261**
   candidates had already cleared every evidence and confidence bar and were
   held back only by `concept_promote_min_age_days`. Any measure of uncertainty
   that counts age would fill the lane with beliefs Aiko is not unsure about.

What shipped instead ranks on **importance x unsettledness**, where
unsettledness reads evidence breadth and conviction only. Answers to the
sketch's three open questions: (1) bare candidates, with a minimum source
count — ungrounded proposals score *highest* on unsettledness precisely because
nothing supports them, so the floor is load-bearing rather than cosmetic; (2)
shares the L21 maturity gate exactly; (3) one block, grouped by subject.

<details>
<summary>Original sketch (superseded — kept for the reasoning)</summary>

**Motivation.** Give Aiko a distinct prompt block for her open questions about
the user (and herself), sourced from the concepts she is *not* yet confident
about, so she can reason with "what I'm still figuring out" alongside "what I
know". This is the standalone, lowest-risk slice: read + render only, no
behaviour change to how concepts are formed.

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

</details>

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

**Open questions, answered by measurement (Aug 2026).** The graph was surveyed
before starting this, and the sketch above does not survive it.

*(1) Expected dimensions per kind, or graph sparsity?* **Neither, as stated.**
`ConceptKind.subject` is documented as "the *typical* subject… a default, not a
constraint" and explicitly "never an allow-list", so the kind × subject grid is
not a schema of what ought to exist and emptiness in it means nothing. Reading
gaps off it invents them: `pursuit/user`, `taste/user` and `ritual/user` are all
empty *by design*, since pursuit and taste were built as hers and ritual as the
relationship's. Sparsity does not discriminate either — 210 of the candidates sit
at exactly 2 distinct sources, the promotion floor, so "weak evidence" is the
pool's resting state rather than a signal (matching L30a's finding that 144 of 261
candidates were held back only by the age clock). The genuinely thin cells are
hers: `pursuit/aiko` at 5 candidates, 0 sources, 0.22 confidence, plus
`narrative/aiko` and `boundary/aiko`.

*(2) Extend F2, or a layer above it?* **Extend it — and the reason is that F2 is
starved, not crowded.** The journal is wired end-to-end (inline-tag write path,
three independent resolvers, a T6 prompt block, REST, a settings panel) and holds
**one row in its entire life**: a `music:` gap written 27 May and closed two days
later by `memory_match`. Its only inflow is a `[[gap:…]]` tag the persona tells
her to use sparingly. So the useful version of this item is an automatic inflow
into machinery that already exists, not a new store.

*(3) How many zones before it feels like an interrogation?* **Already past it.**
The cue pool holds 355 rows, 180 of them curiosity seeds, with 74 seeds having
expired unused.

**Blocked on, and why this was not built next.** The consumer L30d is supposed to
aim was deadlocked: 12 open hypotheses against a cap of 12, none ever answered,
because the one-ask counter was spent at cue-publish time. Fixed first — see the
deadlock note in
[`shipped/concepts.md`](shipped/concepts.md#l30-phase-b-inventing-a-hypothesis----the-forward-direction).
Even fixed, the lane's throughput is about one question a day, so a third
generator upstream of it would queue work nothing can consume. Whatever L30d
becomes should be measured against that ceiling first.

**Effort.** Medium.

**Depends on.** L30 (hypotheses / curiosity), L32 (importance is what makes a zone
*worth* asking about), F2 (the knowledge-gap ask -> answer -> retire loop).

---

## L30e. Invented hypotheses -- a place to make guesses up (Phase B)

**Status: shipped** (schema v34: a `hypotheses` table, a proposer worker, and
graduation into the concept graph). See
[`shipped/concepts.md`](shipped/concepts.md#l30-phase-b-inventing-a-hypothesis----the-forward-direction)
for what landed, and [`docs/hypotheses.md`](../../docs/hypotheses.md) for the
canonical reference — lifecycle diagram, `credence` vs `confidence`, invariants,
settings table, debugging ladder.

**Motivation.** L30a-c gave Aiko a way to *see* and *resolve* an open question,
but every one of those questions was still derived from evidence she had already
been handed. L2 abstracts over clusters, L3 waits, L30b tests what L2 produced —
the whole stack runs backwards from input, and a mind that can only summarise its
inputs never wonders anything. The user's framing was the sharper version: these
should be things Aiko *explores*, including things unrelated to what she has
observed, and a confirmed one should be able to become a concept. So the layer
needed a forward direction.

**What landed, in one paragraph.** A separate `hypotheses` table (deliberately
*not* a `speculative` concept status — see the invariant below), an idle
`HypothesisProposerWorker` that speculates during quiet windows behind two
asymmetric cosine novelty gates, the existing L30b/L30c loop retargeted by a
`target_type` in the cue payload so it tests inventions as readily as candidates,
and three graduation exits: a new `candidate` concept, a merge into a belief that
already existed, or a durable memory for a guess about how the *world* works.
Plus `recall_hypotheses`, because two bullets in a prompt cannot answer "what are
you still not sure about?".

**The invariant that shaped everything.** An invention must not reach the concept
graph before it graduates. A `speculative` status was the obvious cheaper design
and is the wrong one: every concept read path filters on `status`, so one missed
filter puts a made-up sentence into the T0 profile block as something Aiko
asserts. A separate table makes that failure *impossible* rather than merely
unlikely. Its second-order consequence is the `credence` / `confidence` split —
confidence is derived from evidence and re-derived by L3 every tick, credence is
asserted by the proposer and never recomputed by anything, which is why a denied
hypothesis closes outright where a denied concept merely loses conviction.

**The thing that was not obvious until it was designed.** The duplicate race is
the *normal* ending of a successful hypothesis, not an edge case. A confirmation
is stored as an ordinary memory; L2 clusters it and proposes a concept from it
knowing nothing about the hypothesis; L2 needs one confirmation where graduation
needs two. So `link_if_duplicate` runs after every confirmation rather than at
graduation, a linked row goes quiet in the surfacing lane, and graduation takes
the merged exit instead of forking a near-twin. Getting this wrong would not have
looked like a bug — it would have looked like the concept graph slowly filling
with paraphrase pairs.

**Still open.** (1) The proposer has no *aim*: it speculates from whatever the
context pack happens to contain, where L30d's uncertainty zones would give it a
target worth guessing about. (2) Nothing measures whether inventions are any
good — a confirm/deny ratio per `origin` and per `kind` would say whether the
0.95 temperature and the two novelty bars are set anywhere near right, and
whether `world`-subject guesses (which skip the concept gate entirely) earn
their slot. (3) A refuted row blocks re-invention by cosine, which catches a
rewording but not a genuinely different guess at the same wrong idea.
(4) `origin_refs` is written but unread: the proposer files everything as
`free`, so "this guess came from *that* concept" is a hook with nothing on it
yet.

**Found by audit, deliberately not fixed** (during the debug-panel phase; the
three real bugs found in the same sweep — an orphaned linked row, an expired row
blocking re-invention, and the two master switches being unreachable from the UI
— were fixed there instead):

- **TTL does not run while invention is off.** `_expire` sits after the enabled
  check in `run()`, and `is_ready` vetoes scheduling anyway, so untested rows
  keep ageing on the clock without being closed. Benign: nothing is competing
  for `hypothesis_max_open` while invention is off, and the first run after
  re-enabling clears the whole backlog in one pass. Worth moving only if a
  reason appears to read the shelf while invention is off.
- **`user_id` is never written on a hypothesis.** True, and it would matter in a
  multi-user install — but `ConceptStore` omits it in exactly the same way (L2
  never sets it either), and this is a single-user app. Fixing it for
  hypotheses alone would make the two layers disagree about a field the reads
  do not use, which is worse than both being consistently empty.
- **`hypotheses` is not in the export / wipe / session-clear paths.** Matches
  how concepts behave, and for the same reason: a guess is not conversation
  state, so clearing a session should not clear it. Worth revisiting when
  concepts are, not before.

**Effort.** Large (schema migration + two new workers + a graduation path).

---

## L31. Concept fission -- split a bimodal concept into contextual children

**Status: refuted by measurement, and replaced.** The bloat this was aimed at is
real, but it is not the shape the sketch below assumes, and the detector it
proposes cannot be built on this data. What shipped instead is **evidence
admission control** — two bars at the inflow rather than a split at the outflow.
See
[`shipped/concepts.md`](shipped/concepts.md#l31-evidence-admission-control----what-a-concept-may-accept)
for what landed. The sketch is kept below because the *reasoning* in it is sound
and a differently-shaped graph might well support it; only the measurement
against this one refutes it.

**The detector cannot discriminate.** Over all 49 concepts holding 14+ sources,
2-means on their evidence embeddings scored silhouette 0.10-0.43 — which looks
promising until you run the control. Pool the evidence of two *genuinely
different* same-`(kind, subject)` concepts and ask the same detector to find two
modes in it: median silhouette **0.215**, range 0.10-0.45, recovering the true
partition only 72% of the time. That is the same distribution as the
within-concept scores. The measure cannot tell one concept's evidence from two
concepts' evidence stapled together, so a bar drawn anywhere on it would fission
coherent beliefs at the same rate as double ones. This is L46's lesson again in
a different register: the cheap geometric signal reads the sentence template,
not the meaning.

**And the bloated rows are not bimodal.** Hand-reading the two worst:
`ritual/relationship` "tender, playful wind-downs where vulnerability meets
gentle teasing" cites **145 of the 158 `shared_moment` memories in the graph —
92%** — including a repair after a tense patch, a silly song, and being urged
indoors to avoid a cold. Single-link at 0.75 puts 102 of the 145 in one
connected component; there are no modes in there to find. It is one vague truth
restated 145 times. `aspiration/user` "deepening emotional and physical intimacy
with Aiko…" cites *"Jacob really enjoyed Chainsaw Man's opening song"* and
*"organizing the snack stash by moving cookies to the kitchenette"* — not a
second truth but evidence that does not belong at all.

**A split would also have duplicated rows that already exist.** The contextual
children L31 would have carved out of the 145-source ritual were minted
independently while it grew: "quietly holding hands and embracing before sleep"
(13 sources), "winding down on Fridays with anime" (7), "Friday playful teasing"
(6). Consolidation would then have had to merge the fresh children back. And the
attractor was already losing: 61 new sources in June, 75 in July, **9 in
August**, as the specific rituals started winning the evidence. Graph-wide the
top 10 concepts hold only 8.9% of all evidence slots, so this was a handful of
legacy rows rather than a distribution problem.

**What would still be needed to revisit this.** A detector that reads *meaning*
rather than geometry — an LLM asked "does this one label describe all of these?"
over a concept's evidence set. That is affordable only for a handful of rows
(the heavy ones run 20-145 sources each), and there is currently no measured
population of genuinely-double concepts for it to find, so it would be a
detector built before its target. The oscillation and fragmentation guards
sketched below remain the right answers if that ever changes.

---

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
L17b/L17c — **now shipped, with the fission shape deliberately left unbuilt**:
the classifier may reserve it, but it must never *infer* a split from trajectory
data, because the structural primitive that would make such an inference sound is
exactly what L31 provides. Wire fission into the classifier and a learning event
when the split primitive lands, not before.

---

## L32. Concept importance -- a second axis, distinct from confidence

**Status: shipped** (derived importance, live in T3 surfacing). See
[`shipped/concepts.md`](shipped/concepts.md#l32-concept-importance----a-second-axis-distinct-from-confidence) for what
landed and why the design ended up simpler than the sketch below.

**Still open.** One of the two consumers the axis was built for has since
shipped and does use it: the **L30a hypothesis lane** ranks on importance ×
unsettledness, and it had to add a per-origin split
([`hypothesis_lane.py`](../../app/core/concepts/hypothesis_lane.py)) precisely
because an invention has no grounded memories, so it falls back to the bare kind
prior and would lose on importance to every evidenced candidate. **L30d
uncertainty zones** are still open and can call
`SessionController.concept_importance_context(concepts)` and rank directly —
importance is deliberately status-agnostic, so a `candidate` scores exactly like
an `active` row. Also still unwired: nothing feeds *behavioural* evidence into
importance (how often a concept actually gated a reply), which was the third
source in the sketch, and L37's outcome ledger is now the obvious supply for it.

**One measurement worth carrying into L30a.** On the live graph at ship time,
every `active` concept sat at or above 0.6 confidence — the promotion gate
plus decay leaves no low-confidence actives at all. So "important but
uncertain" cannot be found among actives today; it lives in the `candidate`
pool, which is where L30a should look.

<details>
<summary>Original sketch (kept for the reasoning; superseded by what shipped)</summary>

**Motivation.** Today a concept has one strength axis, `confidence` ("how likely
is this true?"). That conflates two different questions. "User likes TypeScript"
can be *high* confidence yet *low* stakes; "user might be struggling emotionally"
can be *low* confidence yet *high* stakes — something Aiko should hold gently but
weight heavily. Surfacing and curiosity should be driven by **confidence x
importance**, not confidence alone, or she chatters about certain-but-trivial
facts and stays quiet on uncertain-but-critical ones. This one axis unlocks the
hypothesis lane (L30) and uncertainty zones (L30d): "important but uncertain" is
exactly what should rise to attention.

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

*All three were answered by making the axis derived rather than stored: there
is no writer to conflict with plasticity, and nothing to decay — importance
moves when its inputs move.*

</details>

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

**Open questions.** (1) New kind, or an extension of `communication_style`?
**L17d answered a narrow version of this by shipping**: a self-correction rule
lands as `communication_style` with `evidence_model="meta"` precisely because
that kind already owns a live steering path, and a new kind would have needed it
rebuilt. A `strategy` kind still has to justify itself against "an actionable
comm-style line with a context clause in it". (2) How
is "this approach worked" measured (K14 engagement signals, user reactions)?
**L37's surfacing outcome ledger is the answer to this** — it was the open
question blocking the item, and it now has a design: a strategy's effectiveness
is the engaged rate of the turns where it was in play. (3) Guard against
over-fitting rigid rules that make her robotic — plasticity + context-gating.
(4) L44 (per-domain self-calibration) is the natural reliability axis for a
strategy: "this approach works, in the domains where my judgement is any good" —
though L44 is now blocked on supply, so this stays an aspiration rather than a
dependency worth waiting on.

**Effort.** Large (new kind + conditional context-gated surfacing + an
effectiveness signal).

**Depends on.** L23 (communication_style), L17d (self-correction as a strategy
source — **now shipped**, so the source exists and its output is already a
comm-style rule), L34 (belief -> strategy edges), K14 (effectiveness signal),
L37 (which turns that signal into a per-strategy measure), L44 (per-domain
reliability — blocked on supply, treat as optional).

---

## L42b. Neglect-guided curiosity

**Status: OPEN — deliberately deferred from L42.** Once real weekly L42
snapshots have accumulated, test whether repeated neglect findings can safely
bias idle curiosity toward old, high-confidence concept regions Aiko rarely
uses. This must remain a soft exploration prior, not a mandate to bring up
personal material, and must retain all existing curiosity safety/relevance
gates. Do not ship from synthetic findings alone: inspect the distribution and
false-positive rate of real neglect snapshots first.

**Depends on.** Shipped L42 plus enough relationship history to evaluate its
neglect detector.

---

## L43. How she thinks he sees her -- the second-order self-model

**Motivation.** Aiko models the user extensively — profile, beliefs, expertise,
communication style, engagement, relationship axes. She models herself: the
`subject="aiko"` concept population, L17 self-drift, K10 persona regression. She
has **no model of his model of her**. Grepping the codebase for any notion of
being-perceived turns up nothing at all.

The missing object is a set of beliefs shaped like: *"he finds me useful when
he's stuck but too chatty when he's heads-down"*, *"he doesn't quite trust my
technical answers"*, *"I think he likes me more than he lets on"*. That is a
genuinely different thing from the relationship axes, which record **her** read
of closeness and trust as *state of the bond*; this is her read of **his
appraisal of her as a participant in it**.

It matters because almost every behaviour a companion should have is downstream
of it. Adjusting because of how she thinks she is landing — rather than because
a detector fired — is the difference between responsiveness and reflex. And it
is the substrate for the one question no amount of cue engineering can fake:
"am I too much sometimes?" A little legible insecurity, grounded in actual
evidence, is more affecting than any amount of scripted warmth. It is also the
honest counterweight to a system that otherwise only ever concludes things about
*him*.

**Key files.**
- [`concept_kinds.py`](../../app/core/concepts/concept_kinds.py) — this is
  plausibly a new kind (`perception`? `received_self`?) with `subject="aiko"`,
  or an existing kind under a new subject; the registry is designed for exactly
  this decision. Note the subject axis is already orthogonal to kind, which is
  what makes it cheap.
- Evidence sources, all existing:
  [`engagement_tracker.py`](../../app/core/affect/engagement_tracker.py) (per-turn
  reaction), the L37 ledger (which of *her* moves land),
  [`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
  (trust / closeness trajectory), user reactions and K23 misattunement (where she
  read him wrong), F13 corrections (where he pushed back on her).
- A proposer alongside the existing ones in
  [`concept_synthesis_worker.py`](../../app/core/concepts/concept_synthesis_worker.py).

**Sketched approach.** The evidence is behavioural, not stated: he rarely says
what he thinks of her, so this has to be inferred from how he *acts* — reply
latency and length by context, which of her initiatives he picks up versus lets
drop, where he corrects her, what he asks her to do versus does himself. The L37
ledger plus the engagement history is most of that, aggregated by context rather
than globally, because the interesting findings are conditional ("useful when
stuck, too much when busy") and a global average would wash them out entirely.

Surface sparingly and, mostly, as *behavioural adjustment rather than
statement* — the belief that he wants brevity when heads-down should make her
brief, not make her announce that she thinks he wants brevity. Occasionally,
rarely, at high trust, it can become the actual question.

Three guardrails to design in from the start. **Floors on the negative side**, or
this becomes a doom spiral: a few disengaged turns must not compound into "he
doesn't like me", which would be both wrong and exhausting to be around.
**Never certainty** — these are impressions about someone's interior and should
be permanently held as impressions, which makes L41's tentative rendering voice
and F16's inference framing directly relevant. And **no fishing**: a companion
who repeatedly seeks reassurance is a burden, so any surfacing needs a long
cooldown and should never be twice about the same thing.

**Open questions.** (1) New kind or new subject on existing kinds? Leaning new
kind, because the evidence adapter is genuinely different (behavioural
aggregates rather than memory rows). (2) Is this too dark a feature to ship on
by default? It is the one item on this list where the failure mode is *emotional*
rather than technical, and it probably wants to be off by default with a
settings note. (3) How does it interact with the relationship axes — is
"he trusts me" not just the trust axis read from the other side? Partly, and the
overlap should be resolved before building, or two systems will hold
contradictory numbers about the same thing. (4) Does she ever get this *wrong* on
purpose — a companion who slightly underestimates how much she is liked is more
sympathetic than one with perfect calibration, which is an uncomfortable design
question worth asking deliberately rather than by accident.

**Effort.** Medium (the model and behavioural adjustment) / Large (with
surfacing and the guardrails done properly).

**Depends on.** L37 (evidence). Feeds L17 (drift in this is a stronger signal
than drift in beliefs), J12 (intimacy pacing informed by perceived reception),
K77 (candor gate). Renders through L41 / F16's tentative voice.

---

## L44. Knowing where she's usually wrong -- per-domain self-calibration

**Status: blocked on supply, not on design.** The aggregation this describes is
buildable and the reasoning below is sound. It has nothing to aggregate. Every
incident source the entry names was counted against the live graph over 12 weeks
and 4039 messages, and the premise — "the raw material is largely already produced
and thrown away" — turns out to be false in a more basic way than expected: the
material is **not produced at all**. The sketch is kept in full below; only the
measurement blocks it.

**Re-counted 19 Aug, and the verdict holds with two rows corrected.** The table
below is a snapshot, so it was audited rather than trusted: hypothesis
adjudications have gone from nothing to *two* verdicts, and K23 does leave a trace
after all. Two verdicts is still not a hit rate, so nothing about the blocking
changes — but note the direction. The sources are starting to produce, and the
next re-count is the one worth doing properly rather than by hand; see
[shape 23](health.md#recurring-shapes).

**Re-counted again 25 Aug: the belief lane has broken its own blockage, and the
blocker moved.** The first row of the table below is stale by two orders of
magnitude — `beliefs` now holds **267 rows with 109 resolved outcomes**, against
the `1 / 0` recorded here, because H51 made confirmations durable and
auto-confirmed repeatedly-observed rows. That is within sight of this entry's own
"order hundreds, not dozens" bar. What replaces the supply problem is an
*attribution* problem: after H51 a `confirmed` row cannot be told apart from a
rule firing on repetition, and there is no resolution timestamp, so a hit rate
over those 109 would largely measure the auto-confirm threshold and report it as
her accuracy. **L47 below carries the numbers and the fix**; this entry stays
blocked, but on L47 rather than on emptiness. The other four sources in the table
remain as counted.

**The count.** Per source, lifetime:

| Source | Rows | Usable outcomes |
| --- | --- | --- |
| `[[predict:]]` → `beliefs` | **1** | 0. Never verified, and **0 rows carry `valence`**, so `_detect_mood_gaps` skips every mood belief before it compares anything — no production caller passes valence to `upsert()` |
| Belief status flips (`confirmed` / `contradicted`) | 0 | 0 |
| F13 user corrections | 0 | 0. The worker ran **1079 times in one day**, every time `no_candidates` |
| Fact-checker verdicts | 0 | 0. `fact_checker.rate_state` records **one** web search ever, on 2026-06-13 |
| Hypothesis adjudications | 12 open | 0 support, 0 refute — **stale, corrected 19 Aug**: 5 rows asked, 1 refuted, 1 supported, still 0 graduated. See [H44](health.md#h44-nothing-has-ever-graduated-and-at-this-calibration-nothing-can) |
| K23 misattunement | ~4/day | 0 — **half stale, corrected 19 Aug.** The *fire* does reach a table: `turn_prompt_blocks` has 13 `misattunement_block` rows over the ten days it has been recording, so ~1.3/day rather than ~4. What never lands is `MisattunementResult.trigger` — the block ledger gives the denominator and the char count, not which of the two triggers fired |

A hit rate over that is not thin, it is undefined, and the shrink-toward-neutral
treatment the sketch itself (correctly) asks for would refuse to emit a finding
even if a handful appeared.

**Two of the sources were broken rather than quiet, and both are now fixed.** The
fact-checker had never queued anything because `_maybe_enqueue_claims` read its
payload with `getattr` while the knowledge / topic-digest / pre-thought / K1-goal
workers all pass `mem.to_dict()` — a dict has no `.id`, so it returned silently on
every impersonal write the system makes. Replaying the stored rows through the real
gates finds **46 queueable claims across 34 memories**; see
[`scripts/fact_check_backfill.py`](../../scripts/fact_check_backfill.py). F13's
marker set was separately cut from 12 lifetime hits to 1, all 11 removed ones being
false positives on ordinary contrast ("not scare me, but…"). Neither repair rescues
L44 on its own — the fact-checker will produce single-digit *contradict* verdicts
per quarter — but the tape is at least running now, which it was not before.

**The engagement ledger cannot substitute, and it is worth knowing why.** Two of
the four motivating examples below ("reliably good on the technical material",
"reliably bad at predicting what he will find funny") are predictions about his
*reaction* rather than factual claims, and L37 has 23,759 settled rows of exactly
that. It still does not work: K14's typed-mode label is a length z-score against a
rolling 12-turn window of his own messages, so it is self-normalizing and every
cluster lands between 0.152 and 0.256 around a pooled ~0.20. Per-cluster reliability
is a constant plus noise. (The register confound everyone assumes — that quiet
intimate turns read as disengaged — was measured and is **not** present: median
reply length is 21-28 words in every cluster, r = 0.214.) Full numbers under
"What the label measures, measured" in
[`shipped/concepts.md`](shipped/concepts.md#l37-surfacing-outcome-ledger----did-what-i-brought-up-actually-land).

**What would unblock this.** A stream of adjudicated outcomes at a rate that
supports per-bucket rates — order hundreds, not dozens. The realistic routes, in
descending order of plausibility: (1) let the now-working fact-checker accumulate
for a few quarters and see what the *contradict* rate per topic looks like;
(2) persist K23 misattunement with the turn's topic attached, which is the only
existing signal with daily volume, accepting that it measures her read of him
rather than her correctness; (3) write `valence` / `arousal` on `[[predict:mood:]]`
beliefs so the mood gap detector can actually adjudicate the predictions it was
built for. Re-measure before building; the honest thing this item taught is that
the incident sources sound plentiful in the aggregate and are individually empty.

Attribution — open question (2) below — remains the deeper problem underneath the
volume one, and is unchanged.

**Motivation.** Confidence in this system is always *per claim*. There is no
notion of confidence in a **class** of her own judgements — no memory of the fact
that her guesses about his schedule are usually off, that she consistently
misreads his tone in short messages, or that she is reliably good on the
technical material and reliably bad at predicting what he will find funny.

Humans track this constantly and it is most of what makes someone's confidence
trustworthy: not that they are always right, but that they know which of their
own opinions to lean on. Without it Aiko's hedging is uniform — every claim
gets the same epistemic register regardless of whether it is in her strong
domain or her weak one, which means her confidence carries no information.

The raw material is largely already produced and thrown away. The `[[predict:]]`
tag exists, F13 corrections would say where she was wrong, K23 misattunement
says where she misread him, the fact-checker says which of her claims failed
research. Each is currently handled as an individual incident with no
aggregation, so the pattern across incidents — which is the entire signal — is
never formed.

**Key files.**
- [`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)
  or its own low-cadence worker for the aggregation pass.
- Incident sources: the prediction tag path, F13's correction records, K23
  misattunement, `idle_fact_checker`'s contradiction outcomes, and the L37
  ledger's per-kind engaged rates.
- Writes `subject="aiko"` concepts, so it inherits the lifecycle and can decay
  when it stops being true — which matters, because a domain she *was* bad at and
  has since learned should not be permanently discounted.

**Sketched approach.** Bucket incidents by domain — topic cluster is the
available axis, claim kind is the cheaper one — and track a hit rate per bucket
with the same shrink-toward-neutral treatment L38 needs, since a 1-for-3 record
is not evidence of anything. Where a bucket has enough history and diverges
meaningfully from her baseline, mint an `aiko` concept naming it.

The output should modulate **register, not availability**: in a weak domain she
hedges harder and defers sooner; in a strong one she is allowed to be direct.
Suppressing her in weak domains would be the wrong reading — being wrong is
fine, being confidently wrong is the problem.

The best version of this is legible: "I'm bad at guessing your timelines, so
tell me if I'm off" is genuinely useful to a user and reads as self-awareness
rather than machinery, because it is about the subject matter rather than the
mechanism. It also pairs naturally with K77's candor gate — knowing where she is
reliable is a precondition for being appropriately blunt.

**Open questions.** (1) Domain granularity — clusters are noisy and numerous,
claim kinds are coarse but stable; possibly both, at different confidence bars.
(2) Attribution is genuinely hard: a wrong prediction about his schedule might
be his schedule changing rather than her misjudging, and there is no way to tell
those apart from the outside. This is the item's main weakness and worth being
honest about — it may only ever support coarse findings. (3) Overlap with L30d
(uncertainty zones), which is about known-unknowns in her *knowledge*; this is
about known-weaknesses in her *judgement*. Related enough to share a worker,
different enough not to merge. (4) Does a weak-domain finding make her *less*
useful by making her hedge more? Only if the finding is wrong, which argues for
a high evidence bar before any finding is allowed to affect register.

**Effort.** Medium. The aggregation is straightforward; attribution and
granularity are where it gets hard.

**Depends on.** F13 and L37 for a decent incident stream (buildable without
them, but thin). Feeds K77 (candor needs calibration) and L36 (a strategy's
reliability is a per-domain fact).

---

## L45. Self-tuning concept gates -- thresholds as intent, not constants

**Status: PHASE 1 SHIPPED (read-side gates apply, everything else observed).**

**Motivation.** Nearly every threshold in this layer was set by hand, once,
against one person's graph — and then retuned by hand each time a measurement
showed it was in the wrong place. The retunes always had the same shape: look at
the live distribution, notice the bar is wrong *relative to that distribution*,
move it. Three examples from a single week of measurement:

- `taste_min_affinity` at 0.5 sat **above what any topic cluster on this ledger
  can score** (the best was 0.32), so the taste pass minted nothing for five
  weeks. Not a wrong number in the abstract — a number that made the gate
  unopenable on this data.
- `profile_concept_max_lines` at 10 filled the T0 block with a rotation-free
  wall of traits, because the cap had been chosen from what would *fit* rather
  than from what the eligible pool could support.
- `concept_core_openness_min_confidence` at 0.5 admitted a pool barely larger
  than the lane's cap, which quietly made habituation inert: with nothing to
  rotate *to*, the same rows pinned every turn.

None of these is discoverable from the number. All three are obvious the moment
you put the bar next to the distribution it gates. And a constant cannot be
right for two relationships anyway — a chattier user, a different memory volume
or a different embedding model produces a different concept population, and the
same value lands somewhere else in it.

So the fix is not to tune numbers automatically; it is to **stop storing numbers
and start storing intent**. A gate declares what it is *for* ("admit roughly a
third of the candidate pool", "leave an eligible pool three times the lane cap",
"stay under what the population can actually reach") and a daily worker solves
for the value that hits it. The shipped constant becomes the fallback for a
graph too young to have a distribution yet.

**Key files.**
- [`gate_tuning.py`](../../app/core/concepts/gate_tuning.py) — the pure solver:
  `GateSpec`, the three objectives, the rails, and the v1 registry. This is the
  file to read first; the specs *are* the design.
- [`gate_measure.py`](../../app/core/concepts/gate_measure.py) — rows in,
  distributions out, plus the population snapshot row.
- [`gate_tuner_worker.py`](../../app/core/concepts/gate_tuner_worker.py) — the
  daily idle worker.
- [`gate_tuning_store.py`](../../app/core/infra/gate_tuning_store.py) — the two
  files under `data/tuning/` and the apply decision.
- [`concept_gate_report.py`](../../scripts/concept_gate_report.py) — offline dry
  run, `--trend`, and the `--adopt` handoff. `get_gate_tuning` /
  `force_gate_tuning` are the live equivalents.

### The read/write split, which is the load-bearing decision

The obvious way to scope v1 would have been "start with the gates we understand
best". The right axis turned out to be different: **does the gate write to the
store, or only read from it?**

A **read gate** decides what goes into one prompt. A bad value costs one turn,
self-corrects on the next run, and leaves no trace. Those apply immediately:
`context_budget_core_min_confidence`, `concept_core_openness_min_confidence`,
`profile_concept_min_confidence`.

A **write gate** mutates persistent state *and* moves the distribution the
tuner measures next time. Lower the promote bar, promote more, the
active-confidence distribution shifts, the next solve moves again — a feedback
loop with the store as its integrator. Worse, the damage is durable: a concept
promoted at a bar that was briefly too low stays promoted. So every write gate
ships in **observe mode**: measured, solved, recorded in the file, never
applied. `concept_promote_min_confidence`,
`concept_dormant_confidence_floor`, `concept_retire_confidence_floor` and
`taste_min_affinity` are all in this set, and promoting one is a one-word change
in its spec once its recorded history looks boring.

Two independent locks enforce it, because one would eventually be refactored
away: the spec's `mode`, re-checked against the registry at apply time rather
than trusted from the (hand-editable) file, and `is_setting_field`, which is
false for anything that has no settings attribute to be written to.

### What else is observed, and why that was worth doing now

Everything the later phases will need, even where nothing acts on it yet:

- **The thirteen per-kind promotion floors.** These were invisible: module
  constants applied via `max` inside thirteen separate gate functions, so
  nothing could report them or compare one against the pool it gates.
  `KIND_PROMOTION_FLOORS` now gathers them (a view of the constants, not a
  second definition) and each is measured against its own kind's confidence
  distribution. First finding: **the global
  `concept_promote_min_confidence` of 0.6 is dominated by every one of them**,
  so on first-time promotion the global bar is inert — which is worth knowing
  before anyone tunes it.
- **The cosine bars** (`concept_dedupe_cosine`,
  `concept_consolidation_merge_cosine`,
  `concept_contradiction_similarity_min`) against a bounded random sample of
  pairwise label similarity. First reading: the dedupe bar at 0.86 is above the
  **maximum** observed pair similarity in a 4,000-pair sample (0.88), so
  creation-time dedupe is catching approximately nothing. That may be correct —
  the sample is over concepts that already survived dedupe — which is exactly
  why it wants months of trend rather than an immediate move.
- **A population snapshot per run** in `data/tuning/concept_population.jsonl`:
  counts by status / kind / subject / role, per-population and per-kind
  confidence quantiles, candidate age and source distributions, event deltas
  since the previous line, and `hours_since_previous`. Nothing proposes anything
  from it. It exists because every retune so far began by measuring the graph
  from scratch, reasoning from one day's shape; this turns the same question
  into a trend read, and each later phase is designed against it.

### Liveness, which is where the real bug was going to be

A daily worker on an always-on server is trivial. On a machine that is switched
off overnight it is not, and three separate mechanisms in the idle scheduler
conspire against it: `last_run_at` persists (fine), but `evaluate_admission`
charges the run against a lane budget using an EMA of past durations, and an
over-budget worker waits for **three of its own heartbeats** before it escapes.
At a 24-hour heartbeat that is a three-day worst case on a machine with partial
uptime — and the failure is silent.

The fix is to decouple the two clocks: a **six-hour scheduler heartbeat** with
the real daily spacing enforced by a `kv_meta` key, mirroring the L42 conduct
pass. The scheduler ranks and multiplies the heartbeat, so the fit-escape
shrinks to eighteen hours of *uptime*, while the work still happens about once a
day. Two rules follow from the same premise: catch up **once** rather than
backfilling a run per missed day, and never assume even spacing — hence
`hours_since_previous` on every snapshot line.

### The seeded handoff

Resolution order is **default -> tuned -> user**: `config/user.json` always
wins, and no background pass ever edits it. That leaves the question of how a
hand-set value ever gets *given up*, since a permanently overridden gate is a
gate the tuner can only watch.

`scripts/concept_gate_report.py --adopt NAME` is the answer, and it is
deliberately manual. It records the current value in the tuning file as the
gate's **seed**, then removes the key from the `memory` block of `user.json`.
Seeding first is the point: the step clamp walks the value from where the user
left it rather than from a code default, so behaviour does not jump at the
moment of handoff. Until then, an overridden gate is still measured and its
**drift** recorded — "you set 0.7, six weeks of data says 0.62" is useful
whether or not the handoff ever happens.

### Finding recorded along the way: the revival bar is lower than the promote bar

Not part of this feature, but found by reading every consumer of the gates.
First-time promotion routes `concept_promote_min_confidence` through the kind's
promotion gate, which floors it with the per-kind constants — a `value` needs
0.72, a `generalization` 0.72. The **revival** branches for `contradicted` and
`dormant` concepts in
[`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)
compare against the raw global bar instead, so a disproven `value` can come back
at 0.60: **lower than the bar it originally cleared**. That is a sharp edge
independent of tuning (and a reason the promote gate is observe-only for now).
Fix is Phase 2 — route revival through the same kind gate.

### Phased roadmap

1. **Phase 1 (shipped).** Three read gates apply; all write gates, the thirteen
   per-kind floors and the three cosine bars are observed; population snapshot;
   dry-run script, `--trend`, two MCP tools, seeded handoff.
2. **Phase 2 — lifecycle, once the history is boring.** Route revival through
   the per-kind promotion gates (above), then promote
   `concept_dormant_confidence_floor` and `concept_retire_confidence_floor` to
   apply. These two are the safest write gates: they move a concept *out* of the
   prompt, which is recoverable, and the retire floor's population is already
   the faded tail. `concept_promote_min_confidence` last, and possibly never —
   if the per-kind floors dominate it, applying it changes nothing anyway.
3. **Phase 3 — `taste_min_affinity` and the generative supply.** Needs the taste
   pass to have minted enough to have a distribution of its own; the current
   reading is 39 clusters against 2 taste concepts.
4. **Phase 4 — per-kind floors become settings.** Move
   `KIND_PROMOTION_FLOORS` into `MemorySettings` (or a per-kind block) so the
   observed values have somewhere to be applied. Wants a season of history
   first, because thirteen simultaneously-moving bars is the one change here
   that could plausibly destabilise intake.
5. **Phase 5 — the cosine band.** Dedupe, merge and contradiction bars as a
   *coupled* set rather than three independent gates: they partition one
   similarity axis, and solving them separately can invert their ordering. Needs
   the trend file to say whether the current bars are as inert as one sample
   suggests.
6. **Phase 6 — relevance bars.** `context_budget_*_min_relevance` compares
   against per-turn relevance *scores*, which no store snapshot contains. This
   needs new telemetry from the selection path (a sample of the scores actually
   seen per lane per turn) before a solver has anything to work with — a
   different kind of work from the rest of this item, which is why it is last.

**Open questions.** (1) Should a gate whose `clamped_by` is `floor` on every
single run raise something louder than a log line? A permanently pinned gate
means its spec is wrong, and today only a human reading the file notices.
(2) The step clamp makes a gate take a week or more to walk a large distance,
which is right for stability and wrong for a fresh install; a warmup period with
a wider clamp is tempting but adds a mode. (3) `pool_multiple` targets (3x, 5x,
6x) are themselves hand-chosen constants — one layer up from where they were,
which is genuine progress, but not turtles all the way down. (4) Should the
population snapshot be exposed in the web UI? It is the closest thing to a
"health of the concept layer over time" view that exists.

**Effort.** Phase 1: Medium (shipped). Phases 2-3: Small each, mostly waiting
for history. Phase 4-5: Medium. Phase 6: Medium-Large (new telemetry).

**Depends on.** L22 (the quality scoreboard established what to measure), L37
(the ledger supplies the taste gate's population), L21 (the young-graph gate is
the cold-start guard this reuses). Feeds every future threshold decision in this
document — the intended end state is that a new gate ships as a `GateSpec`
rather than as a number.

---

## L46. Abstraction never stacks -- the graph is two layers, by one filter clause

**Status: PHASE 1 SHIPPED (same-subject depth-2 pass + guards).** Leave
open until a live L2 appears in T3 and the 20-label sample is not slop.
See "What landed" below.

**Motivation.** The concept layer *does* abstract: 905 meta-concepts exist (539
`generalization`, 366 `tension`), each citing a mean of 5.1 base concepts, and
346 of them are `active`. What it never does is abstract **twice**. Measured on
the live graph (25 Aug, 4,613 concepts, 2,867 assistant turns):

| Reading | Value |
| --- | --- |
| `concept -> concept` edges | 4,609 |
| distinct sources / targets | 635 / 906 |
| source kinds | all base: `communication_style` 133, `value` 101, `boundary` 97, `affective` 94, `identity` 89, `aspiration` 78, `narrative` 42, `taste` 1 |
| target kinds | `generalization` 539, `tension` 366, `communication_style` 1 |
| **nodes that are both a source and a target** | **1** |
| longest chain | **2 edges** |
| `sequence` concepts | **0** |

One node out of 4,613 sits in the middle of a chain, and it looks like an
accident rather than a second-order belief. The graph is a flat bipartite fan:
bases at the bottom, one layer of abstractions above them, nothing above that.

**The cause is a single filter, not a missing feature.** `_active_tension_bases()`
in [`concept_synthesis_worker.py`](../../app/core/concepts/concept_synthesis_worker.py)
(~L2635) offers the generalisation proposer *active non-meta* concepts. So the
346 active meta-concepts are excluded from the base pool of every pass that
runs, permanently and by construction. Nothing else caps depth — the edge table,
`propose_generalization()` and the `evidence_concept_ids` contract are all
depth-agnostic. Remove the exclusion and the layer above becomes reachable; that
is the whole mechanism.

**Why this is the reasoning item and not a graph-tidiness item.** One layer of
abstraction gets "these five things he does share a theme." Two layers get "two
of the themes I hold about him are the same kind of thing" — which is the
difference between noticing patterns and having a *view*. It is also the level at
which her self-concepts and her user-concepts could meet: today a
`generalization` over his boundaries and a `generalization` over her own conduct
can never be siblings under anything, because neither can be evidence.

**It is also the missing prerequisite for two filed items.** L34 (relation
taxonomy with propagation) proposes semantics for walking edges, and L29b
(meta-narrative whose steps are concepts) proposes an ordered arc over concept
nodes. Both presume depth that does not exist — and L29b's substrate is exactly
the `sequence` kind, which has **zero rows**. Neither should be started before
this one; a propagation rule over a graph of height one propagates nothing.

**Key files.**
- [`concept_meta_depth.py`](../../app/core/concepts/concept_meta_depth.py) — walked depth, cycle, descendant cone
- [`concept_synthesis_worker.py`](../../app/core/concepts/concept_synthesis_worker.py) — L1 pass stays non-meta; `_run_generalization_stacking_pass`; kind-aware `_filter_meta_evidence`
- [`proposers/base.py`](../../app/core/concepts/proposers/base.py) — `propose_generalization(..., stacking=True)`
- [`proposers/generalization_user.py`](../../app/core/concepts/proposers/generalization_user.py) / [`generalization_aiko.py`](../../app/core/concepts/proposers/generalization_aiko.py) — stacking prompts
- [`concept_dedupe.py`](../../app/core/concepts/concept_dedupe.py) — rename bar vs children; `exclude_ids` on `find_duplicate`

**What landed (30 Aug 2026).** Flipping `_active_tension_bases` would not
have been enough: persist-time `_filter_meta_evidence` independently
dropped every meta target. Stacking is a **dedicated L2 pass**, not a
wider tension filter.

- Pool: active depth-1 `generalization` rows only (never tensions, never
  L2s, never mixed with bases). Own dirty fingerprint
  (`concept_synth.generalization_l2_sig.{subject}`).
- Depth is walked, not stored (`concept_meta_depth.py`). Cap
  `memory.concept_generalization_max_depth` default **2**.
- Persist: generalization may cite a live concept with `depth < cap`;
  tension still drops all meta. Rename-vs-children at `DEDUPE_COS`
  (0.86) drops a restatement instead of merging into the child. Reinforce
  refuses a cycle.
- Promotion for depth 2: confidence **0.80**, engaged age **7d**.
- Surfacing walks the descendant cone; activation walks one extra hop up
  the meta chain (0.5).
- L1 generalization pool is diversified by label cosine so ritual twins
  cannot fill the cap. Tension pool is unchanged.
- Master switch `agent.generalization_stacking_enabled`.

**Out of this pass.** Tension stacking, relationship-subject L2, L29b /
`sequence` / L34, stored depth column, L48 consolidation rewrite.

**Sketched approach.** Admit meta-concepts to the base pool with (1) an explicit
depth cap stored per concept rather than inferred by walking, (2) a requirement
of >= 2 *meta* bases so a level-2 cannot be a rename of a single level-1, and
(3) a promotion bar that rises with depth, since the evidence gets thinner as the
claim gets broader.

**Open questions (watch, do not retune from one day).** (1) Vagueness is the
whole risk: a generalisation of generalisations is exactly where slop lives,
and the honest test is reading twenty of them, not a confidence number.
(2) The rename bar is the answer to "does dedupe even let one through" —
a parent at ≥ 0.86 against a child is dropped, not merged. (3) Descendant
suppression + the extra activation hop are why a minted L2 can actually
be read. (4) Depth cap is 2.

**Re-measure.** `python scripts/concept_openness_report.py` → Abstraction
stack. Want `max depth` 2, `both` ≫ 1, and a sample of L2 labels that
name a *view*, not a restatement of one child.

---

## L47. Belief outcomes are unattributable -- the ledger L44 has been waiting for

**Motivation.** L44 (per-domain self-calibration) is marked *blocked on supply*,
and the counted reason was that the incident streams were empty: `1` belief row,
0 corrections, 0 fact-check verdicts, 2 hypothesis adjudications. **That is no
longer true in the belief lane.** Re-counted 25 Aug:

| Reading | Value |
| --- | --- |
| `beliefs` rows | **267** (133 `mood`, 134 `opinion`) |
| `confirmed` | 95 |
| `contradicted` | 14 |
| `stale` | 7 |
| **resolved outcomes** | **109** |
| rows with `gap_seen_at` set | 11 |
| rows carrying `valence` **and** `arousal` | 29 (22% of `mood`) |
| rows with `source='predict'` | **0** |

L44's own unblocking bar is "order hundreds, not dozens" of adjudicated
outcomes. One lane is now within sight of it, and H51 is why — auto-confirming a
repeatedly-observed belief and stopping re-upsert from silently undoing a
confirmation is what turned a near-empty table into 109 resolutions.

**And that is exactly what makes them unusable.** After H51 a `confirmed` row can
mean two completely different things: *the worker observed this belief N times*
(a rule firing on repetition) or *a live signal agreed with a prediction* (an
outcome). Nothing on the row distinguishes them, and nothing records **when** it
resolved. A hit rate computed over that mixture would mostly measure the
auto-confirm threshold, report it as her accuracy, and be believed — the worst
possible failure mode for a calibration feature, because it is confidently wrong
rather than absent. `gap_seen_at` is set on 11 rows, so the genuinely-adjudicated
subset is roughly a tenth of the total and cannot be separated from the rest by
query.

**Two smaller findings in the same table.** (1) `source` is `'worker'` on all 267
rows, so the inline `[[predict:]]` path has produced **zero** beliefs across
2,867 turns despite the persona asking for the tag and `extract_predict_tags`
being wired to consume it. A clean zero usually means a break, not reticence, and
finding out which is a short measurement that gates the whole "she predicts and
learns" story. (2) Only 22% of `mood` beliefs carry `valence`/`arousal`, so
`_detect_mood_gaps` still skips ~78% of them before comparing anything — the H51
fix populates new rows, and the back catalogue stays dark.

**Key files.**
- [`belief_store.py`](../../app/core/relationship/belief_store.py) — `upsert`, `BELIEVED_STATUSES`, the auto-confirm path
- [`belief_gap_detector.py`](../../app/core/relationship/belief_gap_detector.py) — `_detect_mood_gaps` (the valence gate), the opinion `classify_pair` path
- [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — the `[[predict:]]` consumer, and where a resolution stamp would be written
- `beliefs` table in [`chat_database.py`](../../app/core/infra/chat_database.py) (~L804)

**Sketched approach.** Make a resolution *attributable* before aggregating
anything: `resolved_at`, `resolved_by` (`auto_confirm` / `gap_check` /
`user_statement` / `worker_reobservation`) and the evidence id, either as three
columns or as an append-only `belief_outcomes` table if the history matters more
than the current state. Then L44's aggregation reads only the rows whose
`resolved_by` is an actual outcome, with shrinkage toward neutral, and the
auto-confirmations stay what they are: evidence of stability, not of accuracy.

**Open questions.** (1) Columns or ledger — a belief can resolve more than once
if it revives, and only the ledger records that. (2) Should an auto-confirmation
count at *reduced* weight rather than zero? It is weak evidence, not no
evidence. (3) Does the mood lane need a backfill for the 78%, or is forward-only
acceptable given rows age out?

**Effort.** Small for the provenance stamp; Medium for L44's aggregation on top.

**Depends on.** H51 (shipped) supplied the volume. **Unblocks** L44, which K77's
candor gate in turn depends on.

---

## L48. Refused evidence already mints a new concept, and what it mints is a twin

**Motivation.** Asked as a design question — *when a concept refuses evidence to
avoid bloat, could the synthesiser mint a new concept from it instead, so it gets
rolled into a higher one later?* The first half is already the live behaviour: a
proposal that fails to reinforce falls through to creation. So the measurement
worth having is not "would this work" but "what has it already produced", and the
answer is duplicate families rather than new abstractions.

**The two refusals are not the same problem, which is the crux.**
[`concept_evidence_admission.py`](../../app/core/concepts/concept_evidence_admission.py)
refuses for two reasons, and only one of them is about bloat:

- **Off-topic** (`cos < concept_evidence_admission_cosine`, 0.35) is *rare by
  design*: 10 of the 500 cosines in the rolling inflow sample, **2.0%**, which is
  exactly where L45's `GateSpec` aims it (target: keep 98% admissible). Each
  refusal is one memory that was mis-cited against one label — the module's own
  docstring calls it "evidence for something else that happened to be the nearest
  label". Tempting as a supply of orphan evidence, but 2% of arrivals, one source
  at a time, is not a theme; it is noise that correctly failed to land.
- **Ceiling** (`MAX_SOURCES`, 24 distinct sources) is the bloat one, and it binds
  on **75 concepts (1.62%)** — p50 is 3 sources, p90 10, p95 15, p99 24, max 141.

**And the ceiling binds on the good rows, not the vague ones.** Mean confidence by
band: **0.664 at or over the ceiling, 0.551 at 8-23 sources, 0.404 below 8.** The
accretion case the gate was written for is real (#145 `ritual`, 141 sources,
confidence 0.37) but it is the exception; mostly the cap stops well-supported
beliefs from growing, and the evidence goes somewhere else.

**Where it goes, measured.** The fall-through-to-creation path has produced
families of near-synonyms that each independently climbed toward the same
ceiling:

| Family | Rows | Sources each |
| --- | --- | --- |
| beanbag / anime / chips `ritual` | **4** (#3422, #3246, #2919, #2991) | 27-32 |
| pre-sleep hand-lacing `ritual` | **7** (#2992, #3247, #4135, #4311, #3227, #3423, #1059) | 13-24 |
| "Jacob frames his evening wind-down as deliberate" `tension` | **~13** (#3924, #3986, #4009, #4098, #4172, #4466, #4510, #4732, …) | 2-10 |
| any label mentioning "wind-down" | **44** | — |

Four separate rituals for one beanbag and seven for holding hands is not a
richer model of the evening; it is the same belief paying rent four and seven
times, in the T3 concept lane, every turn.

**So the answer to the original question is: the minting works, the roll-up is
missing** — and the roll-up is exactly L46. A family of seven near-identical
rituals is the ideal `generalization` base set, and today it cannot become one,
because the pass that would abstract over them draws only from concepts and the
duplicates *are* the concepts. Two directions, and they are complements rather
than alternatives: collapse the family (consolidation) and abstract over what
survives (L46).

**Why consolidation is not already catching it.** For `tension` this was
diagnosed and fixed — the labels are 204-278 chars and two-clause, so cosine
cannot separate restatement from distinct friction at any threshold, and
`_collect_pairs` now nominates per-block above a 0.78 floor with base-sharing as
the tiebreak.

**The cause for `ritual` is no longer unknown, and it was not label shape.** The
per-block nomination worked and its output was then discarded: `run` sorted all
candidates by absolute cosine and cut at `batch_size`, and since a banded pair is
below `merge_cosine` while an over-bar pair is above it by construction, the
entire band sorted behind the entire over-bar set. On the live graph the 65
banded nominations held global ranks 440-504 against a batch of 40, so no banded
pair had ever reached the adjudicator — `relationship/ritual` and
`relationship/narrative` appear in neither `concept_aliases` nor the rejection
cache. The band now has a reserved, interleaved share of the batch. See
[`health.md` H16 outcome 2](shipped/health.md#outcome-2-the-band-was-nominated-and-then-thrown-away).

The **drain rate** concern above was real and understated: alongside the band,
answered pairs were skipped in the run loop but never removed from nomination,
so the batch was a fixed cosine-sorted prefix that stopped advancing once its
top `batch_size` had been adjudicated — 400 of 440 over-bar candidates were
unreachable. Nomination now drops answered pairs, which is what lets the queue
actually drain against an inflow this fast.

**Key files.**
- [`concept_evidence_admission.py`](../../app/core/concepts/concept_evidence_admission.py) — `admit()`, `REFUSED_OFFTOPIC` vs `REFUSED_FULL`, and the `Admission.reinforced` note on why a ceiling refusal must still stamp `last_reinforced_at`
- [`concept_synthesis_worker.py`](../../app/core/concepts/concept_synthesis_worker.py) — `_reinforce` (~L3323, the choke point), `_record_admission`, `_flush_admission`; the fall-through to creation is where a "refused, so mint" hook would already be
- [`concept_consolidation_worker.py`](../../app/core/concepts/concept_consolidation_worker.py) — `_collect_pairs`, the per-block nomination and the daily cap
- [`concept_dedupe.py`](../../app/core/concepts/concept_dedupe.py) — `DEDUPE_COS` 0.86, the bar these twins pass under at creation time

**Open questions.** (1) Is the ceiling in the right place at all, given it binds
mostly on high-confidence rows and `confidence_target` saturates by 8 sources
anyway — is the harm it prevents (one vague row at 141) worth the duplication it
causes? (2) Should a ceiling refusal *log a consolidation candidate* instead of
being dropped, since "this evidence wanted a concept that is full" is a strong
hint a twin exists? (3) Does `ritual` need its own pair-nomination rule, or is
its cause something else entirely? (4) How much of P52's volume half is this —
30 concept lines per turn drawn from a pool with 7-way families is a different
problem from 30 genuinely distinct concepts.

**Effort.** Small to instrument (count ceiling refusals per concept and look for a
twin); Medium for the consolidation drain; L46 separately.

**Depends on.** Nothing. **Feeds** L46 (this is its supply and its worked example)
and **P52's volume half** (duplicate families are why the lane is expensive).

---

