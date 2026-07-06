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
- **B4.** Phase 5 reaction polish — mint `embarrassed` / `nervous` /
  `defiant`; teach the persona the stacked-overlay idiom.
- **B7.** Open-vocabulary touch gestures — let Aiko invent new
  `[[touch:...]]` kinds (model-supplied, no config, emoji optional).

### C. Proactive + presence — [`proactive.md`](proactive.md)

- **C2.** Window-title-aware activity (privacy-gated).
- **C3.** Persisting last-fired typed-proactive cooldown to disk.
- **C4.** TTS-on-typed-proactive toggle.

### D. New tools / capabilities — [`tools.md`](tools.md)

- **D1.** Calendar / reminders tool.
- **D2.** Image vision tool. **Shipped.**
- **D3.** Fast synchronous web-search brain tool (+ knowledge-DB
  write-back).

Dev / debug tooling (DT-series):

- **DT1.** Virtual clock / time-travel for time-gated features (the
  highest-leverage debug tool here).
- **DT2.** Relationship state inspector — one-shot consolidated snapshot.
- **DT3.** Feature-flag catalog + "minimal mode" preset.
- **DT4.** Scenario / conversation replay harness.

### F. Awareness + grounding — [`awareness.md`](awareness.md)

- **F4.** Source-cited memories (`metadata.source_url`).
- **F5.** Conflicting-memory detector.
- **F11.** Relevance-driven memory resurrection — proactively recall a
  stale `archive` memory when a dormant topic re-activates, as a hedged
  callback ("vaguely remember you mentioning macro photography..."),
  driven by latent relevance rather than age.
- **F10.** Topic-graph utilisation. **Fully shipped (F10a-l)** (LLM cluster
  labels, RAG diversity, multi-hop expansion, cluster-scoped
  `recall_topic`, interest-map prompt block, self-aware knowledge-gap
  notice, per-cluster topic temperature from shared-moment vibes,
  per-topic confidence self-model, cluster-scoped memory hygiene for the
  F5 conflict + K35 consolidation sweeps, semantic topic tracking that
  names K6/K18 topic shifts + return-to-known, per-cluster rolling
  `topic_digest` memory surfaced as the coarse RAG line, and cluster
  management UX — rename / pin / forget per cluster in the Memory tab).

Temporal awareness (K-time family, in [`awareness.md`](awareness.md)):

- **K-time1.** Wall-clock prefixes on chat history. **Shipped.**
- **K-time2.** Date-anchored retrieval for relative-time queries
  (`time_expr.parse_time_window` + RAG date-window boost + empty-window
  anti-confabulation guard, plus the direct `[start, end]` message-recall
  fallback for verbatim "what did we say then"). **Shipped.**
- **K-time3.** Upcoming-horizon block — pre-computed future relative
  times. **Shipped.**
- **K-time4.** Session-elapsed & mid-session gap awareness. **Shipped.**
- **K-time5–9.** Canonical `timephrase` module + single "now" seam,
  richer ISO now-anchor, worker time toolkit, "today" anchor in the
  extract workers, memory ages fed to the consolidation merge, and
  per-cluster recency (`TopicGraph.cluster_activity`) in the knowledge-map
  reflection so "hot vs. gone quiet" territory reads through. **Shipped.**

### G. Background workers — [`workers.md`](workers.md)

- *Cleanup* — drop or wire the unused
  `consolidator_state.last_cluster_index` column.

New worker ideas show up in [`patterns.md`](patterns.md) until they
earn a G-letter; several (K1, K8, K10, K14, K21) are worker-shaped.

### I. Integration / wiring gaps — [`integration.md`](integration.md)

Shipped-but-under-wired features (no UI, no live WS update, or a
silent failure path). Cheap individually, compounding in aggregate.

- ~~**I1.** Beliefs tab doesn't live-update (WS handlers missing).~~ **Shipped** → [`shipped.md`](shipped/features.md#reliability-pass--i1--i2--i4--i5-finish-the-wiring-batch).
- ~~**I2.** MessageIndexer silently drops messages on embed failure.~~ **Shipped**.
- **I3.** Agenda has no REST endpoint or UI.
- ~~**I4.** Settings-drawer coverage gaps for shipped knobs.~~ **Shipped**.
- ~~**I5.** Persona-window banners ignore their master switches.~~ **Shipped**.
- **I6.** Chat history hard-capped at 200 with no "load older".
- **I7.** Embedding-model swap wipes LanceDB with only a log line.
- **I8.** No React error boundary.
- **I9.** Mobile responsiveness + PWA installability (LAN-responsive
  is cheap; full installable auto-updating PWA needs an HTTPS origin).
- **I10.** Make `llm.routes` the single runtime source; retire the
  legacy `chat_llm` mirror (config-file slimming already done; this is
  the code follow-up).

### H. Immersion polish — [`immersion.md`](immersion.md)

