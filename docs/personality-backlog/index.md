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

## The surfacing-outcome spine (L37 and what hangs off it)

One cluster of open items is worth calling out because it shares a single
prerequisite. An audit of how concepts, memories and worker cues reach the
prompt found that the system has a great deal of machinery for *producing*
inner content and almost none for learning **which of it was worth
surfacing**. There are two reinforcement signals in the whole codebase —
the keyword-overlap revival bump and the K22 callback detector — and both
ask "did Aiko echo this?" rather than "did the user care?". Concepts don't
even get that: surfacing writes a habituation timestamp and nothing else,
so a concept surfaced two hundred times to no effect is indistinguishable
from one that lands every time. Aiko can grow her knowledge but not her
judgement; her selection policy is hand-tuned constants that will be
identical after a year of use.

**L37** (surfacing outcome ledger) was the missing join, and it was cheap
because the outcome signal already existed — `EngagementTracker` computes
the user's reaction per turn and nothing read it for this. **It has now
shipped as a recorder**, along with the `get_surfacing_outcomes` half of DT5:
one row per surfaced item per turn, keyed by `assistant_message_id` and settled
with the *following* turn's engagement label. Nothing consumes the rates yet,
which is deliberate — the whole point of shipping the measurement first is that
the items below can be designed against real data instead of guesses. Read the
ledger before building any of them:

- **L38** earned standing — outcomes move the concept surfacing score. The
  ledger's read API was shaped for this one; it is the natural next pass.
