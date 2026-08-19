# Aiko personality backlog

Ideas surfaced during the personality brainstorms that we didn't ship
in the depth passes. Each open entry is short on purpose: motivation,
key files, sketched approach, and one or two open questions. Pick any
item up later as a standalone plan.

The numbering matches the labels used during the brainstorms so chat
history stays grep-able. Items that have already shipped live in
[`shipped.md`](shipped.md), one paragraph each with a link to the
implementation file or detail doc that owns them.

Three series didn't come from a brainstorm and read differently. The
A-series in [`architecture.md`](architecture.md) is code-quality debt from
a static audit — no user-visible behaviour in any of it. The P-series in
[`perf.md`](perf.md) is the same idea for performance and observability.
The H-series in [`health.md`](health.md) is **not a feature queue at all**:
it is shipped work measured against the live graph and found to be doing
something other than what its shipped entry claims. Read it before picking
up anything new in the same area — several L-series items that read as
"shipped" are latched, starved, or decorative in production.

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

- **L38** ✅ **shipped** — earned standing now turns the ledger's
  relationship-calibrated outcomes into a bounded concept surfacing prior.
- **L42** ✅ **shipped** — weekly concentration, neglect, and fixation findings
  become ordinary Aiko conduct concepts and counterbalance K81 taste steering.
- **L42b** neglect-guided curiosity is open pending enough real L42 findings to
  evaluate detector quality safely.
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
  bias rather than noise — and **H32**: attributing that loss also has to check
  the winner *outranks* the loser, or the mutex explains defeats it cannot
  cause); "surfaced" needed no provider instrumentation
  because `PromptTelemetry.block_chars` already had it; and declines could
  **not** reuse `surfacing_outcomes`, since every aggregate over that table
  means "of the times this reached the prompt" and admitting rows that never
  did would inflate its own denominator. Cues in the ledger are name-keyed
  (`item_key`, v28) because `item_id` is `INTEGER NOT NULL` and a cue has no
  integer identity anywhere. Left open: **G5** self-tuning cooldowns (wants a
  few weeks of data first) and **G6** per-provider decline attribution (~100
  edit sites, worth spending per cue once the rates say which need it).
