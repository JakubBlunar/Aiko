# Background workers

Shared scheduling for idle background jobs. G1 (`IdleWorkerScheduler`)
shipped as part of schema v8; G2 (schedule learning) and G3 (idle
curiosity) shipped on top of it. See [`shipped.md`](shipped.md) and
[`docs/memory-tiers.md`](../memory-tiers.md) for implementation
details.

Open: **G4** (cue outcome accounting) below. New background workers should
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

## G4. Cue outcome accounting -- which of the 50-odd workers earn their keep?

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
