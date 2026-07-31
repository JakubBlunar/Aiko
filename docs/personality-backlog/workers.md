# Background workers

Shared scheduling for idle background jobs. G1 (`IdleWorkerScheduler`)
shipped as part of schema v8; G2 (schedule learning) and G3 (idle
curiosity) shipped on top of it. See [`shipped.md`](shipped.md) and
[`docs/memory-tiers.md`](../memory-tiers.md) for implementation
details.

Open: **G5** (self-tuning cooldowns) and **G6** (per-provider decline
attribution) below — both follow-ups to G4, which has shipped. New background workers should
register with the existing
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py)
rather than spinning up their own threads, and should mirror the
INFO-level audit logging pattern established by
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
and [`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py).

For new worker ideas not yet committed to a section letter, see
[`patterns.md`](patterns.md) — several entries (K1 long-term goals,
K8 affect rupture, K10 persona regression, K14 engagement signals,
K21 fresh-eyes resummary) would naturally take the shape of an idle
worker.

---

## G4. Cue outcome accounting — SHIPPED

The accounting and the report have shipped; see
[`shipped/awareness.md`](shipped/awareness.md#g4-cue-outcome-accounting--which-of-the-50-odd-workers-earn-their-keep).
`get_cue_outcomes` reports the armed-to-surfaced ratio per cue, the decline
reasons, and which registered cues have never been armed at all.

Two pieces of the original sketch were deliberately **not** shipped with it
and are groomed separately below: self-tuning cooldowns (**G5**) and
per-provider decline attribution (**G6**). Three of the sketch's assumptions
turned out to be wrong and are worth keeping in mind for the follow-ups —
the gap-cue "one-of lottery" is a deterministic priority order rather than a
tie-break, "surfaced" needed no provider instrumentation at all (P31a's
`block_chars` already had it), and declines could not reuse L37's
`surfacing_outcomes` table without corrupting every aggregate over it.

<details>
<summary>Original design reasoning (kept for the follow-ups)</summary>

**Motivation.** There are somewhere north of fifty workers registered on the
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py),
many of them making LLM calls, and there is no way to answer the only question
that matters about any of them: *did the cue this worker produced ever reach
Aiko, and did the conversation go better when it did?* Today we can see that a
worker **ran** (`get_idle_workers_status` reports overdue seconds, average
duration, error counts) but not that it **mattered**.

The gap is real and structural, not just missing dashboards. A worker writes a
finding to a `kv_meta` journal; a T6 provider decides whether to render it,
often behind a topic gate that silently returns `""` when the live conversation
has moved on; the gap-cue family runs a one-of lottery where only one of
`turning_over`, `sleep_return`, `away_activities` and `forward_curiosity` can
fire per assembly; and `_question_balance_suppressed()` can veto several more.
Every one of those is a legitimate design decision, and every one of them
discards work **without leaving a trace**. A worker whose gate never matches
looks exactly like a worker that is quietly doing its job.

Consequences worth fixing: cooldowns and daily caps are all hand-picked
constants with no evidence behind them, LLM-calling workers can burn the budget
producing cues that are structurally unreachable, and there is no way to retire
a cue type that does not work.

**Key files.**
- [`idle_worker_scheduler.py`](../../app/core/proactive/idle_worker_scheduler.py)
  — the existing per-worker stats block is the natural home for the aggregate
  view; it already tracks duration EMA and error counts per worker.
- The provider side is spread across
  [`inner_life_part2.py`](../../app/core/session/inner_life_part2.py) and
  [`inner_life_part3.py`](../../app/core/session/inner_life_part3.py), where the
  journal-watermark and topic-gate patterns live. These are the sites that
  currently drop cues silently.
- [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — T6
  assembly order and the gap-cue one-of guard.
- Reuses L37's `surfacing_outcomes` table with `kind="cue"`; see
  [`concepts.md`](concepts.md).

**Sketched approach.** Record three states per cue, not one: **armed** (a worker
wrote a finding), **surfaced** (a provider actually rendered it into the
prompt), and **settled** (the engagement outcome on the following turn, per
L37's off-by-one attribution). The armed-to-surfaced ratio alone is the
diagnostic that does not exist today and would immediately expose the
never-reachable gates.

When a provider declines to render, record *why* — topic gate, watermark
cooldown, lost the one-of lottery, question-balance suppression. This is a small
enumeration and it turns "the cue vanished" into "the cue lost to
`turning_over` eleven times this week", which is actionable in a way the current
debug logs are not.

With that in place, self-tuning cooldowns become a small increment rather than a
guess: a cue type with a healthy engaged rate earns a shorter cooldown, one
that consistently lands flat earns a longer one, both clamped to a sane band
around the configured default so the setting stays meaningful and a bad week
cannot silence a cue permanently. This should be opt-in behind a setting —
observability first, adaptation second, because the aggregate numbers will
probably change how the cooldowns should be shaped in ways we cannot predict
from here.

**Open questions.** (1) Is a topic-gated miss a *failure*? Mostly no — it is the
gate working — but the ratio still needs watching, because a gate that never
matches and a gate that matches appropriately are different situations with the
same signature. (2) Attribution when several cues fire together is shared
credit, same as L37; over a week that is fine, per turn it is noise. (3) Should
a worker whose armed-to-surfaced ratio is near zero have its *interval*
lengthened automatically, or is that too clever and better left as a report a
human acts on? Leaning report-only for the LLM-calling workers, where the cost
is real but so is the risk of switching off something that was about to matter.
(4) Retention, per P34.

**Effort.** Small (accounting + a report) / Small-Medium (self-tuning
cooldowns on top).

**Depends on.** L37 (the ledger and the attribution model). Related to P36
(starvation reporting) — that answers "did it run", this answers "did it
matter", and they belong in the same MCP view.

</details>

---

## G5. Self-tuning cue cooldowns

**Motivation.** Every cue cooldown and daily cap is a hand-picked constant.
G4 now measures what they produce, so tuning them stops being a guess: a cue
with a healthy engaged rate can earn a shorter cooldown, one that
consistently lands flat a longer one.

**Sketched approach.** Read `CueDecisionStore.reach` plus the cue rows in the
L37 leaderboard (surfaced cues settle with an engagement label already), and
scale the configured cooldown by a factor clamped to a sane band around the
default — so the setting stays meaningful and a bad week cannot silence a cue
permanently. Opt-in behind a setting: observability first, adaptation second.

**Wait for data before building this.** Two of G4's numbers have to be read
first, and both can invalidate the design. A cue whose `reach_rate` is near
zero needs its *gate* looked at, not its cooldown — shortening the interval
would just produce more supersessions. And the five cues in `coarse_arming`
have an over-counted armed denominator, so their rates are floors; driving a
feedback loop off a floor would systematically over-shorten exactly the cues
whose measurement is weakest. Those five want G6-grade attribution (or a
real watermark) before they join any automatic loop.

**Open question.** Should a near-zero reach rate lengthen the worker's
*interval* automatically, or stay a report a human acts on? Leaning
report-only for the LLM-calling workers, where the cost is real but so is the
risk of switching off something that was about to matter.

**Effort.** Small-Medium. **Depends on.** G4 (shipped) + a few weeks of data.

---

## G6. Per-provider decline attribution

**Motivation.** G4 attributes the two *structural* declines precisely — a
gap cue that lost the priority mutex names its winner, and the K47
question-balance veto is named — but everything a cue's own gates refuse is
bucketed as `provider`. That covers the interesting middle: a topic gate that
never matches, a cooldown that is too long, a picker where no candidate ever
clears the thresholds. Those are different problems with the same label
today.

**Why it was deferred rather than finished.** The four `inner_life_part*.py`
files hold 94 render providers and **491** `return ""` sites between them;
the ~15 cue providers are the gate-heavy ones (`turning_over` alone has
about ten distinct decline paths), so a full sweep is on the order of a
hundred edits across files already close to the 1,500-line budget. That is a
large mechanical change with real regression risk, spent before any data
says which cues need it — and G4's `reach_rate` plus `never_armed` already
identify *which* cues are failing, just not why.

**Sketched approach.** Do it per cue, worst `reach_rate` first, rather than
as a sweep. Each decline site gains a one-line
`self._note_cue_decline("topic_gate")` before its `return ""`; the recorder
already accepts an arbitrary reason string, so no schema or read-path change
is needed — `decline_reasons` picks up the new values automatically. Prefer
splitting a cue provider into a helper when its gate cascade is long enough
that the instrumentation makes it unreadable; several are already candidates
for that on size grounds alone.

**Open question.** Should `provider` decline reasons distinguish "gate did
not match" from "gate matched but the picker found nothing"? The second is a
corpus problem (not enough reflections, no active goals) and the first is a
tuning problem, and conflating them is most of what makes the current
bucket unhelpful.

**Effort.** Small per cue, Medium for all of them.
**Depends on.** G4 (shipped).

---

## G-CLEANUP. `consolidator_state.last_cluster_index` is dead weight

Trivial cleanup item, parked here so it doesn't get forgotten.
The schema carries
[`consolidator_state.last_cluster_index`](../../app/core/memory/memory_consolidator.py)
but nothing reads it — the comment in the source flags it as
unused. Either wire incremental clustering (the original intent)
or drop the column in the next schema bump. Effort: trivial.

For perf / observability gaps that aren't workers in their own
right (turn-level embed budget, idle-worker queue visibility,
typed-mode prefetch, etc.), see
[`perf.md`](perf.md).