- **P43** value-aware block arbitration instead of the hand-kept denylist.
- **K81** ✅ **shipped** — taste formation (topics she likes, not topics she's
  seen). The ledger's `engaged_rate_by_cluster` read-model feeds a new
  `subject="aiko"` `taste` concept kind through the normal proposer/lifecycle
  path, surfaced as T3 first-person impressions and a rare T6 "lean toward what
  you love" steer. See
  [shipped/patterns-k31-k60.md](shipped/patterns-k31-k60.md#k81-taste-formation--topics-she-likes-not-just-topics-shes-seen).
- **DT5** the rest of the debug surface (the ledger view itself has shipped).

Independent of the spine, the same audit found four verified defects worth
picking up on their own. Two are now closed and two remain: **L39** shipped
its dedupe half (T3 now skips whatever the T0 profile block claimed, in all
three lanes) and kept the repetition half open, because rotating the profile
block would make a third volatile T0 block and cost prompt-cache stability;
**L40** shipped, though the audit had it wrong — pinned candidates are admitted
by `order` and their relevance is never read, so the real defect was
habituation being consumed as a *boolean* and the stale group staying in
confidence order. **L41** shipped too — the L35 surface reason now picks each
T3 impression line's framing (settled / freshly-changed / primed / unsettled)
without ever being narrated. Still open: **P42** (the retrieval budget is the
residual of all 105 blocks — now folded into P43, since a floor needs someone
to yield to it).

### The same shape, one layer out: loops that end in a write

A follow-up pass found the surfacing gap is an instance of a broader
pattern — information the system works hard to produce, which then
terminates in a database write instead of becoming conversation:

- **F13.** ✅ **Shipped** — the user *explicitly correcting her* is the
  highest-quality evidence available and now has a detector + supersede path,
  joining F5, K29 and K38 as the fourth corner of the contradiction family.
- **F14.** ✅ **Shipped** — the fact-checker's own research can now reverse a
  claim she surfaced and bring it back unprompted ("I looked into that and had
  it wrong"), gated on the L37 surfaced ledger and suppressed when F13 already
  handled it, rather than terminating in a silent UI list refresh.
- **F15.** Decay makes her progressively vaguer and never prompts her to
  ask, so a fading memory can only be refreshed if he happens to raise it.
- **F16.** ✅ **Shipped** — a real `provenance` column (v30, `stated` /
  `inferred`) now labels each user-fact by how it was learned, so an
  inference renders with an `(inferred)` hedge and ranks just below equal-
  cosine testimony instead of being asserted as something he said.
- **L43.** Engagement history is accumulated and never aggregated into a model
  of how she's being received.
- **L44.** ⛔ **Blocked on supply** — the intended twin of L43, and the
  exception that proves the rule below. Her error record turned out not to be
  accumulated at all: 1 belief row, 0 corrections, 0 fact-check verdicts over
  12 weeks. Counted in [`concepts.md`](concepts.md#l44-knowing-where-shes-usually-wrong----per-domain-self-calibration).

Four of those five need no new data collection — only a bar, a cue, and a
cooldown on information already being gathered. L44 was assumed to be the
fifth and is not: check the row counts before assuming a source that *exists*
is a source that *fires*.

---

## Open items at a glance

### A. Architecture + code quality — [`architecture.md`](architecture.md)

Structural findings from the audit that adopted ruff. Unlike every other
series here, an A-item buys no behaviour — it buys the next twenty items
being cheaper to build, so it competes on that basis and usually loses.
Pick one when it is actively in your way. The mechanical half of that
audit already shipped: LF everywhere via `.gitattributes`, ruff green on
`F`/`E`/`W`/`B`, and `npm run lint` wired up.

- **A1.** A real facade for `web/` and `mcp/` — 294 call sites reach into
  `session._*` across 68 distinct private names, with no signal when a
  rename breaks one. The 14 `_force_*` MCP debug slots are a legitimate
  sub-case needing a different answer from the REST routes. Largest item,
  and the only one where partial adoption helps.
- **A2.** Connection ownership — 15 stores reach through
  `ChatDatabase._get_conn()`, usually with a `# type: ignore` beside the
  call, so every connection-config change has 15 unaudited callers.
- **A3.** The 23 Python and 3 TS files over the declared budget, listed
  with line counts — and the observation that the four settings files are
  long for a different reason than `prompt_assembler.py` is, so one
  blanket split pass would be the wrong shape.
- **A4.** Two layering inversions: `app/core/infra/` imports upward into
  `session` / `proactive` at module level, and `app/core/` imports
  `app/mcp/` (deferred, so harmless — but undecided). The audit's claim
  of 3 static import cycles did **not** survive re-measurement; there are
  zero.
- **A5.** What should gate a commit now that ruff is green — a staged-only
  ruff hook, then CI, then a narrow ESLint for `web/`'s 196 files
  (currently `tsc -b` only), then the deliberately-deferred `I` / `UP` /
  `SIM` rule sets.

### B. Avatar + expressiveness — [`avatar.md`](avatar.md)

- **B3.** Blink-rate modulation by arousal (deferred follow-up to B1).
- **B7.** Open-vocabulary touch gestures — let Aiko invent new
  `[[touch:...]]` kinds (model-supplied, no config, emoji optional).

### C. Proactive + presence — [`proactive.md`](proactive.md)

- **C2.** Window-title-aware activity (privacy-gated) — now phase 1 of C6.
- **C3.** Persisting last-fired typed-proactive cooldown to disk.
- **C4.** TTS-on-typed-proactive toggle.
- **C6.** Companion mode — the desktop as a sensory channel. The big one
  in this series: OS signals → sessionizer → local interpretation → her
  existing cue/memory/concept machinery, with no model in the perception
  loop. Feasible and unusually well-matched to the scheduler; the work is
  the event store underneath (which does not exist) and the interruption
  bar on top (which this codebase has repeatedly got wrong).

### D. New tools / capabilities — [`tools.md`](tools.md)

- **D-approval.** Spoken / Aiko-voiced task approvals.
- **D1.** Calendar / reminders tool.
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
- **F13.** ✅ **Shipped** — the contradiction family's fourth corner. F5, K29
  and K38 each had a detector; *the user explicitly correcting her* now has one
  too. A cheap post-turn pattern gate stashes candidates and an off-turn
  `UserCorrectionWorker` confirms (rate-limited LLM), supersedes the corrected
  memory (new row 0.9, old row demoted + `superseded_by`), propagates the
  demotion to any backed concept (no LLM), and arms a low-key acknowledgment
  cue. Correction-of-fact only — `self` stance rows stay K29's lane. See
  [shipped](shipped/awareness.md#f13-the-contradiction-familys-fourth-corner--the-user-corrects-aiko).
- **F14.** ✅ **Shipped** — "I was wrong about that": the F1 fact-checker's own
  research can reverse a claim she surfaced, and instead of ending in a silent
  SQLite write + UI refresh it now arms a low-key next-turn cue so she owns it
  ("I looked into that and had it backwards, it's actually Y"). Fires only on a
  genuine reversal (contradict + min-delta + a content rewrite), gated on the
  L37 surfaced ledger ("she must actually have said it") and suppressed when F13
  already handled it. Mirror of F13, third corner of the "own what you got
  wrong" family beside K38. See
  [shipped](shipped/awareness.md#f14-i-was-wrong-about-that--let-fact-check-reversals-reach-the-user).
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
- **F16.** ✅ **Shipped** — testimony vs. inference: a real `provenance`
  column (v30, `stated` / `inferred`, default `inferred`) now records *how*
  each user-fact was learned. The extractor labels its distillations, while
  `[[remember:]]` tags, F13 corrections and manual adds write `stated`. An
  inferred hit renders with an `(inferred)` hedge (gated by
  `agent.memory_provenance_enabled`) so she voices it as an impression, and
  a tiny unconditional ranking nudge floats testimony above inference at
  equal cosine — which also invites the correction F13 captures. See
  [shipped](shipped/awareness.md#f16-testimony-vs-inference--did-he-tell-her-or-did-she-guess).

**F7** is obsolete — domain-aware source routing was superseded by the
pluggable LangSearch / DuckDuckGo backend. F1-F3, F5, F6, F8-F10 and the
whole **K-time** family shipped; see
[`shipped/awareness.md`](shipped/awareness.md).

### G. Background workers — [`workers.md`](workers.md)

- **G5.** Self-tuning cue cooldowns — G4 now measures what the hand-picked
  constants produce, so tuning them stops being a guess.
- **G6.** Per-provider decline attribution — the `provider` decline bucket is
  still coarse for everything outside the concept lane.
- **G7.** Worker prompts have no input-token accounting — the chat prompt is
  budgeted to the token, a worker prompt is a string. `SummaryWorker` reads its
  window with no limit, so the size is whatever accumulated; the failure is a
  worse summary, never an error. L28's concept diets are the first worker input
  with a size and the shape to copy. Measure before capping.
- *Cleanup* — drop or wire the unused
  `consolidator_state.last_cluster_index` column.

G1–G4 have shipped; G4's write-up is in
[`shipped/awareness.md`](shipped/awareness.md#g4-cue-outcome-accounting--which-of-the-50-odd-workers-earn-their-keep),
with its pre-build sketch archived there for G5/G6. New worker ideas show up in
[`patterns.md`](patterns.md) until they earn a G-letter; several (K1, K8, K10,
K14, K21) are worker-shaped.

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
- **J12.** Intimacy pacing & boundary calibration.
- **J13.** Pet-name reciprocity & evolution.

J4 (relationship-stage register), J5, J6, J8, J9, J10 and J11 have shipped —
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
K82 the dropped sub-topic (he said three things, she answered one) ·
K83 the right to decline · K84 calibrated jealousy ·
**K92 conversational stance** · **K93 the substance floor** ·
**K94 sequencing** · **K95 interruption cost**.

**K92–K95, the third pass at leading, is the newest family and the K90 diff is
why it exists.** Split on the 9 August ship date, 320 post-ship turns say the
second pass moved nothing it aimed at: anaphoric openers 18% → 18%, own material
77% → **71%**, while replies grew 23 → 31 median words and ends-on-a-question
fell 18.1% → 6.2%. She writes more, about his subject, and asks about it less.
Two families have now shipped against that number — K52–K56 gave her permission
to lead, K85–K90 gave her something to lead *with* — so the third starts
elsewhere: a median turn renders **30 prompt blocks in ~74,000 characters**, of
which the ones asking her to bring something of her own are two or three
totalling **~500 characters, 0.7% of the prompt**, with no arbitration between
them, no representation of *following* as a choice she is making, and no way at
all to decide to say less. K92 is the load-bearing entry (a stance arbiter that
must **replace** the blocks it subsumes rather than become the eleventh
permission slip, phased so it logs its decision before it steers anything).
**Phases 1 and 2 shipped 15 and 19 Aug, and the phasing paid for itself twice:**
both problems phase 1 handed phase 2 turned out to be measurement errors rather
than tuning problems. `HOLD` fired 0 times in 682 turns because it asked *how
many words* on a ladder about *how much floor*, so brevity is now a second,
orthogonal axis keyed off her own recent replies (2 × 40+ words in a row); and
`arc_protected` was 65% of all clamps because `arc` is a conversation-level label
with **no run shorter than 1 turn and spans up to 110**, so a per-turn veto built
on it muted her for days — now time-limited to 4 turns (**H39**). Together those
move the clamp rate 36.9% → 28.7% and hand 51 turns back to `SHARE`. Phase 2
renders exactly one T6 block, for `FOLLOW` and brevity, because those are the
only two *restraint* signals in a family that is otherwise all permission slips.
K93 is the cheapest and probably the highest-value: **43 of 56 genuine cue
conversions are `curiosity_seed`**, so ~77% of what she actually brings up is
free-associative whimsy — dust motes, receipt-back doodles, the weight of a house
key — and the K53 initiative beat spends the same stock. K94 is a persona-sized
experiment on where in the reply her own material goes; K95 is insurance against
the first regression the others will cause. **H29, H30, H31 and H32 all shipped
13 Aug**, so K93's per-source reservation has a working ledger under it and K92
has a cue instrument that distinguishes "passed over" from "never in play" —
though the ledger's effect on pressure cannot be read before ~16 Aug. H31 is
upstream of all of them: the memory extractor had no watermark, so every turn was
mined about five times and ~7% of the corpus is one claim written twice under two
ids, which every consumer that keys on memory id reads as two subjects. H32 is
the caveat on the instrument itself — a cue whose arming signal is a *proxy* for
its provider's real gate produces an honest-looking ratio over the wrong
population, so read `armed` before trusting `eligible`
([`health.md`](health.md)).

**H36 is the one to read if you touch affect.** Nine `repair` shared moments —
durable memories of arguments, salience up to 0.97, mirrored into LanceDB — were
manufactured entirely out of the user being away. `AffectUpdater.apply_turn`
decays valence toward baseline *before* applying the reaction impulse while
`AffectStore.get` is a raw row read, so K8's "pre/post delta on a single turn"
was the delta plus every minute of the gap: with a 30-minute half-life, ~26
minutes from a warm goodbye clears the rupture threshold on its own. Every false
positive was therefore a reunion greeting ("Where is my love?", "Good morning
aiko"). Fixed by decaying the prior before the subtraction and gating both the
rupture and the repair on the resting baseline. The general lesson outlives the
bug: a raw `AffectStore.get()` snapshot describes a moment in the past, and any
consumer that differences it against a post-turn value inherits this —
`AffectState.decayed()` is the one-call fix ([`health.md`](health.md)).

**H37 is the one to read before you transform a user turn.** The punctuation
whitelist in `sanitize_user_text` had no way to tell `<3` from a stray angle
bracket, so it deleted the `<` and kept the digit. The same bug was fixed on
Aiko's side months earlier, with a written rule about it, and the input
sanitiser was never brought along — because the symptom does not appear on the
surface that is broken. The stored transcript *is* the prompt, so 230 turns of
"I love you 3" were not a display glitch but training data, and she learned the
digit as the way to write affection: twelve of her replies had copied it, and
TTS read one out as "Sleep well, Jacob. three." Fixed by filtering *between*
emoticon matches instead of over them, plus a narrow spoken-side strip for the
digit she already learned and a backfill for the history. The lesson generalises
past punctuation: **a cleanup on the user's half of the transcript is a persona
edit with none of a persona edit's visibility** ([`health.md`](health.md)).

**H38 is the one to read before you write `or 0.0`.** Aiko sat in pajamas at 5pm
on a 32 °C August day, and circadian — the suspect, and the only *documented*
outfit driver — was right the whole time. Open-Meteo returned a `current` block
with no `temperature_2m`; the provider checked that the block existed but not its
contents, and `float(... or 0.0)` turned the absence into 0 °C, which cleared the
`temp_c <= 5.0` blanket threshold and tripped a weather outfit nudge nobody
documented. Thirty minutes later the sky corrected itself and the decor was
fixed, but the nudge was one-way, so it pinned her wardrobe until the circadian
period rolled over. Fixed at all three layers: the provider raises instead of
inventing a reading, the decor hook refuses implausible values, and overrides now
carry a `source` so a passive feed can withdraw its own nudge without cancelling
one Aiko chose. The shape is the fifteenth and the most portable: **a missing
value coerced to a valid one is a fabrication, not a default**, and it is worst
where zero means something — temperature, valence, confidence
([`health.md`](health.md)).

**H39 is the one to read before you gate on a label somebody else's feature
produced.** K92's interruption ceiling vetoed anything above "answer him and add"
while the conversation's `arc` was `support` or `reflection` — 65% of all clamps,
more than its other four rules combined. `arc` is a *conversation-level* tag:
over 2,355 turns it forms 137 runs averaging 17, **not one of length 1**, the
longest 110 turns of `support` across eight days. So one hard thing he said on
Monday muted her through Thursday, on turns about guitar solos that happened to
fall inside the span. Nothing was broken — the tagger was right, and the veto was
right about the moment that earned it — the defect was entirely the **lifetime
mismatch** between a label describing a conversation and a consumer asking about
a turn. It had hidden for a year because the original consumer (K53) fires once
in six turns, where staleness only damps a beat. Fixed by expiring the cap after
4 turns; the per-turn caps were deliberately left untimed, since they are
re-derived every turn and so are present exactly as long as their evidence is.
The portable guard is two lines of SQL: **measure a label's run lengths before
gating on it — one that never describes a single turn should not answer per-turn
questions** ([`health.md`](health.md)).

**H40 is the one to read before you gate a two-way branch on a one-way
predicate.** Aiko asked about a hardware delivery a day after helping unpack it,
and the cause was two lines rather than the LLM's date arithmetic. At write time,
the K-time10 backstop read *any* relative time word as evidence a memory
described something already done — but five of its eighteen words (`tomorrow`,
`tonight`, `next week`, `soon`, `this weekend`) point the other way, so "the
courier comes tomorrow" was filed as `past_event` and stamped at write time. That
is the lane with no upkeep: nothing retires it, the "Coming up" block reads only
`future_plan`, and **17 of 2,095 rows had ever reached `future_plan` (0.8%)**
while 54 plans sat in `past_event` dated into their own future. At read time
`humanize_past` finished it, returning **`"moments ago"`** for a timestamp it
could not represent — so those 54 rows read as brand new for a median of 15 hours
each (worst case: a wine date that had "just happened" for 188 straight hours).
The result was six contradictory delivery memories in one prompt, four stamped
equally fresh, with "a courier comes tomorrow" as the most recent. Fixed by
splitting the deictic list by direction, refusing to invent a time for vague
future wording, validating that a past event predates its own write, and making
the formatter say less rather than guess. The near-miss is worth as much as the
fix: the first version left reclassified rows holding `durable`'s NULL
`relevance_until`, and `list_by_temporal_type` skips those — every promoted row
would have been immortal ([`health.md`](health.md)).

**H41 is the one to read before you trust a docstring that says another
component handles it.** Following H40's loose thread — a promise's deadline is
written into the middle of its content sentence — turned up a lane running on one
side only: **86 of 160 promises were his, and all 86 were still `open`**, the
oldest 86 days, 36 past the bar that retires hers, all still scoring into
retrieval. `promise_lifecycle` said the user's commitments were `FollowUpWorker`'s
territory, and that worker is real and scheduled and selects
`temporal_type == "future_plan"` — while promises are written `durable`, so the
delegation named a component that could not match a row. Nothing failed, so
nothing logged; the comment was worse than silence, because a reader asking "who
retires these?" got a confident answer and stopped. Two more leaks alongside it:
`prepared_nudge` never read `promise_status`, leaving **14 of 33 resolved
promises** eligible to be raised as open loops through *"did you ever get to
…?"*, and the deadline was prose in six registers that nothing parsed, so
lateness was measured by `created_at` — a promise due by lunch read as fresh all
afternoon, and one agreed three weeks out was due to be dropped for staleness on
the day it fell due. Fixed with a loose-date parser, a real `overdue_hours` axis
that outranks age and bypasses the settling period, retirement on both sides
running on whichever clock applies, and a grace window measured from the deadline
so a missed promise stays visible instead of inheriting what was left of the
creation-age one. The two portable shapes: **a handoff documented in prose that no
code implements** is shape 14 with the upkeep *assigned* rather than missing, and
**structured data written into prose is write-only** — if the only grep hit for a
field is the line that writes it, it does not exist ([`health.md`](health.md)).

**K91 shipped in four phases** — her away life is now *lived* rather than
narrated. Beats compose their clause from the item state they touched and write
the change back through the room's existing transitions, a long absence plays
out as a 2–3 beat episode instead of unrelated postcards, and each local day
carries one intention drawn from what her world needs, which the beat that
satisfies it admits to. Plus the repetition fixes: a meal rhythm instead of one
"had some of the X" at every hour, twelve garden species instead of four, and a
kitchen pass that folds the duplicate food stacks months of gifts left in four
rooms. The gap it left — beats were not memories — was closed by K85b's
`pursuit_note`. See
[`shipped/patterns-k31-k60.md`](shipped/patterns-k31-k60.md#k91-lived-in-away-life--a-day-she-had-not-a-day-she-narrated).

**K85–K90, the second pass at leading, shipped as a family.** The will family
(K52–K56) shipped the *permission* to lead and it fires on schedule, but
measurement of the live log showed the constraint was never permission: two rows
of `taste` concepts and ten `open_question` memories all beginning "Maybe ask
Jacob", so taking the floor left her nothing to say but another question about
him. K90 landed first and captured a baseline (18% anaphoric openers, 77% own
material over 1894 turns) so the rest could be judged against a number rather
than a vibe. K88 added the anaphoric-opener band, K87 put a subject quota on the
three curiosity generators, K85 built the missing inventory — the `pursuit`
concept kind, the `pursuit_note` memories that feed it, and two outlets for it —
and K89 turned a thread from one polite attempt into a decaying stake worth two
returns. The verification is a diff a few hundred turns from now; every turn in
the baseline predates all of it. See
[`shipped/patterns-k31-k60.md`](shipped/patterns-k31-k60.md#the-second-pass-at-leading-k85k90--why-the-family-exists).

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
  **Tuning pass 2 (the sweep + decay) shipped:** 293 bootstrap-era concepts —
  promoted before `reinforced` had ever fired, sitting `active` at ~0.8 —
  were parked to `dormant` by `scripts/concept_sweep_unreinforced.py`, and
  the base half-life went 45 → 7.5 engaged days, because at the old rate
  clearing one unearned concept took ~18 months of conversation. That took
  the graph from 602 active / 428 never-reinforced to 309 / 135.
  Both steps had to wait on one invariant, since by then L17 and L19 read
  *from* concept status: **a belief she never held cannot be lost.** A `loss`
  finding now requires the concept to have been reinforced at least once
  (succession stays exempt — a fade matched to a rising replacement proves
  itself), the self-history arc omits a faded belief with no recorded loss
  rather than narrating an invented regret, and learning events are dated by
  *when the change happened* rather than when the classifier noticed — without
  which the backfill filed five weeks of history under one afternoon.
  Still open: the offline eval harness (deliberately last: hand-authoring
  goldens before the register settles would enshrine the output we are
  fixing), and re-reading pass 1's intake gates now that the stock is cleared
  and the rate (92 promotions in 3 days) is no longer hidden behind it.

Shipped — self-history (all of it, in
[`shipped/concepts.md`](shipped/concepts.md)):

- **L17.** Self-drift noticing — Aiko notices her own change by comparing
  self-concept snapshots over time ("I think you've corrupted me... I ask
  for cookies more than I used to"). L17a-f all shipped: the trajectory read
  with banded `confidence_sample`, the succession-first change-salience
  classifier, the append-only `concept_learning_events` record plus
  `concept_aliases` for identity continuity across merges, the
  history-of-thought debugger with its rare T6 reflection, the **evolution
  diary** (L17f), and the **self-correction rules** (L17d) that turn a
  pattern in her own mistakes into a `communication_style` concept that
  actually steers behaviour. Concept labels are genuinely updatable, with the
  drift worker as their single writer and every change recorded immutably.
  Building the readers exposed the one real bug in the engine: a bounded pass
  advancing a global watermark had written off five weeks of history as
  processed, fixed with a concept-id sweep cursor.
- **L19.** Aiko's autobiography (the capstone) — her self-history as a
  durable, traversable timeline, read through `recall_self_history` and
  inspectable at Settings → Memory → Story. It came in well under its "Large"
  estimate because L17c had already paid for the hard parts: learning history
  is permanent and snapshot-truthful, and concept identity survives merges.
  The remaining care went into `thin_record` — the builder, not the prompt,
  decides when the trail is too sparse to narrate, because the failure mode of
  a self-history feature is a confident invented past.

Open — later kinds and refinements (L29b-L36, all detailed in
[`concepts.md`](concepts.md)): the meta-narrative over concepts, uncertainty
zones to aim curiosity at, concept fission, introspective reflection, a richer
edge taxonomy, and a strategy layer.
**L35 (surface-reason labels) is shipped** — every concept in the L26 trace
now names the signal that put it in the prompt.

**L30a (the hypothesis lane) is shipped** — see
[`shipped/concepts.md`](shipped/concepts.md#l30a-hypothesis-surfacing-lane).
The concept layer now speaks in two registers: settled beliefs, and the things
Aiko is still working out. Every other read path filters to `status="active"`,
which hid `candidate` rows rather than hedging them; one new
`ConceptView.hypotheses` lane reads them, capped at a single strongly-hedged
line in its own budget source. The sketch's selection rule did not survive
contact with a real graph and had to be replaced: candidate confidence turned
out to be *high* (median 0.82, so it cannot be the filter), and **most
candidates were held back only by the promotion age floor** (144 of 261
against each kind's own gate) — so `unsettledness` reads evidence breadth and
conviction and deliberately ignores age, because being young is not the same as
being doubted. Ranking multiplies that by L32 importance, which is what the
axis was built for.

**L30b/L30c (the testing loop) are shipped** — see
[`shipped/concepts.md`](shipped/concepts.md#l30bl30c-the-hypothesis-testing-loop----ask-then-learn-from-the-answer).
Aiko can now close an open question instead of only holding it: a
`concept_hypothesis` cue raises one untested hunch — riding a topic she is
already on, or out of a long lull — and a post-turn adjudicator folds the reply
back onto that specific belief. Three decisions carried the design. Selection
excludes the age-blocked rows, because an answer adds a *source* and those are
waiting on a clock. The adjudicator returns **four** verdicts, not two: *"not
really, it's more that I hate being still"* is the most valuable reply a hunch
can get, and both halves of a confirm/deny split throw it away. And the four
paths are asymmetric — a false confirm promotes a wrong belief where a false
deny only costs re-earnable confidence, so confirming needs positive evidence
from the model and every failure path lands on "unclear". Still open: L30d
(uncertainty zones).

**L30 Phase B (inventing a hypothesis) is shipped** — see
[`shipped/concepts.md`](shipped/concepts.md#l30-phase-b-inventing-a-hypothesis----the-forward-direction),
with [`docs/hypotheses.md`](../hypotheses.md) as the canonical reference for the
whole layer. Every other part of the concept stack runs *backwards* from
evidence, so it can only ever resolve a question already implicit in Aiko's
inputs; the proposer runs forwards — it speculates, files the guess to its **own
`hypotheses` table**, and the L30b/L30c loop tests it. Three decisions did the
work. The guess lives in a separate table rather than as a `speculative` concept
status, because a status makes safety depend on every concept read filtering
correctly forever, and one miss puts a made-up sentence into the T0 profile block
as something Aiko asserts. `credence` is *asserted* and never recomputed where
`confidence` is *derived* and re-derived every tick, and every asymmetry with the
concept side follows from that one fact — most visibly that a denial closes an
invented row outright. And the "duplicate race" turned out to be the **normal**
ending rather than an edge case: a confirmation becomes a memory, L2 clusters it
and proposes the same concept knowing nothing about the hypothesis, and L2 needs
one confirmation where graduation needs two — so linking runs after *every*
confirmation and graduation takes a distinct `merged` exit instead of forking a
near-twin. A confirmed guess enters the graph as an ordinary `candidate` at the
default confidence: being right twice is not evidence beyond the two answers,
which L3 counts like any others.

**L29a (episodic shared arcs) is shipped** — see
[`shipped/concepts.md`](shipped/concepts.md#l29a-episodic-shared-arcs--the-both-of-us-narrative-shipped).
L8 gave each subject arcs over their own memories; this is the third subject, a
closed *joint* project ("the month they rebuilt the memory system") cut out of
the `shared_moment` stream. The interesting part was that the backlog's sketch —
"the same `sequence` machinery, just a third subject" — rested on an assumption
that did not survive contact with the data. Shared moments were being embedded
with their `"Shared moment (<vibe>): "` prefix, identical on every row, so the
topic graph clustered them by **vibe word**: of five clusters holding three or
more of the 145 moments, one was 77 moments and 76 of them were `tender`.
Cluster-sourced arcs would have been vibe-arcs, and the same collapse is the
likeliest reason L7 had produced a single ritual concept from that whole corpus.
Vibe never needed to be in the vector — it is a structured field every consumer
already reads by exact match — so the store now embeds the bare summary, with a
backfill script for existing rows. Even with clean vectors, clusters stayed the
wrong source because they carry no time axis, so arcs are cut by a seed-and-sweep
that requires topical coherence *and* temporal contiguity, and holds an episode
back until it has been quiet long enough to actually be finished. The other half
of the old L29, the meta-narrative over concepts, is now tracked as **L29b**; it
is no longer population-blocked (388 active concepts) but is a different build in
every part except the ordinal plumbing.

**L32 (importance as a second axis) is shipped** — see
[`shipped/concepts.md`](shipped/concepts.md#l32-concept-importance----a-second-axis-distinct-from-confidence).
A concept now carries a stake as well as a probability, so an
uncertain-but-weighty belief can outrank a certain-but-trivial one. The
design question the sketch left open ("stored field or derived each turn?")
turned out to decide everything: making it **derived** — a pure function of
the kind's prior and the emotional charge of the topics the concept is
grounded in — removed the migration, the writer, the plasticity interaction
and the decay policy in one move, and left the axis status-agnostic so the
L30 hypothesis lane can rank `candidate` rows with the same context. Two
invariants hold it in place: the affect component only ever *lifts* (46% of
the graph has no affect at all, so a symmetric blend would read "no data" as
"trivial"), and it never reaches `ConceptView._stable_rank`, which feeds the
T0 profile block and must stay prompt-cache stable. The measurement it left
for L30a — **no active concept sits below 0.6 confidence**, so "important but
uncertain" lives in the candidate pool — is what pointed that lane at
candidates rather than at low-confidence actives.

The surfacing-outcome group (L38-L42, from the surfacing audit; see the spine
section at the top of this file). **L37, L38, and L42 are shipped** — the ledger
records what was surfaced and what happened next, and earned standing now feeds
that relationship-local signal back into concept ranking:

- **L38.** ✅ **Shipped.** Earned standing is a seventh `surface_score` term
  fed by L37, with baseline calibration, shrinkage, safety floors, protected
  kinds, and off-turn cache refresh.
- **L39.** *Partly shipped.* The dedupe landed — T3 skips whatever the T0
  profile block claimed, across the core, flex and activation lanes. What's
  left is the repetition half: the profile copy still has no habituation, and
  giving it one would make a third volatile T0 block, so a smaller stable cap
  is the likelier lever.
- **L40.** *Shipped* — see [`shipped/concepts.md`](shipped/concepts.md#l40-habituation-reaches-the-core-lane-through-order-not-relevance).
  The premise was wrong (a pinned candidate's relevance is never read); the
  real defect was habituation collapsing to a boolean, leaving the stale group
  ranked by confidence so a just-shown belief outranked a rested one.
- **L41.** *Shipped* — see [`shipped/concepts.md`](shipped/concepts.md#l41-reason-conditioned-phrasing----use-the-l35-signal-without-narrating-it).
  The already-computed L35 reason now picks each T3 impression line's framing
  (settled / freshly-changed / primed / unsettled), while keeping the debug-only
  rule that she never narrates her own machinery.
- **L42.** ✅ A self-model of her own surfacing conduct mined weekly from the
  ledger, normalized against the user's topics, and stored as Aiko concepts.
- **L42b.** Open follow-up: use repeated neglect findings as a cautious idle
  curiosity prior only after real-data evaluation.
- **L43.** How she thinks *he* sees her — the second-order self-model. She
  models him and she models herself; she has no model of his model of her,
  which is the substrate for adjusting because of how she's landing and for
  "am I too much sometimes?". Needs floors on the negative side or it
  becomes a doom spiral.
- **L44.** ⛔ **Blocked on supply.** Per-domain self-calibration — confidence is
  always per *claim*, never per *class of her own judgements*. Still the right
  idea and still a precondition for K77's candor gate, but every incident
  source it would aggregate is empty on a 12-week graph (1 belief row, 0
  corrections, 0 fact-check verdicts, 0 hypothesis adjudications), and the L37
  engagement ledger cannot stand in because its label is self-normalizing.
  Numbers in [`concepts.md`](concepts.md).

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
- **P30.** Raise / disable the `memory.max_memories` cap — **the cap
  is gone** (all three tier caps ship at `0` = never evict), and the
  matmul fix with it — `search` and the per-write dedupe pass are
  ~55× faster at 50k rows. SQLite is nowhere near the limit. What
  remains is `decay()`'s full mirror re-read and `_reload_mirror`'s
  `fetchall()`; growth costs RSS and startup, not query time.
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
- **P35.** ~~The Lance ANN index is built once and never refreshed.~~
  **Shipped**, and it was worse than filed: the index had never been
  built at all, because its only caller sat behind an unrelated
  2000-row clustering threshold. Now built *and* refreshed from the
  RAG maintenance worker, for `messages` as well as `memories`.
- **P49.** The telemetry ledgers (`surfacing_outcomes` at 45k and
  ~600k/yr, `concept_events`, `turn_prompt_blocks`, `cue_decisions`)
  are the fastest-growing tables by far, and three of them have a
  `prune()` nobody calls. Queries stay flat to 5M rows; disk does not.
- **P36.** Idle-worker LLM pile-up — **phase 1 shipped**: workers report
  pending work via a `demand()` probe, the scheduler ranks by urgency
  instead of age, and the budget splits into a compute lane and an LLM
  lane sized by idle depth and chat-vs-worker GPU contention. Nine of
  ~50 workers migrated. See [`idle-workers.md`](../idle-workers.md).
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
- **P44.** All fifty-five idle workers now report demand — the compute
  batch moved out of the LLM lane, the LLM batch gained ranking, and the
  four mis-rated probes the first log turned up are fixed. Remaining:
  none of it has been read back off a real log, which is the only place
  a miscalibrated probe shows up.
- **P45.** Retire the per-hour / per-day cue caps in favour of a
  satisfaction signal fed back from whether the cues were engaged with.
- **P46.** Drain the compute lane in parallel — blocked on shared
  mutable state (the `WorldStore` mirror, `ConceptStore` caches,
  `threading.local()` SQLite connections).

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