- **H1.** Conversation-arc surfacing via `[[arc:...]]` tag.
- **H2.** Calendar / time context block.
- **H3.** Mood drift narrator.
- **H4.** Document-recall recency boost.
- **H5.** Second scene / travel semantics.
- **H6.** Audible backchannels ("mm-hm" while the user speaks).
- **H7.** Listen-while-speaking — soften the half-duplex voice lock.
- *Minor polish* — second TTS provider, SSML prosody, barge-in
  default flip (do P25 first).

### J. Shared-moments follow-ups — [`moments.md`](moments.md)

- **J1.** Multi-user moments / participant attribution.
- **J2.** Exportable timeline (markdown / PDF).
- **J3.** Axes-aware proactive nudges.
- **J4.** Relationship-stage register.
- **J5.** Reconnection ritual after a long absence. **Shipped.**
- **J6.** Conflict-repair memory. **Shipped.**
- **J7.** Moment-detection tuning (+ gift/promise ordering bug).
- **J8.** Milestone celebration beats.
- **J9.** Reciprocal vulnerability. **Shipped.**
- **J10.** Appreciation beats. **Shipped.**
- **J11.** Affection-style learning ("how he likes to be cared for").
- **J12.** Intimacy pacing & boundary calibration.
- **J13.** Pet-name reciprocity & evolution.

### K. Patterns to explore — [`patterns.md`](patterns.md)

K10 persona regression tests (SHIPPED, on-demand; background worker deferred) ·
K11 counterfactual cache (SHIPPED) · K12 calendar-linked anticipation ·
K19 cold-start companion onboarding ·
K21 fresh-eyes thread re-summary (SHIPPED) ·
K26 Aiko-side voice evolution ·
K33 cozy mode · K37 emotional contagion (SHIPPED) ·
K39 energy / spoons model · K40 comfortable silence ·
K41 mid-stream self-correction ·
K42 multi-bubble reply bursts (texting rhythm) ·
K46 stance persistence (don't cave on taste pushback) ·
K47 question/share balance (stop interviewing) (SHIPPED) ·
K48 tease rhythm budget (SHIPPED) ·
K49 messiness permission (typed imperfection) ·
K50 typed-mode delivery pacing ·
K62 co-experience companion · K63 long-arc callbacks ·
K64 freedom of thought (mind-wandering over the topic graph —
associative wandering, interest drift, curiosity gradient,
knowledge-map reflection).

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
block, K45 mood inertia, and K51 cue-register rotation
have shipped — see [`shipped.md`](shipped.md).)

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

Shared machinery (kind-agnostic):

- **L1.** Concept store + schema — `concepts` + the typed `concept_edges`
  influence graph + `ConceptKind` registry. Designed for **meta-concepts**
  from day one (concepts referencing concepts): dependency ordering,
  cascade on retirement, `min`-bounded confidence, depth/cycle guard.
- **L2.** Concept synthesis worker — the proposer (idle, worker LLM;
  per-kind proposer via the registry; the only thing that *creates*
  concepts).
- **L3.** The lifecycle engine — the single writer of every concept's
  confidence/plasticity/status: accrual + auto-promotion (candidate ->
  active on recurrence + >=2 evidence + stability; per-kind gate), and the
  host for the L15/L16/L17 passes.
- **L4.** Cluster co-activation signal (the "Maker Mode" primitive).
- **L5.** Concept prompt surfacing (per-kind targets) + `recall_concept`.
- **L6.** Concepts UI + MCP debug (optional human-in-the-loop
  accelerant).
