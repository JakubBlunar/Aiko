# Aiko personality backlog

Ideas surfaced during the personality brainstorms that we didn't ship
in the depth passes. Each open entry is short on purpose: motivation,
key files, sketched approach, and one or two open questions. Pick any
item up later as a standalone plan.

The numbering matches the labels used during the brainstorms so chat
history stays grep-able. Items that have already shipped live in
[`shipped.md`](shipped.md), one paragraph each with a link to the
implementation file or detail doc that owns them.

The K-series in [`patterns.md`](patterns.md) is a separate beast —
companion-AI design patterns we haven't tried yet, sketched at one
paragraph each rather than fully scoped. Treat patterns.md as a
prompt for the next brainstorm, not a queue.

---

## Open items at a glance

### B. Avatar + expressiveness — [`avatar.md`](avatar.md)

- **B3.** Blink-rate modulation by arousal (deferred follow-up to B1).
- **B7.** Open-vocabulary touch gestures — let Aiko invent new
  `[[touch:...]]` kinds (model-supplied, no config, emoji optional).

### C. Proactive + presence — [`proactive.md`](proactive.md)

- **C2.** Window-title-aware activity (privacy-gated).
- **C3.** Persisting last-fired typed-proactive cooldown to disk.
- **C4.** TTS-on-typed-proactive toggle.

### D. New tools / capabilities — [`tools.md`](tools.md)

- **D-approval.** Spoken / Aiko-voiced task approvals.
- **D1.** Calendar / reminders tool.
- **D3.** Fast synchronous web-search brain tool (+ knowledge-DB
  write-back).
- **D7.** Anticipatory routine assistance — act on what she's learned.

Dev / debug tooling (DT-series):

- **DT2.** Relationship state inspector — one-shot consolidated snapshot.
- **DT3.** Feature-flag catalog + "minimal mode" preset.
- **DT4.** Scenario / conversation replay harness (the deterministic-clock
  half is now unblocked by DT1).

### F. Awareness + grounding — [`awareness.md`](awareness.md)

- **F4.** Source-cited memories (`metadata.source_url`).
- **F11.** Relevance-driven memory resurrection — proactively recall a
  stale `archive` memory when a dormant topic re-activates, as a hedged
  callback ("vaguely remember you mentioning macro photography..."),
  driven by latent relevance rather than age.

**F7** is obsolete — domain-aware source routing was superseded by the
pluggable LangSearch / DuckDuckGo backend. F1-F3, F5, F6, F8-F10 and the
whole **K-time** family shipped; see
[`shipped/awareness.md`](shipped/awareness.md).

### G. Background workers — [`workers.md`](workers.md)

- *Cleanup* — drop or wire the unused
  `consolidator_state.last_cluster_index` column.

New worker ideas show up in [`patterns.md`](patterns.md) until they
earn a G-letter; several (K1, K8, K10, K14, K21) are worker-shaped.

### I. Integration / wiring gaps — [`integration.md`](integration.md)

Shipped-but-under-wired features (no UI, no live WS update, or a
silent failure path). Cheap individually, compounding in aggregate.

- **I9.** Mobile responsiveness + PWA installability (LAN-responsive
  is cheap; full installable auto-updating PWA needs an HTTPS origin).

