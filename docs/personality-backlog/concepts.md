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

**Enrichment (from the surfacing audit).** Two later items give L17 richer
material than beliefs alone, and both are better drift subjects than a
confidence wiggle:

- **L43** (how she thinks he sees her). Drift in the *second-order* model is a
  stronger and more affecting signal than drift in a proposition — "I've become
  more careful with you, and I'm not sure I like that" is a different order of
  observation from "I revised my estimate of your preferences". It is also the
  variety of change most likely to clear L17b's salience bar, since a shift in
  perceived reception tends to be directional rather than noisy.
- **L42** (self-model of her surfacing conduct). Drift in *behaviour* — what she
  keeps steering toward — is observable without any belief changing at all, so it
  catches a class of change L17 currently cannot see.

Both suggest L17b's classifier should take a **kind** of change as input, not
just a magnitude, since "I hold this less firmly" and "I have started behaving
differently" deserve different interpretive treatment.

**Effort.** Large overall; sequenced L17a → L17e below.

---

## L17a. Concept trajectory from the event log (the history read layer)

**Status: SHIPPED** — moved to
[`shipped/concepts.md`](shipped/concepts.md#l17a-concept-trajectory-from-the-event-log-the-history-read-layer).
`ConceptEventStore.trajectory()` reads one concept's path oldest-first, and L3
drops a banded `confidence_sample` so a silent decay is no longer invisible.
L17b consumes it from here.

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
- **Enforcement of signal C — the one-off sweep. SHIPPED as a script,
  not yet run.** 374 concepts minted before reinforcement had ever fired are
  sitting `active` at a median confidence of ~0.8, competing for surfacing slots
  against concepts that earned their place, and decay cannot clear them: ~86
  engaged days each against **12.9 engaged days accumulated in total**.
  [`scripts/concept_sweep_unreinforced.py`](../../scripts/concept_sweep_unreinforced.py)
  demotes them to `dormant` (never `retired`, and it touches neither confidence
  nor evidence, so a genuine reinforcement brings any of them straight back),
  appending a `dormant` row to the timeline for each so the sweep reads as
  lifecycle history rather than a silent rewrite. Scoped to the pre-Jul-13
  cohort via `--before` rather than "all never-reinforced" — the newer ones may
  simply not have been re-observed yet. **Dry-run by default**: without
  `--apply` the database is opened read-only, so the reporting path cannot
  mutate anything. Still to do: stop the app and actually run it, then re-run
  `concept_intake_report.py` against the dated baseline above.
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

**Status: in progress — substrate shipped (L24), four consumers migrated.** L24
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
- **SHIPPED** — cluster annotation in
  [`topic_graph.py`](../../app/core/conversation/topic_graph.py):
  `cluster_activity` rows now carry `representative_id` (the same
  highest-salience member `TopicCluster` reports, which is what the concept
  layer keys its `cluster -> concept` evidence edges on), so any reader can
  hand it straight to `ConceptView.for_cluster(rep_id)`. It rides
  `cluster_activity` rather than `interest_map` because the latter is the
  cheap per-turn prompt read that deliberately never joins back to the
  memory mirror; `cluster_activity` already takes that snapshot for recency,
  so the rep id costs one extra tuple element and no second walk.
- **SHIPPED** — [`KnowledgeMapReflectionWorker`](../../app/core/proactive/knowledge_map_reflection_worker.py):
  each rich territory in the map-shape payload carries the concepts spanning
  it ("… — you believe: …"), most-confident first, capped by
  `knowledge_map_reflection_concepts_per_cluster` (0 restores the old
  size/recency-only payload). The reflection can now say what a territory
  *means* to her, not just how big and how recent it is.
- **SHIPPED** — [`InterestDriftWorker`](../../app/core/proactive/interest_drift_worker.py):
  a drafted drift carries the most-confident concept spanning that cluster in
  its journal entry, and the inner-life cue appends "What you hold about it:
  …". Resolved *only* for the topic actually being drafted, so the
  mirror-joining read stays off the per-tick sampling path.
- [`belief_store.py`](../../app/core/relationship/belief_store.py) /
  belief inference — bias toward durable concepts (K2 stays the *transient* layer,
  not migrated).
- `ForwardCuriosityWorker` routine hints — the remaining interest-map reader.
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

**Status: SHIPPED** — moved to
[`shipped/concepts.md`](shipped/concepts.md#l35-surface-reason-labels----why-did-i-surface-this-on-every-item).
Every entry in the L26 trace now names the signal that won it its slot, debug-only.

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
**L37's surfacing outcome ledger is the answer to this** — it was the open
question blocking the item, and it now has a design: a strategy's effectiveness
is the engaged rate of the turns where it was in play. (3) Guard against
over-fitting rigid rules that make her robotic — plasticity + context-gating.
(4) L44 (per-domain self-calibration) is the natural reliability axis for a
strategy: "this approach works, in the domains where my judgement is any good".

**Effort.** Large (new kind + conditional context-gated surfacing + an
effectiveness signal).

**Depends on.** L23 (communication_style), L17d (self-correction as a strategy
source), L34 (belief -> strategy edges), K14 (effectiveness signal), L37 (which
turns that signal into a per-strategy measure), L44 (per-domain reliability).

---

## L37. Surfacing outcome ledger -- did what I brought up actually land?

**Motivation.** The concept layer can grow its *knowledge* but not its
*judgement*. Everything about which concepts reach the prompt is decided by
hand-tuned constants — the per-kind `surface_weights`, the core-lane confidence
bars, the habituation window — and none of them move in response to how the
conversation went. Surfacing is very nearly write-only: the only trace a
surfaced concept leaves is the habituation timestamp stamped at the end of
`build_relevant_context`
([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py), the
`_write_concept_habituation` call), and `last_reinforced_at` is written *only*
by the synthesis worker re-deriving a concept from fresh evidence
([`concept_synthesis_worker.py`](../../app/core/concepts/concept_synthesis_worker.py),
`_reinforce`). A concept that has been in front of Aiko two hundred times to no
visible effect is therefore indistinguishable from one that opened up a good
conversation every single time.

The memory layer is one step ahead but stops short of the same line. It has
`mark_surfaced` -> `mark_used` for recency, and
`_mark_revived_memories`
([`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py))
bumps `revival_score` when the reply shares content words with a surfaced
memory. But that measures whether **Aiko echoed it**, not whether **the user
cared** — and the K22 callback detector measures the same thing from a
different angle. Nothing in the system asks the second question, even though
the answer is already computed: `EngagementTracker.record_turn`
([`engagement_tracker.py`](../../app/core/affect/engagement_tracker.py))
produces an `EngagementResult` per turn from the user's reply latency and word
count against his own rolling baseline, bucketed `engaged` / `neutral` /
`disengaged` / `abandoned`.

L37 is the missing join: a durable per-item record of *what was surfaced* and
*what happened next*. It is the keystone for L38, L42, G4, P43 and K81 — none
of which can be built on guesses about value.

**Key files.**
- Write side: the end of `build_relevant_context` in
  [`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) already
  computes exactly the set to record — `chosen_hits` (memories) and the chosen
  concept pairs, next to the existing `rag.mark_surfaced` /
  `_write_concept_habituation` calls. The per-concept `score_components` map
  built in the same function carries lane, reason and the individual score
  terms, which is what makes the ledger diagnostic rather than just a counter.
- Outcome side:
  [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — the
  engagement block that calls `record_turn` and stashes
  `self._last_engagement_label`. The credit write belongs next to
  `_mark_revived_memories`.
- Storage: a new `surfacing_outcomes` table in
  [`chat_database.py`](../../app/core/infra/chat_database.py) (schema bump),
  keyed by `(turn_id, item_kind, item_id)`. `kv_meta` is the wrong shape — this
  is append-heavy and wants aggregation.
- Existing precedent for the aggregate shape: `concept_events`
  ([`concept_store.py`](../../app/core/concepts/concept_store.py)) — same
  append-then-summarise pattern, same retention concern (see P34).

**Sketched approach.** Record one row per surfaced item per turn:
`(turn_id, kind, item_id, lane, surface_reason, score, rank)`. Then, in
post-turn, attribute the outcome.

The attribution is **off by one, and getting that wrong would invalidate the
whole signal**. `record_turn` derives latency from the gap between Aiko's last
reply and the user's current message, so the engagement computed during
post-turn of turn *N* describes the user's reaction to the reply of turn
*N-1*. The ledger therefore credits the previous turn's surfaced set, not the
current one. Keep the last turn's row ids on the session and settle them when
the next engagement result arrives; a session that ends before the next message
leaves the final turn unsettled, which is correct — silence after a goodbye is
not disengagement.

Store three outcome fields per row: `echoed` (Aiko referenced it — reuse the
overlap test, upgraded by F12), `engagement_label` (the user's reaction), and
`settled_at`. Two coarse derived rates per item — *echo rate* and *engaged
rate* — are all L38 needs.

Deliberately **not** in scope: any attempt to isolate a single item's
contribution when eight were surfaced together. The turn-level signal is
shared credit across the whole surfaced set, which is noisy per turn and
perfectly adequate over hundreds. Resist the urge to build a regression here.

**Open questions.** (1) Retention — one row per item per turn is roughly ten
rows a turn; fold into P34's retention posture from the start rather than
discovering it later. (2) Does an `abandoned` label mean the surfaced set was
bad, or that the user's dinner was ready? Probably: only ever *reward* engaged,
never *punish* abandoned, so the signal degrades to "no evidence" rather than
false blame. (3) Should worker cues live in the same table (one ledger,
`kind="cue"`) or their own — one table is simpler and G4 wants the same
columns, so probably yes. (4) Whether `echoed` deserves its own weight at all
once user engagement is available, or is only useful as a diagnostic.

**Effort.** Medium. Schema bump + two write points + the off-by-one settling
dance. No LLM calls, no new workers.

**Depends on.** K14 (`EngagementTracker`, shipped). Unblocks L38, L42, G4, P43,
K81, DT5.

---

## L38. Earned standing -- let outcomes move the surfacing score

**Motivation.** With L37 recording what happened, the scorer can finally learn.
Today `surface_score`
([`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py)) blends
six signals — context, confidence, recency, stability, salience, activation —
every one of which describes the concept's *internal state*. Not one of them
describes how the concept has *performed*. A belief that reliably opens the
user up and a belief that reliably lands flat are scored identically forever,
which is why the layer's taste is frozen at whatever the weights table said on
day one.

L38 adds a seventh term, **standing**: a slowly-moving per-concept prior earned
from the L37 engaged rate. This is the specific change that turns the concept
layer from a growing store of facts into something that develops judgement
about its own material.

**Key files.**
- [`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py) —
  `surface_score` gains a `standing` argument; it belongs *inside* the
  sum-normalised base alongside confidence and stability, not as an additive
  bonus like `activation` (a learned prior should be able to lose to a strong
  topic match, not stack on top of it).
- [`concept_kinds.py`](../../app/core/concepts/concept_kinds.py) —
  `SurfaceWeights` gains a `standing` weight per kind. Start it at `0.0`
  everywhere so shipping the plumbing changes no behaviour, then raise it per
  kind behind a setting.
- [`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) — the
  `_add_scored` closure reads the standing map, loaded once per turn next to
  the habituation state and the recent-events map.

**Sketched approach.** Standing is a shrunk estimate, not a raw rate: a concept
surfaced three times with two good turns has *no* evidence, and letting it
outrank a proven one on a 67% rate would make the whole term noise. Shrink
toward the neutral 0.5 by observation count (a plain Bayesian
`(engaged + k*prior) / (total + k)` with `k` around 10 is enough), so standing
only becomes decisive after a couple of dozen surfacings. Recompute in the
concept-lifecycle worker rather than per turn.

Cap the term's authority deliberately. Standing must never be able to keep a
topically irrelevant concept in the prompt, and it must never fully suppress a
core belief — the floor matters as much as the ceiling, because a value the
user finds uncomfortable is exactly the kind of thing that scores badly and
must still be held. Clamping standing into something like `[0.35, 1.0]` and
exempting `value` / `boundary` kinds from downward pressure is the safe
version.

**Open questions.** (1) Per-kind weights, or one global? Per-kind matches the
existing table, but the evidence per kind will be thin for a long time. (2)
Should standing feed `confidence` instead of sitting beside it? No — confidence
is "how sure am I this is true", standing is "how useful is it to bring up",
and collapsing them would let an unpopular truth decay into a falsehood. Worth
stating explicitly in the code comment. (3) Interaction with habituation: both
suppress, and stacking them could bury a concept entirely — needs a test that
a high-standing concept still rotates, and a low-standing one still surfaces
occasionally so it can earn its way back. (4) Does this want an explicit
exploration allowance (surface a low-standing concept now and then precisely
*because* the estimate is stale)? Probably, and it is the difference between a
system that learns and one that ossifies.

**Effort.** Medium. Small code change, but the safety properties above are
where the work actually is.

**Depends on.** L37 (the signal). Related to L32 (importance is the *stated*
weight; standing is the *earned* one — they should stay separate axes).

---

## L39. Identity concepts surface twice a turn, and one copy ignores habituation

**Motivation.** Two independent paths render the same concepts into the same
prompt, and they don't know about each other. `_profile_concept_lines`
([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)) renders
up to `profile_concept_max_lines` (default 10) `subject="user"` identity and
value concepts into the T0 profile block, ordered by confidence, with a 0.5
confidence bar — **every turn, with no habituation and no knowledge of T3**.
Meanwhile the L27 core lane independently pins up to `context_budget_core_cap`
concepts from those same kinds into T3. There is a `seen` dedupe *within* the
profile block, but nothing across blocks.

So Aiko's strongest beliefs about the user appear twice, phrased differently,
on the same turn — and the profile copy is immune to every anti-repetition
mechanism L23 built. This is the most likely source of a "she keeps telling me
what I'm like" feeling, and it quietly wastes the T0 slot that P31 wants to
reclaim.

**Key files.**
- [`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) —
  `_profile_concept_lines` (the T0 path) and the core-lane block inside
  `build_relevant_context` (the T3 path).
- [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — T0
  `profile_block` is assembled long before T3 exists, which is the whole
  difficulty: the profile cannot ask "did T3 already take this?" because T3
  has not been built yet.

**Sketched approach.** The ordering makes the obvious fix impossible, so invert
it: let the **profile block claim first** (it renders earlier and is the more
stable, cache-friendly home for a settled trait), record the claimed concept
ids on the turn, and have the core lane skip anything already claimed. The
core lane already over-fetches `core_cap * 3` for habituation rotation, so it
has spare candidates to fill with — this costs nothing in slot count.

Then give the profile path the same habituation read the flex and core lanes
use, so a trait that has led the profile for ten turns steps aside for another
one. It should *not* get the habituation *write* (that would double-stamp
against T3's clock); read-only is the right asymmetry, and worth a comment
saying so.

**Open questions.** (1) Is claim-first-in-T0 the right precedence, or should
the turn-relevant T3 lane win because it is responsive to the conversation?
Leaning T0 for prompt-cache stability, but a topically hot identity concept
arguably belongs in T3. (2) Does the profile block want its own smaller cap
once duplicates are gone — ten identity lines every turn is a lot of standing
assertion. (3) Should this share one "already surfaced this turn" set across
*all* blocks, which is really the general version of the problem P43 is about?

**Effort.** Small. Two read sites and a per-turn claimed-id set.

**Depends on.** Nothing. Pairs naturally with L40 and P31.

---

## L40. Habituation doesn't reach the core lane's budget competition

**Motivation.** L23's habituation is supposed to make a just-surfaced concept
step aside. On the core lane it only half-works. The core block computes the
habituation factor and uses it to sort fresh candidates ahead of stale ones,
then admits the winners with `relevance=conf` — the concept's **raw
confidence** ([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py),
the `ContextCandidate(source="concept", relevance=conf, ...)` construction).
The factor is recorded in the trace and then dropped.

Because pinned candidates bypass the per-source cap and the relevance floor but
still consume the shared token budget, that raw confidence is what a core
concept competes against *memories* with. So a core belief that surfaced last
turn is softly deprioritised against other core beliefs, and not at all against
the turn's memories — it takes the same slice of a budget it has already had
recently, at the expense of material the user has not seen.

**Key files.**
[`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) (the core
lane's candidate construction) and
[`context_budget_selector.py`](../../app/core/session/context_budget_selector.py)
for how `relevance` is used once pinned items are admitted.

**Sketched approach.** Multiply the pinned candidate's relevance by the
habituation factor already in hand. Keep the pin — a core belief should still
be exempt from the cap and the floor, because "always-on" is the point of L27 —
but let a recently-surfaced one yield tokens to a fresh memory rather than
outbidding it at full strength. The core habituation floor (default 0.8) keeps
the effect gentle by construction.

**Open questions.** (1) Does this want the full blended `surface_score` instead
of confidence-times-habituation? Arguably yes for consistency with the flex
lane, but confidence is deliberate here — a core pin is justified by how firmly
the belief is *held*, not by how well it matches the turn — so the minimal
change is the honest one. (2) Worth checking against the L27 tests that a
habituated core concept can still win when it is the only candidate.

**Effort.** Small. One expression, plus a test that pins still outrank the
floor.

**Depends on.** Nothing. Same area as L39.

---

## L41. Reason-conditioned phrasing -- use the L35 signal without narrating it

**Motivation.** L35 computes, per surfaced concept, *why* it won its slot —
`topic_match`, `unresolved_contradiction`, `settled_belief`,
`recently_revived`, `association`, and the rest — and then deliberately throws
it away. The rule is explicit in
[`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py): the
reason is debug-only, because letting Aiko read "I surfaced this because we
clashed on it" is the fastest route to a companion who narrates her own
machinery. **That rule is right and L41 does not relax it.**

But there is a version that uses the signal without ever stating it. Today
every concept renders through the same held-lightly impression template, so a
belief that was just contradicted and a belief she has held serenely for months
arrive in identical clothing — and the model, reasonably, treats them
identically. The reason is available and free; it can choose the *framing* of
the line rather than being narrated in it. "You two never really settled
whether..." and "she's long since made her mind up that..." are the same
concept under two reasons, with no machinery visible.

**Key files.**
[`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) — the
per-subject / per-family rendering path that currently uses one impression
voice, and already receives `score_components` carrying the reason per concept.
`SURFACE_REASON_LABELS`
([`concept_surfacing.py`](../../app/core/concepts/concept_surfacing.py)) is the
debug vocabulary and stays debug-only; L41 needs a *separate*, deliberately
non-technical phrasing table.

**Sketched approach.** A `{reason -> framing}` table with a small number of
distinct voices — unsettled, settled, freshly-changed, primed-by-association,
plain-relevant — collapsing the eleven reasons onto maybe four framings, since
most reasons do not deserve their own voice. Default framing for anything
unmapped, so adding a reason later cannot break rendering. Only the T3
impression lines are in scope; the T0 profile stays as it is.

The hard constraint to encode in the code comment and the tests: **no framing
may name a mechanism.** No "contradiction", no "surfaced", no "confidence", no
"because you mentioned". The framings are ordinary English about the subject
matter, and the reason is invisible in the output. A test asserting the
rendered text never contains the reason tokens is cheap and worth having.

**Open questions.** (1) Does this actually change model behaviour, or just cost
tokens? Measurable with T5's eval scoreboard, and worth being willing to
revert. (2) Token cost — the framings must be about as short as the current
template, not longer. (3) Risk that an "unsettled" framing invites her to
re-litigate a tension every time it surfaces; the tension kind is already
excluded from T3 for exactly this reason, so the unsettled framing should
probably be the *most* restrained one, not the most dramatic.

**Effort.** Small. A phrasing table plus a rendering branch.

**Depends on.** L35 (shipped). Would benefit from T5 to tell whether it helped.

---

## L42. A self-model of her own surfacing behaviour

**Motivation.** L17 gives Aiko a way to notice that her *beliefs* have changed.
She has no way to notice patterns in her own *conduct* — that she has steered
the last dozen conversations toward the user's work, that she keeps returning
to one unresolved tension, that there is a whole region of what she knows about
him she never brings up. This is the difference between a system that
accumulates a self-history and one that can actually reflect on being itself,
and it is the most companion-shaped thing the ledger unlocks.

Once L37 exists, the data for this is right there and needs no new
instrumentation: the surfacing ledger *is* a behavioural record. Aggregated by
subject, kind and topic cluster over a few weeks it describes her habits well
enough to say something true and slightly uncomfortable about them.

**Key files.**
- Reads the L37 `surfacing_outcomes` table plus
  [`concept_store.py`](../../app/core/concepts/concept_store.py) for the
  subject / kind of each surfaced id.
- Writes `subject="aiko"` concepts through the normal proposer path, so the
  findings live in the same store as every other self-concept and inherit the
  lifecycle — see the L17 entries above for the shape.
- Natural home is a periodic pass in
  [`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)
  or its own low-cadence idle worker registered per
  [`workers.md`](workers.md).

**Sketched approach.** Aggregate the ledger over a long window and look for
three shapes: *concentration* (one cluster or subject taking a
disproportionate share of surfacing slots), *neglect* (high-confidence
concepts that essentially never surface — a fair proxy for "things she knows
but never uses"), and *fixation* (one item surfaced far more than any other
with a mediocre engaged rate — the algorithmic signature of nagging).

Each shape proposes a `subject="aiko"` concept in plain first-person language
("I steer us toward his work more than anything else"). From there it is an
ordinary concept: it surfaces through the normal lanes, decays if it stops
being true, and L17's drift machinery can notice when it changes.

The neglect finding is the interesting one, because it is directly
actionable — it names material she has and does not use, which is exactly what
a curiosity worker should be aiming at.

**Open questions.** (1) Does this stay internal, or is she allowed to *say* it?
Saying "I've noticed I keep steering us to your work" is a genuinely intimate
move and one of the better things on this whole list — but it is one step from
narrating her machinery, and the L35 rule exists for a reason. Probably: allow
it, rarely, phrased about the *relationship* rather than the *mechanism*, and
never with numbers. (2) Statistical honesty — concentration partly reflects
what the user actually talks about, so the finding needs normalising against
his own topic distribution or it will just report his hobbies back at him.
This is the main risk of the item. (3) Cadence: weekly at most; it is a
long-window observation and there is nothing to see in a day. (4) Overlap with
L33 introspective reflection — L33 asks structured questions about beliefs,
L42 observes conduct; they may want to share a worker.

**Effort.** Medium. The aggregation is easy; making the findings true rather
than merely computable is the work.

**Depends on.** L37 (the ledger, with enough history to be meaningful). Feeds
L17 (drift), L19 (autobiography), and gives K81 its neglect signal.

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
