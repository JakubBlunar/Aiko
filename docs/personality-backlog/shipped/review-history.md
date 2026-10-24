# Historical backlog review notes

Dated review snapshots retained on 24 Oct 2026. Their counts, route/process state and then-open claims are historical; use [the current index](../index.md) for current remaining scope. A planning note here does not mean its proposal shipped.

# Aiko personality backlog

Ideas surfaced during the personality brainstorms that we didn't ship
in the depth passes. Each open entry is short on purpose: motivation,
key files, sketched approach, and one or two open questions. Pick any
item up later as a standalone plan.

The numbering matches the labels used during the brainstorms so chat
history stays grep-able. Items that have already shipped live in
[`shipped.md`](../shipped.md), one paragraph each with a link to the
implementation file or detail doc that owns them.

Three series didn't come from a brainstorm and read differently. The
A-series in [`architecture.md`](../architecture.md) is code-quality debt from
a static audit — no user-visible behaviour in any of it. The P-series in
[`perf.md`](../perf.md) is the same idea for performance and observability.
The H-series in [`health.md`](../health.md) is **not a feature queue at all**:
it is shipped work measured against the live graph and found to be doing
something other than what its shipped entry claims. Read it before picking
up anything new in the same area — several L-series items that read as
"shipped" are latched, starved, or decorative in production. It carries a
[status index](../health.md#status-index) covering all 54 entries; the 43
closed ones keep their full text in
[`shipped/health.md`](health.md), so `health.md` itself is the open
work plus the
[recurring-shapes catalogue](health-patterns.md#recurring-shapes). Below, a bare
[`health.md`](../health.md) citation may point at either file — the status
index resolves it.

The K-series in [`patterns.md`](../patterns.md) is a separate beast —
companion-AI design patterns we haven't tried yet, sketched at one
paragraph each rather than fully scoped. Treat patterns.md as a
prompt for the next brainstorm, not a queue.

### Keeping it honest

Two conventions, because both have already been violated silently:

- **When an entry ships, move the write-up to `shipped/` and leave the open file
  a one-line status row.** An entry that ships in *halves* keeps its open half
  where it was and links back. The failure this prevents is a shipped narrative
  sitting in the open file, where the next reader treats it as work to do.
- **When something ships, re-read what pointed at it.** A "still open" note
  written at ship time is a snapshot, and nothing re-checks it: L30a claimed
  L30b/L30c were open for as long as they had been shipped, and L32 said both
  its consumers did not exist while one of them was live and consuming it.
  The give-away is a dependency named in the present tense.

[`scripts/check_backlog_links.py`](../../../scripts/check_backlog_links.py) catches
the mechanical half of this — broken paths and, more usefully, heading anchors
that stopped resolving when a heading was reworded on its way into `shipped/`.
Run it after any migration. It cannot catch a stale *claim*; only re-reading can.

## 24 Oct 2026: selective context and grounded reasoning

**Analysis and backlog only; all nine proposals remain open.** The
[RS1-RS9 review](../reasoning-surfacing.md) traces retrieval, concept evidence,
cue competition, working state, speech and prompt-cache behavior against the
current source. It distinguishes why an item was selected from why it is
supported, applies now or explains a result. No current model-quality or live
cache measurements are claimed.

Start with end-to-end evaluation, accurate final-inclusion receipts and source-
linked concept recall. Then compare smaller sufficient evidence sets and fewer
competing optional steers. RS6 proposes a gated, bounded `reason_about` tool
before speech, tested against selected context alone and selective reasoning
effort. RS7 preserves prefix reuse with revision-aware evidence, and RS8/RS9
extend existing causal-check and usefulness-learning owners. These are follow-up
slices under L49-L55, K92/K96/K97/K101 and P43/P52, not duplicate cognition loops.
No runtime, persona, settings or live memory changed.

## 23 Oct 2026: reasoning, social meaning and companion expression

The review added five patterns, [K102-K106](audit-2026-10-24.md#patterns-k102):
pragmatic frames, precise misunderstanding repair, cause-specific mixed feelings,
meaningful voice/avatar delivery, and negotiated shared shorthand. Each has
examples, a bounded experiment, existing owners and acceptance checks.

The review also deepens L36 with outcome-grounded strategy transfer, J12 with
explicit friend/flirty/romantic preferences, and T5 with paired social scenarios.
Existing L49-L55 remain the reasoning roadmap; their bounded implementations
are not reopened as new features. On 24 Oct, a current configured-model T5
baseline and [bounded K102/K103 implementation](social-understanding.md)
landed. Broader behavioral and listening comparisons remain open; no general
improvement in naturalness is claimed. The implementation edits conditional
handling and runtime code, with evaluation isolated from live conversations,
memories, relationship state and settings.

## 23 Oct 2026: model migration and learning provenance

The DeepSeek follow-up review adds
[K107](../patterns.md#k107-provider-outcomes-are-not-character-evidence): provider
outcomes must not become durable character evidence. Shared stream integrity,
private-thinking and UTF-8 guards, structured turn provenance, delayed-memory
source checks, tool argument validation and reasoning metrics are implemented.
Ordinary text-only refusal attribution and broader behavioral validation remain
open. The concise recommendation and implementation summary live in
[LLM providers](../../llm-providers.md#follow-up-review-stream-integrity-and-remaining-migration-checks).
Only documentation and synthetic regression fixtures are retained.

## 1 Oct 2026: conversation archive recall

**In progress, 24 Oct 2026.** [D8](../tools.md#d8-conversation-archive-recall-with-daily-overviews)
now has a user-scoped, paged `conversation_history` tool and compute-only daily
source outlines across sessions, with source drill-down, coverage reporting and
transactional edit/deletion invalidation. Recent days collect during idle time;
historical backfill is explicitly bounded. Generated topic summaries remain open.
See [implementation and review commands](../../conversation-archive.md). This adds
no always-on prompt and does not replace memory extraction or session compaction.

## 30 Sep 2026: immersion without changing models or Live2D

**Backlog only; all eight proposals remain open.**
[IM1-IM8](../immersion.md#30-sep-2026-immersion-without-a-new-rig-or-model) add shared
quiet sessions, explicit pointing/selection, a bounded shared reader,
inspectable Aiko-made artifacts, joint creation, source-linked keepsakes,
a small rules-driven game and opt-in ambient audio. Each has a first slice,
current-model budget, existing owner and acceptance checks.

**Start with IM1 and IM2:** shared presence and a concrete common referent,
without extra inference. IM3 extends K62; IM4 extends H19/H28; IM6 builds on
Together/H12/J2; IM8 scopes H27's audio idea. These do not replace Live
L19-L24 delivery work or reopen shipped rituals/backchannels. No new model,
fine-tuning, continuous audiovisual inference or rig changes are proposed.

## 30 Sep 2026: Live field review

**Analysis and backlog only.** The [dated field review](live-initiative-review.md#field-review-30-sep-2026)
finds 384 retained policy results with no main-speech proposal, including 28
idle reconsiderations that all choose nonverbal actions. Eight source cues
behind the expired runtime urge tail remain pending and unexpired; C6 has
only one newly drafted cue in the review window. Twelve idle decisions follow
a zero-client record, but sixteen follow a connected-client record, so
disconnection alone does not explain the sample.

Prioritize joinable opportunity diagnostics and verified rollout identity
(L19/L30), durable candidate resolution (L21/L22), and a temporally explicit
policy menu (L23), then source freshness and capability-aware scheduling.
The review preserves measurement limits, separates hypotheses from confirmed
mechanisms, and gives bounded acceptance checks under the existing L19-L30
owners. No runtime behavior, permissions or cadence were changed.

**Implementation follow-up:** [phases 5-7](../live-initiative.md#implementation-log)
now add content-free decision traces, source-bounded candidate retries,
eligibility-before-menu filtering, explicit quiet-opening prompt context and
zero-client idle-wake suppression. These are partial implementations, not
closure of L19-L30; restart-safe outcomes, real-model replay, source renewal
and delivery settlement remain open. The running app was not restarted.

## 28 Sep 2026: Live initiative architecture

**Architecture evaluation followed by a limited Phase 1 code change.** The
[Live initiative evaluation and plan](../live-initiative.md) adds **Live L19-L31**
with verified source findings, a read-only runtime snapshot, concrete ownership
contracts, implementation steps, dependencies and acceptance tests.

The snapshot recorded 74 attention/acknowledgement executions, no main-wake
execution attempts, no active candidate urges and no scheduled wait, despite
Live and unprompted speech being enabled. These bounded counters locate an
investigation, not a lifetime failure rate or proof of one root cause.

**Recommended start:** L19 diagnostics plus one L21/L22 candidate-lifecycle and
wake path, then L20 availability and L24 delivery protection before real speech.
Phase 1 has shipped a one-shot eligible-cue wake and removed the premature
heartbeat opportunity mark; the broader candidate lifecycle, funnel diagnostics
and delivery protection are still open. The plan's implementation log records
the shortcut and validation.
Build toward a small-model decision loop over grounded, durable candidates;
reuse the brain, cue pool, curiosity, tasks, concepts and memory. Later items
cover extensible worker/plugin impulses, bounded learning, desktop/mobile
delivery and a deliberately deferred email integration pilot. Shipped L17/L18
remain foundations; this is not a second implementation of those passes.

## 27 Sep 2026: prompt surfacing and brain decisions

**Implementation update:** initial pilots now cover content-free assembly
traces, staged pooled claims and topical-share admission, essential handling
bundles, two-writer evidence admission with retry checkpoints, strict read-only
premise recall, and answer/delivery feedback attribution. Broader provider
coverage, paraphrase entailment and multi-turn naturalness evaluation remain
open. [Current boundaries](../../prompt-surfacing.md#implemented-pilots-and-remaining-scope).

The new [prompt-surfacing reference](../../prompt-surfacing.md) maps all **125
registered blocks**, source/refresh ownership, gates, conditional handling,
stance, tool routing, Live handoff, delivery and memory writes. It complements
[context-budget.md](../../context-budget.md), which covers T3 rather than every
route into the brain. This follow-up is documentation and proposals only.

**Priorities:** make omissions and final claims traceable (G6/P43); introduce
[K100 evidence-bound memory admission](../patterns.md#k100-evidence-bound-memory-admission);
extend K92's collect/choose/commit pilot; then test
[K101 premise-to-action routing](../patterns.md#k101-route-an-unresolved-premise-to-a-bounded-next-step).
P50 needs semantics-aware handling selection, not another cap. H7/H44 and K99
remain the owners of answer/delivery follow-through.

The [fresh evidence table](../../prompt-surfacing.md#evidence-from-the-running-installation)
uses 394 recorded turns and 3,900 cue decisions over 14 days, with 528 generic
`provider` declines. Only 12 of 1,044 surviving memory rows created in that
window carry the scalar source-message link; other provenance was not fully
audited. The live process reports 123 blocks versus 125 in the checkout, so
these measurements are not an evaluation of the latest rollout. Earlier
rolling snapshots below are preserved with their original denominators.

## 27 Sep 2026: naturalness and cognitive continuity

**Backlog only; no runtime behavior or settings changed.** This pass used
read-only SQLite reports and the running app's MCP diagnostics. The goal
is an individual companion with coherent interests, revisable understanding,
and good timing, not a gender stereotype or a claim to replicate a human
brain. More animation, questions, or hidden reasoning are not automatic
improvements. All examples below are synthetic; measurements are aggregate
software behavior, not personal profiles or transcript excerpts.

### Evidence snapshot

The audit date is local 27 Sep; the report clock was still 26 Sep UTC.
Rolling windows below are relative to execution, not frozen datasets.

| Scope | Observation | What it does not prove |
| --- | --- | --- |
| 14-day lead/follow report, 397 assistant turns | Median 33 words; 2.8% end in a question; 13% anaphoric openers; 65% lexical own-material share | Lexical novelty is not initiative, reasoning, or enjoyment |
| 14-day prompt telemetry | Mean 33.3 nonempty blocks and 76,181 summed block characters per turn | Volume alone does not prove harmful competition; compare K92/P43 candidates at equal budgets |
| 14-day cue report, 3,934 decisions | Hypothesis cues surface on 10/396 eligible decisions; 530 declines still say only `provider` | Low reach is not evidence to lower relevance or cooldown gates |
| Current second-thought runtime | Enabled; 3 scheduled, 3 failed, 0 queued | A small process-local failure sample cannot identify the exception or measure thought quality |
| Current Live runtime | 90 executions (74 attend, 9 remain-present, 7 acknowledge); 0 main-wake proposals; 11 retained cue urges expired | This bounded sample is not a longitudinal silence/expiry rate; silence can be correct |
| Lifetime hypothesis snapshot | 75 rows; 12 ever asked; 1 of those has a verdict; 0 graduated | An unscored ask does not prove the user answered; H7/H44 remain the existing repair lane |
| Lifetime belief snapshot | 393 beliefs; 365 checked; 118 with a gap stamp | Belief tracking is present; these counts do not establish its accuracy |

Reproduce the offline portion with
[`lead_follow_report.py`](../../../scripts/lead_follow_report.py)
(`--windows 7,14`),
[`cue_reach_report.py`](../../../scripts/cue_reach_report.py) (`--days 14`),
and [`block_firing_report.py`](../../../scripts/block_firing_report.py)
(`--window-days 14`). Read-only MCP calls were
`get_live_situation_frame` and `get_second_thought_state`; do not replay
messages into the live relationship to reproduce the counts. Additional
SQL aggregates used `mode=ro` plus `PRAGMA query_only=ON` over `beliefs`,
`hypotheses`, `turn_prompt_blocks`, and `surfacing_outcomes`, with distinct
assistant-message denominators. No memory text or personal details are
needed to repeat those aggregates.

### Recommended order

| Priority | Item | Why it comes here |
| --- | --- | --- |
| First | [K96 operational follow-up](../patterns.md#k96-one-shot-per-thought--in-turn-deliberation) | Diagnose the enabled post-reply pass's failures before adding more thinking |
| First | [K92/K93](../patterns.md#k92-conversational-stance--one-decision-per-turn-not-ten-permission-slips), [G6](../workers.md), [H7/H44](../health.md#h44-nothing-has-ever-graduated-and-at-this-calibration-nothing-can) | Finish candidate selection/attribution and close existing answer loops; these are not new features |
| Shipped | [Live L18](audit-2026-10-24.md#live-mode-l18) | Purpose-aware admission and exact cue handoff; T5 behavior evaluation remains |
| Shipped | [Live L17](audit-2026-10-24.md#live-mode-l17) | One bounded reconsideration after a missed opening; T5 behavior evaluation remains |
| Shipped | [K97](audit-2026-10-24.md#patterns-k97) | Evidence-linked working understanding; behavioral comparison remains open |
| Shipped | [K98](audit-2026-10-24.md#patterns-k98) | One answer-earned continuation without retaining the ask; behavioral comparison remains open |
| Shipped | [K99](audit-2026-10-24.md#patterns-k99) | Bounded generated/presented/played/acknowledged provenance; behavioral comparison remains open |
| Across all | [T5 naturalness evaluation](../testing.md#27-sep-2026-multi-turn-naturalness-track) | Measure continuity, appropriate silence, correction, and non-repetition with negative controls |

**Existing polish to reuse, not duplicate:** Live L5/L9 cover delivery and
listening manners; K42 covers texting bursts; K49 covers typed imperfection;
K62 covers shared media. Test those against the same T5 scenarios after the
content path works. Do not synthesize jealousy, random mistakes, or emotional
dependency as a shortcut to feeling alive. Quiet companionship is a valid
outcome, and a user ending a conversation is not a negative reward.

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
with the *following* turn's engagement label. It shipped with nothing consuming
the rates, deliberately — the point of measuring first is that the items below
get designed against real data instead of guesses. **Three consumers have since
been built** and this list is the reason to check before assuming a fourth is
free: `concept_lifecycle_worker` reads `stats_for("concept", …)` for L38 earned
standing, `concept_synthesis_worker` reads it for proposal quality, and
`gate_tuner_worker` takes it as a tuning input. Read the ledger *and* those
three before building any of them:

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
  [shipped/patterns-k31-k60.md](patterns-k31-k60.md#k81-taste-formation--topics-she-likes-not-just-topics-shes-seen).
- **DT5** the rest of the debug surface (the ledger view itself has shipped).

Independent of the spine, the same audit found four verified defects worth
picking up on their own. Three are now closed: **L39** shipped both halves —
T3 now skips whatever the T0 profile block claimed in all three lanes, and the
repetition half took the cheaper lever (`profile_concept_max_lines` 10 → 4)
because rotating the profile block would have made a third volatile T0 block
and cost prompt-cache stability;
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
  12 weeks. Counted in [`concepts.md`](../concepts.md#l44-knowing-where-shes-usually-wrong----per-domain-self-calibration).

Four of those five need no new data collection — only a bar, a cue, and a
cooldown on information already being gathered. L44 was assumed to be the
fifth and is not: check the row counts before assuming a source that *exists*
is a source that *fires*.

---