Everything else in the series has landed: I1, I2, I4 and I5 in the
reliability pass ([`features.md`](shipped/features.md#reliability-pass--i1--i2--i4--i5-finish-the-wiring-batch)),
and I3, I6, I7, I8, I10 in
[`shipped/integration.md`](shipped/integration.md).

### H. Immersion polish — [`immersion.md`](immersion.md)

- **H2.** Calendar / time context block. *Partly superseded* — circadian
  and K3 cover most of it; holiday proximity + user birthday remain.
- **H5.** Second scene / travel semantics.
- **H6.** Audible backchannels ("mm-hm" while the user speaks).
- **H7.** Listen-while-speaking — soften the half-duplex voice lock.
- **H10.** Autonomous idle-life on the avatar.
- **H12.** Aiko-initiated intentional gifts.
- **H23.** Avatar shared-moment snapshot.
- **H24.** Occasion- / season-aware outfits.
- **H25.** Show-and-tell.
- *Minor polish* — second TTS provider, barge-in default flip (**now
  unblocked**: P25 shipped, so the client drops its scheduled audio when
  speech is cut off — an interrupt is actually silent). SSML prosody
  shipped.

H0, H1, H3, H4, H8, H9, H11 and H13-H22 have shipped — see
[`shipped/immersion.md`](shipped/immersion.md).

### J. Shared-moments follow-ups — [`moments.md`](moments.md)

- **J1.** Multi-user moments / participant attribution.
- **J2.** Exportable timeline (markdown / PDF).
- **J3.** Axes-aware proactive nudges.
- **J7.** Moment-detection tuning (+ gift/promise ordering bug).
- **J8.** Milestone celebration beats.
- **J12.** Intimacy pacing & boundary calibration.
- **J13.** Pet-name reciprocity & evolution.

J4 (relationship-stage register), J5, J6, J9, J10 and J11 have shipped —
see [`shipped/moments.md`](shipped/moments.md).

### K. Patterns to explore — [`patterns.md`](patterns.md)

Still open: K12 calendar-linked anticipation ·
K19 cold-start companion onboarding ·
K33 cozy mode ·
K40 comfortable silence · K41 mid-stream self-correction ·
K42 multi-bubble reply bursts (texting rhythm) ·
K49 messiness permission (typed imperfection) ·
K50 typed-mode delivery pacing · K62 co-experience companion ·
K77 candor gate · K78 vocal-affect read (prosody-in) ·
K79 hesitation tell (typing latency).

K39 (energy / spoons) was absorbed by the shipped K68 embodied vitality —
same mechanic, broader framing.

**The "will" family (K52–K56)** — Aiko follows every topic the user
sets and never opens her own; every initiative cue is hedged into
silence and nothing structurally counters the assistant prior.
**ALL SHIPPED**: K56 persona counterweight ("leading vs following"
rewrite) · K52 wants ledger (desire with growing pressure) ·
K53 initiative turns (deterministic floor-taking — the "may" →
"must, occasionally" flip) ·
K55 thread ownership (one circle-back to a thread she opened) ·
K54 Aiko-side topic appetite (she's allowed to be bored and
negotiate the topic — once per conversation, with an offer).

**The directed-emotions family (K57–K60)** — Aiko's moods were
objectless scalars: she could be "sad" in general but never
*miffed at {user_name} because he broke a promise*.
**ALL SHIPPED**: K57 directed emotion episodes (lonely / miffed /
warm_glow / smug / playful_jealous / hurt — cause line, intensity,
decay, acknowledgment-driven resolution + visible thaw) ·
K58 emotion speech weighting (smug/pouty/sulky/mischievous minted
end-to-end; register recipes; intensity-banded imperative +
prosody hints) ·
K59 tease economy (payback ledger — banked on pushback / light
miffed, collected as a callback tease conversations later) ·
K60 tsundere mask (expression policy over K57: warmth expressed
through denial, caught-caring beat, budgeted dere-slips,
closeness-eroded mask — `agent.expression_mask` dial, off by
default).

(K1 long-term goals, K2 theory-of-mind, K3 routine awareness,
K4 dialogue-act tagging, K5 mood-shell tilt, K6 novelty
detector, K7 forgetting protocol, K8 affect rupture-and-repair,
K9 topic-graph browser, K13 stylometric mirror,
K14 implicit engagement signals,
K15 vulnerability budget,
K16 unified ambient grounding line, K17 clarification-repair,
K18 topic stagnation, K20 metacognitive calibration,
K22 callback / inside-joke detector, K23 subtle misattunement
detection, K24 sensory anchoring layer, K25 memory
confidence time-decay, K27 day colour, K28 "what I've been
turning over", K29 opinion injection, K30 self-noticing cues,
K31 + K32 soft physicality, K34 forward curiosity worker,
K35 memory consolidation worker, K36 "things I did
while you were away", K38 self-correction cue,
K43 promise follow-through, K44 felt-language affect
block, K45 mood inertia, K46 stance persistence,
K47 question/share balance, K48 tease rhythm,
K51 cue-register rotation, K61 knowledge grounding,
K63 long-arc callbacks, K64 freedom of thought,
K65 worker modernization, and K66-K76 — earned familiarity,
dormant-interest re-opener, embodied vitality, implicit-need
reading, growth witness, self-callback, wellbeing concern,
shared ritual formation, humor-style calibration,
user-expertise calibration and flashbulb encoding —
have shipped; see [`shipped.md`](shipped.md).)

### L. Higher-order concepts — [`concepts.md`](concepts.md)

A new abstraction layer *above* topic clusters: cross-cluster
**concepts** that link semantically-distant clusters the proximity-only
topic graph can't ("Home Server + Mechanical Keyboard + Self-hosting +
Virtual AI"). The LLM *proposes* candidates; they *auto-promote* to real
concepts as confidence accrues (recurs + spans >=2 evidence + stable),
mirroring the `beliefs` lifecycle one level up. Sits on top of the F10
topic-graph work. Built **kind-parameterized** from day one: a shared
store + lifecycle + a `ConceptKind` registry (subject x evidence-model),
so new kinds are a registry entry, not a migration. Two substrate
decisions are locked in up front because they are pervasive: **one typed
influence graph** (`concept_edges`: every evidence / reference /
contradiction / influence link is a typed, signed edge — a new linkage is
a new `relation` value, not a new table) and **one lifecycle engine** (the
single writer of `confidence` / `plasticity` / `status`; L3/L15/L16/L17
are passes of it, not competing workers). v1 ships identity end-to-end;
every other kind is a stub.

**The layer is built.** L1-L16, L18, L20, L21 and L23-L28 have all
shipped: the store and typed edge graph, the per-kind proposers, the
single-writer lifecycle engine, ten concept kinds across three subjects
(including the two meta kinds), plasticity, belief revision, the UI and
MCP debug surface, and the trace. It ships **off** by default
(`agent.concepts_enabled`) because synthesis is a recurring
maintenance-LLM cost that wants a mature topic graph behind it. The
finished write-ups live in [`shipped/concepts.md`](shipped/concepts.md);
[`concepts.md`](concepts.md) keeps the design preamble plus the items
that still carry open work. What follows is only what is still open.

Open — quality and pruning:

- **L22.** Concept-quality evaluation. *Partly shipped.* The measurement
  half exists: `concept_quality.py`, `GET /api/concepts/quality`, the
  `get_concept_quality` MCP tool, and the layer-health strip in the
  Concepts panel, covering production / promotion / reinforcement rates,
  the confidence and evidence distributions, near-duplicate pairs, and
  per-(kind, subject) label-register rates. The three spurious-concept
  signals are computed but **advisory only** — nothing is demoted on
  them yet. **Tuning pass 1 (intake) shipped:** `identity` was the only
  kind with no promotion floors of its own and the largest kind in the
  graph, so it got `identity_evidence_gate` (3 sources + a real stability
  delay); the engaged-age anchor now lands before the gate reads it, so an
  offline gap can no longer mature a candidate on idle time; and the
  pruning section grew intake-*rate* metrics plus
  `scripts/concept_intake_report.py`, since the standing never-reinforced
  count is far too slow to show whether a threshold change worked.
  **The one-off sweep of the 374 concepts minted before reinforcement had
  ever fired now exists** as `scripts/concept_sweep_unreinforced.py`
  (dry-run by default; demotes the pre-Jul-13 cohort to `dormant`, keeping
  confidence so a genuine reinforcement revives them) — it just hasn't been
  run yet. That is a bootstrap-era backlog, not a leak: `reinforced` was
  zero for the graph's first nine days and on the most recent day of use it
  outpaced discovery 4:1, so the mechanism is fine and decay simply cannot
  clear the stock at ~86 engaged days a head against 12.9 accumulated.
  Writing the sweep also forced a fix to `dormant -> active` revival, which
  had no reinforcement check and would have undone it on the next tick.
  Still open, in priority order: running the sweep; tuning pass 2 (per-kind decay — the
  ordering is already right via `plasticity_default`, the absolute scale is
  ~6x too slow); and the offline eval harness (deliberately last:
  hand-authoring goldens before the register settles would enshrine the
  output we are fixing).

Open — self-history:

- **L17.** Self-drift noticing — Aiko notices her own change by comparing
  self-concept snapshots over time ("I think you've corrupted me... I ask
  for cookies more than I used to"). Broken into L17a-f in
  [`concepts.md`](concepts.md): the trajectory read layer, the
  change-salience classifier, the learning-event record, self-correction
  meta-concepts, the surfacing debugger, and the evolution diary. **L17a
  is shipped** — `trajectory()` plus banded `confidence_sample` events, so
  the rest of the chain has a history to read.
- **L19.** Aiko's autobiography (capstone) — her self-history as a
  durable, traversable timeline: traverse the self-concept graph +
  snapshots to genuinely answer "Have you changed?". Depends on L17.

Open — later kinds and refinements (L29-L36, all detailed in
[`concepts.md`](concepts.md)): relationship & meta narratives, concept
hypotheses and the curiosity loop that tests them, concept fission,
importance as a second axis beside confidence, introspective reflection,
a richer edge taxonomy, and a strategy layer. **L35 (surface-reason
labels) is shipped** — every concept in the L26 trace now names the
signal that put it in the prompt.

### P. Performance + observability — [`perf.md`](perf.md)

Cross-cutting gaps that aren't features in their own right but
compound across every K-series entry:

- **P7.** Typed-mode prefetch parity with voice.
- **P11.** Reclaim background-worker `num_predict` from reasoning
  leakage (try `/no_think` on qwen3-family workers).
- **P16.** Post-turn inner-life blocks the brain loop — now
  **measurable** (`post_turn_ms`); collect numbers before attempting
  the Large fast/slow-lane split.
- **P24.** Voice latency batch: reaction-tag TTS gate, double STT
  pass, first-chunk threshold.
- **P26.** Lip-sync rides the server clock, not the playback clock —
  **partly shipped**: the client-side analyser path exists but is
  wired for mobile audio-owners only; extend it to desktop.
- **P30.** Raise / disable the `memory.max_memories` cap (topic-graph
  persistence removed the `O(n²)` wall; mirror sweeps P5/P17 are the
  remaining blockers).
- **P31.** Audit + trim the baseline system prompt (~25-30k resting
  floor). The **measurement shipped as P31a** — `get_prompt_block_costs`
  ranks blocks by tokens × tier — and its first finding is that the
  persona (~19.5k estimated tokens) dominates everything else, so the
  remaining work is the persona/grammar trim and lazy-rendering the
  occasional blocks.
- **P32.** Concept layer (L-series) worker budget + unbounded graph
  growth — proposer cadence in the idle budget, snapshot thinning,
  per-turn cost limited to L23 selection. The "dirty-subgraph-only
  lifecycle walk" line is **already satisfied** by L3's batched
  round-robin; what's left is making it dirty-*triggered*.
- **P33.** Inner-life providers walk the whole memory mirror every
  turn — the kind-filtered `list_top` / `list_recent` fix **shipped**
  (along with the correctness bug it was hiding: catchphrase blocks
  silently never surfaced). Still open: a kind index for the unbounded
  `iter_by_kind` walks, and F9's whole-corpus concatenation. Becomes the
  wall the moment P30 lifts the cap.
- **P34.** Unbounded tables — `messages`, its LanceDB mirror, and the
  append-only `concept_events`; retention posture per table.
- **P35.** The Lance ANN index is built once and never refreshed
  (silent degradation to flat scan; hard prerequisite for P30).
- **P36.** Idle-worker LLM pile-up — ~22-28 LLM-capable workers
  draining sequentially under a 6 s soft budget, with no starvation
  reporting and no LLM-specific ceiling.
- **P37.** Residual React re-renders — the per-token and per-mic-frame
  subscriptions **shipped** (bucketed streaming signature; `audioLevel`
  moved to the leaves). Left: hoisting the Virtuoso `itemContent` /
  `Footer` closures so their identity is stable.
- **P38.** Live2D channels allocate a fresh store snapshot 5-8x per
  frame; cache one per tick.
- **P39.** Concept snapshot + quality report N+1 the evidence edges
  (and the quality report is O(n²) pairwise on embeddings).

(P1-P6, P8-P10, P12-P15, P17-P23, P25, P27-P29, P31a, P40 and P41 have
shipped — the embed budget and prompt-build telemetry, the slice-cache
and RAG batch-lookup work, the Lance scan push-downs, the streaming
accumulator and the streaming-draft rework, the RAG reader-writer lock,
async compaction, the heuristic tool-pass gate, then the memory pass:
lazy STT loading behind `stt.enabled`, TTS gated on `tts.enabled` with a
real release path, the `get_memory_breakdown` and
`get_prompt_block_costs` measurement tools, client audio flush on abort,
and the two missing `messages` indexes. See
[`shipped/perf.md`](shipped/perf.md).)

The two measurement tools are the load-bearing part of that list: most of
what remains open here (P16's split, P30's cap raise, P31's trim, P36's
worker ceiling) was being argued from static code reading, and the audit
that produced P33-P41 found several such arguments to be wrong. Measure
first.

### T. Testing + evaluation — [`testing.md`](testing.md)

How we keep the behavioural system regression-safe as the concept work
(L-series) piles on. Organising idea: **bracket the LLM** — test the
deterministic prompt-build (before) and tag-parse / post-turn (after)
halves with a scripted LLM double; treat model *content quality* as a
separate eval track, never a red/green unit gate. The offline pytest
counterpart to the live DT-series debug tooling.

- **T1.** Shared `FakeChatClient` + `BehaviorHarness` (the missing seam;
  kills per-file stub duplication; unblocks T2/T3).
- **T2.** End-to-end behavioural chain tests (worker -> cue -> prompt ->
  reply -> post-turn -> state delta).
- **T3.** Worker registry conformance + smoke + LLM fault injection
  (parametrized over all ~51 workers).
- **T4.** Prompt-build + tag-parse contract (golden) tests (pins the
  T0->T6 cache ladder + the inline-tag grammar).
- **T5.** LLM behavioural eval suite — a scoreboard, not a CI gate
  (umbrella over K10 persona regression + L22 concept eval).
- **T6.** Determinism seams (seeded RNG; the `Clock` half landed with DT1 —
  `timephrase.now` / `utcnow` is now the single process-wide "now").

---

## How to pick one up

1. Re-read the relevant domain file. Each entry is small enough that
   the file itself is your context.
2. Spin up a plan with `CreatePlan`. Most items fit in a single plan;
   nothing here needs a multi-phase rollout.
3. Validate the same way the depth passes did: focused suite ->
   full `pytest -q` -> spot-check the running app.
4. When the work lands, move the entry out of its domain file into the
   matching [`shipped/<area>.md`](shipped/) and add a bullet for it in the
   [`shipped.md`](shipped.md) index. Keep the body verbatim and add one
   `../` to its relative links — the file sits a directory deeper. Then
   update any inbound links in [`AGENTS.md`](../../AGENTS.md), the
   relevant `docs/` detail doc, or code comments.

---

## Related docs

- [`plugin-system.md`](plugin-system.md) — full plugin-system vision
  (P1 declarative MCP + skills shipped; P2 code entrypoint / `PluginApi`,
  P3 `hooks`, P4 provider contracts deferred).
- [`docs/plugins.md`](../plugins.md) — the shipped P1 plugin-bundle format.
- [`docs/memory-tiers.md`](../memory-tiers.md) — schema v8 memory
  tiers + `IdleWorkerScheduler`.
- [`docs/aiko-room.md`](../aiko-room.md) — world / room / garden.
- [`docs/shared-moments-and-relationship.md`](../shared-moments-and-relationship.md)
  — schema v7 shared moments + relationship axes.
- [`docs/presence-and-activity.md`](../presence-and-activity.md) —
  C1 typed-mode proactive + presence + activity awareness.
- [`docs/alexia-model-notes.md`](../alexia-model-notes.md) — Alexia
  rig audit; B4 + B5 reference.