- **L21.** Cold-start + anti-premature-proposal guard (quietly no-op for
  a new user; don't mint spurious early concepts).
- **L22.** Concept-quality evaluation + observability (spurious-concept
  guard + offline eval harness, sibling to K10; MCP graph dump).
- **L23.** Surfacing salience / selection budget (which active concepts
  win the prompt this turn, within a token budget).
- **L24.** Integration contract — existing derivers (interest_map,
  beliefs, profile, goals) consume concepts instead of
  running parallel/contradictory. (Aiko's self-model is already
  concepts-only — the daily self-image worker was removed.)
- **L25.** Edge referential integrity across the memory lifecycle
  (archive / consolidate / delete keep `concept_edges` consistent).
- **L26.** Concept trace + "how Aiko is thinking" observability
  (per-turn: which concepts entered the prompt; graph/transition dump).
  Build a thin version early — it validates every other entry.

Proposer discipline + register fold into L2 (anti-Barnum: specific,
falsifiable concepts) and L5 (offered tentatively, not asserted).
- **L9.** Identity concepts as living, confidence-bearing beliefs
  (confidence + last-reinforced + supporting evidence; strengthen,
  weaken, or be disproven). Near-term, on top of L1-L5.

Concept kinds (each a registry entry over the shared machinery; identity
ships in v1). **Kind and subject (`user` / `aiko` / `relationship`) are
orthogonal** — most kinds exist for both people (Aiko has her own values,
boundaries, affect); "self-concepts" are just `subject=aiko`, enabled by
L11, not a separate kind:

- **L7.** Relationship (deferred) — recurring shared-moment rituals.
- **L8.** Narrative (deferred) — ordered-memory story arcs.
- **L10.** Value (deferred) — the normative *why* under choices; subject
  `user` **or** `aiko` (Aiko has her own values).
- **L11.** Subject=aiko enablement (deferred) — not a kind; the plumbing
  that lets *every* kind exist about Aiko herself (her self-model),
  foundation for L17/L19.
- **L12.** Tension / contradiction (deferred) — concepts *over* concepts
  ("Maker Mode a lot but no walks"); handle with care.
- **L13.** Affective (deferred) — durable topic <-> mood associations.
- **L14.** Aspiration / trajectory (deferred) — where the user is heading.
- **L18.** Boundary (deferred) — behaviour-gating constraints; the
  canonical medium-plasticity kind.
- **L20.** Abstraction hierarchy (deferred) — generalization concepts
  *over* other concepts ("things he builds for enjoyment"); the founding
  example, `relation=generalizes`.
- (Lighter candidates folded into identity for now: rhythm/temporal,
  taste, expertise.)

Capstone:

- **L19.** Aiko's autobiography — her self-history as a durable,
  traversable timeline (the "two histories"): traverse the self-concept
  graph + snapshots to genuinely answer "Have you changed?". Depends on
  L11 + L16 + L17.

Cross-cutting properties (attributes every concept carries):

- **L15.** Bidirectional confidence / belief revision — a contradicted
  concept triggers a re-check of its evidence memories via F1/F5
  (trigger, not a blind write; preserves memory as source of truth).
- **L16.** Concept plasticity — a per-concept learning rate so drift is
  bounded and believable (core traits sticky, tastes fluid;
  trust/duration-modulated).
- **L17.** Self-drift noticing — Aiko notices her own change by comparing
  self-concept snapshots over time ("I think you've corrupted me... I ask
  for cookies more than I used to").

### P. Performance + observability — [`perf.md`](perf.md)

Cross-cutting gaps that aren't features in their own right but
compound across every K-series entry:

- **P3.** Slice-cache validation cost.
- **P4.** RAG memory-hit batch lookups.
- **P5.** Novelty warm-up Lance scan.
- **P6.** MessageIndexer queue visibility.
- **P7.** Typed-mode prefetch parity with voice.
- **P9.** Frontend streaming token append cost.
- **P10.** Schedule-learner missing index.
- **P11.** Reclaim background-worker `num_predict` from reasoning
  leakage (try `/no_think` on qwen3-family workers).
- **P15.** One user-text embed per turn, shared across RAG /
  novelty / opinion / gaps + the post-turn burst.
- **P16.** Post-turn inner-life blocks the brain loop.
- **P17.** K22 callback detector scans the full memory mirror
  every turn.
- **P18.** Streaming accumulator rebuilds the full reply per delta
  (O(n²)).
- **P19.** RAG: one global lock + three sequential Lance searches.
- **P20.** Synchronous LLM compaction stalls the turn mid-flight.
- **P21.** K29 borderline LLM gate runs during prompt assembly.
- **P22.** Inner-life provider sweep: tiering + shared reads.
- **P23.** K28 turning-over full Lance scan on the hot path
  (P5 sibling).
- **P24.** Voice latency batch: reaction-tag TTS gate, double STT
  pass, first-chunk threshold.
- **P25.** Client audio flush on TTS stop (barge-in prerequisite).
- **P26.** Lip-sync rides the server clock, not the playback clock.
- **P27.** STT Whisper model loaded eagerly + unconditionally
  (biggest resident-RAM lever).
- **P28.** TTS engine + PyTorch load even when `tts.enabled=false`;
  never released.
- **P29.** No process-memory observability (RSS breakdown + the
  unidentified second python process).
- **P30.** Raise / disable the `memory.max_memories` cap (topic-graph
  persistence removed the `O(n²)` wall; mirror sweeps P5/P17 are the
  remaining blockers).
- **P31.** Audit + trim the baseline system prompt (~25-30k resting
  floor; rank inner-life blocks by token × frequency × tier, trim
  persona/grammar duplication, lazy-render the occasional blocks).
- **P32.** Concept layer (L-series) worker budget + unbounded graph
  growth — proposer cadence in the idle budget, dirty-subgraph-only
  lifecycle walks, snapshot thinning, per-turn cost limited to L23
  selection.

(P1 per-turn embed budget + timing, P2 prompt-build phase
telemetry, P8 idle-worker queue visibility + multi-worker drain,
P12 bulk memory-mirror on startup, P13 route-driven worker
model + context, and P14 heuristic tool-pass gate have shipped —
see [`shipped.md`](shipped.md).)

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
- **T6.** Determinism seams (`Clock` + seeded RNG; shared with DT1).

---

## How to pick one up

1. Re-read the relevant domain file. Each entry is small enough that
   the file itself is your context.
2. Spin up a plan with `CreatePlan`. Most items fit in a single plan;
   nothing here needs a multi-phase rollout.
3. Validate the same way the depth passes did: focused suite ->
   full `pytest -q` -> spot-check the running app.
4. When the work lands, move the entry from its domain file into
   [`shipped.md`](shipped.md) (one paragraph) and update any inbound
   links in [`AGENTS.md`](../../AGENTS.md), the relevant `docs/`
   detail doc, or code comments.

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
