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

### C. Proactive + presence — [`proactive.md`](proactive.md)

- **C2.** Window-title-aware activity (privacy-gated).
- **C3.** Persisting last-fired typed-proactive cooldown to disk.
- **C4.** TTS-on-typed-proactive toggle.

### D. New tools / capabilities — [`tools.md`](tools.md)

- **D1.** Calendar / reminders tool.
- **D2.** Image vision tool.

### F. Awareness + grounding — [`awareness.md`](awareness.md)

- **F4.** Source-cited memories (`metadata.source_url`).
- **F5.** Conflicting-memory detector.

### G. Background workers — [`workers.md`](workers.md)

- *Cleanup* — drop or wire the unused
  `consolidator_state.last_cluster_index` column.

New worker ideas show up in [`patterns.md`](patterns.md) until they
earn a G-letter; several (K1, K8, K10, K14, K21) are worker-shaped.

### I. Integration / wiring gaps — [`integration.md`](integration.md)

Shipped-but-under-wired features (no UI, no live WS update, or a
silent failure path). Cheap individually, compounding in aggregate.

- ~~**I1.** Beliefs tab doesn't live-update (WS handlers missing).~~ **Shipped** → [`shipped.md`](shipped.md#reliability-pass--i1--i2--i4--i5-finish-the-wiring-batch).
- ~~**I2.** MessageIndexer silently drops messages on embed failure.~~ **Shipped**.
- **I3.** Agenda has no REST endpoint or UI.
- ~~**I4.** Settings-drawer coverage gaps for shipped knobs.~~ **Shipped**.
- ~~**I5.** Persona-window banners ignore their master switches.~~ **Shipped**.
- **I6.** Chat history hard-capped at 200 with no "load older".
- **I7.** Embedding-model swap wipes LanceDB with only a log line.
- **I8.** No React error boundary.

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

### K. Patterns to explore — [`patterns.md`](patterns.md)

K10 persona regression tests ·
K11 counterfactual cache · K12 calendar-linked anticipation ·
K19 cold-start companion onboarding ·
K21 fresh-eyes thread re-summary ·
K26 Aiko-side voice evolution ·
K33 cozy mode · K37 emotional contagion ·
K39 energy / spoons model · K40 comfortable silence ·
K41 mid-stream self-correction ·
K42 multi-bubble reply bursts (texting rhythm) ·
K46 stance persistence (don't cave on taste pushback) ·
K47 question/share balance (stop interviewing) ·
K48 tease rhythm budget ·
K49 messiness permission (typed imperfection) ·
K50 typed-mode delivery pacing.

**The "will" family (K52–K56)** — Aiko follows every topic the user
sets and never opens her own; every initiative cue is hedged into
silence and nothing structurally counters the assistant prior.
**SHIPPED**: K56 persona counterweight ("leading vs following"
rewrite) · K52 wants ledger (desire with growing pressure) ·
K53 initiative turns (deterministic floor-taking — the "may" →
"must, occasionally" flip). Remaining:
K54 Aiko-side topic appetite (she's allowed to be bored and
negotiate the topic) ·
K55 thread ownership (one circle-back to a thread she opened).

**The directed-emotions family (K57–K59)** — Aiko's moods are
objectless scalars: she can be "sad" in general but never *miffed
at {user_name} because he broke a promise*, and the absence
detector explicitly suppresses missing-you feelings ("not a
complaint... warm welcome-back").
K57 directed emotion episodes (lonely / miffed / warm_glow / smug /
playful_jealous / hurt — with a cause line, intensity, decay, and
an acknowledgment-driven resolution arc) ·
K58 emotion speech weighting (mint smug/pouty/sulky/wistful
reactions end-to-end; per-emotion register recipes; intensity
scales the prompt imperative + prosody) ·
K59 tease economy (payback ledger — "noted. that's going in the
ledger", collected as a callback tease conversations later) ·
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

(P1 per-turn embed budget + timing, P2 prompt-build phase
telemetry, P8 idle-worker queue visibility + multi-worker drain,
P12 bulk memory-mirror on startup, P13 route-driven worker
model + context, and P14 heuristic tool-pass gate have shipped —
see [`shipped.md`](shipped.md).)

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

- [`docs/memory-tiers.md`](../memory-tiers.md) — schema v8 memory
  tiers + `IdleWorkerScheduler`.
- [`docs/aiko-room.md`](../aiko-room.md) — world / room / garden.
- [`docs/shared-moments-and-relationship.md`](../shared-moments-and-relationship.md)
  — schema v7 shared moments + relationship axes.
- [`docs/presence-and-activity.md`](../presence-and-activity.md) —
  C1 typed-mode proactive + presence + activity awareness.
- [`docs/alexia-model-notes.md`](../alexia-model-notes.md) — Alexia
  rig audit; B4 + B5 reference.