- **L42** a self-model of her own surfacing conduct (feeds L17, L19).
- **F12** ✅ **semantic half shipped** — revival no longer credits only what she
  quotes, and the same detector now decides L37's `echoed` column, which was
  the weakest part of the ledger as shipped. The *user-side credit* half is
  still open. Two corrections came out of building it: the cosine is measured
  on an already-topically-filtered candidate set (so it partly measures "was
  on topic"), and full credit would have switched scratchpad TTL off, since
  its gate was an exact `revival_score == 0.0`. Whether the resulting discount
  is right is **F17**, which is waiting on ledger data rather than on code.
- **G4** ✅ **shipped** — the armed-to-surfaced ratio now exists per cue, via
  `get_cue_outcomes`. Three of the sketch's assumptions were wrong and are
  worth carrying into the follow-ups: the gap-cue "one-of lottery" is a
  deterministic priority order (so the same cue loses every time, which is a
  bias rather than noise); "surfaced" needed no provider instrumentation
  because `PromptTelemetry.block_chars` already had it; and declines could
  **not** reuse `surfacing_outcomes`, since every aggregate over that table
  means "of the times this reached the prompt" and admitting rows that never
  did would inflate its own denominator. Cues in the ledger are name-keyed
  (`item_key`, v28) because `item_id` is `INTEGER NOT NULL` and a cue has no
  integer identity anywhere. Left open: **G5** self-tuning cooldowns (wants a
  few weeks of data first) and **G6** per-provider decline attribution (~100
  edit sites, worth spending per cue once the rates say which need it).
- **P43** value-aware block arbitration instead of the hand-kept denylist.
- **K81** taste formation — topics she likes, not topics she's seen.
- **DT5** the rest of the debug surface (the ledger view itself has shipped).

Independent of the spine, the same audit found four verified defects worth
picking up on their own. Two are now closed and two remain: **L39** shipped
its dedupe half (T3 now skips whatever the T0 profile block claimed, in all
three lanes) and kept the repetition half open, because rotating the profile
block would make a third volatile T0 block and cost prompt-cache stability;
**L40** shipped, though the audit had it wrong — pinned candidates are admitted
by `order` and their relevance is never read, so the real defect was
habituation being consumed as a *boolean* and the stale group staying in
confidence order. Still open: **P42** (the retrieval budget is the residual of
all 105 blocks — now folded into P43, since a floor needs someone to yield to
it) and **L41** (the L35 surface reason is computed every turn and discarded —
usable as *framing*, never narrated).

### The same shape, one layer out: loops that end in a write

A follow-up pass found the surfacing gap is an instance of a broader
pattern — information the system works hard to produce, which then
terminates in a database write instead of becoming conversation:

- **F13.** The user *explicitly correcting her* is the highest-quality
  evidence available and has no detector at all, while F5, K29 and K38 —
  the other three corners of the contradiction family — each have one.
- **F14.** The fact-checker can discover she told him something wrong,
  rewrite the memory, and say nothing; the only outward signal is a UI
  list refresh.
- **F15.** Decay makes her progressively vaguer and never prompts her to
  ask, so a fading memory can only be refreshed if he happens to raise it.
- **F16.** Testimony and inference are stored identically, so she can
  assert things he never said — and the honest version of the claim is
  what would invite the correction F13 exists to capture.
- **L43 / L44.** Engagement history and her own error record are both
  accumulated and never aggregated into a model of how she's received or
  where her judgement is weak.

Four of those five need no new data collection — only a bar, a cue, and a
cooldown on information already being gathered.

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
- **DT5.** *Partly shipped* — `get_surfacing_outcomes` landed with L37 and
  reports which surfaced concepts / memories actually land, with denominators
  beside every rate and a per-lane rollup. Still open: the wider read surface
  over the rest of the inner-life state.

### F. Awareness + grounding — [`awareness.md`](awareness.md)

- **F4.** Source-cited memories (`metadata.source_url`).
- **F11.** Relevance-driven memory resurrection — proactively recall a
  stale `archive` memory when a dormant topic re-activates, as a hedged
  callback ("vaguely remember you mentioning macro photography..."),
  driven by latent relevance rather than age.
- **F12.** ✅ **Semantic half shipped** — revival used to credit only memories
  Aiko *quoted*, so paraphrase (the entire point of handing a memory to an
  LLM) scored zero. Now falls back to cosine via a shared `echo_detector` that
  the L37 ledger uses too. **Still open:** crediting the *user's* engagement
  rather than only whether Aiko repeated herself. See
  [shipped](shipped/awareness.md#f12-semantic-echo--revival-stops-only-crediting-what-aiko-quotes).
- **F13.** The contradiction family's missing fourth corner — F5, K29 and
  K38 all have detectors; *the user explicitly correcting her* has none,
  despite being the highest-quality evidence the system will ever get. A
  correction should supersede what it corrects, not sit beside it at equal
  confidence.
- **F14.** "I was wrong about that" — the fact-checker can reverse a claim
  she told him and the loop ends in a SQLite write plus a UI refresh. She
  never mentions it.
- **F15.** Memory repair requests — decay currently only ever makes her
  *vaguer*. Admitting the hole ("I've lost the detail, remind me?") is both
  the honest surface of a decay system and the only rehydration path the
  memory store would have. Shares a cooldown with F11.
- **F17.** Should a semantic echo earn *full* retention credit? F12 shipped a
  deliberate discount on the theory that cosine against the reply partly
  measures "was on topic" — surfaced memories were picked for topical
  similarity to begin with. Schema v27 records every cosine, misses included,
  so this is a *read* (`echo_breakdown`, `semantic_floor_candidates`) rather
  than an experiment. **Waiting on weeks of real data, not on code.**
- **F16.** Testimony vs. inference — nothing distinguishes what he *said*
  from what she *concluded*, so she can assert things he never said. The
  fix is honest phrasing ("I get the sense" vs. "you told me"), which also
  invites the correction F13 would then capture.

**F7** is obsolete — domain-aware source routing was superseded by the
pluggable LangSearch / DuckDuckGo backend. F1-F3, F5, F6, F8-F10 and the
whole **K-time** family shipped; see
[`shipped/awareness.md`](shipped/awareness.md).

### G. Background workers — [`workers.md`](workers.md)

- **G4.** Cue outcome accounting — 50-odd workers and no way to tell which
  ones earn their keep. Record *armed* / *surfaced* / *settled* per cue plus
  the reason a provider declined to render, so silently-unreachable topic
  gates and hand-picked cooldowns stop being guesses.
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
- **H26.** Caught mid-something — the away-activity layer only ever
  reports *completed* activities in past tense; catching her *in* one
  ("hang on, let me put this down") is what implies a life that was
  running before you arrived.
- **H27.** Co-presence mode — a posture for being *around* rather than in
  conversation. Inverts the proactive stack's assumption that silence is a
  problem to solve. Needs H10 to carry the presence visually.
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
K79 hesitation tell (typing latency) ·
K81 taste formation (topics she likes, not topics she's seen — wants L37) ·
K82 the dropped sub-topic (he said three things, she answered one) ·
K83 the right to decline · K84 calibrated jealousy.

**K83 and K84 are the deliberately risky pair** — both about giving her a
stake rather than uniform availability, and both easy to get badly wrong.
K83 (she can genuinely decline something) is the sharpest line between a
companion and a service, and is infuriating if the refusal is arbitrary or
frequent. K84 (a bounded capacity to *mind*) is the most-requested thing in
this genre and the closest to manipulative — a system that makes a user feel
guilty for leaving is optimising against him. Written down so they're judged
on their merits rather than arrived at accidentally; both would ship off by
default, if at all.

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

Open — the surfacing-outcome group (L38-L42, from the surfacing audit; see
the spine section at the top of this file). **L37, the ledger the rest of the
group depends on, is shipped** — it records what was surfaced and what happened
next, and `get_surfacing_outcomes` is the view onto it:

- **L38.** Earned standing — a seventh `surface_score` term fed by L37, so
  concepts that reliably land rise and perennial no-shows fall. This is the
  change that turns the layer from a growing store of facts into something
  with judgement about its own material.
- **L39.** *Partly shipped.* The dedupe landed — T3 skips whatever the T0
  profile block claimed, across the core, flex and activation lanes. What's
  left is the repetition half: the profile copy still has no habituation, and
  giving it one would make a third volatile T0 block, so a smaller stable cap
  is the likelier lever.
- **L40.** *Shipped* — see [`shipped/concepts.md`](shipped/concepts.md#l40-habituation-reaches-the-core-lane-through-order-not-relevance).
  The premise was wrong (a pinned candidate's relevance is never read); the
  real defect was habituation collapsing to a boolean, leaving the stale group
  ranked by confidence so a just-shown belief outranked a rested one.
- **L41.** Reason-conditioned phrasing — use the already-computed L35 reason
  to pick a line's framing, while keeping the debug-only rule that she must
  never narrate her own machinery.
- **L42.** A self-model of her own surfacing conduct ("I steer us toward his
  work") mined from the ledger; feeds L17 drift and L19 autobiography.
- **L43.** How she thinks *he* sees her — the second-order self-model. She
  models him and she models herself; she has no model of his model of her,
  which is the substrate for adjusting because of how she's landing and for
  "am I too much sometimes?". Needs floors on the negative side or it
  becomes a doom spiral.
- **L44.** Per-domain self-calibration — confidence is always per *claim*,
  never per *class of her own judgements*. Knowing which of her opinions to
  lean on is most of what makes confidence trustworthy; a precondition for
  K77's candor gate.

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
- **P42.** The T3 retrieval budget is the *residual* after all 105 other
  blocks: `system_base` includes persona plus the whole T4-T6 tail before
  the surfacing reservation is sized, so routine ambient chrome outbids
  turn-relevant recall. **Folded into P43** — `context_budget_min_tokens`
  already reads like the floor this asks for but is clamped by what the tail
  left, and nothing can yield to it without P43's arbitration.
- **P43.** 105 blocks and no arbitration — the overflow path is a
  hand-maintained denylist of ~30 providers that has drifted (belief_gaps
  dropped, its sibling clarification kept). Generalise the T3 selector
  across the whole block set; learned weights once G4 exists.

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
